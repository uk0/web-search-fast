"""Obscura — the lightweight ("less") second browser backend.

Obscura (https://github.com/h4ckf0r0day/obscura) is a Rust headless browser
engine that speaks the Chrome DevTools Protocol. We drive it with Playwright
over CDP exactly like Camoufox, but it is far lighter (~30 MB RAM, no Chromium)
— the "less" backend to Camoufox's full "sim" fingerprint browser.

Scope is deliberately narrow, set by what obscura 0.1.3 actually implements
(verified over its live CDP surface):

    works : goto, content(), evaluate, title, wait_for_load_state, wait_for_timeout
    absent: screenshot, click/fill/type, wait_for_selector, inner_text (locators)

So this pool is used ONLY for page-content fetching. Screenshots and any
interactive/visual work stay on Camoufox. Obscura also does not render
XHR-injected (SPA) content, so it is best for static / server-rendered pages.

Cross-container note: obscura advertises ``webSocketDebuggerUrl`` as
``ws://127.0.0.1:9222``, which is wrong from any other container. We therefore
ALWAYS connect to the configured direct ws URL and never use HTTP /json
discovery — otherwise Playwright would follow the advertised 127.0.0.1 and fail.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Page, async_playwright

logger = logging.getLogger(__name__)

_DEFAULT_CDP_URL = "ws://obscura:9222/devtools/browser"
_DEFAULT_CONCURRENCY = 4
_CONNECT_TIMEOUT_MS = 15_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, min(64, int(raw)))
    except ValueError:
        logger.warning("[obscura] ignoring non-integer %s=%r", name, raw)
        return default


def _redact(url: str) -> str:
    """Drop any credentials from a URL before it is logged or surfaced."""
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return url
    return url


class ObscuraPool:
    """Playwright-over-CDP client for an external obscura server.

    Presents the subset of :class:`~src.scraper.browser.BrowserPool` that the
    content-fetch path uses (``start`` / ``stop`` / ``acquire`` / ``stats``), so
    ``fetch_url_content`` can drive it unchanged. Each ``acquire`` hands out a
    fresh context+page and always releases it — a leaked context would pin an
    obscura worker for the process lifetime.
    """

    def __init__(self, cdp_url: str | None = None, *, concurrency: int | None = None) -> None:
        self._cdp_url = cdp_url or os.environ.get("OBSCURA_CDP_URL", _DEFAULT_CDP_URL)
        self._concurrency = concurrency if concurrency is not None else _env_int(
            "OBSCURA_CONCURRENCY", _DEFAULT_CONCURRENCY
        )
        self._sem = asyncio.Semaphore(self._concurrency)
        self._pw = None
        self._browser: Browser | None = None
        self._started = False
        self._lock = asyncio.Lock()
        self._active_tabs = 0
        self._total_requests = 0
        self._total_failures = 0
        self._connect_count = 0

    async def start(self) -> None:
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            self._started = True
            return
        if self._pw is None:
            self._pw = await async_playwright().start()
        # Direct ws URL only — never HTTP discovery (advertised 127.0.0.1 is
        # wrong from any container other than obscura's own).
        self._browser = await self._pw.chromium.connect_over_cdp(
            self._cdp_url, timeout=_CONNECT_TIMEOUT_MS
        )
        self._connect_count += 1
        self._started = True
        logger.info("[obscura] connected to %s (attempt #%d)",
                    _redact(self._cdp_url), self._connect_count)

    async def _ensure_connected(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._lock:
            await self._connect_locked()

    @asynccontextmanager
    async def acquire(self, *, label: str | None = None, session: str | None = None,
                      load_images: bool = False) -> AsyncGenerator[Page, None]:
        """Yield a Playwright page backed by obscura, releasing it on exit.

        ``session``/``load_images`` are accepted for interface parity with
        :class:`BrowserPool` and are no-ops here (obscura has no per-tab resource
        blocker and no persistent-session concept in this pool).
        """
        await self._ensure_connected()
        assert self._browser is not None
        async with self._sem:
            context = await self._browser.new_context()
            page = await context.new_page()
            self._active_tabs += 1
            self._total_requests += 1
            try:
                yield page
            except Exception:
                self._total_failures += 1
                raise
            finally:
                self._active_tabs -= 1
                # Release exactly once. A dropped connection makes close() raise;
                # swallow it — the tab dies with the connection either way, and a
                # raising finally would mask the real error.
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass

    async def stop(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            self._started = False

    @property
    def stats(self) -> dict:
        connected = bool(self._browser is not None and self._browser.is_connected())
        return {
            "backend": "obscura",
            "started": self._started and connected,
            "connected": connected,
            "cdp_url": _redact(self._cdp_url),
            "concurrency": self._concurrency,
            "active_tabs": self._active_tabs,
            "total_requests": self._total_requests,
            "total_failures": self._total_failures,
            "connect_count": self._connect_count,
        }
