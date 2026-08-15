"""Unit tests for src/scraper/screenshot.py — AsyncMock pages, no network."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from src.scraper.screenshot import (
    ElementNotFoundError,
    ScreenshotError,
    ScreenshotTimeoutError,
    capture_screenshot,
)

_BOTTOM_SCROLL = "scrollTo(0, document.body.scrollHeight)"
_TOP_SCROLL = "scrollTo(0, 0)"


def _make_page(
    *,
    url: str = "https://example.com/page",
    doc_w: int = 1200,
    doc_h: int = 2400,
    viewport: dict | None = None,
    shot: bytes = b"not-a-real-image",
) -> AsyncMock:
    page = AsyncMock()
    page.url = url
    page.viewport_size = viewport or {"width": 1280, "height": 720}
    page.screenshot = AsyncMock(return_value=shot)
    page.set_viewport_size = AsyncMock(return_value=None)
    page.wait_for_timeout = AsyncMock(return_value=None)
    page.locator = Mock()  # sync in the real API

    def _eval(script: str, *args):
        if _BOTTOM_SCROLL in script:
            return doc_h
        if "scrollWidth" in script:
            return {"width": doc_w, "height": doc_h}
        return None

    page.evaluate = AsyncMock(side_effect=_eval)
    return page


def _make_locator(
    *,
    count: int = 1,
    box: dict | None = {"x": 0, "y": 0, "width": 120, "height": 48},
    shot: bytes = b"element-bytes",
) -> AsyncMock:
    loc = AsyncMock()
    loc.count = AsyncMock(return_value=count)
    loc.bounding_box = AsyncMock(return_value=box)
    loc.screenshot = AsyncMock(return_value=shot)
    return loc


def _png_bytes(w: int, h: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big") + b"IHDR"
        + w.to_bytes(4, "big") + h.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00" + b"\x00" * 4
    )


def _eval_scripts(page: AsyncMock) -> list[str]:
    return [call.args[0] for call in page.evaluate.await_args_list]


# --- viewport mode ------------------------------------------------------------

async def test_viewport_args_and_metadata():
    page = _make_page()
    result = await capture_screenshot(page, mode="viewport")

    kwargs = page.screenshot.await_args.kwargs
    assert kwargs["full_page"] is False
    assert kwargs["type"] == "png"
    assert "quality" not in kwargs
    assert "clip" not in kwargs

    assert result.mime_type == "image/png"
    assert result.format == "png"
    assert result.truncated is False
    assert (result.width, result.height) == (1280, 720)  # viewport fallback dims
    assert result.captured_url == "https://example.com/page"
    assert isinstance(result.elapsed_ms, int) and result.elapsed_ms >= 0
    assert result.image == b"not-a-real-image"


async def test_viewport_skips_pre_scroll():
    page = _make_page()
    await capture_screenshot(page, mode="viewport")
    assert page.evaluate.await_count == 0  # no scrolling, no measuring


# --- full_page mode -----------------------------------------------------------

async def test_full_page_pre_scrolls_then_returns_to_top():
    page = _make_page()
    await capture_screenshot(page, mode="full_page", max_height=20_000, max_width=10_000)

    scripts = _eval_scripts(page)
    bottoms = [i for i, s in enumerate(scripts) if _BOTTOM_SCROLL in s]
    tops = [i for i, s in enumerate(scripts) if _TOP_SCROLL in s]
    assert bottoms, "expected at least one scroll-to-bottom before full_page capture"
    assert tops and tops[-1] > bottoms[-1], "must return to the top after pre-scroll"
    assert page.screenshot.await_args.kwargs["full_page"] is True


async def test_full_page_pre_scroll_opt_out():
    page = _make_page()
    await capture_screenshot(page, mode="full_page", pre_scroll=False)

    scripts = _eval_scripts(page)
    assert len(scripts) == 1 and "scrollWidth" in scripts[0]  # measurement only


async def test_full_page_past_cap_truncates():
    page = _make_page(doc_h=30_000)
    result = await capture_screenshot(page, mode="full_page", max_height=20_000, max_width=10_000)

    kwargs = page.screenshot.await_args.kwargs
    assert kwargs["full_page"] is True
    assert kwargs["clip"] == {"x": 0, "y": 0, "width": 1200, "height": 20_000}
    assert result.truncated is True
    assert (result.width, result.height) == (1200, 20_000)  # clip fallback dims


async def test_full_page_under_cap_not_truncated():
    page = _make_page(doc_h=2400)
    result = await capture_screenshot(page, mode="full_page", max_height=20_000, max_width=10_000)

    assert page.screenshot.await_args.kwargs["clip"]["height"] == 2400
    assert result.truncated is False


async def test_height_cap_env_override(monkeypatch):
    monkeypatch.setenv("SCREENSHOT_MAX_HEIGHT", "1000")
    page = _make_page(doc_h=2400)
    result = await capture_screenshot(page, mode="full_page")

    assert page.screenshot.await_args.kwargs["clip"]["height"] == 1000
    assert result.truncated is True


async def test_measure_failure_still_clips_to_caps():
    page = _make_page()

    def _eval(script: str, *args):
        if "scrollWidth" in script:
            raise RuntimeError("execution context destroyed")
        return None

    page.evaluate = AsyncMock(side_effect=_eval)
    result = await capture_screenshot(
        page, mode="full_page", pre_scroll=False, max_height=20_000, max_width=10_000,
    )

    # Unmeasurable page: clip at the caps anyway (Playwright trims to the doc).
    assert page.screenshot.await_args.kwargs["clip"] == {
        "x": 0, "y": 0, "width": 10_000, "height": 20_000,
    }
    assert result.truncated is False  # unknown — never claimed


# --- element mode ---------------------------------------------------------

async def test_element_capture_uses_locator():
    page = _make_page()
    loc = _make_locator(shot=b"element-bytes")
    page.locator = Mock(return_value=loc)

    result = await capture_screenshot(page, mode="element", selector="#hero")

    page.locator.assert_called_once_with("#hero")
    assert loc.screenshot.await_count == 1
    assert page.screenshot.await_count == 0
    assert loc.screenshot.await_args.kwargs["type"] == "png"
    assert result.image == b"element-bytes"
    assert (result.width, result.height) == (120, 48)  # bounding-box fallback dims
    assert page.evaluate.await_count == 0  # element mode: no pre-scroll by default


async def test_element_missing_raises():
    page = _make_page()
    page.locator = Mock(return_value=_make_locator(count=0))

    with pytest.raises(ElementNotFoundError, match="no element matches selector '#nope'"):
        await capture_screenshot(page, mode="element", selector="#nope")


async def test_element_ambiguous_raises():
    page = _make_page()
    page.locator = Mock(return_value=_make_locator(count=3))

    with pytest.raises(ElementNotFoundError, match="matches 3 elements"):
        await capture_screenshot(page, mode="element", selector="div")


async def test_element_not_visible_raises():
    page = _make_page()
    page.locator = Mock(return_value=_make_locator(box=None))

    with pytest.raises(ElementNotFoundError, match="not visible"):
        await capture_screenshot(page, mode="element", selector="#hidden")


async def test_element_too_tall_raises():
    page = _make_page()
    page.locator = Mock(return_value=_make_locator(
        box={"x": 0, "y": 0, "width": 800, "height": 50_000},
    ))

    with pytest.raises(ScreenshotError, match="over the 20000px cap"):
        await capture_screenshot(page, mode="element", selector="#feed", max_height=20_000)


# --- formats, scale, validation ---------------------------------------------

async def test_jpeg_quality_and_scale():
    page = _make_page()
    result = await capture_screenshot(page, image_format="jpeg", quality=70, scale="device")

    kwargs = page.screenshot.await_args.kwargs
    assert kwargs["type"] == "jpeg"
    assert kwargs["quality"] == 70
    assert kwargs["scale"] == "device"
    assert result.mime_type == "image/jpeg"
    assert result.format == "jpeg"

    # jpeg without an explicit quality gets the sane default
    page2 = _make_page()
    await capture_screenshot(page2, image_format="jpg")
    assert page2.screenshot.await_args.kwargs["quality"] == 80


async def test_png_with_quality_raises():
    with pytest.raises(ValueError, match="quality only applies to jpeg"):
        await capture_screenshot(_make_page(), image_format="png", quality=90)


async def test_invalid_args_raise():
    page = _make_page()
    with pytest.raises(ValueError, match="invalid mode"):
        await capture_screenshot(page, mode="fullscreen")
    with pytest.raises(ValueError, match="requires a CSS selector"):
        await capture_screenshot(page, mode="element")
    with pytest.raises(ValueError, match="only valid with mode='element'"):
        await capture_screenshot(page, mode="viewport", selector="#x")
    assert page.screenshot.await_count == 0  # validation failures never touch the tab


# --- timeout & viewport override ---------------------------------------------

async def test_timeout_raises_and_restores_viewport():
    page = _make_page(viewport={"width": 1280, "height": 720})

    async def _hang(**kwargs):
        await asyncio.Event().wait()  # never set — cancelled by the budget guard

    page.screenshot = AsyncMock(side_effect=_hang)

    with pytest.raises(ScreenshotTimeoutError):
        await capture_screenshot(page, mode="viewport", viewport=(800, 600), timeout=0.05)

    sizes = [call.args[0] for call in page.set_viewport_size.await_args_list]
    assert sizes[0] == {"width": 800, "height": 600}  # override applied
    assert sizes[-1] == {"width": 1280, "height": 720}  # restored on the way out


async def test_real_png_dims_win_over_fallback():
    page = _make_page(shot=_png_bytes(321, 654))
    result = await capture_screenshot(page, mode="viewport")

    # Parsed from the bytes, not the 1280x720 viewport fallback.
    assert (result.width, result.height) == (321, 654)
