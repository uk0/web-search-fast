"""Compare Google search variants — with/without warm-up — to find what bypasses CAPTCHA."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

QUERY = sys.argv[1] if len(sys.argv) > 1 else "python asyncio tutorial"


async def variant_no_warmup(page, query: str):
    from src.engine.google import GoogleSearchEngine
    eng = GoogleSearchEngine()
    url = eng.build_search_url(query)
    await page.goto(url, wait_until="domcontentloaded", timeout=10_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    return await eng.parse_results(page, 10), page.url


async def variant_with_warmup(page, query: str):
    from src.engine.google import GoogleSearchEngine
    eng = GoogleSearchEngine()
    await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=8_000)
    url = eng.build_search_url(query)
    await page.goto(url, wait_until="domcontentloaded", timeout=10_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    return await eng.parse_results(page, 10), page.url


async def variant_ncr(page, query: str):
    """Use /ncr (no country redirect) + direct search."""
    from src.engine.google import GoogleSearchEngine
    eng = GoogleSearchEngine()
    from urllib.parse import quote_plus
    url = f"https://www.google.com/search?q={quote_plus(query)}&num=10&hl=en&pws=0"
    await page.goto(url, wait_until="domcontentloaded", timeout=10_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    return await eng.parse_results(page, 10), page.url


async def variant_referer(page, query: str):
    """Send Referer header as if coming from Google homepage."""
    from src.engine.google import GoogleSearchEngine
    eng = GoogleSearchEngine()
    from urllib.parse import quote_plus
    url = f"https://www.google.com/search?q={quote_plus(query)}&num=10&hl=en"
    await page.goto(url, wait_until="domcontentloaded", timeout=10_000,
                    referer="https://www.google.com/")
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    return await eng.parse_results(page, 10), page.url


async def main() -> None:
    from src.scraper.browser import BrowserPool
    variants = {
        "no_warmup": variant_no_warmup,
        "with_warmup": variant_with_warmup,
        "ncr_pws0": variant_ncr,
        "with_referer": variant_referer,
    }
    pool = BrowserPool(pool_size=1, max_pool_size=1, headless=True)
    await pool.start()
    for name, fn in variants.items():
        t0 = time.monotonic()
        async with pool.acquire() as page:
            try:
                results, url = await fn(page, QUERY)
                elapsed = (time.monotonic() - t0) * 1000
                blocked = "/sorry/" in url or "captcha" in url.lower()
                print(f"[{name:14}] {len(results):2} results in {elapsed:5.0f}ms  blocked={blocked}  url={url[:100]}")
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                print(f"[{name:14}] ERROR in {elapsed:5.0f}ms  {type(exc).__name__}: {str(exc)[:80]}")
    await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
