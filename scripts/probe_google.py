"""Google-focused probe — measure latency/quality across pool efficiency knobs.

Compares resource-blocking on/off and homepage warm-up on/off, over several
queries, against the live Google engine. Drives the efficiency tuning.

Usage:
    .venv/bin/python scripts/probe_google.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

QUERIES = [
    "rust async runtime comparison",
    "postgres index types explained",
    "kubernetes operator pattern",
    "react server components guide",
    "python asyncio event loop",
]


async def _run_variant(name: str, *, block_resources: bool, warmup: bool) -> None:

    from src.engine.google import GoogleSearchEngine
    from src.scraper.browser import BrowserPool

    pool = BrowserPool(pool_size=2, max_pool_size=2, headless=True,
                       humanize=0.5, block_resources=block_resources)
    await pool.start()
    eng = GoogleSearchEngine()

    oks, lat_total, res_total = 0, 0.0, 0
    print(f"\n=== variant: {name} (block_resources={block_resources}, warmup={warmup}) ===")
    for q in QUERIES:
        t0 = time.monotonic()
        try:
            async with pool.acquire() as page:
                if warmup:
                    results = await eng.search(page, q, max_results=10)
                else:
                    # Direct search, skip homepage warm-up
                    url = eng.build_search_url(q, 1)
                    await eng._navigate(page, url, retries=1, timeout=10_000)
                    if eng._is_blocked(page.url):
                        results = []
                    else:
                        await eng._handle_consent(page)
                        await eng._wait_for_results(page)
                        results = [] if eng._is_blocked(page.url) else await eng.parse_results(page, 10)
            ms = (time.monotonic() - t0) * 1000
            blocked = "BLOCKED" if not results else ""
            if results:
                oks += 1
                lat_total += ms
                res_total += len(results)
            print(f"  [{q[:34]:34}] {len(results):2} results  {ms:6.0f}ms  {blocked}")
        except Exception as exc:
            ms = (time.monotonic() - t0) * 1000
            print(f"  [{q[:34]:34}] ERROR {ms:6.0f}ms  {type(exc).__name__}: {str(exc)[:60]}")

    await pool.stop()
    if oks:
        print(f"  --> {oks}/{len(QUERIES)} ok, avg {lat_total / oks:.0f}ms, avg {res_total / oks:.1f} results")
    else:
        print(f"  --> 0/{len(QUERIES)} ok (all blocked/failed)")


async def main() -> None:
    # nowarmup FIRST this run — rules out "later variant wins because IP cooled"
    await _run_variant("nowarmup+block", block_resources=True, warmup=False)
    await _run_variant("warmup+block", block_resources=True, warmup=True)


if __name__ == "__main__":
    asyncio.run(main())
