from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qs, quote_plus, urlparse

from playwright.async_api import Page

from src.api.schemas import SearchResult
from src.engine.base import BaseSearchEngine

logger = logging.getLogger(__name__)


def _decode_bing_url(tracking_url: str) -> str:
    """Decode Bing tracking URL to get the real destination URL."""
    try:
        parsed = urlparse(tracking_url)
        if "/ck/a" not in parsed.path:
            return tracking_url
        params = parse_qs(parsed.query)
        u_values = params.get("u", [])
        if not u_values:
            return tracking_url
        u_val = u_values[0]
        # Remove 'a1' prefix used by Bing's encoding
        if u_val.startswith("a1"):
            raw = u_val[2:]
        else:
            return tracking_url
        # Add base64 padding
        raw += "=" * (4 - len(raw) % 4)
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass
    return tracking_url


class BingSearchEngine(BaseSearchEngine):
    """Bing search engine implementation."""

    name: str = "bing"
    # Bing renders results client-side; wait for the algo list to populate.
    ready_selector: str = "#b_results li.b_algo, li.b_algo"
    ready_timeout_ms: int = 6_000

    def build_search_url(self, query: str, page: int = 1) -> str:
        """Build Bing search URL using global.bing.com to avoid geo-redirect."""
        encoded_query = quote_plus(query)
        url = f"https://global.bing.com/search?q={encoded_query}&count=10&setlang=en&setmkt=en-US"
        if page > 1:
            first = (page - 1) * 10 + 1
            url += f"&first={first}"
        return url

    async def parse_results(self, page: Page, max_results: int = 10) -> list[SearchResult]:
        """Parse search results from Bing SERP — h2>a anchors are the source of truth."""
        raw: list[dict] = await page.evaluate("""() => {
            const items = Array.from(
                document.querySelectorAll('#b_results > li.b_algo, li.b_algo')
            );
            return items.map(el => {
                // Title + URL from the primary h2 anchor; some result blocks
                // (deep links, ads) nest the anchor deeper, so fall back to
                // any descendant h2 a, or the first http anchor in the block.
                let linkEl = el.querySelector('h2 a[href^="http"]')
                    || el.querySelector('h2 a')
                    || el.querySelector('a[href^="http"]');
                const title = linkEl ? (linkEl.innerText || linkEl.textContent || '').trim() : '';
                const url = linkEl ? (linkEl.getAttribute('href') || '') : '';
                // Snippet: prefer the explicit caption paragraph, then any
                // descriptive paragraph, then a long descriptive span.
                let snippet = '';
                const snippetEl =
                    el.querySelector('div.b_caption p')
                    || el.querySelector('p.b_lineclamp4')
                    || el.querySelector('p.b_lineclamp3')
                    || el.querySelector('p.b_lineclamp2')
                    || el.querySelector('p');
                if (snippetEl) snippet = (snippetEl.innerText || '').trim();
                if (!snippet) {
                    const desc = el.querySelector('div.b_caption, div.tpcn');
                    if (desc) snippet = (desc.innerText || '').trim().substring(0, 300);
                }
                return { title, url, snippet };
            });
        }""")

        if not raw:
            logger.warning("[bing] no result elements found on page")
            await self._dump_page_diagnostics(page)
            return []

        logger.info("[bing] found %d result elements", len(raw))

        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        for item in raw:
            if len(results) >= max_results:
                break
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if not title or not url or not url.startswith("http"):
                continue
            url = _decode_bing_url(url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            results.append(SearchResult(title=title, url=url, snippet=snippet))

        logger.info("[bing] extracted %d valid results from %d elements", len(results), len(raw))
        return results
