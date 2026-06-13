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
# Min seconds between browser restarts — absorbs restart storms when several
# concurrent requests all hit failures at once
_RESTART_COOLDOWN_SECS = 30.0
# Context-creation attempts (each with a different proxy) before giving up
_CONTEXT_PROXY_ATTEMPTS = 3
# Error substrings meaning the browser process itself is gone
_BROWSER_DEAD_MARKERS = (
    "Connection closed",
    "has been closed",
    "Browser closed",
    "Target closed",
)


def _is_browser_dead_error(exc: Exception) -> bool:
    s = str(exc)
    return any(marker in s for marker in _BROWSER_DEAD_MARKERS)
# Auto-scaling defaults — initial pool is half of the ceiling
_DEFAULT_POOL_SIZE = 18
_DEFAULT_MAX_POOL_SIZE = 36
_SCALE_UP_THRESHOLD = 0.8   # scale up when 80% of semaphore slots are in use
_SCALE_DOWN_THRESHOLD = 0.3  # scale down when <30% utilization for cooldown period
_SCALE_COOLDOWN_SECS = 10    # minimum seconds between scaling events

# Resource types aborted when block_resources is on — never needed for text
# extraction. Scripts/stylesheets are kept (Google/Bing hydrate via JS).
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


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
        block_resources: bool = True,
        geo_fingerprint: bool = True,
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
        self._block_resources = block_resources
        # Per-context IP-derived geo fingerprint (only meaningful w/ rotation)
        self._geo_fingerprint = geo_fingerprint
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
        self._last_restart_time = 0.0
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

    async def restart(self, *, force: bool = False) -> None:
        """Stop and re-create the browser. Serialized via lock to avoid races.

        Concurrent failures tend to pile up restart requests; once one
        coroutine has restarted, the rest skip (cooldown) instead of
        bouncing the browser again and killing fresh in-flight tabs.
        """
        async with self._restart_lock:
            since_last = time.monotonic() - self._last_restart_time
            if not force and self._started and since_last < _RESTART_COOLDOWN_SECS:
                logger.info("[pool] restart skipped — browser restarted %.0fs ago", since_last)
                self._consecutive_failures = 0
                return
            self._restart_count += 1
            logger.warning("[pool] restarting browser (restart #%d, consecutive_failures=%d)",
                           self._restart_count, self._consecutive_failures)
            await self.stop()
            await self.start()
            self._consecutive_failures = 0
            self._last_restart_time = time.monotonic()
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

    def record_failure(self, *, browser_related: bool = True) -> None:
        """Record a failed request.

        Proxy-caused failures (``browser_related=False``) count toward the
        total but NOT toward the consecutive counter that triggers browser
        restarts — a dead proxy is not a dead browser.
        """
        self._total_failures += 1
        if browser_related:
            self._consecutive_failures += 1
        logger.warning("[pool] failure recorded (consecutive=%d, total=%d, browser_related=%s)",
                       self._consecutive_failures, self._total_failures, browser_related)

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
        # Drop stale geo fingerprints — bridge ports/exit IPs may change
        from src.scraper import geo as geomod
        geomod.clear_cache()
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

    def _apply_geo_fingerprint(self, ctx_kwargs: dict, proxy_config: dict, original_url: str) -> None:
        """Add IP-consistent locale/timezone/geolocation to context kwargs.

        Uses the cached fingerprint for this proxy when available; otherwise
        kicks off a background resolve (no latency on this request) so the
        next request through the same proxy gets a matching fingerprint.
        """
        if not (self._geo_fingerprint and original_url):
            return
        from src.scraper import geo as geomod

        fp = geomod.peek(original_url)
        if fp:
            ctx_kwargs.update(geomod.context_geo_kwargs(fp))
        elif geomod.should_resolve(original_url):
            asyncio.create_task(geomod.resolve(original_url, dict(proxy_config)))

    async def _install_resource_blocker(self, page: Page) -> None:
        """Abort heavy subresource requests (image/media/font) to speed loads.

        Scripts and stylesheets pass through — Google/Bing build their SERP
        client-side, so killing JS would break extraction. Best-effort: any
        routing error falls back to letting the request continue.
        """
        if not self._block_resources:
            return

        async def _route(route) -> None:
            try:
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        try:
            await page.route("**/*", _route)
        except Exception as exc:
            logger.debug("[pool] resource blocker install failed: %s", exc)

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
        original_url = ""
        try:
            t0 = time.monotonic()

            if self._proxy_rotator:
                # Per-request proxy via browser context. A context failure is
                # almost always a bad proxy or a dead browser — try the next
                # proxy instead of bouncing the whole browser (which would
                # kill every in-flight tab); restart only when the browser
                # process is actually gone.
                last_exc: Exception | None = None
                page = None
                for ctx_attempt in range(1, _CONTEXT_PROXY_ATTEMPTS + 1):
                    proxy_config = self._proxy_rotator.next()
                    original_url = proxy_config.pop("_original_url", "")
                    proxy_server = proxy_config.get("server", "")
                    if len(proxy_server) > 45:
                        proxy_server = f"{proxy_server[:30]}...{proxy_server[-10:]}"
                    logger.info("[pool] req#%d — using proxy: %s (attempt %d/%d)",
                                req_id, proxy_server, ctx_attempt, _CONTEXT_PROXY_ATTEMPTS)
                    if original_url:
                        asyncio.create_task(self._record_proxy_usage(original_url))
                    ctx_kwargs: dict = {"proxy": proxy_config}
                    self._apply_geo_fingerprint(ctx_kwargs, proxy_config, original_url)
                    try:
                        context = await self._browser.new_context(**ctx_kwargs)  # type: ignore[union-attr]
                        page = await context.new_page()
                        break
                    except Exception as exc:
                        last_exc = exc
                        if context:
                            try:
                                await context.close()
                            except Exception:
                                pass
                            context = None
                        if original_url:
                            self._proxy_rotator.mark_failed(original_url)
                            asyncio.create_task(self._record_proxy_failure(original_url))
                        if _is_browser_dead_error(exc):
                            logger.error("[pool] req#%d — browser appears dead (%s), restarting",
                                         req_id, str(exc)[:100])
                            await self.restart()
                        else:
                            logger.warning("[pool] req#%d — context failed via proxy (%s), trying next",
                                           req_id, str(exc)[:100])
                if page is None:
                    raise RuntimeError(
                        f"context creation failed after {_CONTEXT_PROXY_ATTEMPTS} proxies: {last_exc}"
                    )
                # Tag so engines can fail fast instead of retrying a dead proxy
                page._wsm_rotating = True  # type: ignore[attr-defined]
            else:
                # Default: explicit per-request context for guaranteed tab-level
                # isolation (own cookie jar / storage / cache — no cross-search
                # leakage). Closed in finally alongside the page.
                try:
                    context = await self._browser.new_context()  # type: ignore[union-attr]
                    page = await context.new_page()
                except Exception as exc:
                    logger.error("[pool] req#%d — new_context() failed: %s, restarting browser", req_id, exc)
                    if context:
                        try:
                            await context.close()
                        except Exception:
                            pass
                        context = None
                    await self.restart()
                    context = await self._browser.new_context()  # type: ignore[union-attr]
                    page = await context.new_page()

            await self._install_resource_blocker(page)

            open_ms = (time.monotonic() - t0) * 1000
            logger.info("[pool] req#%d — tab opened in %.0fms (semaphore slots: %d/%d)",
                        req_id, open_ms, self._semaphore._value, self._pool_size)
            self._active_tabs += 1
            await self._push_stats()
            try:
                yield page
            except Exception as exc:
                # Feedback loop: navigation failures caused by the proxy bench
                # it in the rotator and bump fail_count in the DB.
                if self._proxy_rotator and original_url:
                    from src.scraper.proxy import is_proxy_error
                    if is_proxy_error(exc, rotating=True):
                        self._proxy_rotator.mark_failed(original_url)
                        asyncio.create_task(self._record_proxy_failure(original_url))
                raise
            else:
                if self._proxy_rotator and original_url:
                    self._proxy_rotator.mark_ok(original_url)
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
