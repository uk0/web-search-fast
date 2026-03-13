from __future__ import annotations

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify


def _get_main_content_element(html: str) -> Tag | None:
    """Parse HTML, strip non-content tags, and return the main content element."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "iframe", "noscript"]):
        tag.decompose()

    return (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"role": "main"})
        or soup.find("div", {"id": "content"})
        or soup.find("div", {"class": "content"})
        or soup.body
    )


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
    """Extract all links from the main content area."""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body
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
