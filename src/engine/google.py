from __future__ import annotations

import logging
from urllib.parse import quote_plus

from playwright.async_api import Page

from src.api.schemas import SearchResult
from src.engine.base import BaseSearchEngine

logger = logging.getLogger(__name__)


class GoogleSearchEngine(BaseSearchEngine):
    """Google search engine implementation."""

    name: str = "google"

    def build_search_url(self, query: str, page: int = 1) -> str:
        encoded_query = quote_plus(query)
        start = (page - 1) * 10
        url = f"https://www.google.com/search?q={encoded_query}&num=10"
        if start > 0:
            url += f"&start={start}"
        return url

    async def search(self, page: Page, query: str, max_results: int = 10) -> list[SearchResult]:
        """Override to warm up Google session before searching."""
        # Visit Google homepage first to establish cookies (fast, short timeout)
        try:
            await self._navigate(page, "https://www.google.com/", retries=0, timeout=5_000)
            await self._handle_consent(page)
        except Exception:
            logger.debug("Google homepage warm-up failed, proceeding anyway")

        # Now perform the actual search
        url = self.build_search_url(query, 1)
        await self._navigate(page, url, retries=1, timeout=10_000)

        # Handle consent if it appears on SERP
        await self._handle_consent(page)

        return await self.parse_results(page, max_results)

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

        # Detect if blocked
        current_url = page.url
        if "/sorry/" in current_url or "captcha" in current_url.lower():
            logger.warning("Google blocked the request (captcha/sorry page)")
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
