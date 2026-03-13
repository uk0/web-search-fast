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

    def build_search_url(self, query: str, page: int = 1) -> str:
        """Build Bing search URL using global.bing.com to avoid geo-redirect."""
        encoded_query = quote_plus(query)
        url = f"https://global.bing.com/search?q={encoded_query}&count=10&setlang=en&setmkt=en-US"
        if page > 1:
            first = (page - 1) * 10 + 1
            url += f"&first={first}"
        return url

    async def parse_results(self, page: Page, max_results: int = 10) -> list[SearchResult]:
        """Parse search results from Bing SERP."""
        raw: list[dict] = await page.evaluate("""() => {
            let items = Array.from(document.querySelectorAll('li.b_algo'));
            if (!items.length) {
                items = Array.from(document.querySelectorAll('#b_results li.b_algo'));
            }
            return items.map(el => {
                const linkEl = el.querySelector('h2 a');
                const title = linkEl ? linkEl.innerText.trim() : '';
                const url = linkEl ? (linkEl.getAttribute('href') || '') : '';
                const snippetEl = el.querySelector('div.b_caption p') || el.querySelector('p');
                const snippet = snippetEl ? snippetEl.innerText.trim() : '';
                return { title, url, snippet };
            });
        }""")

        if not raw:
            logger.warning("No Bing result elements found on page")
            await self._dump_page_diagnostics(page)
            return []

        results: list[SearchResult] = []
        for item in raw:
            if len(results) >= max_results:
                break
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            if not title or not url or not url.startswith("http"):
                continue
            url = _decode_bing_url(url)
            results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results
