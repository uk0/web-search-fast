from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from urllib.parse import urldefrag, urlsplit

from playwright.async_api import Page

from src.api.schemas import SearchResult, SubLink
from src.scraper.browser import BrowserPool
from src.scraper.parser import extract_links, extract_main_content

logger = logging.getLogger(__name__)

# Depth fan-out. The pool runs 18/36 tabs wide, so the old cap of 5 left most
# of it idle and serialized the crawl into waves. Env: DEPTH_CONCURRENCY.
_DEPTH_CONCURRENCY = 12
_MAX_DEPTH_CONCURRENCY = 32

# Per-page wall-clock cap derived from the remaining budget: no single page may
# drain the crawl, and a page needing more than the max isn't worth the wait.
_PAGE_TIMEOUT_MAX = 8.0
_PAGE_TIMEOUT_MIN = 3.0

# Tail cut: once this share of pages is in, the stragglers get a fixed grace
# instead of the rest of the budget — the slowest page must not set the wall
# clock for the whole crawl.
_STRAGGLER_RATIO = 0.6
_STRAGGLER_GRACE = 4.0

# Tail of the budget kept free so cancelled tabs unwind *inside* the deadline.
_CANCEL_DRAIN_SECS = 0.4

# Navigation caps. The read reserve keeps enough budget to pull the DOM back
# out after a slow navigation instead of timing out with nothing to show.
_NAV_TIMEOUT_CAP = 12.0
_READ_RESERVE_SECS = 0.5
_READ_TIMEOUT_CAP = 8.0

# A salvaged DOM below this is just about:blank / an error shell — not content.
_MIN_SALVAGE_HTML = 500

# Size guards: extraction is CPU-bound (blocks the loop) and the response has
# to stay bounded, so cap both the input HTML and the extracted text.
_MAX_HTML_CHARS = 2_000_000
_MAX_CONTENT_CHARS = 20_000
_MAX_SUB_CONTENT_CHARS = 5_000

# A depth-2 page yielding less than this is likely a JS shell — worth one short
# settle + re-read (no networkidle, no scroll) when the budget allows.
_MIN_USEFUL_CHARS = 200
_SETTLE_MS = 700

# Share of a depth-3 page budget spent on the parent page; the rest funds subs.
_MAIN_PAGE_SHARE = 0.6
_SUB_MIN_BUDGET = 1.5

# URLs whose bytes can never become article text — never worth a tab.
_SKIP_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub", ".mobi",
    ".zip", ".rar", ".7z", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".iso",
    ".exe", ".msi", ".dmg", ".pkg", ".apk", ".deb", ".rpm", ".bin", ".torrent",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tif", ".tiff", ".avif",
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".otf", ".eot",
})

# Auto-scroll tuning for lazy-load / infinite-scroll pages
_SCROLL_MAX_STEPS = 12
_SCROLL_SETTLE_MS = 450  # wait after each scroll for content to load


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        logger.warning("[depth] ignoring non-integer %s=%r", name, raw)
        return default


def _resolve_concurrency(pool: BrowserPool, targets: int) -> int:
    """Fan-out for one crawl: env override, bounded by the pool and the work."""
    limit = _env_int("DEPTH_CONCURRENCY", _DEPTH_CONCURRENCY, 1, _MAX_DEPTH_CONCURRENCY)
    pool_size = getattr(pool, "_pool_size", None)
    if isinstance(pool_size, int) and pool_size > 1:
        limit = min(limit, pool_size - 1)  # keep a slot for other traffic
    return max(1, min(limit, targets))


def _normalize_url(url: str) -> str:
    """Dedupe key: fragment dropped, host lowercased, trailing slash removed."""
    parts = urlsplit(urldefrag(url.strip())[0])
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}{query}"


