"""Helpers for extracting full article content from RSS entries."""

from __future__ import annotations

import html as _html
import re
from typing import Tuple

from bs4 import BeautifulSoup

try:  # Optional dependency during unit tests
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover - best effort import
    trafilatura = None


_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_URL_ONLY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORDPRESS_FOOTER_RE = re.compile(r"the post .+ appeared first on", re.IGNORECASE)


def _looks_like_footer(line: str) -> bool:
    if not line:
        return False
    low = line.strip().lower()
    if _WORDPRESS_FOOTER_RE.search(low):
        return True
    if low.startswith("the post"):
        return True
    if low.startswith("appeared first on"):
        return True
    if low in {"גיקטיים", "geektime", "appeared first on", "the post"}:
        return True
    return False
_MEDIA_TAGS = ("figure", "figcaption", "img", "picture", "video", "iframe", "embed", "object", "source")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SUBTITLE_TAGS = ("h2", "h3", "h4")


def _remove_footer_lines(lines):
    return [line for line in lines if not _looks_like_footer(line)]


def _strip_embedded_media(soup: BeautifulSoup) -> None:
    for tag_name in _MEDIA_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    # Drop generic caption containers based on class/id naming.
    for tag in soup.find_all(class_=re.compile(r"caption", re.IGNORECASE)):
        tag.decompose()
    for tag in soup.find_all(id=re.compile(r"caption", re.IGNORECASE)):
        tag.decompose()
    for node in soup.find_all(string=_WORDPRESS_FOOTER_RE):
        parent = node.parent
        if parent:
            parent.decompose()
        else:
            node.extract()
    for tag in list(soup.find_all()):
        text = tag.get_text(strip=True)
        if _looks_like_footer(text):
            tag.decompose()


def _extract_subtitle_and_strip_headings(soup: BeautifulSoup) -> str:
    subtitle = ""
    body_started = False
    for element in list(soup.find_all(True)):
        if element.name in _HEADING_TAGS:
            if not subtitle and not body_started and element.name in _SUBTITLE_TAGS:
                subtitle = element.get_text(" ", strip=True)
            element.decompose()
            continue
        if element.name == "p" and element.get_text(strip=True):
            body_started = True
    return subtitle

def _get_entry_content_html(entry) -> str:
    """Return the richest HTML snippet available on the feed entry."""

    content = getattr(entry, "content", None)
    if content:
        if isinstance(content, list) and content:
            first = content[0]
            value = getattr(first, "value", None)
            if value:
                return value
            if isinstance(first, dict):
                value = first.get("value")
                if value:
                    return value
        elif isinstance(content, dict):
            value = content.get("value")
            if value:
                return value

    summary_detail = getattr(entry, "summary_detail", None)
    if summary_detail:
        stype = getattr(summary_detail, "type", None)
        if not stype and isinstance(summary_detail, dict):
            stype = summary_detail.get("type")
        if stype and "html" in stype:
            value = getattr(summary_detail, "value", None)
            if value:
                return value
            if isinstance(summary_detail, dict):
                value = summary_detail.get("value")
                if value:
                    return value

    summary = getattr(entry, "summary", None)
    if summary:
        return summary
    description = getattr(entry, "description", None)
    if description:
        return description
    return ""


def html_to_text(html_content: str) -> tuple[str, str]:
    if not html_content:
        return "", ""
    soup = BeautifulSoup(html_content, "lxml")
    _strip_embedded_media(soup)
    subtitle = _extract_subtitle_and_strip_headings(soup)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\n ", "\n", text)
    lines = [line.strip() for line in text.splitlines()]
    filtered = [line for line in lines if line and not _URL_ONLY_RE.fullmatch(line)]
    filtered = _remove_footer_lines(filtered)
    return "\n".join(filtered).strip(), subtitle


def _normalize_text_block(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\n ", "\n", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not _URL_ONLY_RE.fullmatch(line)]
    lines = _remove_footer_lines(lines)
    return "\n\n".join(lines).strip()


def fetch_article_text(url: str) -> str:
    if not url or trafilatura is None:
        return ""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        extracted = trafilatura.extract(downloaded, output_format="txt")
        if not extracted:
            return ""
        return _normalize_text_block(extracted)
    except Exception:
        return ""


def text_to_html(text: str) -> str:
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    cleaned = [p for p in paragraphs if not _URL_ONLY_RE.fullmatch(p)]
    cleaned = _remove_footer_lines(cleaned)
    if not cleaned:
        cleaned = [p for p in paragraphs if p]
    return "".join(f"<p>{_html.escape(p)}</p>" for p in cleaned)


def _word_count(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"\w+", text))


def resolve_article_content(entry, link: str | None = None, *, allow_fetch: bool = False, min_words: int = 80) -> Tuple[str, str, str]:
    """Return (plain_text, html_content, subtitle) for the entry.

    When ``allow_fetch`` is True and the feed-provided content is short, we
    attempt to download the full article using ``trafilatura``.
    """

    html_content = _get_entry_content_html(entry)
    plain_text, subtitle = html_to_text(html_content)
    normalized_html = text_to_html(plain_text)

    if allow_fetch and link and _word_count(plain_text) < max(20, min_words):
        fetched_text = fetch_article_text(link)
        if fetched_text:
            plain_text = fetched_text
            normalized_html = text_to_html(fetched_text)
            subtitle = ""

    return plain_text, normalized_html, subtitle
