"""Byte-budgeting for screenshots returned through MCP tools.

MCP tools return images as base64 inside the JSON-RPC response, which inflates
raw bytes by ~4/3 and lands the whole payload directly in the LLM context. A
full-page PNG of a long article can be 5-15 MB raw (~20 MB of base64) — every
screenshot must therefore pass through :func:`fit_image_to_budget` before it
is returned from a tool.

Shrink ladder — the cheapest sufficient strategy wins, in order:

1. keep the bytes as-is if they already fit the budget;
2. re-encode (PNG or other formats) to JPEG at high quality;
3. lower the JPEG quality stepwise;
4. downscale dimensions by halving and re-encode.

Pillow is an optional dependency: it is used only when already importable.
Without Pillow the module degrades to a pass-through that reports the image
was left untouched and flags that it exceeds the budget — it never crashes.

Encoding, decoding, and resizing are pure CPU work: the public coroutine
:func:`fit_image_to_budget` offloads them to a worker thread via
``asyncio.to_thread`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import struct
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Image as MCPImage

__all__ = [
    "BUDGET_ENV_VAR",
    "DEFAULT_BUDGET_BYTES",
    "BudgetedImage",
    "base64_size",
    "fit_image_to_budget",
    "fit_image_to_budget_sync",
    "to_mcp_image",
]

# Raw-byte budget; ~4/3 of this actually hits the wire as base64.
BUDGET_ENV_VAR = "MCP_IMAGE_BUDGET_BYTES"
DEFAULT_BUDGET_BYTES = 1_048_576  # 1 MiB raw ≈ 1.4 MiB base64 in the model context

_JPEG_QUALITIES = (85, 70, 55, 40)  # ladder rungs 2+3: re-encode, then degrade
_DOWNSCALE_QUALITY = 70  # quality used once we start halving dimensions
_MIN_DIMENSION = 64  # never halve an axis below this — unreadable thumbnails help nobody
_MAX_HALVINGS = 4

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _load_pil() -> Any:
    """Return the ``PIL.Image`` module when Pillow is installed, else ``None``."""
    try:
        return importlib.import_module("PIL.Image")
    except ImportError:
        return None


# Resolved once at import time; tests patch this to simulate a missing Pillow.
PIL_IMAGE: Any = _load_pil()


def base64_size(raw_len: int) -> int:
    """Bytes of standard padded base64 for ``raw_len`` raw bytes — the wire cost."""
    return ((raw_len + 2) // 3) * 4


def _default_budget() -> int:
    """Budget from ``MCP_IMAGE_BUDGET_BYTES``; invalid or missing → default."""
    raw = os.environ.get(BUDGET_ENV_VAR)
    if raw is None:
        return DEFAULT_BUDGET_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BUDGET_BYTES
    return value if value > 0 else DEFAULT_BUDGET_BYTES


def _sniff(data: bytes) -> tuple[str | None, tuple[int, int] | None]:
    """Best-effort format/dimensions from magic bytes — works without Pillow."""
    if data.startswith(_PNG_MAGIC):
        if len(data) >= 24 and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
            return "png", (int(width), int(height))
        return "png", None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", None  # dimensions refined via Pillow when available
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", None
    return None, None


@dataclass(frozen=True)
class BudgetedImage:
    """Outcome of budgeting one screenshot for an MCP response.

    ``base64_bytes`` is the number that actually lands in the model context:
    MCP transports images base64-encoded inside the JSON-RPC response.
    """

    data: bytes
    format: str | None  # lowercase ("png", "jpeg"); None when undetectable
    width: int | None
    height: int | None
    original_bytes: int
    final_bytes: int
    base64_bytes: int
    budget_bytes: int
    within_budget: bool
    steps: tuple[str, ...]  # human-readable transformations applied to ``data``

    @property
    def summary(self) -> str:
        """One line the tool can surface next to the image (what was done)."""
        head = (
            f"{self.format or 'unknown'} {self.width or '?'}x{self.height or '?'}, "
            f"{self.final_bytes} B raw / {self.base64_bytes} B base64"
        )
        return f"{head}: {'; '.join(self.steps)}"


def _result(
    data: bytes,
    fmt: str | None,
    dims: tuple[int, int] | None,
    original: int,
    budget: int,
    steps: tuple[str, ...],
) -> BudgetedImage:
    return BudgetedImage(
        data=data,
        format=fmt,
        width=dims[0] if dims else None,
        height=dims[1] if dims else None,
        original_bytes=original,
        final_bytes=len(data),
        base64_bytes=base64_size(len(data)),
        budget_bytes=budget,
        within_budget=len(data) <= budget,
        steps=steps,
    )


def _probe(data: bytes, fmt: str | None, dims: tuple[int, int] | None) -> tuple[str | None, tuple[int, int] | None]:
    """Refine format/dimensions with Pillow (header-only read, no full decode)."""
    try:
        with PIL_IMAGE.open(io.BytesIO(data)) as img:
            probed_fmt = (img.format or "").lower() or None
            return fmt or probed_fmt, dims or (int(img.size[0]), int(img.size[1]))
    except Exception:  # undecodable — keep whatever the magic bytes said
        return fmt, dims


def _shrink(data: bytes, src_fmt: str | None, original: int, budget: int) -> BudgetedImage:
    """Walk the ladder until the encoded image fits; best effort otherwise."""
    try:
        img = PIL_IMAGE.open(io.BytesIO(data))
        img.load()  # force the full decode now so failures are caught here
    except Exception:
        # A too-big screenshot is still better than none: pass through, flagged.
        return _result(
            data,
            src_fmt,
            None,
            original,
            budget,
            (f"decode failed: image left untouched; exceeds budget ({original} B > {budget} B)",),
        )

    src = (img.format or src_fmt or "image").lower()
    full_size = (int(img.size[0]), int(img.size[1]))
    if img.mode not in ("RGB", "L"):
        try:
            img = img.convert("RGB")  # JPEG cannot carry alpha
        except Exception as exc:  # conversion can fail/OOM on pathological images
            return _result(
                data,
                src_fmt,
                full_size,
                original,
                budget,
                (f"convert failed ({exc}): image left untouched; exceeds budget ({original} B > {budget} B)",),
            )

    best: bytes = data
    best_fmt: str | None = src_fmt
    best_dims: tuple[int, int] | None = full_size
    best_steps: tuple[str, ...] = ()
    codec_error: str | None = None  # first encoder refusal, surfaced in ``steps``

    def attempt(candidate: Any, quality: int, steps: tuple[str, ...]) -> BudgetedImage | None:
        """Encode one rung; return a finished result when it fits the budget."""
        nonlocal best, best_fmt, best_dims, best_steps, codec_error
        buf = io.BytesIO()
        try:
            candidate.save(buf, format="JPEG", quality=quality, optimize=True)
        except Exception as exc:  # e.g. Pillow refuses a JPEG axis over 65500 px
            codec_error = codec_error or f"jpeg encode failed ({exc})"
            return None
        payload = buf.getvalue()
        size = (int(candidate.size[0]), int(candidate.size[1]))
        if len(payload) < len(best):
            best, best_fmt, best_dims, best_steps = payload, "jpeg", size, steps
        if len(payload) <= budget:
            return _result(payload, "jpeg", size, original, budget, steps)
        return None

    # Rungs 2+3: re-encode as JPEG, walking the quality down.
    for quality in _JPEG_QUALITIES:
        done = attempt(img, quality, (f"re-encoded {src} -> jpeg q{quality}",))
        if done is not None:
            return done

    # Rung 4: halve dimensions, re-encode at a fixed moderate quality.
    resample = getattr(PIL_IMAGE, "Resampling", PIL_IMAGE).LANCZOS
    current = img
    factor = 1
    for _ in range(_MAX_HALVINGS):
        width, height = int(current.size[0]), int(current.size[1])
        if min(width, height) < 2 * _MIN_DIMENSION:
            break
        try:
            current = current.resize((width // 2, height // 2), resample)
        except Exception as exc:  # resampling can fail/OOM on pathological sizes
            codec_error = codec_error or f"downscale failed ({exc})"
            break
        factor *= 2
        steps = (
            f"downscaled {factor}x ({full_size[0]}x{full_size[1]} -> {int(current.size[0])}x{int(current.size[1])})",
            f"re-encoded {src} -> jpeg q{_DOWNSCALE_QUALITY}",
        )
        done = attempt(current, _DOWNSCALE_QUALITY, steps)
        if done is not None:
            return done

    # Budget unreachable — return the smallest candidate produced, flagged.
    note = f"budget unreachable: best effort {len(best)} B > budget {budget} B"
    if codec_error is not None:
        note = f"{codec_error}; {note}"
    return _result(best, best_fmt, best_dims, original, budget, best_steps + (note,))


def fit_image_to_budget_sync(data: bytes, budget_bytes: int | None = None) -> BudgetedImage:
    """Shrink screenshot bytes under a raw-byte budget (blocking variant).

    ``budget_bytes`` of ``None`` or <= 0 falls back to ``MCP_IMAGE_BUDGET_BYTES``
    (env), then :data:`DEFAULT_BUDGET_BYTES`. Every path reports what it did in
    ``steps``. This call blocks on CPU-bound codec work — from async code use
    :func:`fit_image_to_budget` instead.
    """
    budget = budget_bytes if budget_bytes is not None and budget_bytes > 0 else _default_budget()
    original = len(data)
    fmt, dims = _sniff(data)

    if PIL_IMAGE is not None and (fmt is None or dims is None):
        fmt, dims = _probe(data, fmt, dims)

    if original <= budget:
        return _result(data, fmt, dims, original, budget, (f"kept original ({original} B <= budget {budget} B)",))

    if PIL_IMAGE is None:
        return _result(
            data,
            fmt,
            dims,
            original,
            budget,
            (f"pillow not installed: image left untouched; exceeds budget ({original} B > {budget} B)",),
        )

    return _shrink(data, fmt, original, budget)


async def fit_image_to_budget(data: bytes, budget_bytes: int | None = None) -> BudgetedImage:
    """Async facade over :func:`fit_image_to_budget_sync`.

    Decoding/encoding/resizing are pure CPU work, so they run in a worker
    thread via ``asyncio.to_thread`` — the event loop is never blocked.
    """
    return await asyncio.to_thread(fit_image_to_budget_sync, data, budget_bytes)


def to_mcp_image(result: BudgetedImage) -> MCPImage:
    """Wrap budgeted bytes as the FastMCP ``Image`` returned from a ``@mcp.tool``.

    Verified against the installed mcp 1.27.1: ``Image(data=..., format=...)``,
    where ``format`` becomes ``image/{format.lower()}`` and ``None`` with raw
    data defaults to ``image/png``; ``Image.to_image_content()`` produces the
    base64 ``ImageContent`` block for the JSON-RPC response.
    """
    return MCPImage(data=result.data, format=result.format)
