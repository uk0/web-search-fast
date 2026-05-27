from __future__ import annotations

import logging
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from playwright.async_api import Page

from src.api.schemas import SearchResult
from src.engine.base import BaseSearchEngine

logger = logging.getLogger(__name__)


def _resolve_ddg_url(raw_url: str) -> str | None:
    """Extract the real destination URL from a DuckDuckGo redirect link.

    DDG HTML-lite hrefs look like:
      //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=...
    We need to pull the actual URL from the ``uddg`` query parameter.
    Protocol-relative URLs (``//...``) are also normalised.
    """
    if not raw_url:
        return None

    # Normalise protocol-relative URLs
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url

    # Already a direct http(s) link — return as-is
    if raw_url.startswith("http") and "duckduckgo.com/l/" not in raw_url:
        return raw_url

    # Extract uddg parameter from DDG redirect
    try:
        parsed = urlparse(raw_url)
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [None])[0]
        if uddg:
            return unquote(uddg)
    except Exception:
        pass

    # Fallback: if it's a valid http URL, return it
    if raw_url.startswith("http"):
        return raw_url

    return None


class DuckDuckGoSearchEngine(BaseSearchEngine):
    """DuckDuckGo search engine implementation."""

    name: str = "duckduckgo"
    # html.duckduckgo.com is synchronous, but waiting briefly for .result
    # guards against future markup drift and slow proxies.
    ready_selector: str = "div.result, article[data-testid='result']"
    ready_timeout_ms: int = 4_000

    def build_search_url(self, query: str, page: int = 1) -> str:
        """Build DuckDuckGo search URL."""
        encoded_query = quote_plus(query)
        # Use HTML-only mode to avoid JS-heavy SPA rendering issues
        return f"https://html.duckduckgo.com/html/?q={encoded_query}"

    async def search(self, page: Page, query: str, max_results: int = 10) -> list[SearchResult]:
        """Override to use HTML-lite version which is more reliable."""
        url = self.build_search_url(query)
        await self._navigate(page, url, retries=1, timeout=10_000)
        await self._wait_for_results(page)
        return await self.parse_results(page, max_results)

    async def parse_results(self, page: Page, max_results: int = 10) -> list[SearchResult]:
        """Parse search results from DuckDuckGo SERP via a single JS evaluation."""
        raw: list[dict] = await page.evaluate("""() => {
            // Try selector sets in priority order
            let elements = Array.from(document.querySelectorAll('div.result'));
            if (!elements.length)
                elements = Array.from(document.querySelectorAll('article[data-testid="result"]'));
            if (!elements.length)
                elements = Array.from(document.querySelectorAll('div.results div.result__body'));

            return elements.map(el => {
                // Title + href: try selectors in order
                const linkEl = el.querySelector('a.result__a')
                    || el.querySelector('a[data-testid="result-title-a"]')
                    || el.querySelector('h2 a');
                const title = linkEl ? (linkEl.textContent || '').trim() : '';
                const href  = linkEl ? (linkEl.getAttribute('href') || '') : '';

                // Snippet: try selectors in order
                const snippetEl = el.querySelector('a.result__snippet')
                    || el.querySelector('div[data-result="snippet"] span')
                    || el.querySelector('span[data-testid="result-snippet"]')
                    || el.querySelector('span.result__snippet');
                const snippet = snippetEl ? (snippetEl.textContent || '').trim() : '';

                return { title, href, snippet };
            });
        }""")

        if not raw:
            logger.warning("[duckduckgo] no result elements found on page")
            await self._dump_page_diagnostics(page)
            return []

        logger.info("[duckduckgo] found %d result elements", len(raw))

        results: list[SearchResult] = []
        for idx, item in enumerate(raw):
            if len(results) >= max_results:
                break
            title = item.get("title", "")
            href = item.get("href", "")
            snippet = item.get("snippet", "")
            url = _resolve_ddg_url(href)
            if not title or not url:
                logger.debug(
                    "[duckduckgo] element #%d: empty title=%r or url=%r (raw=%r), skipping",
                    idx, title[:30] if title else None, url, href[:80] if href else None,
                )
                continue
            results.append(SearchResult(title=title, url=url, snippet=snippet))

        logger.info("[duckduckgo] extracted %d valid results from %d elements", len(results), len(raw))
        return results