def _is_crawlable(url: str) -> bool:
    """False for non-HTTP URLs and obvious binary/document downloads."""
    if not url.startswith(("http://", "https://")):
        return False
    tail = urlsplit(url).path.lower().rsplit("/", 1)[-1]
    dot = tail.rfind(".")
    return dot <= 0 or tail[dot:] not in _SKIP_EXTENSIONS


def _page_budget(deadline: float, pages_left: int, concurrency: int) -> float:
    """Per-page slice of the remaining budget — one slow page can't eat it all."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    waves = math.ceil(max(1, pages_left) / max(1, concurrency))
    share = remaining / waves
    return min(remaining, max(_PAGE_TIMEOUT_MIN, min(_PAGE_TIMEOUT_MAX, share)))


def _parse(html: str, base_url: str, limit: int, want_links: bool) -> tuple[str, list[dict[str, str]]]:
    """Blocking parse — offloaded to a thread so the event loop keeps pumping."""
    text = extract_main_content(html)[:limit]
    links = extract_links(html, base_url) if want_links else []
    return text, links


async def _extract(
    html: str, base_url: str, limit: int, *, want_links: bool = False,
) -> tuple[str, list[dict[str, str]]]:
    if not html:
        return "", []
    if len(html) > _MAX_HTML_CHARS:
        html = html[:_MAX_HTML_CHARS]  # lxml recovers from the truncated tail
    return await asyncio.to_thread(_parse, html, base_url, limit, want_links)


def _pick_sub_links(links: list[dict[str, str]], parent_url: str, max_sub: int) -> list[dict[str, str]]:
    """Drop self/anchor links, binaries and duplicates; keep document order."""
    seen = {_normalize_url(parent_url)}
    picked: list[dict[str, str]] = []
    for link in links:
        url = link.get("url", "")
        if not _is_crawlable(url):
            continue
        key = _normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        picked.append(link)
        if len(picked) >= max_sub:
            break
    return picked


async def _auto_scroll(page: Page, deadline: float) -> None:
    """Scroll to the bottom repeatedly to trigger lazy-load / infinite scroll.

    Stops when the page height stops growing, the step cap is hit, or the time
    deadline passes. Best-effort — any error just ends scrolling.
    """
    try:
        prev_height: float = -1
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


async def _read_content(page: Page, url: str, deadline: float) -> str:
    """Serialize the DOM, bounded by what is left of the page budget."""
    limit = min(_READ_TIMEOUT_CAP, max(1.0, deadline - time.monotonic()))
    try:
        return await asyncio.wait_for(page.content(), timeout=limit)
    except Exception as exc:
        logger.warning("page.content() failed for %s: %s", url, str(exc)[:120])
        return ""


async def fetch_page_content(
    page: Page, url: str, timeout: float = 15, *, render: bool = False, scroll: bool = False,
) -> str:
    """Fetch a single page and return its HTML content.

    Default (depth-crawl) path uses 'domcontentloaded' for speed. For the
    single-page get_page_content path, pass render=True to wait for JS-driven
    content to settle (networkidle) and scroll=True to trigger lazy-load /
    infinite-scroll content below the fold — both bounded by `timeout`.

    Site errors are swallowed (return "") — a dead target site is normal, but a
    navigation that merely timed out still gets its partial DOM salvaged rather
    than throwing away the time already spent.
    Proxy-caused errors are re-raised when proxy rotation is active so the
    BrowserPool feedback hook can bench the proxy and callers can retry.
    """
    from src.scraper.proxy import is_proxy_error

    rotating = bool(getattr(page, "_wsm_rotating", False))
    deadline = time.monotonic() + timeout
    nav_ms = int(max(1.0, min(timeout, _NAV_TIMEOUT_CAP) - _READ_RESERVE_SECS) * 1000)
    navigated = True
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=nav_ms)
    except Exception as exc:
        if rotating and is_proxy_error(exc, rotating=True):
            logger.warning("fetch_page_content proxy error for %s: %s", url, exc)
            raise
        navigated = False
        logger.warning("fetch_page_content failed for %s: %s", url, str(exc)[:120])

    if navigated and render:
        # Let XHR/fetch-driven (SPA) content render. networkidle often won't
        # fire on pages with long-poll/websocket traffic, so cap it short and
        # treat the timeout as "settled enough" — non-fatal.
        remaining = deadline - time.monotonic()
        if remaining > 1:
            try:
                await page.wait_for_load_state("networkidle", timeout=int(min(remaining, 4) * 1000))
            except Exception:
                pass
    if navigated and scroll:
        await _auto_scroll(page, deadline)

    html = await _read_content(page, url, deadline)
    if not navigated and len(html) < _MIN_SALVAGE_HTML:
        return ""  # nothing useful landed before the failure
    return html


async def _crawl_page(
    pool: BrowserPool, url: str, budget: float, *, limit: int, want_links: bool,
) -> tuple[str, list[dict[str, str]]]:
    """Fetch one URL in a pooled tab and return (main text, in-content links)."""
    deadline = time.monotonic() + budget
    async with pool.acquire(label=f"depth:{url[:48]}") as page:
        html = await fetch_page_content(page, url, budget)
        text, links = await _extract(html, url, limit, want_links=want_links)
        # JS-light shell: one short settle + re-read beats returning nothing.
        if len(text) < _MIN_USEFUL_CHARS and deadline - time.monotonic() > _SETTLE_MS / 1000 + 0.5:
            try:
                await page.wait_for_timeout(_SETTLE_MS)
                settled = await page.content()
            except Exception:
                settled = ""
            if len(settled) > len(html):
                text2, links2 = await _extract(settled, url, limit, want_links=want_links)
                if len(text2) > len(text):
                    text, links = text2, links2
    return text, links


async def enrich_with_content(pool: BrowserPool, result: SearchResult, timeout: float = 30) -> SearchResult:
    """Depth 2: fetch the result URL and extract main content.

    Proxy errors are absorbed here (the acquire hook already benched the
    proxy) — the result is kept, just without enriched content.
    """
    if not _is_crawlable(result.url):
        return result
    try:
        text, _ = await _crawl_page(pool, result.url, timeout, limit=_MAX_CONTENT_CHARS, want_links=False)
    except Exception as exc:
        logger.warning("enrich_with_content skipped for %s: %s", result.url[:100], str(exc)[:100])
        return result
    if text:
        result.content = text
    return result


async def enrich_with_sub_links(
    pool: BrowserPool, result: SearchResult, timeout: float = 30, max_sub: int = 5,
) -> SearchResult:
    """Depth 3: fetch content + extract and follow sub-links."""
    if not _is_crawlable(result.url):
        return result
    deadline = time.monotonic() + timeout
    try:
        text, links = await _crawl_page(
            pool, result.url, max(1.0, timeout * _MAIN_PAGE_SHARE),
            limit=_MAX_CONTENT_CHARS, want_links=True,
        )
    except Exception as exc:
        logger.warning("enrich_with_sub_links skipped for %s: %s", result.url[:100], str(exc)[:100])
        return result
    if text:
        result.content = text

    targets = _pick_sub_links(links, result.url, max_sub)
    remaining = deadline - time.monotonic()
    if not targets or remaining < _SUB_MIN_BUDGET:
        return result
    sub_budget = min(remaining, _PAGE_TIMEOUT_MAX)

    async def fetch_sub(link: dict[str, str]) -> SubLink:
        try:
            content, _ = await asyncio.wait_for(
                _crawl_page(pool, link["url"], sub_budget, limit=_MAX_SUB_CONTENT_CHARS, want_links=False),
                timeout=sub_budget,
            )
        except Exception:
            content = ""
        return SubLink(url=link["url"], title=link.get("title", ""), content=content)

    sub_results = await asyncio.gather(*[fetch_sub(lnk) for lnk in targets], return_exceptions=True)
    result.sub_links = [s for s in sub_results if isinstance(s, SubLink)]
    return result


def _group_targets(results: list[SearchResult]) -> list[tuple[SearchResult, list[SearchResult]]]:
    """One crawl per unique URL — duplicates ride along on the primary."""
    groups: dict[str, tuple[SearchResult, list[SearchResult]]] = {}
    for r in results:
        if not _is_crawlable(r.url):
            continue
        key = _normalize_url(r.url)
        if key in groups:
            groups[key][1].append(r)
        else:
            groups[key] = (r, [])
    return list(groups.values())


async def crawl_results(
    pool: BrowserPool,
    results: list[SearchResult],
    depth: int = 1,
    timeout: float = 30,
) -> list[SearchResult]:
    """Enrich results in place under a hard wall-clock budget.

    Every result comes back, in the original order: pages that were skipped,
    failed, or ran out of budget are simply un-enriched. Partial results beat a
    timeout error, and the crawl never runs past `timeout` — once most pages
    are in, the stragglers are cut short rather than setting the wall clock.
    """
    if depth <= 1 or not results:
        return results

    start = time.monotonic()
    budget = max(0.0, float(timeout))
    deadline = start + budget
    # Stop working slightly early so cancelled tabs unwind inside the budget.
    work_deadline = deadline - min(_CANCEL_DRAIN_SECS, budget * 0.1)

    groups = _group_targets(results)
    if not groups:
        logger.info("[depth] nothing crawlable among %d result(s)", len(results))
        return results

    concurrency = _resolve_concurrency(pool, len(groups))
    sem = asyncio.Semaphore(concurrency)
    pages_left = len(groups)
    enriched_count = 0

    async def _enrich(primary: SearchResult, clones: list[SearchResult]) -> None:
        nonlocal pages_left, enriched_count
        try:
            async with sem:
                page_budget = _page_budget(work_deadline, pages_left, concurrency)
                if page_budget <= 0:
                    return
                coro = (
                    enrich_with_content(pool, primary, page_budget)
                    if depth == 2
                    else enrich_with_sub_links(pool, primary, page_budget)
                )
                await asyncio.wait_for(coro, timeout=page_budget)
        except asyncio.TimeoutError:
            logger.info("[depth] page budget exhausted for %s", primary.url[:80])
        except Exception as exc:
            logger.warning("[depth] enrichment failed for %s: %s", primary.url[:80], str(exc)[:120])
        finally:
            pages_left -= 1
            if primary.content:
                enriched_count += 1
            for clone in clones:
                clone.content = primary.content
                if primary.sub_links:
                    clone.sub_links = list(primary.sub_links)

    tasks = [asyncio.create_task(_enrich(primary, clones)) for primary, clones in groups]
    unfinished = set(tasks)
    cut, tightened = work_deadline, False
    while unfinished:
        finished, unfinished = await asyncio.wait(
            unfinished, timeout=max(0.0, cut - time.monotonic()), return_when=asyncio.FIRST_COMPLETED,
        )
        if not finished:
            break  # deadline (or straggler cut) reached
        # Only pages that actually yielded content count toward the tail cut —
        # a burst of instant DNS failures must not pass for "most pages are in".
        if not tightened and unfinished and enriched_count >= math.ceil(len(tasks) * _STRAGGLER_RATIO):
            cut, tightened = min(cut, time.monotonic() + _STRAGGLER_GRACE), True
    if unfinished:
        for task in unfinished:
            task.cancel()
        drain = min(_CANCEL_DRAIN_SECS, max(0.0, deadline - time.monotonic()))
        await asyncio.wait(unfinished, timeout=drain)
        logger.warning("[depth] cut %d/%d page(s) short (straggler_cut=%s)", len(unfinished), len(tasks), tightened)

    logger.info(
        "[depth] depth=%d %d target(s) in %.0fms — %d enriched (concurrency=%d, budget=%.0fs)",
        depth, len(groups), (time.monotonic() - start) * 1000,
        sum(1 for r in results if r.content), concurrency, budget,
    )
    return results
