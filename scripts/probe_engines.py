"""Local engine probe — run Google + Bing against the live BrowserPool.

Usage:
    .venv/bin/python scripts/probe_engines.py "your query"

Prints per-engine results, diagnostics, and timings. Used to drive optimization.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def run(query: str) -> None:
    from src.scraper.browser import BrowserPool
    from src.engine.google import GoogleSearchEngine
    from src.engine.bing import BingSearchEngine
    from src.engine.duckduckgo import DuckDuckGoSearchEngine

    pool = BrowserPool(pool_size=1, max_pool_size=1, headless=True)
    await pool.start()
    print(f"\n=== Pool ready — running searches for: {query!r} ===\n")

    engines = {
        "google": GoogleSearchEngine(),
        "bing": BingSearchEngine(),
        "duckduckgo": DuckDuckGoSearchEngine(),
    }

    for name, engine in engines.items():
        print(f"\n--- {name.upper()} ---")
        t0 = time.monotonic()
        try:
            async with pool.acquire() as page:
                results = await engine.search(page, query, max_results=10)
            elapsed = (time.monotonic() - t0) * 1000
            print(f"[{name}] {len(results)} results in {elapsed:.0f}ms")
            for i, r in enumerate(results[:5], 1):
                snippet = (r.snippet or "")[:90].replace("\n", " ")
                print(f"  {i}. {r.title[:70]}\n     {r.url[:90]}\n     {snippet}")
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            print(f"[{name}] FAILED in {elapsed:.0f}ms: {type(exc).__name__}: {exc}")

    await pool.stop()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "python asyncio tutorial"
    asyncio.run(run(q))
