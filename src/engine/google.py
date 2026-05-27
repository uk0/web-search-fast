from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote_plus

from playwright.async_api import Page

from src.api.schemas import SearchResult
from src.engine.base import BaseSearchEngine

logger = logging.getLogger(__name__)


class GoogleSearchEngine(BaseSearchEngine):
    """Google search engine implementation."""

    name: str = "google"
    # Google's SERP is JS-hydrated; wait for the result container to populate.
    ready_selector: str = "#rso h3, #search h3"
    ready_timeout_ms: int = 6_000

    def build_search_url(self, query: str, page: int = 1) -> str:
        encoded_query = quote_plus(query)
        start = (page - 1) * 10
        # hl=en pins the SERP language; gl/lr left default so locale follows Camoufox.
        url = f"https://www.google.com/search?q={encoded_query}&num=10&hl=en"
        if start > 0:
            url += f"&start={start}"
        return url

    async def search(self, page: Page, query: str, max_results: int = 10) -> list[SearchResult]:
        """Override to warm up Google session and bail out fast on CAPTCHA blocks."""
        # Visit Google homepage first to establish cookies (fast, short timeout)
        try:
            await self._navigate(page, "https://www.google.com/", retries=0, timeout=5_000)
            await self._handle_consent(page)
        except Exception:
            logger.debug("Google homepage warm-up failed, proceeding anyway")

        # Now perform the actual search
        url = self.build_search_url(query, 1)
        await self._navigate(page, url, retries=1, timeout=10_000)

        # Fast-fail: if Google redirected to /sorry/ CAPTCHA page, don't waste
        # 10s waiting for a selector that will never appear.
        if self._is_blocked(page.url):
            logger.warning("[google] CAPTCHA/sorry page detected immediately at %s", page.url[:120])
            return []

        # Handle consent if it appears on SERP
        await self._handle_consent(page)

        # Re-check after consent — clicking through can navigate to /sorry/
        if self._is_blocked(page.url):
            logger.warning("[google] CAPTCHA detected after consent at %s", page.url[:120])
            return []

        # Wait for SERP hydration before scraping
        await self._wait_for_results(page)

        if self._is_blocked(page.url):
            logger.warning("[google] CAPTCHA detected after hydration at %s", page.url[:120])
            return []

        return await self.parse_results(page, max_results)

    @staticmethod
    def _is_blocked(url: str) -> bool:
        u = url.lower()
        return "/sorry/" in u or "captcha" in u

    async def _wait_for_results(self, page: Page) -> bool:
        """Race result-selector vs CAPTCHA redirect — Google's /sorry/ kick-in
        happens via JS after domcontentloaded, so a plain selector wait would
        stall the full 6s even when we've already been blocked.
        """
        if not self.ready_selector:
            return True

        async def _watch_block() -> str:
            # Poll URL — wait_for_url("**/sorry/**") didn't fire reliably on
            # Firefox under Camoufox in testing; polling is dependable.
            while True:
                if self._is_blocked(page.url):
                    return "blocked"
                await asyncio.sleep(0.2)

        async def _watch_ready() -> str:
            await page.wait_for_selector(self.ready_selector, timeout=self.ready_timeout_ms)
            return "ready"

        selector_task = asyncio.create_task(_watch_ready())
        block_task = asyncio.create_task(_watch_block())
        done, pending = await asyncio.wait(
            {selector_task, block_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        # Suppress cancellation noise on the loser
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for task in done:
            try:
                outcome = task.result()
            except Exception:
                continue
            if outcome == "blocked":
                return False
            if outcome == "ready":
                return True
        return False

    async def _handle_consent(self, page: Page) -> None:
        """Click through Google cookie consent if present."""
        try:
            # Google consent form buttons
            for selector in [
                'button[id="L2AGLb"]',       # "Accept all" button
                'button[aria-label*="Accept"]',
                'button:has-text("Accept all")',
                'button:has-text("I agree")',
                'form[action*="consent"] button',
            ]:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    # Brief wait for consent to process
                    await page.wait_for_load_state("domcontentloaded", timeout=3000)
                    logger.info("[google] Clicked consent button: %s", selector)
                    return
        except Exception:
            pass

    async def parse_results(self, page: Page, max_results: int = 10) -> list[SearchResult]:
        results: list[SearchResult] = []

        if self._is_blocked(page.url):
            logger.warning("[google] blocked at parse stage: %s", page.url[:120])
            return results

        # Use JS-based extraction — Google obfuscates CSS classes, so we
        # walk the DOM from <h3> elements inside #rso instead.
        raw = await page.evaluate("""(maxResults) => {
            const rso = document.querySelector('#rso');
            if (!rso) return [];
            const items = [];
            const h3s = rso.querySelectorAll('h3');
            for (const h3 of h3s) {
                if (items.length >= maxResults) break;
                const a = h3.closest('a');
                if (!a || !a.href || !a.href.startsWith('http')) continue;
                // Walk up to the top-level result container
                let container = h3;
                for (let i = 0; i < 10; i++) {
                    if (!container.parentElement || container.parentElement === rso) break;
                    container = container.parentElement;
                }
                // Extract snippet from longest <span> that isn't the title
                let snippet = '';
                const spans = container.querySelectorAll('span');
                for (const s of spans) {
                    const t = (s.textContent || '').trim();
                    if (t.length > 50 && !t.includes(h3.textContent)) {
                        snippet = t.substring(0, 300);
                        break;
                    }
                }
                items.push({title: h3.textContent || '', url: a.href, snippet});
            }
            return items;
        }""", max_results)

        if not raw:
            logger.warning("No Google results extracted via JS")
            await self._dump_page_diagnostics(page)
            return results

        for item in raw:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if title and url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))

        logger.info("[google] extracted %d results via JS", len(results))
        return results
