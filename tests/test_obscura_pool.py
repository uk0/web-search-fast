"""Unit tests for src/scraper/obscura_pool.py — mocked Playwright, no network."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.scraper.obscura_pool import ObscuraPool, _env_int, _redact


class _FakeBrowser:
    """Stand-in for a Playwright Browser over CDP; counts context lifecycle."""

    def __init__(self) -> None:
        self.contexts_opened = 0
        self.contexts_closed = 0
        self.pages_closed = 0
        self.closed = False
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    async def new_context(self):
        self.contexts_opened += 1
        ctx = AsyncMock()

        async def _close_ctx():
            self.contexts_closed += 1
        ctx.close = AsyncMock(side_effect=_close_ctx)

        async def _new_page():
            page = AsyncMock()

            async def _close_page():
                self.pages_closed += 1
            page.close = AsyncMock(side_effect=_close_page)
            return page
        ctx.new_page = AsyncMock(side_effect=_new_page)
        return ctx

    async def close(self):
        self.closed = True
        self._connected = False


def _patched_pool(browser: _FakeBrowser, **kw) -> ObscuraPool:
    """ObscuraPool whose connect_over_cdp yields the given fake browser."""
    kw.setdefault("cdp_url", "ws://obscura:9222/devtools/browser")
    pool = ObscuraPool(**kw)
    fake_pw = MagicMock()
    fake_pw.chromium.connect_over_cdp = AsyncMock(return_value=browser)
    pool._pw = fake_pw  # skip async_playwright().start()
    return pool


# --- helpers ------------------------------------------------------------------

def test_redact_strips_credentials():
    assert _redact("ws://user:pass@host:9222/x") == "ws://host:9222/x"
    assert _redact("ws://obscura:9222/devtools/browser") == "ws://obscura:9222/devtools/browser"


def test_env_int_clamps_and_defaults(monkeypatch):
    monkeypatch.setenv("OBSCURA_CONCURRENCY", "banana")
    assert _env_int("OBSCURA_CONCURRENCY", 4) == 4
    monkeypatch.setenv("OBSCURA_CONCURRENCY", "999")
    assert _env_int("OBSCURA_CONCURRENCY", 4) == 64  # clamped
    monkeypatch.setenv("OBSCURA_CONCURRENCY", "0")
    assert _env_int("OBSCURA_CONCURRENCY", 4) == 1   # floored


# --- acquire / release --------------------------------------------------------

async def test_acquire_yields_page_and_releases_once():
    browser = _FakeBrowser()
    pool = _patched_pool(browser)
    async with pool.acquire(label="t") as page:
        assert page is not None
        assert pool.stats["active_tabs"] == 1
    # released exactly once — a leaked context would pin an obscura worker
    assert browser.contexts_opened == 1
    assert browser.contexts_closed == 1
    assert browser.pages_closed == 1
    assert pool.stats["active_tabs"] == 0
    assert pool.stats["total_requests"] == 1
    assert pool.stats["total_failures"] == 0


async def test_exception_in_body_still_releases_and_counts_failure():
    browser = _FakeBrowser()
    pool = _patched_pool(browser)
    with pytest.raises(RuntimeError, match="boom"):
        async with pool.acquire():
            raise RuntimeError("boom")
    assert browser.contexts_closed == 1  # released despite the error
    assert browser.pages_closed == 1
    assert pool.stats["active_tabs"] == 0
    assert pool.stats["total_failures"] == 1


async def test_close_errors_are_swallowed_no_double_raise():
    browser = _FakeBrowser()
    pool = _patched_pool(browser)

    async def _boom_ctx():
        raise RuntimeError("context.close failed")

    orig_new = browser.new_context

    async def _new_context():
        ctx = await orig_new()
        ctx.close = AsyncMock(side_effect=_boom_ctx)  # closing raises
        return ctx
    browser.new_context = _new_context

    # A raising close() must not propagate out of the context manager
    async with pool.acquire() as page:
        assert page is not None
    assert pool.stats["active_tabs"] == 0  # counter still released


async def test_concurrency_cap_serializes_excess():
    browser = _FakeBrowser()
    pool = _patched_pool(browser, concurrency=2)
    started = 0
    peak = 0
    release = asyncio.Event()

    async def worker():
        nonlocal started, peak
        async with pool.acquire():
            started += 1
            peak = max(peak, pool.stats["active_tabs"])
            await release.wait()

    tasks = [asyncio.create_task(worker()) for _ in range(5)]
    await asyncio.sleep(0.05)
    assert peak <= 2  # semaphore held the line at the cap
    release.set()
    await asyncio.gather(*tasks)
    assert pool.stats["active_tabs"] == 0


async def test_reconnects_when_disconnected():
    browser1 = _FakeBrowser()
    browser2 = _FakeBrowser()
    pool = ObscuraPool(cdp_url="ws://obscura:9222/devtools/browser")
    fake_pw = MagicMock()
    fake_pw.chromium.connect_over_cdp = AsyncMock(side_effect=[browser1, browser2])
    pool._pw = fake_pw

    async with pool.acquire():
        pass
    browser1._connected = False  # connection dropped
    async with pool.acquire():
        pass
    assert fake_pw.chromium.connect_over_cdp.await_count == 2  # reconnected
    assert pool.stats["connect_count"] == 2


async def test_stop_closes_browser_and_marks_stopped():
    browser = _FakeBrowser()
    pool = _patched_pool(browser)
    async with pool.acquire():
        pass
    await pool.stop()
    assert browser.closed is True
    assert pool.stats["started"] is False
    assert pool.stats["connected"] is False


async def test_stats_shape_and_redaction():
    pool = _patched_pool(_FakeBrowser(), cdp_url="ws://user:secret@obscura:9222/devtools/browser")
    s = pool.stats
    assert s["backend"] == "obscura"
    assert "secret" not in s["cdp_url"]  # credentials never surfaced
    for key in ("started", "connected", "concurrency", "active_tabs",
                "total_requests", "total_failures", "connect_count"):
        assert key in s
