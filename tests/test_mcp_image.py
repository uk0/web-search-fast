"""Unit tests for src/scraper/mcp_image.py — MCP image payload budgeting.

All images are synthesized in-memory with a stdlib-only PNG encoder; no disk,
no network, no real browser. The Pillow-dependent shrink ladder is exercised
through a deterministic fake PIL module patched onto the module under test, so
these tests pass whether or not Pillow is installed. One real-Pillow test
auto-skips when Pillow is absent.
"""

from __future__ import annotations

import base64
import random
import struct
import zlib

import pytest
from mcp.server.fastmcp import Image as FastMCPImage

from src.scraper import mcp_image
from src.scraper.mcp_image import (
    BUDGET_ENV_VAR,
    DEFAULT_BUDGET_BYTES,
    base64_size,
    fit_image_to_budget,
    fit_image_to_budget_sync,
    to_mcp_image,
)


def make_png(width: int, height: int, total_len: int | None = None, noise_seed: int | None = None) -> bytes:
    """Minimal valid PNG (RGB, filter 0), optionally zero-padded past IEND to
    an exact total length (header/IHDR stay valid; decoders stop at IEND)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    if noise_seed is not None:
        rng = random.Random(noise_seed)
        raw = b"".join(b"\x00" + rng.randbytes(width * 3) for _ in range(height))
    else:
        raw = (b"\x00" + b"\x7f" * (width * 3)) * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")
    if total_len is not None and len(png) < total_len:
        png += b"\x00" * (total_len - len(png))
    return png


class FakePILImage:
    """Mimics the slice of PIL.Image.Image that mcp_image touches."""

    def __init__(self, size, mode="RGBA", fmt="PNG", jpeg_cost=None):
        self.size = size
        self.mode = mode
        self.format = fmt
        self.jpeg_cost = jpeg_cost or (lambda w, h, q: (w * h) // 10)

    def load(self):
        return None

    def convert(self, mode):
        return FakePILImage(self.size, mode, self.format, self.jpeg_cost)

    def resize(self, size, resample=None):
        assert resample == FakePILModule.Resampling.LANCZOS
        return FakePILImage(size, self.mode, self.format, self.jpeg_cost)

    def save(self, buf, format=None, quality=75, optimize=False):
        assert format == "JPEG"
        buf.write(b"J" * self.jpeg_cost(self.size[0], self.size[1], quality))


class FakePILModule:
    """Stands in for the PIL.Image module (mcp_image.PIL_IMAGE)."""

    class Resampling:
        LANCZOS = "LANCZOS"

    def __init__(self, image=None, error=None):
        self._image = image
        self._error = error

    def open(self, fp):
        if self._error is not None:
            raise self._error
        return self._image


class TestShrinkLadder:
    def test_quality_rung_fits(self, monkeypatch):
        data = make_png(1000, 800, total_len=500_000)
        costs = {85: 120_000, 70: 90_000}
        fake = FakePILModule(FakePILImage((1000, 800), jpeg_cost=lambda w, h, q: costs.get(q, 5_000)))
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", fake)

        r = fit_image_to_budget_sync(data, budget_bytes=100_000)

        assert r.within_budget
        assert r.original_bytes == 500_000
        assert r.final_bytes == 90_000  # q85 missed, q70 fit
        assert r.format == "jpeg"
        assert (r.width, r.height) == (1000, 800)
        assert r.steps == ("re-encoded png -> jpeg q70",)
        assert "jpeg q70" in r.summary

    def test_downscale_rung_fits(self, monkeypatch):
        data = make_png(1000, 800, total_len=400_000)
        # 80_000 B at full size regardless of quality; 20_000 B once halved.
        fake = FakePILModule(FakePILImage((1000, 800), jpeg_cost=lambda w, h, q: max(1_000, (w * h) // 10)))
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", fake)

        r = fit_image_to_budget_sync(data, budget_bytes=30_000)

        assert r.within_budget
        assert r.final_bytes == 20_000
        assert (r.width, r.height) == (500, 400)
        assert r.steps == (
            "downscaled 2x (1000x800 -> 500x400)",
            "re-encoded png -> jpeg q70",
        )

    def test_budget_unreachable_returns_best_effort(self, monkeypatch):
        # 100x100 is below the halving floor, and every JPEG rung lands at
        # 150_000 B — smaller than the original but still over budget.
        data = make_png(100, 100, total_len=200_000)
        fake = FakePILModule(FakePILImage((100, 100), jpeg_cost=lambda w, h, q: 150_000))
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", fake)

        r = fit_image_to_budget_sync(data, budget_bytes=50_000)

        assert not r.within_budget
        assert r.final_bytes == 150_000 < r.original_bytes
        assert r.format == "jpeg"
        assert (r.width, r.height) == (100, 100)
        assert r.steps[0] == "re-encoded png -> jpeg q85"
        assert "budget unreachable" in r.steps[-1]

    def test_encoder_refusal_degrades_instead_of_raising(self, monkeypatch):
        # Real Pillow raises OSError("encoder error -2") for a JPEG axis over
        # 65500 px; an MCP tool must get a flagged image back, not a traceback.
        class RefusingSave(FakePILImage):
            def save(self, buf, format=None, quality=75, optimize=False):
                raise OSError("encoder error -2 when writing image file")

        data = make_png(100, 100, total_len=40_000)
        # mode="RGB" so convert() is skipped and save() is reached directly.
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", FakePILModule(RefusingSave((70_000, 8), mode="RGB")))

        r = fit_image_to_budget_sync(data, budget_bytes=1_000)

        assert r.data == data  # original handed back untouched
        assert not r.within_budget
        assert any("jpeg encode failed" in s for s in r.steps)

    def test_convert_failure_degrades_instead_of_raising(self, monkeypatch):
        class RefusingConvert(FakePILImage):
            def convert(self, mode):
                raise MemoryError("cannot allocate RGB buffer")

        data = make_png(100, 100, total_len=40_000)
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", FakePILModule(RefusingConvert((10_000, 20_000))))

        r = fit_image_to_budget_sync(data, budget_bytes=1_000)

        assert r.data == data
        assert not r.within_budget
        assert any("convert failed" in s for s in r.steps)

    def test_decode_failure_passes_through_flagged(self, monkeypatch):
        data = make_png(10, 10, total_len=9_000)
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", FakePILModule(error=OSError("broken stream")))

        r = fit_image_to_budget_sync(data, budget_bytes=1_000)

        assert r.data == data
        assert not r.within_budget
        assert any("decode failed" in s for s in r.steps)


class TestWithoutPillow:
    def test_under_budget_untouched_with_sniffed_metadata(self, monkeypatch):
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", None)
        data = make_png(320, 200)

        r = fit_image_to_budget_sync(data, budget_bytes=10 * len(data))

        assert r.data == data
        assert r.within_budget
        assert r.format == "png"
        assert (r.width, r.height) == (320, 200)  # parsed from IHDR, no Pillow
        assert r.final_bytes == r.original_bytes == len(data)
        assert r.steps == (f"kept original ({len(data)} B <= budget {10 * len(data)} B)",)

    def test_over_budget_flagged_not_crashed(self, monkeypatch):
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", None)
        data = make_png(320, 200, total_len=50_000)

        r = fit_image_to_budget_sync(data, budget_bytes=10_000)

        assert r.data == data  # pass-through, never raises
        assert not r.within_budget
        assert r.final_bytes == 50_000
        assert r.budget_bytes == 10_000
        assert any("pillow not installed" in s for s in r.steps)


def test_budget_resolution_env_and_arg(monkeypatch):
    monkeypatch.setattr(mcp_image, "PIL_IMAGE", None)
    data = b"x" * 100

    monkeypatch.setenv(BUDGET_ENV_VAR, "50")
    assert fit_image_to_budget_sync(data).budget_bytes == 50  # env applies
    assert not fit_image_to_budget_sync(data).within_budget

    assert fit_image_to_budget_sync(data, budget_bytes=1_000).budget_bytes == 1_000  # arg beats env

    monkeypatch.setenv(BUDGET_ENV_VAR, "banana")
    assert fit_image_to_budget_sync(data).budget_bytes == DEFAULT_BUDGET_BYTES  # invalid -> default

    monkeypatch.setenv(BUDGET_ENV_VAR, "-5")
    assert fit_image_to_budget_sync(data).budget_bytes == DEFAULT_BUDGET_BYTES  # non-positive -> default


def test_base64_size_matches_real_encoding():
    for n in (0, 1, 2, 3, 4, 5, 57, 100, 999):
        assert base64_size(n) == len(base64.b64encode(b"\x00" * n))


class TestMCPImageHelper:
    def test_png_roundtrip_to_image_content(self):
        data = make_png(16, 16)
        r = fit_image_to_budget_sync(data, budget_bytes=1_000_000)

        img = to_mcp_image(r)

        assert isinstance(img, FastMCPImage)
        content = img.to_image_content()
        assert content.type == "image"
        assert content.mimeType == "image/png"
        assert base64.b64decode(content.data) == data
        assert len(content.data) == r.base64_bytes  # reported wire size is exact

    def test_jpeg_result_gets_jpeg_mime(self, monkeypatch):
        fake = FakePILModule(FakePILImage((100, 100), jpeg_cost=lambda w, h, q: 500))
        monkeypatch.setattr(mcp_image, "PIL_IMAGE", fake)
        r = fit_image_to_budget_sync(make_png(100, 100, total_len=5_000), budget_bytes=1_000)

        assert r.format == "jpeg"
        content = to_mcp_image(r).to_image_content()
        assert content.mimeType == "image/jpeg"
        assert len(content.data) == r.base64_bytes


async def test_async_wrapper_matches_sync(monkeypatch):
    monkeypatch.setattr(mcp_image, "PIL_IMAGE", None)
    data = make_png(32, 32)

    r = await fit_image_to_budget(data, budget_bytes=500_000)

    assert r == fit_image_to_budget_sync(data, budget_bytes=500_000)


@pytest.mark.skipif(mcp_image.PIL_IMAGE is None, reason="Pillow not installed in this environment")
def test_real_pillow_shrinks_noise_png():
    # Incompressible noise: PNG stays ~raw-sized, so the ladder must act.
    data = make_png(300, 300, noise_seed=7)
    assert len(data) > 100_000

    r = fit_image_to_budget_sync(data, budget_bytes=30_000)

    assert r.within_budget
    assert r.final_bytes <= 30_000
    assert r.format == "jpeg"
    assert r.final_bytes < r.original_bytes
    assert r.steps  # always reports what it did
