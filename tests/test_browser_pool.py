from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scraper.browser import BrowserPool


@pytest.fixture
def mock_browser():
    browser = AsyncMock()
    page = AsyncMock()
    page.close = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    return browser, page


@pytest.fixture
def mock_camoufox(mock_browser):
    browser, _ = mock_browser
    camoufox_instance = AsyncMock()
    camoufox_instance.__aenter__ = AsyncMock(return_value=browser)
    camoufox_instance.__aexit__ = AsyncMock(return_value=None)
    return camoufox_instance


class TestBrowserPoolInit:
    def test_defaults(self):
        pool = BrowserPool()
        assert pool._pool_size == 30
        assert pool._headless is True
        assert pool._started is False

    def test_custom_params(self):
        pool = BrowserPool(pool_size=3, headless=False, geoip=False)
        assert pool._pool_size == 3
        assert pool._headless is False
        assert pool._geoip is False


class TestBrowserPoolStart:
    @pytest.mark.asyncio
    async def test_start_creates_browser_no_pages(self, mock_camoufox):
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=3)
            await pool.start()
            assert pool._started is True
            assert pool._browser is not None
            # No pre-created pages — browser.new_page should NOT be called during start
            pool._browser.new_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, mock_camoufox):
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            await pool.start()  # second call should be no-op
            mock_camoufox.__aenter__.assert_awaited_once()


class TestBrowserPoolAcquire:
    @pytest.mark.asyncio
    async def test_acquire_creates_new_page(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()

            async with pool.acquire() as p:
                assert p is page
                browser.new_page.assert_awaited_once()

            # page.close called after context exit
            page.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_acquire_closes_page_on_exception(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()

            with pytest.raises(RuntimeError):
                async with pool.acquire() as p:
                    raise RuntimeError("boom")

            page.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, mock_camoufox, mock_browser):
        browser, _ = mock_browser
        # Each new_page call returns a fresh mock
        browser.new_page = AsyncMock(side_effect=lambda: AsyncMock())

        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2, max_pool_size=2)
            await pool.start()

            active = 0
            max_active = 0

            async def task():
                nonlocal active, max_active
                async with pool.acquire():
                    active += 1
                    max_active = max(max_active, active)
                    await asyncio.sleep(0.05)
                    active -= 1

            await asyncio.gather(*[task() for _ in range(5)])
            assert max_active <= 2


class TestBrowserPoolStop:
    @pytest.mark.asyncio
    async def test_stop_closes_browser(self, mock_camoufox):
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            await pool.stop()
            assert pool._started is False
            assert pool._browser is None
            mock_camoufox.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, mock_camoufox):
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            await pool.stop()
            await pool.stop()  # no-op
            mock_camoufox.__aexit__.assert_awaited_once()


class TestBrowserPoolHealth:
    def test_initial_stats(self):
        pool = BrowserPool()
        stats = pool.stats
        assert stats["total_requests"] == 0
        assert stats["total_failures"] == 0
        assert stats["consecutive_failures"] == 0
        assert stats["restart_count"] == 0

    def test_record_success_resets_consecutive(self):
        pool = BrowserPool()
        pool._consecutive_failures = 5
        pool.record_success()
        assert pool._consecutive_failures == 0

    def test_record_failure_increments(self):
        pool = BrowserPool()
        pool.record_failure()
        pool.record_failure()
        assert pool._consecutive_failures == 2
        assert pool._total_failures == 2

    def test_needs_restart_threshold(self):
        pool = BrowserPool()
        assert pool.needs_restart is False
        pool._consecutive_failures = 2
        assert pool.needs_restart is False
        pool._consecutive_failures = 3
        assert pool.needs_restart is True

    @pytest.mark.asyncio
    async def test_restart_resets_state(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        page.goto = AsyncMock()
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            pool._consecutive_failures = 5
            await pool.restart()
            assert pool._consecutive_failures == 0
            assert pool._restart_count == 1
            assert pool._started is True

    @pytest.mark.asyncio
    async def test_acquire_auto_restarts_on_failures(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        page.goto = AsyncMock()
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()
            pool._consecutive_failures = 3  # trigger threshold

            async with pool.acquire() as p:
                assert p is page

            assert pool._restart_count == 1
            assert pool._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_acquire_restarts_on_new_page_failure(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        # First new_page fails, second succeeds (after restart)
        browser.new_page = AsyncMock(side_effect=[Exception("browser crashed"), page])
        page.goto = AsyncMock()
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()

            async with pool.acquire() as p:
                assert p is page

            assert pool._restart_count == 1

    @pytest.mark.asyncio
    async def test_is_healthy_true(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        page.goto = AsyncMock()
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            assert await pool.is_healthy() is True

    @pytest.mark.asyncio
    async def test_is_healthy_false_when_stopped(self):
        pool = BrowserPool()
        assert await pool.is_healthy() is False

    @pytest.mark.asyncio
    async def test_is_healthy_false_on_error(self, mock_camoufox, mock_browser):
        browser, _ = mock_browser
        browser.new_page = AsyncMock(side_effect=Exception("dead"))
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            assert await pool.is_healthy() is False
