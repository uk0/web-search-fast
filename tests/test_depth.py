"""Tests for fetch_page_content render/scroll handling of dynamic pages."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.scraper.depth import fetch_page_content


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
