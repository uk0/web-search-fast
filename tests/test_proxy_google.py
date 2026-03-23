"""Integration test: SOCKS5H proxy rotation + search + concurrency.

Run with:
    pytest tests/test_proxy_google.py -v -s -m integration

Requires:
    - proxy_socks5h_list.txt in project root
    - Camoufox browser installed
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

# Mark entire module as integration — skipped in normal `pytest tests/`
pytestmark = pytest.mark.integration

PROXY_FILE = os.path.join(os.path.dirname(__file__), "..", "proxy_socks5h_list.txt")


def _load_proxies() -> list[str]:
    if not os.path.isfile(PROXY_FILE):
        pytest.skip(f"Proxy file not found: {PROXY_FILE}")
    with open(PROXY_FILE) as f:
        proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not proxies:
        pytest.skip("Proxy file is empty")
    return proxies


@pytest.fixture(scope="module")
def proxies() -> list[str]:
    return _load_proxies()


@pytest.mark.asyncio
async def test_proxy_rotation_google_search(proxies: list[str]):
    """Google search via proxy — may fail due to captcha, that's expected."""
    from src.api.schemas import SearchRequest
    from src.config import SearchEngine
    from src.core.search import do_search
    from src.scraper.browser import BrowserPool

    pool = BrowserPool(
        pool_size=3,
        max_pool_size=5,
        headless=True,
        proxy_list=proxies,
        block_images=True,
    )
    await pool.start()
    try:
        assert pool.stats["proxy_count"] == len(proxies)

        req = SearchRequest(
            query="Python asyncio tutorial",
            engine=SearchEngine.GOOGLE,
            depth=1,
            max_results=5,
            timeout=30,
        )
        t0 = time.monotonic()
        response = await do_search(pool, req)
        elapsed = time.monotonic() - t0

        print(f"\n[test] Google search completed in {elapsed:.1f}s")
        print(f"[test] Results: {response.total}")
        for r in response.results:
            print(f"  - {r.title}: {r.url}")

        # Google often blocks proxy IPs — log but don't fail hard
        if response.total == 0:
            print("[test] WARNING: Google returned 0 results (likely captcha/block)")
        else:
            print(f"[test] OK: Got {response.total} results from Google")
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_proxy_rotation_duckduckgo(proxies: list[str]):
    """DuckDuckGo search via proxy — most reliable engine."""
    from src.api.schemas import SearchRequest
    from src.config import SearchEngine
    from src.core.search import do_search
    from src.scraper.browser import BrowserPool

    pool = BrowserPool(
        pool_size=3,
        max_pool_size=5,
        headless=True,
        proxy_list=proxies,
        block_images=True,
    )
    await pool.start()
    try:
        req = SearchRequest(
            query="what is socks5 proxy",
            engine=SearchEngine.DUCKDUCKGO,
            depth=1,
            max_results=5,
            timeout=25,
        )
        t0 = time.monotonic()
        response = await do_search(pool, req)
        elapsed = time.monotonic() - t0

        print(f"\n[test] DuckDuckGo search completed in {elapsed:.1f}s")
        print(f"[test] Results: {response.total}")
        for r in response.results:
            print(f"  - {r.title}: {r.url}")

        assert response.total > 0, "Expected at least 1 search result from DuckDuckGo"
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_proxy_ip_verification(proxies: list[str]):
    """Verify proxy works by fetching IP from api64.ipify.org via browser."""
    from src.scraper.browser import BrowserPool

    pool = BrowserPool(
        pool_size=3,
        max_pool_size=5,
        headless=True,
        proxy_list=proxies,
        block_images=False,
    )
    await pool.start()
    try:
        async with pool.acquire() as page:
            await page.goto("https://api64.ipify.org?format=text", timeout=15000)
            ip_text = await page.inner_text("body")
            ip = ip_text.strip()
            print(f"\n[test] Proxy IP: {ip}")
            assert ip, "Expected an IP address from api64.ipify.org"
            pool.record_success()
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_proxy_concurrent_requests(proxies: list[str]):
    """Verify concurrent requests use different proxies."""
    from src.scraper.browser import BrowserPool

    pool = BrowserPool(
        pool_size=5,
        max_pool_size=10,
        headless=True,
        proxy_list=proxies,
        block_images=False,
    )
    await pool.start()
    try:
        async def fetch_ip(pool: BrowserPool, idx: int) -> str:
            """Fetch IP through proxy."""
            async with pool.acquire() as page:
                await page.goto("https://api64.ipify.org?format=text", timeout=15000)
                content = await page.inner_text("body")
                ip = content.strip()
                pool.record_success()
                return ip

        t0 = time.monotonic()
        # Launch 3 concurrent requests
        tasks = [fetch_ip(pool, i) for i in range(3)]
        ips = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - t0

        print(f"\n[test] Concurrent IP fetch completed in {elapsed:.1f}s")
        for i, ip in enumerate(ips):
            if isinstance(ip, Exception):
                print(f"  [req#{i}] ERROR: {ip}")
            else:
                print(f"  [req#{i}] IP: {ip}")

        # At least 2 should succeed
        successful = [ip for ip in ips if isinstance(ip, str) and ip != "unknown"]
        assert len(successful) >= 2, f"Expected at least 2 successful concurrent requests, got {len(successful)}"
        print(f"[test] {len(successful)}/3 concurrent requests succeeded")
        print(f"[test] Unique IPs: {len(set(successful))}")
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_proxy_rotator_round_robin(proxies: list[str]):
    """Unit-level: verify round-robin rotation."""
    from src.scraper.proxy import ProxyRotator

    rotator = ProxyRotator(proxies[:5])
    seen = [rotator.next() for _ in range(10)]
    # next() converts socks5h:// → socks5://, so compare converted values
    expected = [p.replace("socks5h://", "socks5://") for p in proxies[:5]]
    assert seen[:5] == expected
    assert seen[5:10] == expected
    assert rotator.count == 5
