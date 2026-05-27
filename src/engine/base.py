from __future__ import annotations

import abc
import logging
import time

from playwright.async_api import Page

from src.api.schemas import SearchResult

logger = logging.getLogger(__name__)


class BaseSearchEngine(abc.ABC):
    """Abstract base class for search engines."""

    name: str = ""
    # Selector that signals SERP results have rendered. ``search()`` waits for
    # this after navigation so that JS-rendered engines (Google/Bing) finish
    # hydrating before parse_results runs. Engines that render synchronously
    # (DuckDuckGo HTML-lite) may leave this None.
    ready_selector: str | None = None
    ready_timeout_ms: int = 5_000

    @abc.abstractmethod
    def build_search_url(self, query: str, page: int = 1) -> str:
        """Build the search URL for the given query."""
        ...

    @abc.abstractmethod
    async def parse_results(self, page: Page, max_results: int = 10) -> list[SearchResult]:
        """Parse search results from the SERP page."""
        ...

    async def _wait_for_results(self, page: Page) -> bool:
        """Wait for SERP results to render.

        Strategy: try `ready_selector` first (fast path for direct-render pages).
        If that times out, fall back to a short `networkidle` then re-check —
        catches engines like Google that do a JS redirect (?sei=...) after
        domcontentloaded, leaving the initial DOM empty until hydration.
        """
        if not self.ready_selector:
            return True
        t0 = time.monotonic()
        try:
            await page.wait_for_selector(self.ready_selector, timeout=self.ready_timeout_ms)
            logger.debug("[%s] ready_selector matched in %.0fms",
                         self.name, (time.monotonic() - t0) * 1000)
            return True
        except Exception:
            pass

        # Fallback: let the SPA settle, then re-check.
        try:
            await page.wait_for_load_state("networkidle", timeout=4_000)
        except Exception:
            pass
        try:
            elem = await page.query_selector(self.ready_selector)
            if elem:
                logger.debug("[%s] ready_selector found after networkidle (%.0fms)",
                             self.name, (time.monotonic() - t0) * 1000)
                return True
        except Exception:
            pass
        logger.warning(
            "[%s] ready_selector %r not found within %.0fms",
            self.name, self.ready_selector, (time.monotonic() - t0) * 1000,
        )
        return False

    async def _dump_page_diagnostics(self, page: Page) -> None:
        """Log diagnostic info when no results are found — helps debug selector/rendering issues."""
        try:
            url = page.url
            title = await page.title()
            # Lightweight summary always logged
            logger.warning(
                "[%s] DIAGNOSTIC — url=%s title=%r", self.name, url, title,
            )
            # Check for common blocking indicators via JS (single eval, no full HTML fetch)
            block_info = await page.evaluate("""() => {
                const url = location.href;
                const html = document.documentElement.innerHTML.substring(0, 5000).toLowerCase();
                return {
                    captcha: html.includes('captcha') || url.includes('/sorry/'),
                    consent: html.includes('consent') || html.substring(0, 3000).includes('cookie'),
                    bodyLen: document.documentElement.innerHTML.length,
                    childTags: Array.from(document.body?.children || []).slice(0, 10).map(
                        el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
                    ).join(', ')
                };
            }""")
            logger.warning("[%s] DIAGNOSTIC — body_len=%d children=[%s]",
                           self.name, block_info.get("bodyLen", 0), block_info.get("childTags", ""))
            if block_info.get("captcha"):
                logger.warning("[%s] DIAGNOSTIC — CAPTCHA/block detected", self.name)
            if block_info.get("consent"):
                logger.warning("[%s] DIAGNOSTIC — consent/cookie page may be blocking", self.name)
        except Exception as exc:
            logger.warning("[%s] DIAGNOSTIC dump failed: %s", self.name, exc)

    async def _navigate(self, page: Page, url: str, retries: int = 1, timeout: int = 10_000) -> None:
        """Navigate with retry logic for transient failures."""
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                logger.info("[%s] nav attempt %d/%d → %s (timeout=%dms)",
                            self.name, attempt + 1, retries + 1, url[:120], timeout)
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                elapsed = (time.monotonic() - t0) * 1000
                status = resp.status if resp else "no-response"
                logger.info("[%s] nav done in %.0fms — HTTP %s", self.name, elapsed, status)
                if resp and resp.status >= 400:
                    logger.warning("[%s] HTTP %d for %s", self.name, resp.status, url)
                return
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                last_err = exc
                logger.warning(
                    "[%s] nav attempt %d/%d failed after %.0fms (%s: %s)",
                    self.name, attempt + 1, retries + 1, elapsed,
                    type(exc).__name__, str(exc)[:200],
                )
                if attempt < retries:
                    try:
                        await page.goto("about:blank", timeout=3000)
                    except Exception:
                        pass
                    continue
        raise last_err  # type: ignore[misc]

    async def search(self, page: Page, query: str, max_results: int = 10) -> list[SearchResult]:
        """Execute search: navigate to URL, wait for results, then parse."""
        t0 = time.monotonic()
        url = self.build_search_url(query)
        logger.info("[%s] search start: query=%r max_results=%d", self.name, query[:80], max_results)
        await self._navigate(page, url)
        await self._wait_for_results(page)
        results = await self.parse_results(page, max_results)
        elapsed = (time.monotonic() - t0) * 1000
        logger.info("[%s] search done in %.0fms — %d results", self.name, elapsed, len(results))
        if not results:
            await self._dump_page_diagnostics(page)
        return results
