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
        # A prior browser launch/restart may have failed, leaving the pool
        # down. Try to bring it back before giving up so one bad launch
        # doesn't brick every subsequent request.
        logger.warning("[search] pool not started — attempting recovery restart")
        try:
            await pool.restart()
        except Exception as exc:
            raise SearchError(f"Browser pool not initialized: {exc}") from exc
        if not pool._started:
            raise SearchError("Browser pool not initialized")

    start = time.monotonic()
    total_timeout = req.timeout or 25
    logger.info("[search] start: query=%r engine=%s depth=%d max_results=%d timeout=%ds",
                req.query[:80], req.engine.value, req.depth, req.max_results, total_timeout)

    # Cache lookup first — an identical query costs ~3-9s to re-scrape, so a hit
    # is the single biggest latency win available.
    from src.core.cache import get_cached, set_cached
    from src.observability import metrics

    # NB: cache hit/miss counters are published at scrape time from the cache's
    # own totals (metrics.set_cache_stats) — incrementing here too would double
    # count, which the metrics module explicitly warns against.
    cached = await get_cached(req.query, req.engine.value, req.depth, req.max_results)
    if cached is not None:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.info("[search] cache HIT in %dms — query=%r", elapsed, req.query[:60])
        try:
            response = SearchResponse.model_validate(cached)
            # Report this request's own elapsed time, not the cached one.
            response.metadata.elapsed_ms = elapsed
            response.metadata.cached = True
            return response
        except Exception as exc:  # corrupt/stale payload — fall through to a live search
            logger.warning("[search] cached payload unusable (%s), re-searching", exc)

    # Treat a SERP as "good enough" at half the requested results (min 3).
    # Below this, a sparse/blocked engine (e.g. Google on a shopping-heavy
    # query returning 1 organic link) falls through to the next engine, and
    # we keep whichever produced the most results — regularizing the count.
    sufficient = max(3, min(req.max_results, req.max_results // 2 + 1))

    async def _serp_phase() -> tuple[list, SearchEngine]:
        """One page acquisition + engine chain.

        Returns the first engine result set that meets `sufficient`; if none
        do, returns the largest set seen (best effort). Proxy-caused failures
        abort the chain immediately (the page's proxy is dead) and bubble up
        so the caller can retry on a fresh page/proxy.
        """
        from src.engine import health as eng_health
        from src.scraper.proxy import is_proxy_error

        # Reorder by live engine health: an engine known to be CAPTCHA-blocked
        # is skipped outright instead of burning ~2-3s proving it again.
        ordered = eng_health.order_engines(
            req.engine.value, [e.value for e in FALLBACK_ORDER.get(req.engine, [])]
        )
        engine_chain = [SearchEngine(name) for name in ordered]
        async with pool.acquire(label=f"search:{req.query[:40]}") as page:
            last_exc: Exception | None = None
            best_results: list = []
            best_engine = req.engine
            for eng_key in engine_chain:
                # Admission gate: order_engines() ranks, should_skip() decides.
                # It hands out exactly one half-open probe lease, so a recovering
                # engine gets a single trial request instead of a stampede — but
                # never skip the last resort, we must try something.
                if eng_health.should_skip(eng_key.value) and eng_key is not engine_chain[-1]:
                    logger.info("[search] skipping %s — breaker says unavailable", eng_key.value)
                    continue
                engine = ENGINES[eng_key]
                eng_t0 = time.monotonic()
                try:
                    results = await engine.search(page, req.query, req.max_results)
                except Exception as exc:
                    last_exc = exc
                    if is_proxy_error(exc, rotating=True):
                        # The proxy died, not the engine — don't blame the engine.
                        logger.warning(
                            "[search] engine %s hit proxy error (%s) — abandoning this page",
                            eng_key.value, str(exc)[:100],
                        )
                        raise
                    eng_health.record_failure(eng_key.value)
                    metrics.record_search(eng_key.value, "error")
                    logger.warning("[search] engine %s failed: %s", eng_key.value, exc)
                    continue
                eng_ms = (time.monotonic() - eng_t0) * 1000
                if results:
                    eng_health.record_success(eng_key.value, eng_ms)
                    metrics.record_search(eng_key.value, "success", eng_ms / 1000)
                else:
                    # Empty SERP: only a real interstitial (Google /sorry/ or a
                    # captcha URL) is a hard block worth tripping the breaker
                    # fast. A merely-empty parse is often transient (selector
                    # timing on a slow page) and must NOT bench a good engine.
                    landed = (getattr(page, "url", "") or "").lower()
                    blocked = "/sorry/" in landed or "captcha" in landed
                    eng_health.record_failure(eng_key.value, blocked=blocked)
                    metrics.record_search(eng_key.value, "blocked" if blocked else "error",
                                          eng_ms / 1000)
                if len(results) > len(best_results):
                    best_results, best_engine = results, eng_key
                if len(results) >= sufficient:
                    return results, eng_key
                logger.info("[search] engine %s returned %d (<%d) results, trying fallback",
                            eng_key.value, len(results), sufficient)
            if not best_results and last_exc is not None:
                raise last_exc
            return best_results, best_engine

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

        response = SearchResponse(
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
        # Only cache productive answers — caching an empty SERP would pin a
        # transient block/CAPTCHA in place for the whole TTL.
        if results:
            await set_cached(req.query, req.engine.value, req.depth, req.max_results,
                             response.model_dump(mode="json"))
        return response

    try:
        return await asyncio.wait_for(_inner(), timeout=total_timeout)
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        metrics.record_search(req.engine.value, "timeout", elapsed / 1000)
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
            async with pool.acquire(label=f"fetch:{url[:48]}") as page:
                # Single-page reads render JS content + scroll for lazy-load
                html = await fetch_page_content(page, url, timeout, render=True, scroll=True)
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
