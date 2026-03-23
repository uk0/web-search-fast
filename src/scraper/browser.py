from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Browser, Page

logger = logging.getLogger(__name__)

# Max consecutive failures before auto-restart
_MAX_CONSECUTIVE_FAILURES = 3
# Health check: navigate about:blank within this timeout (ms)
_HEALTH_CHECK_TIMEOUT_MS = 5000
# Auto-scaling defaults
_DEFAULT_POOL_SIZE = 30
_DEFAULT_MAX_POOL_SIZE = 90
_SCALE_UP_THRESHOLD = 0.8   # scale up when 80% of semaphore slots are in use
_SCALE_DOWN_THRESHOLD = 0.3  # scale down when <30% utilization for cooldown period
_SCALE_COOLDOWN_SECS = 10    # minimum seconds between scaling events


class BrowserPool:
    def __init__(
        self,
        pool_size: int = _DEFAULT_POOL_SIZE,
        max_pool_size: int = _DEFAULT_MAX_POOL_SIZE,
        headless: bool = True,
        geoip: bool = True,
        humanize: float = 2.0,
        locale: str = "en-US",
        block_images: bool = True,
        proxy: str = "",
        os_target: str = "",
        fonts: list[str] | None = None,
        block_webgl: bool = False,
        addons: list[str] | None = None,
        proxy_list: list[str] | None = None,
    ):
        self._pool_size = pool_size
        self._max_pool_size = max(pool_size, max_pool_size)
        self._min_pool_size = pool_size  # original size is the floor
        self._headless = headless
        self._geoip = geoip
        self._humanize = humanize
        self._locale = locale
        self._block_images = block_images
        self._proxy = proxy
        self._os_target = os_target
        self._fonts = fonts or []
        self._block_webgl = block_webgl
        self._addons = addons or []
        # --- proxy rotation ---
        self._proxy_rotator = None
        if proxy_list:
            from src.scraper.proxy import ProxyRotator
            self._proxy_rotator = ProxyRotator(proxy_list)
            logger.info("[pool] proxy rotation enabled: %d proxies", self._proxy_rotator.count)
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(pool_size)
        self._started = False
        # --- health tracking ---
        self._consecutive_failures = 0
        self._total_requests = 0
        self._total_failures = 0
        self._restart_count = 0
        self._restart_lock = asyncio.Lock()
        self._active_tabs = 0
        # --- auto-scaling ---
        self._last_scale_time = 0.0
        self._scale_lock = asyncio.Lock()
        # --- Redis stats push callback ---
        self._stats_callback = None

    def _build_camoufox_kwargs(self) -> dict:
        """Build kwargs dict for AsyncCamoufox — used by start() and restart()."""
        from camoufox.addons import DefaultAddons

        # Disable geoip when using proxy rotation (can't resolve IP through SOCKS5 auth)
        use_geoip = self._geoip and not self._proxy_rotator

        kwargs: dict = {
            "headless": self._headless,
            "geoip": use_geoip,
            "humanize": self._humanize if self._humanize > 0 else False,
            "locale": self._locale,
        }
        if self._block_images:
            kwargs["block_images"] = True
            kwargs["i_know_what_im_doing"] = True
        if self._proxy and not self._proxy_rotator:
            kwargs["proxy"] = {"server": self._proxy}
        if self._os_target:
            kwargs["os"] = self._os_target
        if self._fonts:
            kwargs["fonts"] = self._fonts
        if self._block_webgl:
            kwargs["block_webgl"] = True
        if self._addons:
            kwargs["addons"] = self._addons
        else:
            # Exclude default addons (uBlock Origin) — download/extraction
            # fails in Docker containers, causing InvalidAddonPath crash.
            kwargs["exclude_addons"] = [DefaultAddons.UBO]
        return kwargs

    async def start(self) -> None:
        if self._started:
            return
        t0 = time.monotonic()
        # Start proxy bridges before browser (bridges must be ready for browser proxy config)
        if self._proxy_rotator:
            await self._proxy_rotator.start_bridges()
        self._camoufox = AsyncCamoufox(**self._build_camoufox_kwargs())
        self._browser = await self._camoufox.__aenter__()
        self._started = True
        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "[pool] started in %.0fms: pool_size=%d, geoip=%s, humanize=%s, "
            "locale=%s, block_images=%s, proxy=%s, proxy_rotation=%d, os=%s, block_webgl=%s",
            elapsed, self._pool_size, self._geoip, self._humanize,
            self._locale, self._block_images,
            bool(self._proxy), self._proxy_rotator.count if self._proxy_rotator else 0,
            self._os_target or "auto", self._block_webgl,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await self._camoufox.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning("[pool] error during stop: %s", exc)
        # Stop proxy bridges after browser
        if self._proxy_rotator:
            await self._proxy_rotator.stop_bridges()
        self._started = False
        self._browser = None
        logger.info("[pool] stopped (requests=%d, failures=%d, restarts=%d)",
                     self._total_requests, self._total_failures, self._restart_count)

    async def restart(self) -> None:
        """Stop and re-create the browser. Serialized via lock to avoid races."""
        async with self._restart_lock:
            self._restart_count += 1
            logger.warning("[pool] restarting browser (restart #%d, consecutive_failures=%d)",
                           self._restart_count, self._consecutive_failures)
            await self.stop()
            await self.start()
            self._consecutive_failures = 0
            logger.info("[pool] browser restarted successfully")

    async def is_healthy(self) -> bool:
        """Quick health check — open a blank page and close it."""
        if not self._started or not self._browser:
            return False
        try:
            page = await self._browser.new_page()
            await page.goto("about:blank", timeout=_HEALTH_CHECK_TIMEOUT_MS)
            await page.close()
            return True
        except Exception as exc:
            logger.warning("[pool] health check failed: %s", exc)
            return False

    def record_success(self) -> None:
        """Record a successful request — resets consecutive failure counter."""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Record a failed request — increments counters."""
        self._consecutive_failures += 1
        self._total_failures += 1
        logger.warning("[pool] failure recorded (consecutive=%d, total=%d)",
                       self._consecutive_failures, self._total_failures)

    @property
    def needs_restart(self) -> bool:
        return self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES

    @property
    def stats(self) -> dict:
        return {
            "started": self._started,
            "pool_size": self._pool_size,
            "max_pool_size": self._max_pool_size,
            "active_tabs": self._active_tabs,
            "total_requests": self._total_requests,
            "total_failures": self._total_failures,
            "consecutive_failures": self._consecutive_failures,
            "restart_count": self._restart_count,
            "proxy_count": self._proxy_rotator.count if self._proxy_rotator else 0,
        }

    def set_stats_callback(self, callback) -> None:
        """Set an async callback that receives stats dict on every state change."""
        self._stats_callback = callback

    async def update_proxies(self, proxy_urls: list[str]) -> None:
        """Hot-reload proxy list without restarting the browser."""
        # Stop old bridges
        if self._proxy_rotator:
            await self._proxy_rotator.stop_bridges()

        if proxy_urls:
            from src.scraper.proxy import ProxyRotator
            self._proxy_rotator = ProxyRotator(proxy_urls)
            await self._proxy_rotator.start_bridges()
            logger.info("[pool] hot-reloaded %d proxies", len(proxy_urls))
        else:
            self._proxy_rotator = None
            logger.info("[pool] proxy rotation disabled (no active proxies)")

        await self._push_stats()

    async def _push_stats(self) -> None:
        """Push current stats via callback (non-blocking, fire-and-forget)."""
        if self._stats_callback:
            try:
                await self._stats_callback(self.stats)
            except Exception:
                pass

    @staticmethod
    async def _record_proxy_usage(proxy_url: str) -> None:
        """Update last_used_at in DB (fire-and-forget)."""
        try:
            from src.admin.repository import record_proxy_usage
            await record_proxy_usage(proxy_url)
        except Exception:
            pass

    @staticmethod
    async def _record_proxy_failure(proxy_url: str) -> None:
        """Increment fail_count in DB (fire-and-forget)."""
        try:
            from src.admin.repository import increment_proxy_failure
            await increment_proxy_failure(proxy_url)
        except Exception:
            pass

    async def _maybe_scale_up(self) -> None:
        """Increase semaphore capacity if utilization is high."""
        if self._pool_size >= self._max_pool_size:
            return
        # Check utilization: (pool_size - available) / pool_size
        available = self._semaphore._value
        utilization = (self._pool_size - available) / self._pool_size if self._pool_size > 0 else 0
        if utilization < _SCALE_UP_THRESHOLD:
            return
        now = time.monotonic()
        if now - self._last_scale_time < _SCALE_COOLDOWN_SECS:
            return
        async with self._scale_lock:
            # Double-check after acquiring lock
            if self._pool_size >= self._max_pool_size:
                return
            old_size = self._pool_size
            # Scale up by 50%, capped at max
            new_size = min(self._max_pool_size, self._pool_size + max(1, self._pool_size // 2))
            delta = new_size - old_size
            self._pool_size = new_size
            # Release extra semaphore slots
            for _ in range(delta):
                self._semaphore.release()
            self._last_scale_time = time.monotonic()
            logger.info("[pool] scaled UP: %d → %d (utilization=%.0f%%)",
                        old_size, new_size, utilization * 100)
            await self._push_stats()

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Page, None]:
        """Acquire a browser tab. Auto-scales and auto-restarts if needed.

        When proxy rotation is enabled, creates a new browser context with
        a per-request proxy, ensuring each request uses a different proxy.
        """
        self._total_requests += 1
        req_id = self._total_requests

        # Pre-check: restart if too many consecutive failures
        if self.needs_restart:
            logger.warning("[pool] req#%d — too many failures, triggering restart before acquire", req_id)
            await self.restart()

        # Auto-scale up if utilization is high
        await self._maybe_scale_up()

        # Acquire semaphore with timeout to prevent indefinite hangs
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.error("[pool] req#%d — semaphore acquire timed out after 15s (pool_size=%d)",
                         req_id, self._pool_size)
            raise RuntimeError(f"Browser pool exhausted (pool_size={self._pool_size})")

        context = None
        try:
            t0 = time.monotonic()

            if self._proxy_rotator:
                # Per-request proxy via browser context
                proxy_config = self._proxy_rotator.next()
                original_url = proxy_config.pop("_original_url", "")
                proxy_server = proxy_config.get("server", "")
                proxy_short = f"{proxy_server[:30]}...{proxy_server[-10:]}" if len(proxy_server) > 45 else proxy_server
                logger.info("[pool] req#%d — using proxy: %s", req_id, proxy_short)
                # Record usage in DB (fire-and-forget)
                if original_url:
                    asyncio.create_task(self._record_proxy_usage(original_url))
                try:
                    context = await self._browser.new_context(  # type: ignore[union-attr]
                        proxy=proxy_config,
                    )
                    page = await context.new_page()
                except Exception as exc:
                    logger.error("[pool] req#%d — context/page failed: %s, restarting", req_id, exc)
                    if original_url:
                        asyncio.create_task(self._record_proxy_failure(original_url))
                    if context:
                        try:
                            await context.close()
                        except Exception:
                            pass
                        context = None
                    await self.restart()
                    proxy_config = self._proxy_rotator.next()
                    original_url = proxy_config.pop("_original_url", "")
                    if original_url:
                        asyncio.create_task(self._record_proxy_usage(original_url))
                    context = await self._browser.new_context(  # type: ignore[union-attr]
                        proxy=proxy_config,
                    )
                    page = await context.new_page()
            else:
                # Default: direct page from browser (no context isolation)
                try:
                    page = await self._browser.new_page()  # type: ignore[union-attr]
                except Exception as exc:
                    logger.error("[pool] req#%d — new_page() failed: %s, restarting browser", req_id, exc)
                    await self.restart()
                    page = await self._browser.new_page()  # type: ignore[union-attr]

            open_ms = (time.monotonic() - t0) * 1000
            logger.info("[pool] req#%d — tab opened in %.0fms (semaphore slots: %d/%d)",
                        req_id, open_ms, self._semaphore._value, self._pool_size)
            self._active_tabs += 1
            await self._push_stats()
            try:
                yield page
            finally:
                self._active_tabs -= 1
                await self._push_stats()
                try:
                    await page.close()
                    if context:
                        await context.close()
                    close_ms = (time.monotonic() - t0) * 1000
                    logger.info("[pool] req#%d — tab closed (total %.0fms)", req_id, close_ms)
                except Exception as exc:
                    logger.warning("[pool] req#%d — tab close failed: %s", req_id, exc)
        finally:
            self._semaphore.release()
