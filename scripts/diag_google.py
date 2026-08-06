"""Diagnose Google SERP structure through real proxies to design a stable parser.

For several queries it dumps: candidate container/selector counts, what the
CURRENT parser extracts, and what a div.MjjYud-based strategy extracts — so we
can pick the most consistent extraction. Uses proxies from proxy_socks5h_list.txt
to avoid the bare-IP CAPTCHA.

Usage: .venv/bin/python scripts/diag_google.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

QUERIES = [
    "python asyncio tutorial",
    "kubernetes operator pattern",
    "rust vs go performance",
    "best mechanical keyboard 2026",
    "climate change latest news",
]

DUMP_JS = r"""() => {
  const q = (s) => document.querySelectorAll(s).length;
  // Current strategy: #rso h3 with closest <a href^=http>
  const cur = [];
  const rso = document.querySelector('#rso');
  if (rso) for (const h3 of rso.querySelectorAll('h3')) {
    const a = h3.closest('a');
    if (a && a.href && a.href.startsWith('http')) cur.push(h3.textContent.trim().slice(0,50));
  }
  // Proposed: organic blocks via div.MjjYud, title h3, link = first a[href] with h3, snippet heuristic
  const prop = [];
  for (const block of document.querySelectorAll('div.MjjYud')) {
    const h3 = block.querySelector('h3');
    if (!h3) continue;
    const a = h3.closest('a[href]') || block.querySelector('a[href]');
    if (!a || !a.href.startsWith('http')) continue;
    if (a.href.includes('google.com/search')) continue;
    prop.push(h3.textContent.trim().slice(0,50));
  }
  // tF2Cxc-based (classic result container)
  const tf = q('div.tF2Cxc');
  return {
    title: document.title,
    url: location.href.slice(0, 90),
    counts: {
      '#rso': q('#rso'), '#search': q('#search'), '#center_col': q('#center_col'),
      'div.g': q('div.g'), 'div.MjjYud': q('div.MjjYud'), 'div.tF2Cxc': tf,
      '#rso h3': q('#rso h3'), 'all h3': q('h3'),
      'a[href] h3': q('a[href] h3'),
      'div[data-text-ad]': q('div[data-text-ad]'),
    },
    current_count: cur.length, current_sample: cur.slice(0, 3),
    proposed_count: prop.length, proposed_sample: prop.slice(0, 3),
  };
}"""


async def main() -> None:
    from src.scraper.browser import BrowserPool
    proxies = [l.strip() for l in Path("proxy_socks5h_list.txt").read_text().splitlines()
               if l.strip() and not l.startswith("#")][:8]
    pool = BrowserPool(pool_size=2, max_pool_size=2, headless=True, humanize=0.5,
                       block_resources=True, proxy_list=proxies)
    await pool.start()
    for q in QUERIES:
        url = f"https://www.google.com/search?q={quote_plus(q)}&hl=en"
        try:
            async with pool.acquire() as page:
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                if "/sorry/" in page.url:
                    print(f"\n[{q}] CAPTCHA"); continue
                try:
                    await page.wait_for_selector("#rso h3, #search h3", timeout=6000)
                except Exception:
                    pass
                info = await page.evaluate(DUMP_JS)
            print(f"\n=== {q} ===")
            print(f"  counts: {info['counts']}")
            print(f"  CURRENT : {info['current_count']} -> {info['current_sample']}")
            print(f"  PROPOSED: {info['proposed_count']} -> {info['proposed_sample']}")
        except Exception as e:
            print(f"\n[{q}] ERROR {type(e).__name__}: {str(e)[:80]}")
    await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
