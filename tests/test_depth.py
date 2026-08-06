"""Tests for fetch_page_content render/scroll handling and budgeted depth crawling."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from src.api.schemas import SearchResult
from src.scraper import depth as depth_mod
from src.scraper.depth import crawl_results, fetch_page_content


def _mock_page(height_sequence=None):
    page = AsyncMock()
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value="<html><body>ok</body></html>")
    page.wait_for_load_state = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    # evaluate: scroll calls return growing then stable heights
    heights = list(height_sequence or [1000, 2000, 2000])
    async def _evaluate(js):
        if "scrollHeight" in js:
            return heights.pop(0) if heights else 2000
        return None
    page.evaluate = AsyncMock(side_effect=_evaluate)
    return page


class TestFetchPageContentModes:
    @pytest.mark.asyncio
    async def test_default_no_render_no_scroll(self):
        page = _mock_page()
        html = await fetch_page_content(page, "https://e.com", timeout=10)
        assert html == "<html><body>ok</body></html>"
        page.goto.assert_awaited_once()
        page.wait_for_load_state.assert_not_called()  # no render
        page.evaluate.assert_not_called()  # no scroll

    @pytest.mark.asyncio
    async def test_render_waits_for_networkidle(self):
        page = _mock_page()
        await fetch_page_content(page, "https://e.com", timeout=10, render=True)
        page.wait_for_load_state.assert_awaited()  # networkidle settle
        args = page.wait_for_load_state.await_args
        assert args.args[0] == "networkidle"

    @pytest.mark.asyncio
    async def test_scroll_triggers_scrolling(self):
        page = _mock_page(height_sequence=[1000, 2000, 3000, 3000])
        await fetch_page_content(page, "https://e.com", timeout=10, scroll=True)
        # at least one scrollHeight evaluate happened
        scroll_calls = [c for c in page.evaluate.await_args_list if "scrollHeight" in c.args[0]]
        assert len(scroll_calls) >= 2

    @pytest.mark.asyncio
    async def test_scroll_stops_when_height_stable(self):
        # height stable immediately → should stop after first step
        page = _mock_page(height_sequence=[1500, 1500])
        await fetch_page_content(page, "https://e.com", timeout=10, scroll=True)
        scroll_calls = [c for c in page.evaluate.await_args_list if "scrollHeight" in c.args[0]]
        assert len(scroll_calls) <= 2  # stopped early, didn't run all 12 steps

    @pytest.mark.asyncio
    async def test_render_scroll_still_returns_content_on_settle_error(self):
        page = _mock_page()
        page.wait_for_load_state = AsyncMock(side_effect=Exception("networkidle timeout"))
        html = await fetch_page_content(page, "https://e.com", timeout=10, render=True, scroll=True)
        assert "ok" in html  # settle error is non-fatal


class TestFetchPageContentFailures:
    @pytest.mark.asyncio
    async def test_nav_timeout_salvages_partial_dom(self):
        page = _mock_page()
        page._wsm_rotating = False
        page.goto = AsyncMock(side_effect=Exception("Navigation timeout of 9000ms exceeded"))
        page.content = AsyncMock(return_value="<html><body>" + "x" * 800 + "</body></html>")
        html = await fetch_page_content(page, "https://e.com", timeout=5)
        assert "xxx" in html  # partial DOM beats throwing the elapsed time away

    @pytest.mark.asyncio
    async def test_nav_failure_with_empty_dom_returns_empty(self):
        page = _mock_page()
        page._wsm_rotating = False
        page.goto = AsyncMock(side_effect=Exception("net::ERR_NAME_NOT_RESOLVED"))
        page.content = AsyncMock(return_value="<html><head></head><body></body></html>")
        assert await fetch_page_content(page, "https://e.com", timeout=5) == ""

    @pytest.mark.asyncio
    async def test_proxy_error_propagates_when_rotating(self):
        page = _mock_page()
        page._wsm_rotating = True
        page.goto = AsyncMock(side_effect=Exception("NS_ERROR_PROXY_CONNECTION_REFUSED"))
        with pytest.raises(Exception, match="NS_ERROR_PROXY"):
            await fetch_page_content(page, "https://e.com", timeout=5)


def _html(marker: str, filler: int = 60) -> str:
    """Page whose <main> holds enough text to be picked as the content block."""
    return f"<html><body><main><p>{marker} {'lorem ipsum ' * filler}</p></main></body></html>"


def _results(*urls: str) -> list[SearchResult]:
    return [SearchResult(title=f"T{i}", url=u) for i, u in enumerate(urls)]


class _FakePool:
    """BrowserPool stand-in: hands out AsyncMock pages, tracks tab usage."""

    def __init__(self, pages=None, *, default=None, pool_size=18, rotating=False):
        self._pages = pages or {}
        self._default = default if default is not None else {"html": _html("default")}
        self._pool_size = pool_size
        self._rotating = rotating
        self.visited: list[str] = []
        self.acquired = 0
        self.active = 0
        self.max_active = 0

    @asynccontextmanager
    async def acquire(self, *, label=None, session=None):
        self.acquired += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield self._page()
        finally:
            self.active -= 1

    def _page(self):
        page = AsyncMock()
        page._wsm_rotating = self._rotating
        state = {"html": ""}

        async def _goto(url, **kwargs):
            self.visited.append(url)
            spec = self._pages.get(url, self._default)
            if spec.get("delay"):
                await asyncio.sleep(spec["delay"])
            if spec.get("error") is not None:
                raise spec["error"]
            state["html"] = spec.get("html", "")

        page.goto = AsyncMock(side_effect=_goto)
        page.content = AsyncMock(side_effect=lambda: state["html"])
        return page


class _DeadPool:
    _pool_size = 18

    def acquire(self, *, label=None, session=None):
        raise RuntimeError("Browser pool exhausted (pool_size=18)")


@pytest.fixture(autouse=True)
def _clean_depth_env(monkeypatch):
    monkeypatch.delenv("DEPTH_CONCURRENCY", raising=False)


class TestUrlFilters:
    def test_binary_urls_rejected(self):
        for url in ("https://a.com/f.pdf", "https://a.com/F.PNG", "https://a.com/x.zip?dl=1",
                    "https://a.com/v.mp4", "ftp://a.com/x", "mailto:a@b.c"):
            assert not depth_mod._is_crawlable(url), url

    def test_html_urls_accepted(self):
        for url in ("https://a.com", "https://a.com/post/1", "https://a.com/p.html",
                    "http://a.com/x?q=.pdf"):
            assert depth_mod._is_crawlable(url), url

    def test_normalize_drops_fragment_and_trailing_slash(self):
        assert depth_mod._normalize_url("https://A.com/p/#x") == depth_mod._normalize_url("https://a.com/p")
        assert depth_mod._normalize_url("https://a.com/p?q=1") != depth_mod._normalize_url("https://a.com/p")

    def test_page_budget_math(self):
        now = time.monotonic()
        cap = depth_mod._PAGE_TIMEOUT_MAX
        assert depth_mod._page_budget(now + 20, 4, 12) == pytest.approx(cap, abs=0.2)   # 1 wave → capped
        assert depth_mod._page_budget(now + 20, 36, 12) == pytest.approx(6.67, abs=0.3)  # 3 waves
        assert depth_mod._page_budget(now + 1, 4, 12) <= 1.0  # never more than what is left
        assert depth_mod._page_budget(now - 1, 4, 12) == 0.0


class TestCrawlSelection:
    @pytest.mark.asyncio
    async def test_depth1_is_passthrough(self):
        pool = _FakePool()
        results = _results("https://a.com/p")
        assert await crawl_results(pool, results, depth=1, timeout=5) is results
        assert pool.acquired == 0

    @pytest.mark.asyncio
    async def test_skips_binary_and_reuses_duplicates(self):
        pool = _FakePool()
        results = _results(
            "https://a.com/page",
            "https://a.com/page#section",  # same page, fragment only
            "https://a.com/report.pdf",
            "https://cdn.a.com/img.PNG",
            "ftp://a.com/x",
        )
        out = await crawl_results(pool, results, depth=2, timeout=5)
        assert len(out) == 5
        assert pool.visited == ["https://a.com/page"]  # one tab for the whole set
        assert out[0].content
        assert out[1].content == out[0].content  # duplicate rides along
        assert [r.content for r in out[2:]] == ["", "", ""]

    @pytest.mark.asyncio
    async def test_failed_page_keeps_result(self):
        pool = _FakePool({"https://bad.com/a": {"error": Exception("net::ERR_FAILED")}})
        out = await crawl_results(pool, _results("https://bad.com/a", "https://ok.com/a"), depth=2, timeout=5)
        assert len(out) == 2
        assert out[0].content == ""
        assert out[1].content

    @pytest.mark.asyncio
    async def test_proxy_error_absorbed_result_kept(self):
        pool = _FakePool({"https://x.com/a": {"error": Exception("NS_ERROR_PROXY_CONNECTION_REFUSED")}},
                         rotating=True)
        out = await crawl_results(pool, _results("https://x.com/a"), depth=2, timeout=5)
        assert len(out) == 1 and out[0].content == ""

    @pytest.mark.asyncio
    async def test_pool_exhaustion_keeps_results(self):
        out = await crawl_results(_DeadPool(), _results("https://a.com/p", "https://b.com/p"), depth=2, timeout=5)
        assert len(out) == 2 and all(r.content == "" for r in out)


class TestCrawlBudget:
    @pytest.mark.asyncio
    async def test_zero_budget_does_no_work(self):
        pool = _FakePool()
        out = await crawl_results(pool, _results("https://a.com/x"), depth=2, timeout=0)
        assert pool.visited == []
        assert out[0].content == ""

    @pytest.mark.asyncio
    async def test_deadline_returns_partial_results(self):
        pool = _FakePool({"https://slow.com/a": {"html": _html("slow"), "delay": 5.0}})
        results = _results("https://fast1.com/a", "https://slow.com/a", "https://fast2.com/a")
        t0 = time.monotonic()
        out = await crawl_results(pool, results, depth=2, timeout=0.6)
        elapsed = time.monotonic() - t0
        assert len(out) == 3
        assert out[0].content and out[2].content  # finished work is kept
        assert out[1].content == ""  # cancelled at the deadline, still returned
        assert elapsed < 1.2  # budget honoured instead of waiting out the 5s page

    @pytest.mark.asyncio
    async def test_straggler_cut_once_most_pages_are_in(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_STRAGGLER_GRACE", 0.15)
        pool = _FakePool({"https://slow.com/a": {"html": _html("slow"), "delay": 5.0}})
        results = _results("https://f1.com/a", "https://f2.com/a", "https://f3.com/a", "https://slow.com/a")
        t0 = time.monotonic()
        out = await crawl_results(pool, results, depth=2, timeout=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0  # tail cut instead of burning the remaining 5s budget
        assert all(r.content for r in out[:3])
        assert out[3].content == ""

    @pytest.mark.asyncio
    async def test_slow_page_does_not_eat_whole_budget(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_PAGE_TIMEOUT_MIN", 0.05)
        monkeypatch.setenv("DEPTH_CONCURRENCY", "1")
        pool = _FakePool({"https://slow.com/a": {"html": _html("slow"), "delay": 5.0}})
        out = await crawl_results(pool, _results("https://slow.com/a", "https://fast.com/a"),
                                  depth=2, timeout=1.4)
        assert out[0].content == ""  # capped at its per-page slice
        assert out[1].content  # queued behind it, still got crawled


class TestCrawlConcurrency:
    @pytest.mark.asyncio
    async def test_env_bounds_concurrency(self, monkeypatch):
        monkeypatch.setenv("DEPTH_CONCURRENCY", "2")
        pool = _FakePool(default={"html": _html("x"), "delay": 0.05})
        out = await crawl_results(pool, _results(*[f"https://s{i}.com/p" for i in range(6)]),
                                  depth=2, timeout=5)
        assert pool.max_active <= 2
        assert pool.acquired == 6
        assert all(r.content for r in out)

    @pytest.mark.asyncio
    async def test_pool_size_bounds_concurrency(self):
        pool = _FakePool(default={"html": _html("x"), "delay": 0.05}, pool_size=3)
        await crawl_results(pool, _results(*[f"https://s{i}.com/p" for i in range(6)]), depth=2, timeout=5)
        assert pool.max_active <= 2  # pool_size - 1, never the full pool

    @pytest.mark.asyncio
    async def test_default_concurrency_beats_old_cap(self):
        pool = _FakePool(default={"html": _html("x"), "delay": 0.05})
        await crawl_results(pool, _results(*[f"https://s{i}.com/p" for i in range(8)]), depth=2, timeout=5)
        assert pool.max_active > 5  # the old _DEPTH_CONCURRENCY=5 wave limit is gone


class TestContentBounds:
    @pytest.mark.asyncio
    async def test_content_is_truncated(self):
        pool = _FakePool(default={"html": _html("big", filler=3000)})
        out = await crawl_results(pool, _results("https://big.com/a"), depth=2, timeout=5)
        assert len(out[0].content) == depth_mod._MAX_CONTENT_CHARS

    @pytest.mark.asyncio
    async def test_oversized_html_still_parses(self):
        huge = "<html><body><main><p>" + "a" * (depth_mod._MAX_HTML_CHARS + 5000) + "</p></main></body></html>"
        pool = _FakePool(default={"html": huge})
        out = await crawl_results(pool, _results("https://big.com/a"), depth=2, timeout=5)
        assert 0 < len(out[0].content) <= depth_mod._MAX_CONTENT_CHARS


class TestDepthThree:
    @pytest.mark.asyncio
    async def test_sub_links_filtered_and_bounded(self):
        parent = "https://p.com/article"
        parent_html = (
            "<html><body><main><p>" + "parent body " * 60 + "</p>"
            f'<a href="{parent}#top">anchor to self</a>'
            f'<a href="{parent}">self</a>'
            '<a href="https://p.com/manual.pdf">binary</a>'
            '<a href="https://s1.com/a">Sub One</a>'
            '<a href="https://s1.com/a">Sub One again</a>'
            '<a href="https://s2.com/b">Sub Two</a>'
            "</main></body></html>"
        )
        pool = _FakePool({
            parent: {"html": parent_html},
            "https://s1.com/a": {"html": _html("sub one", filler=3000)},
            "https://s2.com/b": {"html": _html("sub two")},
        })
        out = await crawl_results(pool, _results(parent), depth=3, timeout=6)
        assert set(pool.visited) == {parent, "https://s1.com/a", "https://s2.com/b"}
        assert "parent body" in out[0].content
        assert sorted(s.url for s in out[0].sub_links) == ["https://s1.com/a", "https://s2.com/b"]
        big_sub = next(s for s in out[0].sub_links if s.url == "https://s1.com/a")
        assert len(big_sub.content) == depth_mod._MAX_SUB_CONTENT_CHARS
