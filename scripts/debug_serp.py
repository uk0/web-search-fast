"""Dump candidate selector counts on Google + Bing SERPs to diagnose parsing."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

QUERY = sys.argv[1] if len(sys.argv) > 1 else "python asyncio tutorial"


async def probe(name: str, url: str) -> None:
    from src.scraper.browser import BrowserPool

    pool = BrowserPool(pool_size=1, max_pool_size=1, headless=True)
    await pool.start()
    print(f"\n=== {name.upper()} — {url[:120]} ===")
    async with pool.acquire() as page:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        # Give SPA a moment
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass

        info = await page.evaluate("""() => {
            const sel = (q) => document.querySelectorAll(q).length;
            return {
                title: document.title,
                url: location.href,
                bodyLen: document.documentElement.innerHTML.length,
                tags: Array.from(document.body?.children || []).slice(0, 12).map(
                    el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
                ).join(', '),
                // Google candidates
                rso: sel('#rso'),
                searchDiv: sel('#search'),
                h3InRso: sel('#rso h3'),
                gResult: sel('div.g'),
                mainContent: sel('#main'),
                anchorsHttp: Array.from(document.querySelectorAll('a[href^="http"]')).length,
                // Bing candidates
                bAlgo: sel('li.b_algo'),
                bAlgoAny: sel('.b_algo'),
                bResults: sel('#b_results'),
                bResultsLi: sel('#b_results li'),
                bResultsAny: sel('#b_results > *'),
                h2Anchor: Array.from(document.querySelectorAll('h2 a[href^="http"]')).slice(0,3).map(a => ({
                    href: a.href.substring(0, 80),
                    text: (a.innerText || '').trim().substring(0, 80),
                })),
            };
        }""")
        print(f"  title    : {info['title']}")
        print(f"  url      : {info['url'][:120]}")
        print(f"  bodyLen  : {info['bodyLen']}")
        print(f"  topTags  : {info['tags']}")
        print(f"  anchors  : {info['anchorsHttp']}")
        print(f"  google   : #rso={info['rso']} #search={info['searchDiv']} #rso h3={info['h3InRso']} div.g={info['gResult']} #main={info['mainContent']}")
        print(f"  bing     : li.b_algo={info['bAlgo']} .b_algo={info['bAlgoAny']} #b_results={info['bResults']} #b_results li={info['bResultsLi']} #b_results>*={info['bResultsAny']}")
        print(f"  h2 anchors sample: {info['h2Anchor']}")
    await pool.stop()


async def main() -> None:
    await probe("google", f"https://www.google.com/search?q={QUERY.replace(' ', '+')}&num=10")
    await probe("bing", f"https://global.bing.com/search?q={QUERY.replace(' ', '+')}&count=10&setlang=en&setmkt=en-US")
    await probe("bing-cn", f"https://www.bing.com/search?q={QUERY.replace(' ', '+')}&count=10")


if __name__ == "__main__":
    asyncio.run(main())
