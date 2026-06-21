from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import Page

from src.api.schemas import SearchResult, SubLink
from src.scraper.browser import BrowserPool
from src.scraper.parser import extract_links, extract_main_content

logger = logging.getLogger(__name__)

# Limit concurrent depth-crawl fetches to avoid overwhelming the browser pool
_DEPTH_CONCURRENCY = 5

# Auto-scroll tuning for lazy-load / infinite-scroll pages
_SCROLL_MAX_STEPS = 12
_SCROLL_SETTLE_MS = 450  # wait after each scroll for content to load


async def _auto_scroll(page: Page, deadline: float) -> None:
    """Scroll to the bottom repeatedly to trigger lazy-load / infinite scroll.

    Stops when the page height stops growing, the step cap is hit, or the time
    deadline passes. Best-effort — any error just ends scrolling.
    """
    try:
        prev_height = -1
        for _ in range(_SCROLL_MAX_STEPS):
            if time.monotonic() >= deadline:
                break
            height = await page.evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); "
                "return document.body.scrollHeight; }"
            )
            await page.wait_for_timeout(_SCROLL_SETTLE_MS)
            if not isinstance(height, (int, float)) or height <= prev_height:
                break  # no new content loaded
            prev_height = height
        # Return to top so content extraction sees the full, settled DOM
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception as exc:
        logger.debug("auto-scroll skipped: %s", exc)


async def fetch_page_content(
    page: Page, url: str, timeout: int = 15, *, render: bool = False, scroll: bool = False,
) -> str:
    """Fetch a single page and return its HTML content.

    Default (depth-crawl) path uses 'domcontentloaded' for speed. For the
    single-page get_page_content path, pass render=True to wait for JS-driven
    content to settle (networkidle) and scroll=True to trigger lazy-load /
    infinite-scroll content below the fold — both bounded by `timeout`.

    Site errors are swallowed (return "") — a dead target site is normal.
    Proxy-caused errors are re-raised when proxy rotation is active so the
    BrowserPool feedback hook can bench the proxy and callers can retry.
    """
    from src.scraper.proxy import is_proxy_error

    rotating = bool(getattr(page, "_wsm_rotating", False))
    deadline = time.monotonic() + timeout
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=min(timeout, 12) * 1000)
    except Exception as exc:
        if rotating and is_proxy_error(exc, rotating=True):
            logger.warning("fetch_page_content proxy error for %s: %s", url, exc)
            raise
        logger.warning("fetch_page_content failed for %s: %s", url, exc)
        return ""

    if render:
        # Let XHR/fetch-driven (SPA) content render. networkidle often won't
        # fire on pages with long-poll/websocket traffic, so cap it short and
        # treat the timeout as "settled enough" — non-fatal.
        remaining = deadline - time.monotonic()
        if remaining > 1:
            try:
                await page.wait_for_load_state("networkidle", timeout=int(min(remaining, 4) * 1000))
            except Exception:
                pass
    if scroll:
        await _auto_scroll(page, deadline)

    try:
        return await page.content()
    except Exception as exc:
        logger.warning("page.content() failed for %s: %s", url, exc)
        return ""


async def enrich_with_content(pool: BrowserPool, result: SearchResult, timeout: int = 30) -> SearchResult:
    """Depth 2: fetch the result URL and extract main content.

    Proxy errors are absorbed here (the acquire hook already benched the
    proxy) — the result is kept, just without enriched content.
    """
    try:
        async with pool.acquire() as page:
            html = await fetch_page_content(page, result.url, timeout)
    except Exception as exc:
        logger.warning("enrich_with_content skipped for %s: %s", result.url[:100], str(exc)[:100])
        return result
    if html:
        result.content = extract_main_content(html)
    return result


async def enrich_with_sub_links(pool: BrowserPool, result: SearchResult, timeout: int = 30, max_sub: int = 5) -> SearchResult:
    """Depth 3: fetch content + extract and follow sub-links."""
    try:
        async with pool.acquire() as page:
            html = await fetch_page_content(page, result.url, timeout)
    except Exception as exc:
        logger.warning("enrich_with_sub_links skipped for %s: %s", result.url[:100], str(exc)[:100])
        return result
    if not html:
        return result
    result.content = extract_main_content(html)
    links = extract_links(html, result.url)[:max_sub]

    async def fetch_sub(link: dict[str, str]) -> SubLink:
        try:
            async with pool.acquire() as p:
                sub_html = await fetch_page_content(p, link["url"], timeout)
        except Exception:
            sub_html = ""
        content = extract_main_content(sub_html) if sub_html else ""
        return SubLink(url=link["url"], title=link.get("title", ""), content=content[:5000])

    if links:
        sub_results = await asyncio.gather(*[fetch_sub(lnk) for lnk in links], return_exceptions=True)
        result.sub_links = [s for s in sub_results if isinstance(s, SubLink)]

    return result


async def crawl_results(
    pool: BrowserPool,
    results: list[SearchResult],
    depth: int = 1,
    timeout: int = 30,
) -> list[SearchResult]:
    """Orchestrate multi-depth crawling with concurrency limiter."""
    if depth <= 1:
        return results

    sem = asyncio.Semaphore(_DEPTH_CONCURRENCY)

    async def _limited(coro):
        async with sem:
            return await coro

    if depth == 2:
        tasks = [_limited(enrich_with_content(pool, r, timeout)) for r in results]
    else:  # depth == 3
        tasks = [_limited(enrich_with_sub_links(pool, r, timeout)) for r in results]

    enriched = await asyncio.gather(*tasks, return_exceptions=True)
    successful = []
    for r in enriched:
        if isinstance(r, SearchResult):
            successful.append(r)
        elif isinstance(r, Exception):
            logger.warning("Depth crawl enrichment failed: %s", r)
    return successful
