"""Compare default Google SERP vs udm=14 (Web filter) — count consistency, speed,
and the right selector for udm=14. Runs inside the green1 container.
"""
import asyncio
import sqlite3
import time
from urllib.parse import quote_plus, urlparse

from camoufox.async_api import AsyncCamoufox
from camoufox.addons import DefaultAddons

QUERIES = [
    "python asyncio tutorial",
    "react server components guide",
    "best mechanical keyboard 2026",
    "climate change latest news",
    "how to bake sourdough bread",
]

# For udm=14 the organic block is div.MjjYud / the link is a:has(h3); dump several.
DUMP = r"""() => {
  const q = (s) => { try { return document.querySelectorAll(s).length } catch(e){ return -1 } };
  // generic organic extraction: every <a href^=http> that directly wraps an <h3>
  const out = []; const seen = new Set();
  for (const a of document.querySelectorAll('a[href^="http"]')) {
    const h3 = a.querySelector('h3'); if (!h3) continue;
    const href = a.href;
    if (href.includes('google.com/')||href.includes('/search?')) continue;
    if (seen.has(href)) continue; seen.add(href);
    out.push(h3.textContent.trim().slice(0,40));
  }
  return {
    blocked: location.href.includes('/sorry/'),
    counts: { rso_h3:q('#rso h3'), MjjYud:q('div.MjjYud'), a_h3:q('a[href] h3'),
              tF2Cxc:q('div.tF2Cxc') },
    organic: out.length, sample: out.slice(0,3),
  };
}"""


def get_proxy():
    db = sqlite3.connect("/data/wsm.db")
    row = db.execute("SELECT url FROM proxies WHERE is_active=1 AND url LIKE 'http%://%@%' "
                     "ORDER BY fail_count ASC, id LIMIT 1").fetchone()
    db.close()
    p = urlparse(row[0])
    return {"server": f"{p.scheme}://{p.hostname}:{p.port}",
            "username": p.username or "", "password": p.password or ""}


async def probe(browser, query, udm):
    suffix = "&udm=14" if udm else ""
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en{suffix}"
    t0 = time.monotonic()
    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_selector("a[href] h3", timeout=8000)
        except Exception:
            pass
        info = await page.evaluate(DUMP)
        ms = int((time.monotonic() - t0) * 1000)
        return info, ms
    finally:
        await ctx.close()


async def main():
    proxy = get_proxy()
    async with AsyncCamoufox(headless=True, geoip=True, humanize=0.5, block_images=True,
                             i_know_what_im_doing=True, proxy=proxy,
                             exclude_addons=[DefaultAddons.UBO]) as browser:
        for query in QUERIES:
            try:
                d, dms = await probe(browser, query, udm=False)
                u, ums = await probe(browser, query, udm=True)
                print(f"\n=== {query} ===")
                print(f"  default: organic={d['organic']} {dms}ms blocked={d['blocked']} counts={d['counts']}")
                print(f"  udm=14 : organic={u['organic']} {ums}ms blocked={u['blocked']} counts={u['counts']}")
                print(f"  udm sample: {u['sample']}")
            except Exception as e:
                print(f"\n[{query}] ERROR {type(e).__name__}: {str(e)[:70]}")


asyncio.run(main())
