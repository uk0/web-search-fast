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

    @abc.abstractmethod
    def build_search_url(self, query: str, page: int = 1) -> str:
        """Build the search URL for the given query."""
        ...

    @abc.abstractmethod
    async def parse_results(self, page: Page, max_results: int = 10) -> list[SearchResult]:
        """Parse search results from the SERP page."""
        ...

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
        """Execute search: navigate to URL and parse results."""
        t0 = time.monotonic()
        url = self.build_search_url(query)
        logger.info("[%s] search start: query=%r max_results=%d", self.name, query[:80], max_results)
        await self._navigate(page, url)
        results = await self.parse_results(page, max_results)
        elapsed = (time.monotonic() - t0) * 1000
        logger.info("[%s] search done in %.0fms — %d results", self.name, elapsed, len(results))
        if not results:
            await self._dump_page_diagnostics(page)
        return results
