from __future__ import annotations
import pytest
from src.scraper.parser import extract_main_content, extract_links, extract_main_content_markdown


SAMPLE_HTML = """
<html>
<head><title>Test</title></head>
<body>
<nav>Navigation</nav>
<main>
<h1>Main Title</h1>
<p>This is the main content paragraph.</p>
<a href="https://example.com/link1">Link 1</a>
<a href="/relative">Relative Link</a>
<a href="javascript:void(0)">JS Link</a>
</main>
<footer>Footer</footer>
</body>
</html>
"""


class TestExtractMainContent:
    def test_extracts_main_text(self):
        text = extract_main_content(SAMPLE_HTML)
        assert "Main Title" in text
        assert "main content paragraph" in text
        assert "Navigation" not in text
        assert "Footer" not in text

    def test_empty_html(self):
        assert extract_main_content("") == ""

    def test_no_main_tag(self):
        html = "<html><body><p>Hello</p></body></html>"
        text = extract_main_content(html)
        assert "Hello" in text


class TestExtractMainContentMarkdown:
    def test_returns_markdown(self):
        md = extract_main_content_markdown(SAMPLE_HTML)
        assert "Main Title" in md
        assert "main content paragraph" in md


# A realistic page with no semantic <main>: a noisy nav/sidebar plus a dense
# article div. The density scorer must pick the article, not the link soup.
NOISY_HTML = """
<html><body>
<div id="sidebar">
  <a href="https://x.com/1">Cat 1</a><a href="https://x.com/2">Cat 2</a>
  <a href="https://x.com/3">Cat 3</a><a href="https://x.com/4">Cat 4</a>
</div>
<div class="post-body">
  <h1>Understanding Async IO</h1>
  <p>Asynchronous programming lets a single thread juggle many tasks by
  yielding control while waiting on I/O. This paragraph is intentionally long
  so that the density score clearly beats the link-heavy sidebar block above.</p>
  <p>The event loop schedules coroutines and resumes them when their awaited
  operations complete, which keeps throughput high without extra threads.</p>
</div>
<div id="comments"><a href="https://x.com/c">A comment link</a></div>
</body></html>
"""


class TestAutoContentDetection:
    def test_picks_dense_block_over_link_soup(self):
        text = extract_main_content(NOISY_HTML)
        assert "Understanding Async IO" in text
        assert "event loop schedules coroutines" in text
        # Sidebar category links must not dominate the extracted content
        assert "Cat 1" not in text

    def test_semantic_main_preferred(self):
        html = """
        <html><body>
        <div class="ad">buy now buy now buy now</div>
        <main><p>The canonical article body lives in a semantic main element
        and contains more than two hundred characters of real prose so that
        the semantic-container shortcut accepts it immediately without having
        to fall through to the density scoring path at all here.</p></main>
        </body></html>
        """
        text = extract_main_content(html)
        assert "canonical article body" in text
        assert "buy now" not in text

    def test_empty_main_falls_through_to_density(self):
        # Empty <main> shell must NOT win — scorer should pick the real div.
        html = """
        <html><body>
        <main></main>
        <div class="story"><p>Real content that is long enough to be detected
        by the density scorer because it comfortably exceeds the minimum text
        threshold required for a block to score above zero in our heuristic.</p></div>
        </body></html>
        """
        text = extract_main_content(html)
        assert "Real content that is long enough" in text


class TestExtractLinks:
    def test_extracts_http_links(self):
        links = extract_links(SAMPLE_HTML)
        urls = [l["url"] for l in links]
        assert "https://example.com/link1" in urls

    def test_resolves_relative_links(self):
        links = extract_links(SAMPLE_HTML, base_url="https://example.com")
        urls = [l["url"] for l in links]
        assert "https://example.com/relative" in urls

    def test_skips_javascript_links(self):
        links = extract_links(SAMPLE_HTML)
        urls = [l["url"] for l in links]
        for u in urls:
            assert not u.startswith("javascript:")

    def test_deduplicates(self):
        html = '<html><body><a href="https://a.com">A</a><a href="https://a.com">A2</a></body></html>'
        links = extract_links(html)
        assert len(links) == 1
