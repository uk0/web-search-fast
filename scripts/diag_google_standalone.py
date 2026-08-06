"""Standalone Google SERP structure probe — runs INSIDE the green1 container.

No src/ imports (container only has compiled app + camoufox). Reads a working
HTTP proxy from /data/wsm.db, launches camoufox, dumps SERP structure for a few
queries. Prints only structure counts — never the proxy credentials.
"""
import asyncio
import sqlite3
from urllib.parse import quote_plus, urlparse

from camoufox.async_api import AsyncCamoufox
from camoufox.addons import DefaultAddons

QUERIES = [
    "python asyncio tutorial",
    "kubernetes operator pattern",
    "rust vs go performance",
    "climate change latest news",
]

DUMP_JS = r"""() => {
  const q = (s) => { try { return document.querySelectorAll(s).length } catch(e){ return -1 } };
  const cur = [];
  const rso = document.querySelector('#rso');
  if (rso) for (const h3 of rso.querySelectorAll('h3')) {
    const a = h3.closest('a');
    if (a && a.href && a.href.startsWith('http')) cur.push(h3.textContent.trim().slice(0,40));
  }
  const prop = [];
  const seen = new Set();
  for (const block of document.querySelectorAll('div.MjjYud')) {
    const h3 = block.querySelector('h3');
    if (!h3) continue;
    const a = h3.closest('a[href]') || block.querySelector('a[href]');
    if (!a || !a.href.startsWith('http')) continue;
    if (a.href.includes('google.com/')) continue;
    if (seen.has(a.href)) continue; seen.add(a.href);
    prop.push(h3.textContent.trim().slice(0,40));
  }
  return {
    title: document.title.slice(0,40),
    blocked: location.href.includes('/sorry/'),
    counts: {
      rso: q('#rso'), search: q('#search'), divg: q('div.g'),
      MjjYud: q('div.MjjYud'), tF2Cxc: q('div.tF2Cxc'),
      rso_h3: q('#rso h3'), all_h3: q('h3'), ad: q('div[data-text-ad]'),
    },
    current: cur.length, current_s: cur.slice(0,3),
    proposed: prop.length, proposed_s: prop.slice(0,3),
  };
}"""


def get_proxy():
    db = sqlite3.connect("/data/wsm.db")
    row = db.execute(
        "SELECT url FROM proxies WHERE is_active=1 AND url LIKE 'http%://%@%' "
        "ORDER BY fail_count ASC, id LIMIT 1"
    ).fetchone()
    db.close()
    if not row:
        return None
    p = urlparse(row[0])
    return {"server": f"{p.scheme}://{p.hostname}:{p.port}",
            "username": p.username or "", "password": p.password or ""}


async def main():
    proxy = get_proxy()
    if not proxy:
        print("no usable proxy in DB"); return
    # Never print the proxy endpoint — this output gets pasted into tickets.
    print(f"using proxy scheme={urlparse(proxy['server']).scheme} (host redacted)")
    async with AsyncCamoufox(headless=True, geoip=True, humanize=0.5,
                             block_images=True, i_know_what_im_doing=True,
                             proxy=proxy, exclude_addons=[DefaultAddons.UBO]) as browser:
        for query in QUERIES:
            url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en"
            try:
                ctx = await browser.new_context()
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_selector("#rso h3, #search h3", timeout=8000)
                except Exception:
                    pass
                info = await page.evaluate(DUMP_JS)
                await ctx.close()
                print(f"\n=== {query} ===")
                print(f"  title={info['title']!r} blocked={info['blocked']}")
                print(f"  counts={info['counts']}")
                print(f"  CURRENT : {info['current']} {info['current_s']}")
                print(f"  PROPOSED: {info['proposed']} {info['proposed_s']}")
            except Exception as e:
                print(f"\n[{query}] ERROR {type(e).__name__}: {str(e)[:70]}")


asyncio.run(main())
