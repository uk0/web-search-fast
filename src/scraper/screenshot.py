"""Screenshot capture for pooled Camoufox tabs.

Three capture modes over an already-acquired Playwright ``Page`` (the caller
owns the tab via ``pool.acquire()`` — this module never closes or navigates
the page):

- ``viewport``  — what is currently visible.
- ``full_page`` — the entire scrollable document. Lazy / infinite-scroll pages
  are pre-scrolled first (bounded, best-effort) so below-the-fold content is
  actually rendered, then scrolled back to the top before capture.
- ``element``   — one element by CSS selector, erroring cleanly when it is
  missing, ambiguous, or not visible.

Full-page captures are hard-capped in both dimensions by clipping (env-tunable
via SCREENSHOT_MAX_HEIGHT / SCREENSHOT_MAX_WIDTH) so a 30000px-tall page can
neither OOM Firefox nor emit a 50MB PNG. When the document exceeds a cap the
result is clipped from the top-left and ``truncated=True`` is reported in the
metadata — never silently.

Every path is bounded by one wall-clock budget (``timeout``): the whole
capture runs under ``asyncio.wait_for`` and each Playwright call additionally
gets the remaining budget. On timeout the page is left usable (worst case:
scrolled away from the top); a viewport override is always restored.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from playwright.async_api import Page, ViewportSize
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

__all__ = [
    "CaptureMode",
    "ScreenshotResult",
    "ScreenshotError",
    "ElementNotFoundError",
    "ScreenshotTimeoutError",
    "capture_screenshot",
]

# --- limits & defaults -------------------------------------------------------
# Caps exist so a huge page cannot OOM the browser or emit an enormous image.
# Firefox refuses render surfaces past ~32767px, hence the hard upper bound.
_DEFAULT_MAX_HEIGHT = 20_000
_DEFAULT_MAX_WIDTH = 10_000
_CAP_LO, _CAP_HI = 256, 32_000

_DEFAULT_TIMEOUT_SECS = 15.0
_MIN_TIMEOUT_SECS = 0.05
_MAX_TIMEOUT_SECS = 120.0

# Tail of the budget reserved for the capture itself — pre-scroll on a slow
# lazy page must never starve the actual screenshot call.
_CAPTURE_RESERVE_SECS = 3.0

# Pre-scroll tuning — mirrors the proven depth-crawl auto-scroll. Kept local
# on purpose: depth.py is owned by another workstream.
_SCROLL_MAX_STEPS = 12
_SCROLL_SETTLE_MS = 450

_MEASURE_TIMEOUT_SECS = 5.0
_PROBE_TIMEOUT_SECS = 5.0
_VIEWPORT_RESTORE_TIMEOUT_SECS = 2.0

_DEFAULT_JPEG_QUALITY = 80
_VIEWPORT_LO, _VIEWPORT_HI = 16, 8192

_MIME = {"png": "image/png", "jpeg": "image/jpeg"}

_MEASURE_JS = (
    "() => ({"
    " width: Math.max(document.documentElement ? document.documentElement.scrollWidth : 0,"
    " document.body ? document.body.scrollWidth : 0),"
    " height: Math.max(document.documentElement ? document.documentElement.scrollHeight : 0,"
    " document.body ? document.body.scrollHeight : 0) })"
)


class ScreenshotError(RuntimeError):
    """Base error for screenshot capture failures."""


class ElementNotFoundError(ScreenshotError):
    """Element mode: selector matched nothing, several nodes, or nothing visible."""


class ScreenshotTimeoutError(ScreenshotError):
    """The capture exceeded its wall-clock budget."""


class CaptureMode(str, Enum):
    VIEWPORT = "viewport"
    FULL_PAGE = "full_page"
    ELEMENT = "element"


@dataclass(slots=True)
class ScreenshotResult:
    image: bytes
    mime_type: str
    width: int
    height: int
    format: str
    truncated: bool
    captured_url: str
    elapsed_ms: int

    def meta(self) -> dict[str, object]:
        """Everything except the raw bytes — what an MCP tool should surface."""
        return {
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "truncated": self.truncated,
            "captured_url": self.captured_url,
            "elapsed_ms": self.elapsed_ms,
            "bytes": len(self.image),
        }


def _env_cap(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(_CAP_LO, min(_CAP_HI, int(raw)))
    except ValueError:
        logger.warning("[screenshot] ignoring non-integer %s=%r", name, raw)
        return default


def _remaining_ms(deadline: float, *, floor_ms: int = 100) -> int:
    """Budget left for one Playwright call; fail fast once it is exhausted."""
    left = (deadline - time.monotonic()) * 1000
    if left <= 0:
        raise ScreenshotTimeoutError("screenshot budget exhausted")
    return max(floor_ms, int(left))


def _remaining_secs(deadline: float, cap: float) -> float:
    return min(cap, max(0.1, deadline - time.monotonic()))


# --- image header parsing -----------------------------------------------------
# Metadata width/height come from the actual bytes: with scale="device" on a
# HiDPI context the pixel dimensions differ from CSS-px, so trusting the DOM
# measurement would lie. Falls back to computed dims when parsing fails.

def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        if w > 0 and h > 0:
            return w, h
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            # Resync via bytes.find (C speed) — a byte-at-a-time scan would
            # block the event loop for ~0.5s on a multi-MB malformed stream.
            nxt = data.find(b"\xff", i)
            if nxt < 0:
                return None
            i = nxt
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers carry no length
            continue
        if marker in (0xD9, 0xDA):
            return None  # hit EOI/SOS without a SOF frame header
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOFn
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w > 0 and h > 0 else None
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _image_size(data: bytes, fmt: str) -> tuple[int, int] | None:
    return _png_size(data) if fmt == "png" else _jpeg_size(data)


def _page_url(page: Page) -> str:
    url = getattr(page, "url", "")
    return url if isinstance(url, str) else ""


async def _auto_scroll(page: Page, deadline: float) -> None:
    """Scroll to the bottom repeatedly so lazy-load / infinite-scroll content
    renders, then return to the top. Best-effort: any error ends scrolling."""
    try:
        prev_height: float = -1
        for _ in range(_SCROLL_MAX_STEPS):
            if time.monotonic() >= deadline:
                break
            height = await page.evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); "
                "return document.body.scrollHeight; }"
            )
            settle_ms = min(_SCROLL_SETTLE_MS, max(0, int((deadline - time.monotonic()) * 1000)))
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            if not isinstance(height, (int, float)) or height <= prev_height:
                break  # no new content loaded
            prev_height = height
        # Back to the top so the capture starts at y=0 with a settled layout.
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception as exc:
        logger.debug("[screenshot] pre-scroll skipped: %s", exc)


async def _measure_document(page: Page, deadline: float) -> tuple[int, int]:
    """Document scroll size in CSS px; (0, 0) when it cannot be measured."""
    try:
        raw = await asyncio.wait_for(
            page.evaluate(_MEASURE_JS), timeout=_remaining_secs(deadline, _MEASURE_TIMEOUT_SECS),
        )
        w, h = int(raw.get("width", 0)), int(raw.get("height", 0))
        if w > 0 and h > 0:
            return w, h
    except Exception as exc:
        logger.debug("[screenshot] document measure failed: %s", exc)
    return 0, 0


async def _shoot(fn: Callable[..., Awaitable[bytes]], **kwargs: Any) -> bytes:
    """One Playwright screenshot call with error classification."""
    try:
        return await fn(**kwargs)
    except PlaywrightTimeoutError as exc:
        raise ScreenshotTimeoutError(f"screenshot call timed out: {exc}") from exc
    except ScreenshotError:
        raise
    except Exception as exc:
        raise ScreenshotError(f"screenshot capture failed: {exc}") from exc


async def _element_shot(
    page: Page, selector: str, cap_h: int, deadline: float, shot_kwargs: dict[str, Any],
) -> tuple[bytes, tuple[int, int]]:
    """Capture one element; returns (bytes, fallback (w, h) from its box)."""
    locator = page.locator(selector)
    probe = _remaining_secs(deadline, _PROBE_TIMEOUT_SECS)
    try:
        count = await asyncio.wait_for(locator.count(), timeout=probe)
    except asyncio.TimeoutError as exc:
        raise ScreenshotTimeoutError(f"probing selector {selector!r} timed out") from exc
    except Exception as exc:
        raise ScreenshotError(f"selector probe failed for {selector!r}: {exc}") from exc
    if count == 0:
        raise ElementNotFoundError(f"no element matches selector {selector!r}")
    if count > 1:
        raise ElementNotFoundError(f"selector {selector!r} matches {count} elements — make it unique")
    try:
        box = await asyncio.wait_for(
            locator.bounding_box(timeout=int(probe * 1000)), timeout=probe + 0.5,
        )
    except asyncio.TimeoutError as exc:
        raise ScreenshotTimeoutError(f"bounding box for {selector!r} timed out") from exc
    except PlaywrightTimeoutError as exc:
        raise ElementNotFoundError(f"element {selector!r} never became visible: {exc}") from exc
    except Exception as exc:
        raise ScreenshotError(f"bounding box failed for {selector!r}: {exc}") from exc
    if not box:
        raise ElementNotFoundError(f"element {selector!r} is not visible (no bounding box)")
    if box.get("height", 0) > cap_h:
        raise ScreenshotError(
            f"element {selector!r} is {int(box['height'])}px tall — over the {cap_h}px cap; "
            "capture it with mode='full_page' instead"
        )
    try:
        data = await locator.screenshot(timeout=_remaining_ms(deadline), **shot_kwargs)
    except PlaywrightTimeoutError as exc:
        raise ScreenshotTimeoutError(f"element screenshot timed out for {selector!r}: {exc}") from exc
    except ScreenshotError:
        raise
    except Exception as exc:
        raise ScreenshotError(f"element screenshot failed for {selector!r}: {exc}") from exc
    return data, (int(box.get("width", 0)), int(box.get("height", 0)))


async def _capture(
    page: Page,
    mode: CaptureMode,
    selector: str | None,
    shot_kwargs: dict[str, Any],
    do_scroll: bool,
    cap_w: int,
    cap_h: int,
    deadline: float,
    t0: float,
) -> ScreenshotResult:
    fmt = str(shot_kwargs["type"])
    if do_scroll:
        # Reserve tail budget so scrolling can never starve the capture call.
        await _auto_scroll(page, deadline - _CAPTURE_RESERVE_SECS)

    truncated = False
    if mode is CaptureMode.VIEWPORT:
        data = await _shoot(
            page.screenshot, full_page=False, timeout=_remaining_ms(deadline), **shot_kwargs,
        )
        vp = page.viewport_size
        fallback = (int(vp["width"]), int(vp["height"])) if isinstance(vp, dict) else (0, 0)
    elif mode is CaptureMode.FULL_PAGE:
        doc_w, doc_h = await _measure_document(page, deadline)
        measured = doc_w > 0 and doc_h > 0
        # Always clip to the caps: Playwright trims the clip to the document
        # rect, so an unmeasurable page still cannot exceed the size limits.
        clip_w = min(doc_w, cap_w) if measured else cap_w
        clip_h = min(doc_h, cap_h) if measured else cap_h
        truncated = measured and (doc_w > cap_w or doc_h > cap_h)
        if truncated:
            logger.info(
                "[screenshot] full page %dx%d exceeds cap %dx%d — clipping",
                doc_w, doc_h, cap_w, cap_h,
            )
        data = await _shoot(
            page.screenshot,
            full_page=True,
            clip={"x": 0, "y": 0, "width": clip_w, "height": clip_h},
            timeout=_remaining_ms(deadline),
            **shot_kwargs,
        )
        fallback = (clip_w, clip_h) if measured else (0, 0)
    else:  # CaptureMode.ELEMENT — selector validated by the caller
        data, fallback = await _element_shot(page, selector or "", cap_h, deadline, shot_kwargs)

    width, height = _image_size(data, fmt) or fallback
    return ScreenshotResult(
        image=data,
        mime_type=_MIME[fmt],
        width=width,
        height=height,
        format=fmt,
        truncated=truncated,
        captured_url=_page_url(page),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


async def capture_screenshot(
    page: Page,
    *,
    mode: CaptureMode | str = CaptureMode.VIEWPORT,
    selector: str | None = None,
    image_format: str = "png",
    quality: int | None = None,
    pre_scroll: bool | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECS,
    max_height: int | None = None,
    max_width: int | None = None,
    viewport: tuple[int, int] | None = None,
    scale: str | None = None,
) -> ScreenshotResult:
    """Capture a screenshot of the current page under one wall-clock budget.

    Args:
        page: an acquired pool tab. Never closed here — the caller owns it.
        mode: ``viewport`` (default), ``full_page``, or ``element``.
        selector: CSS selector, required for (and exclusive to) element mode.
        image_format: ``png`` (lossless, default) or ``jpeg`` (small — use for
            long pages that will travel base64-encoded).
        quality: JPEG quality 1–100 (default 80); invalid with png.
        pre_scroll: force lazy content in by scrolling to the bottom and back
            before capturing. Default: ON for full_page, OFF otherwise.
        timeout: overall budget in seconds; every Playwright call inside is
            additionally bounded by what remains of it.
        max_height / max_width: per-call cap override in px (clamped to
            256–32000). Default: SCREENSHOT_MAX_HEIGHT / SCREENSHOT_MAX_WIDTH
            env vars, then 20000 / 10000.
        viewport: (width, height) override applied before capture and restored
            afterwards — also the way to get a bigger "retina-ish" canvas.
        scale: Playwright screenshot scale, ``css`` or ``device``. ``device``
            yields device-pixel output on HiDPI contexts. The context's device
            scale factor itself is fixed at pool level and cannot be changed on
            a live page.

    Raises:
        ValueError: invalid argument combination (bad mode/format/quality...).
        ElementNotFoundError: element mode selector missing/ambiguous/hidden.
        ScreenshotTimeoutError: budget exceeded.
        ScreenshotError: any other capture failure.
    """
    if isinstance(mode, CaptureMode):
        mode_v = mode
    else:
        try:
            mode_v = CaptureMode(str(mode).strip().lower())
        except ValueError:
            raise ValueError(
                f"invalid mode {mode!r} — expected one of: viewport, full_page, element"
            ) from None

    fmt = str(image_format).strip().lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in _MIME:
        raise ValueError(f"invalid image_format {image_format!r} — expected 'png' or 'jpeg'")
    if fmt == "png" and quality is not None:
        raise ValueError("quality only applies to jpeg output")
    jpeg_quality: int | None = None
    if fmt == "jpeg":
        jpeg_quality = _DEFAULT_JPEG_QUALITY if quality is None else int(quality)
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("quality must be between 1 and 100")

    if mode_v is CaptureMode.ELEMENT:
        if not selector or not selector.strip():
            raise ValueError("element mode requires a CSS selector")
    elif selector:
        raise ValueError("selector is only valid with mode='element'")

    if scale is not None and scale not in ("css", "device"):
        raise ValueError("scale must be 'css' or 'device'")

    if viewport is not None:
        try:
            vp_w, vp_h = int(viewport[0]), int(viewport[1])
        except Exception:
            raise ValueError("viewport must be a (width, height) pair") from None
        if not (_VIEWPORT_LO <= vp_w <= _VIEWPORT_HI and _VIEWPORT_LO <= vp_h <= _VIEWPORT_HI):
            raise ValueError(f"viewport dimensions must be within {_VIEWPORT_LO}..{_VIEWPORT_HI}")

    cap_h = _env_cap("SCREENSHOT_MAX_HEIGHT", _DEFAULT_MAX_HEIGHT) if max_height is None \
        else max(_CAP_LO, min(_CAP_HI, int(max_height)))
    cap_w = _env_cap("SCREENSHOT_MAX_WIDTH", _DEFAULT_MAX_WIDTH) if max_width is None \
        else max(_CAP_LO, min(_CAP_HI, int(max_width)))

    do_scroll = (mode_v is CaptureMode.FULL_PAGE) if pre_scroll is None else bool(pre_scroll)
    budget = min(_MAX_TIMEOUT_SECS, max(_MIN_TIMEOUT_SECS, float(timeout)))

    shot_kwargs: dict[str, Any] = {"type": fmt, "animations": "disabled"}
    if jpeg_quality is not None:
        shot_kwargs["quality"] = jpeg_quality
    if scale is not None:
        shot_kwargs["scale"] = scale

    t0 = time.monotonic()
    deadline = t0 + budget

    original_viewport: ViewportSize | None = None
    if viewport is not None:
        # Restore-first design: remember the old size before touching it.
        current = page.viewport_size
        original_viewport = current if isinstance(current, dict) else None
        try:
            await asyncio.wait_for(
                page.set_viewport_size({"width": vp_w, "height": vp_h}), timeout=budget,
            )
        except Exception as exc:
            raise ScreenshotError(f"viewport override failed: {exc}") from exc
        if original_viewport is None:
            logger.debug("[screenshot] no original viewport recorded — override will persist")

    try:
        return await asyncio.wait_for(
            _capture(page, mode_v, selector, shot_kwargs, do_scroll, cap_w, cap_h, deadline, t0),
            timeout=max(0.05, deadline - time.monotonic()),
        )
    except asyncio.TimeoutError as exc:
        raise ScreenshotTimeoutError(
            f"screenshot ({mode_v.value}) exceeded its {budget:.1f}s budget"
        ) from exc
    finally:
        if original_viewport is not None:
            # Never leave the tab resized — callers may keep using it.
            try:
                await asyncio.wait_for(
                    page.set_viewport_size(original_viewport),
                    timeout=_VIEWPORT_RESTORE_TIMEOUT_SECS,
                )
            except Exception as exc:
                logger.warning("[screenshot] viewport restore failed: %s", exc)
