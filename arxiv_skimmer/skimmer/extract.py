"""Turn an arXiv HTML (LaTeXML) page into clean text with the original LaTeX math intact."""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString

# Page furniture and back-matter that carries no physics.
_DROP_TAGS = ("script", "style", "noscript", "nav", "button", "svg", "img")
_DROP_CLASSES = (
    "ltx_bibliography", "ltx_page_header", "ltx_page_footer", "ltx_tag_section",
    "ltx_tag_subsection", "ltx_tag_subsubsection", "ltx_role_affiliation",
    "ltx_authors_multiline", "package-alerts", "ds-announcement", "modal",
    "html-header", "extra-services", "ltx_pagination", "ltx_ERROR",
)
_DROP_IDS = ("ds-announcement", "headerModal", "footer")


def _mathify(soup: BeautifulSoup) -> None:
    """Replace every <math> node with its original LaTeX, in proper math delimiters."""
    for node in soup.find_all("math"):
        latex = (node.get("alttext") or "").strip()
        if not latex:
            # LaTeXML omits alttext on a few nodes; the rendered glyphs are the fallback.
            latex = node.get_text("", strip=True)
            if not latex:
                node.decompose()
                continue
            node.replace_with(NavigableString(f" {latex} "))
            continue
        latex = re.sub(r"\s+", " ", latex).strip()
        if node.get("display") == "block":
            node.replace_with(NavigableString(f"\n\\[ {latex} \\]\n"))
        else:
            node.replace_with(NavigableString(f"${latex}$"))


def _headings(soup: BeautifulSoup) -> None:
    """Mark headings so the section structure survives the flattening to text."""
    for level in range(1, 7):
        for node in soup.find_all(f"h{level}"):
            text = node.get_text(" ", strip=True)
            if text:
                node.replace_with(NavigableString(f"\n\n{'#' * min(level, 4)} {text}\n"))
            else:
                node.decompose()


def _strip_chrome(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for cls in _DROP_CLASSES:
        for tag in soup.select(f".{cls}"):
            tag.decompose()
    for ident in _DROP_IDS:
        for tag in soup.select(f"#{ident}"):
            tag.decompose()
    # Footnote markers and equation tags are noise once the layout is gone.
    for tag in soup.select(".ltx_tag_equation, .ltx_note_mark, .ltx_role_refnum"):
        tag.decompose()


def html_to_text(html: str) -> Optional[str]:
    """Extract readable article text, or None if this is not a real LaTeXML article."""
    soup = BeautifulSoup(html, "html.parser")

    article = soup.select_one(".ltx_page_content") or soup.select_one("article") or soup.body
    if article is None:
        return None

    _strip_chrome(article)
    _mathify(article)
    _headings(article)

    text = article.get_text("\n", strip=False)

    # LaTeXML output is whitespace-heavy; collapse it without welding words together.
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # A line-broken sentence reads better rejoined; keep blank lines as paragraph breaks.
    text = re.sub(r"(?<![\n.:;?!\]])\n(?![\n#\\])", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text or None


def looks_like_article(text: Optional[str]) -> bool:
    """Guard against arXiv's 'HTML is not available for this paper' placeholder."""
    if not text or len(text) < 4000:
        return False
    lowered = text[:3000].lower()
    if "no html" in lowered and len(text) < 8000:
        return False
    return True
