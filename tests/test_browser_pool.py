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
    # Non-proxy path now uses explicit new_context().new_page() for isolation
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
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
        assert pool._pool_size == 18
        assert pool._max_pool_size == 36
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
                # Explicit per-request context for tab-level isolation
                browser.new_context.assert_awaited_once()

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
    async def test_acquire_restarts_on_new_context_failure(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        # First new_context fails, second succeeds (after restart)
        good_ctx = AsyncMock()
        good_ctx.new_page = AsyncMock(return_value=page)
        good_ctx.close = AsyncMock()
        browser.new_context = AsyncMock(side_effect=[Exception("browser crashed"), good_ctx])
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


class TestRecordFailureClassification:
    def test_proxy_failure_does_not_bump_consecutive(self):
        pool = BrowserPool()
        pool.record_failure(browser_related=False)
        pool.record_failure(browser_related=False)
        pool.record_failure(browser_related=False)
        assert pool._total_failures == 3
        assert pool._consecutive_failures == 0
        assert pool.needs_restart is False

    def test_browser_failure_bumps_consecutive(self):
        pool = BrowserPool()
        pool.record_failure(browser_related=True)
        pool.record_failure()  # default is browser_related
        assert pool._consecutive_failures == 2
        assert pool._total_failures == 2


class TestRestartCooldown:
    @pytest.mark.asyncio
    async def test_preventive_rapid_second_restart_skipped(self, mock_camoufox):
        # Preventive restarts (no expected_generation) are throttled by cooldown
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            await pool.restart()
            count_after_first = pool._restart_count
            await pool.restart()  # within cooldown — skipped
            assert pool._restart_count == count_after_first

    @pytest.mark.asyncio
    async def test_dead_browser_restart_ignores_cooldown(self, mock_camoufox):
        # Dead-browser restart (expected_generation == current) must ALWAYS
        # restart, even immediately after a preventive one — recovery first.
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            await pool.restart()  # preventive, sets cooldown
            count_after_first = pool._restart_count
            gen = pool._browser_generation
            await pool.restart(expected_generation=gen)
            assert pool._restart_count == count_after_first + 1

    @pytest.mark.asyncio
    async def test_stale_generation_restart_deduped(self, mock_camoufox):
        # Two coroutines see the same crash; the second (stale generation)
        # must NOT restart again — one restart per dead generation.
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool()
            await pool.start()
            stale_gen = pool._browser_generation
            await pool.restart(expected_generation=stale_gen)  # first detector restarts
            count = pool._restart_count
            await pool.restart(expected_generation=stale_gen)  # second detector: deduped
            assert pool._restart_count == count


class TestTabTracking:
    @pytest.mark.asyncio
    async def test_acquire_registers_and_deregisters_tab(self, mock_camoufox, mock_browser):
        browser, page = mock_browser
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()
            assert pool.tab_details() == []
            async with pool.acquire(label="search:foo", session="sess-1") as _:
                details = pool.tab_details()
                assert len(details) == 1
                assert details[0]["label"] == "search:foo"
                assert details[0]["session"] == "sess-1"
                assert details[0]["generation"] == pool._browser_generation
            # deregistered after exit
            assert pool.tab_details() == []
            assert pool._tab_high_water >= 1

    @pytest.mark.asyncio
    async def test_reap_closes_stale_tab(self, mock_camoufox, mock_browser):
        import time as _t
        browser, page = mock_browser
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()
            stale_page = AsyncMock()
            pool._tab_registry[999] = {
                "tab_id": 999, "generation": pool._browser_generation, "req_id": 1,
                "session": None, "label": "leaked", "page": stale_page,
                "started_at": _t.monotonic() - 999,  # way past lifetime
            }
            reaped = await pool._reap_once()
            assert reaped == 1
            assert 999 not in pool._tab_registry
            stale_page.close.assert_awaited_once()
            assert pool._leaked_reaped == 1

    @pytest.mark.asyncio
    async def test_reap_keeps_fresh_tab(self, mock_camoufox, mock_browser):
        import time as _t
        browser, page = mock_browser
        with patch("src.scraper.browser.AsyncCamoufox", return_value=mock_camoufox):
            pool = BrowserPool(pool_size=2)
            await pool.start()
            pool._tab_registry[1] = {
                "tab_id": 1, "generation": 1, "req_id": 1, "session": None,
                "label": "fresh", "page": AsyncMock(), "started_at": _t.monotonic(),
            }
            assert await pool._reap_once() == 0
            assert 1 in pool._tab_registry


class TestProxyRotatorCircuitBreaker:
    def _mk(self, n=3):
        from src.scraper.proxy import ProxyRotator
        return ProxyRotator([f"http://proxy{i}:8080" for i in range(n)])

    def test_benched_proxy_skipped(self):
        rot = self._mk(3)
        bad = "http://proxy0:8080"
        rot.mark_failed(bad)
        rot.mark_failed(bad)  # trips breaker at 2 consecutive
        served = {rot.next()["_original_url"] for _ in range(6)}
        assert bad not in served

    def test_single_failure_not_benched(self):
        rot = self._mk(3)
        rot.mark_failed("http://proxy0:8080")
        served = {rot.next()["_original_url"] for _ in range(6)}
        assert "http://proxy0:8080" in served

    def test_mark_ok_resets_breaker(self):
        rot = self._mk(3)
        bad = "http://proxy1:8080"
        rot.mark_failed(bad)
        rot.mark_ok(bad)
        rot.mark_failed(bad)  # 1 consecutive again — not benched
        served = {rot.next()["_original_url"] for _ in range(6)}
        assert bad in served

    def test_all_benched_still_serves(self):
        rot = self._mk(2)
        for url in ("http://proxy0:8080", "http://proxy1:8080"):
            rot.mark_failed(url)
            rot.mark_failed(url)
        cfg = rot.next()  # half-open probe — must not raise
        assert cfg["_original_url"].startswith("http://proxy")

    def test_cooldown_expiry_readmits(self):
        import time as _t
        rot = self._mk(2)
        bad = "http://proxy0:8080"
        rot.mark_failed(bad)
        rot.mark_failed(bad)
        # Force-expire the cooldown
        rot._cooldown_until[bad] = _t.monotonic() - 1
        served = {rot.next()["_original_url"] for _ in range(4)}
        assert bad in served


class TestProxyErrorClassification:
    def test_ns_proxy_errors(self):
        from src.scraper.proxy import is_proxy_error
        assert is_proxy_error("goto: NS_ERROR_PROXY_BAD_GATEWAY")
        assert is_proxy_error("goto: NS_ERROR_PROXY_FORBIDDEN")
        assert is_proxy_error("NS_ERROR_UNKNOWN_PROXY_HOST")
        assert is_proxy_error(RuntimeError("SOCKS5 auth failed"))

    def test_timeout_only_proxy_error_when_rotating(self):
        from src.scraper.proxy import is_proxy_error
        msg = "goto: Timeout 10000ms exceeded."
        assert is_proxy_error(msg, rotating=True)
        assert not is_proxy_error(msg, rotating=False)

    def test_normal_errors_not_classified(self):
        from src.scraper.proxy import is_proxy_error
        assert not is_proxy_error("ValueError: bad parse")
        assert not is_proxy_error("HTTP 404 Not Found")
