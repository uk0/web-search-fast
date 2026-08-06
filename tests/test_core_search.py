from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.api.schemas import SearchRequest, SearchResponse, SearchResult, SearchMetadata
from src.config import SearchEngine
from src.core.search import SearchError, do_search


def _mock_pool(started: bool = True, page_url: str = "https://duckduckgo.com/?q=x") -> MagicMock:
    pool = MagicMock()
    pool._started = started
    page = AsyncMock()
    page.url = page_url  # search.py inspects the landed URL to detect real blocks
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=page)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


class TestDoSearch:
    @pytest.mark.asyncio
    async def test_raises_when_pool_not_started(self):
        pool = _mock_pool(started=False)
        req = SearchRequest(query="test")
        with pytest.raises(SearchError, match="not initialized"):
            await do_search(pool, req)

    @pytest.mark.asyncio
    @patch("src.core.search.crawl_results", new_callable=AsyncMock)
    @patch("src.core.search.ENGINES")
    async def test_returns_response(self, mock_engines, mock_crawl):
        pool = _mock_pool()
        results = [SearchResult(title="R1", url="https://example.com", snippet="S1")]

        mock_engine = AsyncMock()
        mock_engine.search.return_value = results
        mock_engines.__getitem__ = MagicMock(return_value=mock_engine)

        mock_crawl.return_value = results

        req = SearchRequest(query="test", engine=SearchEngine.DUCKDUCKGO)
        resp = await do_search(pool, req)

        assert resp.query == "test"
        assert resp.total == 1
        assert resp.engine == SearchEngine.DUCKDUCKGO
        assert resp.results[0].title == "R1"

    @pytest.mark.asyncio
    @patch("src.core.search.crawl_results", new_callable=AsyncMock)
    @patch("src.core.search.ENGINES")
    @patch("src.core.search.FALLBACK_ORDER", {
        SearchEngine.GOOGLE: [SearchEngine.DUCKDUCKGO, SearchEngine.BING],
    })
    async def test_fallback_on_empty_results(self, mock_engines, mock_crawl):
        pool = _mock_pool()
        results = [SearchResult(title="FB", url="https://fb.com", snippet="Fallback")]

        primary = AsyncMock()
        primary.search.return_value = []
        fallback = AsyncMock()
        fallback.search.return_value = results

        bing = AsyncMock()
        bing.search.return_value = []
        mock_engines.__getitem__ = MagicMock(side_effect=lambda k: {
            SearchEngine.GOOGLE: primary,
            SearchEngine.DUCKDUCKGO: fallback,
            SearchEngine.BING: bing,
        }[k])

        mock_crawl.return_value = results

        req = SearchRequest(query="test", engine=SearchEngine.GOOGLE)
        resp = await do_search(pool, req)

        assert resp.engine == SearchEngine.DUCKDUCKGO
        assert resp.total == 1

    @pytest.mark.asyncio
    @patch("src.core.search.crawl_results", new_callable=AsyncMock)
    @patch("src.core.search.ENGINES")
    @patch("src.core.search.FALLBACK_ORDER", {
        SearchEngine.GOOGLE: [SearchEngine.DUCKDUCKGO, SearchEngine.BING],
    })
    async def test_fallback_on_insufficient_results(self, mock_engines, mock_crawl):
        # Primary returns 1 (< sufficient); fallback returns a full set — the
        # fuller set wins, regularizing the result count.
        pool = _mock_pool()
        primary = AsyncMock()
        primary.search.return_value = [
            SearchResult(title="G1", url="https://g.com/1", snippet="x")
        ]
        full = [SearchResult(title=f"D{i}", url=f"https://d.com/{i}", snippet="x")
                for i in range(8)]
        fallback = AsyncMock()
        fallback.search.return_value = full
        bing = AsyncMock()
        bing.search.return_value = []
        mock_engines.__getitem__ = MagicMock(side_effect=lambda k: {
            SearchEngine.GOOGLE: primary,
            SearchEngine.DUCKDUCKGO: fallback,
            SearchEngine.BING: bing,
        }[k])
        mock_crawl.side_effect = lambda pool, r, **kw: r

        req = SearchRequest(query="test", engine=SearchEngine.GOOGLE, max_results=10)
        resp = await do_search(pool, req)

        assert resp.engine == SearchEngine.DUCKDUCKGO
        assert resp.total == 8


