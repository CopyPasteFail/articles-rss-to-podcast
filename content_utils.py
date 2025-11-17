"""Helpers for extracting full article content from RSS entries."""

from __future__ import annotations

import html as _html
import re
from collections.abc import Iterable
from typing import Any, Callable, Mapping, TypeVar, cast
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

try:  # Optional dependency during unit tests
    import trafilatura
except Exception:  # pragma: no cover - best effort import
    trafilatura = None


_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_URL_ONLY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORDPRESS_FOOTER_RE = re.compile(r"the post .+ appeared first on", re.IGNORECASE)

_FeedMapping = Mapping[str, Any]
_SeqItem = TypeVar("_SeqItem")


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _extract_value_field(data: Any) -> str | None:
    direct = _coerce_str(data)
    if direct:
        return direct
    value_attr = _coerce_str(getattr(data, "value", None))
    if value_attr:
        return value_attr
    if isinstance(data, dict):
        dict_data = cast(_FeedMapping, data)
        dict_value = _coerce_str(dict_data.get("value"))
        if dict_value:
            return dict_value
    return None


def _coerce_sequence(value: _SeqItem | list[_SeqItem] | tuple[_SeqItem, ...] | None) -> list[_SeqItem]:
    if value is None:
        return []
    if isinstance(value, list):
        return cast(list[_SeqItem], value)
    if isinstance(value, tuple):
        tuple_value = cast(tuple[_SeqItem, ...], value)
        return list(tuple_value)
    return [value]


def _media_url_from_item(item: Any) -> str:
    if isinstance(item, dict):
        dict_item = cast(_FeedMapping, item)
        candidate = dict_item.get("url")
        return candidate if isinstance(candidate, str) else ""
    getter = getattr(item, "get", None)
    if callable(getter):
        dict_getter = cast(Callable[[str, Any | None], Any], getter)
        candidate = dict_getter("url", None)
        if isinstance(candidate, str):
            return candidate
    attr_url = getattr(item, "url", None)
    return attr_url if isinstance(attr_url, str) else ""


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


def _remove_footer_lines(lines: Iterable[str]) -> list[str]:
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

def _get_entry_content_html(entry: Any) -> str:
    """Return the richest HTML snippet available on the feed entry."""

    content: Any = getattr(entry, "content", None)
    if isinstance(content, list) and content:
        content_list = cast(list[Any], content)
        first: Any = content_list[0]
        value = _extract_value_field(first)
        if value:
            return value
    else:
        value = _extract_value_field(content)
        if value:
            return value

    summary_detail: Any = getattr(entry, "summary_detail", None)
    if summary_detail:
        stype = _coerce_str(getattr(summary_detail, "type", None))
        if not stype and isinstance(summary_detail, dict):
            summary_mapping = cast(_FeedMapping, summary_detail)
            stype = _coerce_str(summary_mapping.get("type"))
        if stype and "html" in stype:
            value = _extract_value_field(summary_detail)
            if value:
                return value

    summary = _coerce_str(getattr(entry, "summary", None))
    if summary:
        return summary
    description = _coerce_str(getattr(entry, "description", None))
    if description:
        return description
    return ""


def _is_valid_itunes_image_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    return path.endswith((".jpg", ".png"))


def _extract_first_image_url(soup: BeautifulSoup, base_url: str | None = None) -> str:
    """Return the first usable image URL from the soup, or empty string."""

    def normalize(url: str | None) -> str:
        if not url:
            return ""
        url = url.strip()
        if not url or url.lower().startswith("data:"):
            return ""
        return urljoin(base_url or "", url)

    for tag in soup.find_all("img"):
        src_attr = tag.get("src")
        src = src_attr if isinstance(src_attr, str) else ""
        if not src:
            data_src_attr = tag.get("data-src")
            if isinstance(data_src_attr, str):
                src = data_src_attr
        if not src:
            srcset_attr = tag.get("srcset")
            if isinstance(srcset_attr, str) and srcset_attr:
                raw_src = srcset_attr.split(",", 1)[0].strip()
                src = raw_src.split(" ", 1)[0]
        url = normalize(src)
        if url and _is_valid_itunes_image_url(url):
            return url
    return ""


def html_to_text(html_content: str, *, base_url: str | None = None) -> tuple[str, str, str]:
    if not html_content:
        return "", "", ""
    soup = BeautifulSoup(html_content, "lxml")
    lead_image = _extract_first_image_url(soup, base_url)
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
    return "\n".join(filtered).strip(), subtitle, lead_image


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


def resolve_article_content(
    entry: object,
    link: str | None = None,
    *,
    allow_fetch: bool = False,
    min_words: int = 80,
) -> tuple[str, str, str, str]:
    """Return (plain_text, html_content, subtitle, lead_image_url) for the entry.

    When ``allow_fetch`` is True and the feed-provided content is short, we
    attempt to download the full article using ``trafilatura``.
    """

    html_content = _get_entry_content_html(entry)
    plain_text, subtitle, lead_image = html_to_text(html_content, base_url=link or "")
    normalized_html = text_to_html(plain_text)

    if allow_fetch and link and _word_count(plain_text) < max(20, min_words):
        fetched_text = fetch_article_text(link)
        if fetched_text:
            plain_text = fetched_text
            normalized_html = text_to_html(fetched_text)
            subtitle = ""
            if not lead_image:
                # Try to re-extract from fetched content if available via html
                # Trafilatura returns plain text, so leave image empty.
                lead_image = ""

    if not lead_image:
        for item in _coerce_sequence(getattr(entry, "media_content", None)):
            url = _media_url_from_item(item)
            if not url:
                continue
            candidate = urljoin(link or "", url)
            if _is_valid_itunes_image_url(candidate):
                lead_image = candidate
                break
        if not lead_image:
            for item in _coerce_sequence(getattr(entry, "media_thumbnail", None)):
                url = _media_url_from_item(item)
                if not url:
                    continue
                candidate = urljoin(link or "", url)
                if _is_valid_itunes_image_url(candidate):
                    lead_image = candidate
                    break

    if not _is_valid_itunes_image_url(lead_image):
        lead_image = ""

    return plain_text, normalized_html, subtitle, lead_image or ""
