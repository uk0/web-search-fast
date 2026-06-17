from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.engine.google import GoogleSearchEngine
from src.engine.bing import BingSearchEngine
from src.engine.duckduckgo import DuckDuckGoSearchEngine


class TestGoogleEngine:
    def test_build_url(self):
        engine = GoogleSearchEngine()
        url = engine.build_search_url("hello world")
        assert "google.com/search" in url
        assert "hello+world" in url or "hello%20world" in url
        assert "hl=en" in url
        assert "udm=14" in url  # Web filter for consistent organic results

    def test_name(self):
        assert GoogleSearchEngine().name == "google"

    def test_ready_selector_configured(self):
        # SERP hydration wait targets anchor-wrapped result titles (udm=14 safe).
        assert "h3" in (GoogleSearchEngine.ready_selector or "")

    def test_is_blocked_detects_sorry(self):
        assert GoogleSearchEngine._is_blocked("https://www.google.com/sorry/index?continue=...")
        assert GoogleSearchEngine._is_blocked("https://www.google.com/CAPTCHA")
        assert not GoogleSearchEngine._is_blocked("https://www.google.com/search?q=foo")

    def test_warmup_off_by_default(self):
        # Homepage warm-up is an opt-in cost; default must be skip-for-speed.
        assert GoogleSearchEngine.warmup_homepage is False

    @pytest.mark.asyncio
    async def test_search_skips_homepage_when_warmup_off(self):
        # With warmup off, search() must NOT navigate to the homepage first —
        # only the search URL should be requested.
        engine = GoogleSearchEngine()
        engine.warmup_homepage = False
        page = AsyncMock()
        page.url = "https://www.google.com/search?q=x"
        goto_urls = []

        async def _goto(url, **kw):
            goto_urls.append(url)
            return MagicMock(status=200)

        page.goto = AsyncMock(side_effect=_goto)
        page.wait_for_selector = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)
        page.evaluate = AsyncMock(return_value=[{"title": "T", "url": "https://e.com", "snippet": "s"}])

        await engine.search(page, "x", max_results=5)
        assert not any(u == "https://www.google.com/" for u in goto_urls)


class TestBingEngine:
    def test_build_url(self):
        engine = BingSearchEngine()
        url = engine.build_search_url("test query")
        assert "bing.com/search" in url
        assert "test" in url

    def test_name(self):
        assert BingSearchEngine().name == "bing"

    def test_ready_selector_configured(self):
        # Bing renders SERP client-side; the wait selector must target b_algo.
        assert "b_algo" in (BingSearchEngine.ready_selector or "")


class TestDuckDuckGoEngine:
    def test_build_url(self):
        engine = DuckDuckGoSearchEngine()
        url = engine.build_search_url("test query")
        assert "duckduckgo.com" in url
        assert "test" in url

    def test_name(self):
        assert DuckDuckGoSearchEngine().name == "duckduckgo"


class TestDDGUrlResolver:
    """Test _resolve_ddg_url which extracts real URLs from DDG redirect links."""

    def test_redirect_url_with_uddg(self):
        from src.engine.duckduckgo import _resolve_ddg_url
        raw = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc123"
        assert _resolve_ddg_url(raw) == "https://example.com/page"

    def test_protocol_relative_non_redirect(self):
        from src.engine.duckduckgo import _resolve_ddg_url
        raw = "//example.com/page"
        assert _resolve_ddg_url(raw) == "https://example.com/page"

    def test_direct_http_url(self):
        from src.engine.duckduckgo import _resolve_ddg_url
        assert _resolve_ddg_url("https://example.com") == "https://example.com"

    def test_empty_url(self):
        from src.engine.duckduckgo import _resolve_ddg_url
        assert _resolve_ddg_url("") is None
        assert _resolve_ddg_url(None) is None

    def test_redirect_with_encoded_uddg(self):
        from src.engine.duckduckgo import _resolve_ddg_url
        raw = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FHello&rut=x"
        assert _resolve_ddg_url(raw) == "https://en.wikipedia.org/wiki/Hello"

    def test_no_uddg_param(self):
        from src.engine.duckduckgo import _resolve_ddg_url
        raw = "https://duckduckgo.com/l/?foo=bar"
        # Falls back to returning the URL itself since it starts with http
        assert _resolve_ddg_url(raw) == "https://duckduckgo.com/l/?foo=bar"
