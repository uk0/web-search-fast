"""Core search logic — framework-agnostic, used by both FastAPI and MCP server."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from src.api.schemas import (
    SearchMetadata,
    SearchRequest,
    SearchResponse,
)
from src.config import SearchEngine
from src.engine.base import BaseSearchEngine
from src.engine.bing import BingSearchEngine
from src.engine.duckduckgo import DuckDuckGoSearchEngine
from src.engine.google import GoogleSearchEngine
from src.scraper.browser import BrowserPool
from src.scraper.depth import crawl_results, fetch_page_content
from src.scraper.parser import extract_main_content_markdown

logger = logging.getLogger(__name__)


class SearchError(Exception):
    """Raised when a search cannot be performed."""


ENGINES: dict[SearchEngine, BaseSearchEngine] = {
    SearchEngine.GOOGLE: GoogleSearchEngine(),
    SearchEngine.BING: BingSearchEngine(),
    SearchEngine.DUCKDUCKGO: DuckDuckGoSearchEngine(),
}

FALLBACK_ORDER: dict[SearchEngine, list[SearchEngine]] = {
    SearchEngine.GOOGLE: [SearchEngine.DUCKDUCKGO, SearchEngine.BING],
    SearchEngine.BING: [SearchEngine.DUCKDUCKGO, SearchEngine.GOOGLE],
    SearchEngine.DUCKDUCKGO: [SearchEngine.BING, SearchEngine.GOOGLE],
}


async def do_search(pool: BrowserPool, req: SearchRequest) -> SearchResponse:
    """Execute a search with engine fallback and multi-depth crawling."""
    if not pool._started:
        raise SearchError("Browser pool not initialized")

    start = time.monotonic()
    total_timeout = req.timeout or 25
    logger.info("[search] start: query=%r engine=%s depth=%d max_results=%d timeout=%ds",
                req.query[:80], req.engine.value, req.depth, req.max_results, total_timeout)

    async def _serp_phase() -> tuple[list, SearchEngine]:
        """One page acquisition + engine chain.

        Proxy-caused failures abort the whole chain immediately (every
        engine shares the page, hence the same dead proxy) and bubble up
        so the caller can retry on a fresh page/proxy.
        """
        from src.scraper.proxy import is_proxy_error

        engine_chain = [req.engine] + FALLBACK_ORDER.get(req.engine, [])
        async with pool.acquire() as page:
            last_exc: Exception | None = None
            for eng_key in engine_chain:
                engine = ENGINES[eng_key]
                try:
                    results = await engine.search(page, req.query, req.max_results)
                except Exception as exc:
                    last_exc = exc
                    if is_proxy_error(exc, rotating=True):
                        logger.warning(
                            "[search] engine %s hit proxy error (%s) — abandoning this page",
                            eng_key.value, str(exc)[:100],
                        )
                        raise
                    logger.warning("[search] engine %s failed: %s", eng_key.value, exc)
                    continue
                if results:
                    return results, eng_key
                logger.info("[search] engine %s returned 0 results", eng_key.value)
            if last_exc is not None:
                raise last_exc
            return [], req.engine

    async def _inner() -> SearchResponse:
        from src.scraper.proxy import is_proxy_error

        results: list = []
        used_engine = req.engine
        serp_attempts = 2
        for attempt in range(1, serp_attempts + 1):
            try:
                results, used_engine = await _serp_phase()
                break
            except Exception as exc:
                proxy_related = is_proxy_error(exc, rotating=True)
                pool.record_failure(browser_related=not proxy_related)
                remaining_budget = total_timeout - (time.monotonic() - start)
                if attempt < serp_attempts and remaining_budget > 8:
                    logger.warning(
                        "[search] SERP attempt %d/%d failed (%s) — retrying on fresh page/proxy (%.0fs left)",
                        attempt, serp_attempts, str(exc)[:120], remaining_budget,
                    )
                    continue
                logger.error("[search] SERP failed after %d attempt(s): %s", attempt, exc)
                raise

        # Depth crawling with remaining time budget
        elapsed_so_far = time.monotonic() - start
        remaining = max(5, total_timeout - elapsed_so_far)
        logger.info("[search] SERP done in %.0fms, %d results — starting depth=%d crawl (budget=%.0fs)",
                    elapsed_so_far * 1000, len(results), req.depth, remaining)
        results = await crawl_results(pool, results, depth=req.depth, timeout=int(remaining))
        elapsed = int((time.monotonic() - start) * 1000)

        pool.record_success()
        logger.info("[search] complete in %dms — %d results, engine=%s", elapsed, len(results), used_engine.value)

        return SearchResponse(
            query=req.query,
            engine=used_engine,
            depth=req.depth,
            total=len(results),
            results=results,
            metadata=SearchMetadata(
                elapsed_ms=elapsed,
                timestamp=datetime.now(timezone.utc).isoformat(),
                engine=used_engine,
                depth=req.depth,
            ),
        )

    try:
        return await asyncio.wait_for(_inner(), timeout=total_timeout)
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        pool.record_failure()
        logger.warning("[search] TIMEOUT after %dms (limit=%ds) — pool stats: %s",
                       elapsed, total_timeout, pool.stats)
        # Auto-restart if browser seems stuck
        if pool.needs_restart:
            logger.warning("[search] triggering auto-restart due to %d consecutive failures", pool._consecutive_failures)
            await pool.restart()
        raise SearchError(f"Search timed out after {total_timeout}s")


async def fetch_url_content(pool: BrowserPool, url: str, timeout: int = 30) -> str:
    """Fetch a single URL and return its main content as markdown.

    Retries once on a fresh page/proxy when the failure is proxy-caused.
    """
    from src.scraper.proxy import is_proxy_error

    t0 = time.monotonic()
    logger.info("[fetch] start: url=%s timeout=%ds", url[:120], timeout)
    for attempt in (1, 2):
        try:
            async with pool.acquire() as page:
                html = await fetch_page_content(page, url, timeout)
            break
        except Exception as exc:
            remaining = timeout - (time.monotonic() - t0)
            if attempt == 1 and is_proxy_error(exc, rotating=True) and remaining > 5:
                logger.warning("[fetch] proxy error (%s) — retrying on fresh page/proxy (%.0fs left)",
                               str(exc)[:100], remaining)
                continue
            raise
    if not html:
        logger.warning("[fetch] empty content from %s (%.0fms)", url[:120], (time.monotonic() - t0) * 1000)
        return ""
    content = extract_main_content_markdown(html)
    logger.info("[fetch] done in %.0fms — %d chars from %s", (time.monotonic() - t0) * 1000, len(content), url[:120])
    return content
