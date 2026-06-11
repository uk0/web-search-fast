from __future__ import annotations

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

# Tags that never contain article content — stripped before detection.
_NOISE_TAGS = [
    "script", "style", "nav", "header", "footer", "aside",
    "iframe", "noscript", "svg", "form", "button",
]

# Semantic containers tried in priority order before falling back to scoring.
_SEMANTIC_FINDERS = [
    ("main", {}),
    ("article", {}),
    (None, {"role": "main"}),
    (None, {"id": "content"}),
    (None, {"id": "main"}),
    (None, {"id": "main-content"}),
    (None, {"class": "article-body"}),
    (None, {"class": "post-content"}),
    (None, {"class": "entry-content"}),
    (None, {"class": "markdown-body"}),
    (None, {"class": "content"}),
]

# A semantic container must hold at least this much text to be trusted;
# otherwise we fall through to density scoring (handles empty <main> shells).
_MIN_SEMANTIC_TEXT = 200


def _score_block(el: Tag) -> float:
    """Readability-lite score: reward text volume, punish link-heavy nav/menus."""
    text = el.get_text(" ", strip=True)
    text_len = len(text)
    if text_len < 120:
        return 0.0
    link_text = sum(len(a.get_text(strip=True)) for a in el.find_all("a"))
    link_density = link_text / text_len if text_len else 1.0
    if link_density > 0.5:  # menus, tag clouds, link lists
        return 0.0
    paragraphs = len(el.find_all("p"))
    return text_len * (1.0 - link_density) + paragraphs * 30.0


def _get_main_content_element(html: str) -> Tag | None:
    """Parse HTML, strip noise, and return the densest main-content element.

    Strategy: trust a semantic container (``<main>``, ``<article>``, common
    content ids/classes) only if it actually holds substantial text; else
    score every block by text density and pick the winner. Falls back to
    ``<body>`` only when nothing scores.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    # 1. Semantic containers — accept the first with enough text.
    for name, attrs in _SEMANTIC_FINDERS:
        el = soup.find(name, attrs) if name else soup.find(attrs=attrs)
        if isinstance(el, Tag) and len(el.get_text(strip=True)) >= _MIN_SEMANTIC_TEXT:
            return el

    # 2. Density fallback — best-scoring content block.
    best: Tag | None = None
    best_score = 0.0
    for el in soup.find_all(["article", "section", "div", "td"]):
        score = _score_block(el)
        if score > best_score:
            best_score = score
            best = el
    if best is not None:
        return best

    return soup.body


def extract_main_content(html: str) -> str:
    """Extract main readable content from HTML, return as plain text."""
    main = _get_main_content_element(html)
    if main is None:
        return ""
    return main.get_text(separator="\n", strip=True)


def extract_main_content_markdown(html: str) -> str:
    """Extract main readable content from HTML, return as markdown."""
    main = _get_main_content_element(html)
    if main is None:
        return ""
    return markdownify(str(main), strip=["img"]).strip()


def extract_links(html: str, base_url: str = "") -> list[dict[str, str]]:
    """Extract all links from the auto-detected main content area."""
    from urllib.parse import urljoin

    main = _get_main_content_element(html)
    if main is None:
        return []

    links = []
    seen = set()
    for a in main.find_all("a", href=True):
        href = a["href"]
        if base_url:
            href = urljoin(base_url, href)
        if href.startswith("http") and href not in seen:
            seen.add(href)
            links.append({"url": href, "title": a.get_text(strip=True)})

    return links
