from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import quote_plus

from playwright.async_api import Page

from src.api.schemas import SearchResult
from src.engine.base import BaseSearchEngine

logger = logging.getLogger(__name__)

# Homepage warm-up costs a full extra navigation (~1-2s) per search. Probes
# showed it gives no reliable CAPTCHA benefit, so it's off by default; set
# GOOGLE_WARMUP=1 to re-enable (e.g. for EU-routed proxies hitting consent walls).
_WARMUP_HOMEPAGE = os.environ.get("GOOGLE_WARMUP", "").lower() in ("1", "true", "yes")


class GoogleSearchEngine(BaseSearchEngine):
    """Google search engine implementation."""

    name: str = "google"
    # Wait for any anchor-wrapped result title to render (works for both the
    # classic SERP and the udm=14 "Web" view).
    ready_selector: str = "a[href] h3"
    ready_timeout_ms: int = 6_000
    warmup_homepage: bool = _WARMUP_HOMEPAGE

    def build_search_url(self, query: str, page: int = 1) -> str:
        encoded_query = quote_plus(query)
        start = (page - 1) * 10
        # udm=14 = Google's "Web" filter: classic blue-link results only — no
        # AI overview, shopping, recipe, or rich blocks. Gives a consistent ~10
        # organic results per query (vs 1-10 wildly varying on the default SERP)
        # and a much lighter page (faster, less JS). hl=en pins SERP language.
        url = f"https://www.google.com/search?q={encoded_query}&udm=14&hl=en"
        if start > 0:
            url += f"&start={start}"
        return url

    async def search(self, page: Page, query: str, max_results: int = 10) -> list[SearchResult]:
        """Search Google, bailing out fast on CAPTCHA blocks.

        Homepage warm-up is skipped by default (saves ~1-2s/search); enable
        with GOOGLE_WARMUP=1. Consent is still handled on the SERP itself.
        """
        if self.warmup_homepage:
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

        # Class-agnostic extraction: an organic result is an <a href="http...">
        # that wraps an <h3> title. Scoped to the results container to skip
        # header/footer chrome. Works for both the classic SERP and udm=14.
        raw = await page.evaluate("""(maxResults) => {
            const root = document.querySelector('#rso')
                || document.querySelector('#search')
                || document.querySelector('#center_col')
                || document.body;
            const items = [];
            const seen = new Set();
            for (const a of root.querySelectorAll('a[href^="http"]')) {
                if (items.length >= maxResults) break;
                const h3 = a.querySelector('h3');
                if (!h3) continue;
                const url = a.href;
                if (!url) continue;
                if (url.includes('google.com/') || url.includes('/search?')
                    || url.includes('/sorry/') || url.includes('webcache.')) continue;
                if (seen.has(url)) continue;
                const title = (h3.textContent || '').trim();
                if (!title) continue;
                seen.add(url);
                // Walk up to the result container, take the longest descendant
                // text block that isn't the title as the snippet.
                let container = a;
                for (let i = 0; i < 5 && container.parentElement; i++) {
                    container = container.parentElement;
                }
                let snippet = '';
                for (const el of container.querySelectorAll('div, span')) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 60 && !t.includes(title) && t.length > snippet.length) {
                        snippet = t;
                    }
                }
                items.push({title, url, snippet: snippet.substring(0, 300)});
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
