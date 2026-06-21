"""Prove get_page_content handles JS-rendered + infinite-scroll pages.

Compares domcontentloaded-only vs render+scroll against quotes.toscrape.com
(/js = JS-rendered, /scroll = infinite scroll). render+scroll should extract
strictly more content.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    from src.scraper.browser import BrowserPool
    from src.scraper.depth import fetch_page_content
    from src.scraper.parser import extract_main_content

    pool = BrowserPool(pool_size=1, max_pool_size=1, headless=True, humanize=0.0,
                       block_resources=True)
    await pool.start()
    targets = [
        ("JS-rendered", "https://quotes.toscrape.com/js/"),
        ("infinite-scroll", "https://quotes.toscrape.com/scroll"),
    ]
    for label, url in targets:
        async with pool.acquire() as page:
            plain = await fetch_page_content(page, url, timeout=20)
        n_plain = extract_main_content(plain).count("”") + extract_main_content(plain).count('"')
        q_plain = plain.count('class="quote"')
        async with pool.acquire() as page:
            rich = await fetch_page_content(page, url, timeout=20, render=True, scroll=True)
        q_rich = rich.count('class="quote"')
        print(f"\n=== {label}: {url} ===")
        print(f"  domcontentloaded-only : {q_plain} quotes, {len(plain)} bytes")
        print(f"  render+scroll         : {q_rich} quotes, {len(rich)} bytes")
        verdict = "IMPROVED" if q_rich > q_plain else ("same" if q_rich == q_plain else "WORSE")
        print(f"  -> {verdict}")
    await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
