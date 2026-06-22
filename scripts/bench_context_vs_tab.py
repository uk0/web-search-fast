"""Measure per-request overhead: new_context+page vs new_page (tab) in a shared
context. Isolates creation cost (about:blank, no network) to judge whether the
'multiple tabs per instance' scheme can meaningfully improve performance.
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

N = 30


async def main() -> None:
    from camoufox.async_api import AsyncCamoufox
    from camoufox.addons import DefaultAddons

    async with AsyncCamoufox(headless=True, humanize=0.0, block_images=True,
                             i_know_what_im_doing=True,
                             exclude_addons=[DefaultAddons.UBO]) as browser:
        # A) context-per-request (current design)
        ctx_times = []
        for _ in range(N):
            t = time.monotonic()
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto("about:blank")
            await page.close()
            await ctx.close()
            ctx_times.append((time.monotonic() - t) * 1000)

        # B) tab-per-request in ONE shared context (user's scheme)
        shared = await browser.new_context()
        tab_times = []
        for _ in range(N):
            t = time.monotonic()
            page = await shared.new_page()
            await page.goto("about:blank")
            await page.close()
            tab_times.append((time.monotonic() - t) * 1000)
        await shared.close()

    def stat(xs):
        return f"mean={statistics.mean(xs):.1f}ms median={statistics.median(xs):.1f}ms"

    print(f"\ncontext-per-request : {stat(ctx_times)}")
    print(f"tab-in-shared-ctx   : {stat(tab_times)}")
    print(f"per-request saving by using tabs: ~{statistics.mean(ctx_times) - statistics.mean(tab_times):.1f}ms")
    print("\n(compare against a real page load, which is multiple SECONDS)")


if __name__ == "__main__":
    asyncio.run(main())