class TestFetchUrlContent:
    @pytest.mark.asyncio
    @patch("src.core.search.extract_main_content_markdown")
    @patch("src.core.search.fetch_page_content", new_callable=AsyncMock)
    async def test_returns_markdown(self, mock_fetch, mock_extract):
        pool = _mock_pool()
        mock_fetch.return_value = "<html><body>Hello</body></html>"
        mock_extract.return_value = "# Hello"

        from src.core.search import fetch_url_content
        result = await fetch_url_content(pool, "https://example.com")
        assert result == "# Hello"

    @pytest.mark.asyncio
    @patch("src.core.search.fetch_page_content", new_callable=AsyncMock)
    async def test_returns_empty_on_failure(self, mock_fetch):
        pool = _mock_pool()
        mock_fetch.return_value = ""

        from src.core.search import fetch_url_content
        result = await fetch_url_content(pool, "https://example.com")
        assert result == ""


class TestEngineHealthSignals:
    """Empty SERPs must only trip the breaker fast on a REAL block page."""

    @pytest.mark.asyncio
    @patch("src.core.search.crawl_results", new_callable=AsyncMock)
    @patch("src.core.search.ENGINES")
    @patch("src.core.search.FALLBACK_ORDER", {SearchEngine.DUCKDUCKGO: []})
    async def test_transient_empty_serp_does_not_open_breaker(self, mock_engines, mock_crawl):
        from src.engine import health as eng_health
        pool = _mock_pool()
        empty = AsyncMock()
        empty.search.return_value = []
        mock_engines.__getitem__ = MagicMock(return_value=empty)
        mock_crawl.side_effect = lambda pool, r, **kw: r

        req = SearchRequest(query="q", engine=SearchEngine.DUCKDUCKGO, max_results=5)
        await do_search(pool, req)
        # one plain failure must not bench the engine (threshold is 3)
        assert not eng_health.should_skip("duckduckgo")

    @pytest.mark.asyncio
    @patch("src.core.search.crawl_results", new_callable=AsyncMock)
    @patch("src.core.search.ENGINES")
    @patch("src.core.search.FALLBACK_ORDER", {SearchEngine.GOOGLE: []})
    async def test_captcha_page_opens_breaker_immediately(self, mock_engines, mock_crawl):
        from src.engine import health as eng_health
        pool = _mock_pool(page_url="https://www.google.com/sorry/index?continue=x")
        empty = AsyncMock()
        empty.search.return_value = []
        mock_engines.__getitem__ = MagicMock(return_value=empty)
        mock_crawl.side_effect = lambda pool, r, **kw: r

        req = SearchRequest(query="q", engine=SearchEngine.GOOGLE, max_results=5)
        await do_search(pool, req)
        assert eng_health.should_skip("google")  # hard block trips at once


class TestCacheDegradation:
    """A warm cache must keep serving while the browser pool is down."""

    @pytest.mark.asyncio
    @patch("src.core.search.crawl_results", new_callable=AsyncMock)
    @patch("src.core.search.ENGINES")
    @patch("src.core.search.FALLBACK_ORDER", {SearchEngine.DUCKDUCKGO: []})
    async def test_cache_hit_served_when_pool_down(self, mock_engines, mock_crawl):
        results = [SearchResult(title="R", url="https://e.com", snippet="s")]
        engine = AsyncMock()
        engine.search.return_value = results
        mock_engines.__getitem__ = MagicMock(return_value=engine)
        mock_crawl.side_effect = lambda pool, r, **kw: r
        req = SearchRequest(query="warm me", engine=SearchEngine.DUCKDUCKGO, max_results=5)

        # 1) populate the cache with a healthy pool
        await do_search(_mock_pool(), req)

        # 2) pool is now dead AND unrecoverable — the cache must still answer
        dead = _mock_pool(started=False)
        dead.restart = AsyncMock(side_effect=RuntimeError("browser launch failed"))
        resp = await do_search(dead, req)
        assert resp.metadata.cached is True
        assert resp.total == 1
        dead.restart.assert_not_awaited()  # never even needed the browser

    @pytest.mark.asyncio
    async def test_cache_miss_still_fails_when_pool_down(self):
        # No cached entry => the pool guard must still apply.
        dead = _mock_pool(started=False)
        dead.restart = AsyncMock(side_effect=RuntimeError("browser launch failed"))
        with pytest.raises(SearchError, match="not initialized"):
            await do_search(dead, SearchRequest(query="never cached", max_results=5))
