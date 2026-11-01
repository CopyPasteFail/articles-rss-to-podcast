"""Helpers for extracting full article content from RSS entries."""

from __future__ import annotations

import html as _html
import re
import string
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

try:  # Optional dependency during unit tests
    import trafilatura
except Exception:  # noqa: BLE001  # pragma: no cover - best effort import
    trafilatura = None


_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_URL_ONLY_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WORDPRESS_FOOTER_RE = re.compile(r"the post .+ appeared first on", re.IGNORECASE)
_PUNCTUATION_TRANSLATION = str.maketrans("", "", string.punctuation)

_FeedMapping = Mapping[str, Any]


def _coerce_str(value: Any) -> str | None:
    """Normalize feedparser values to strings so the content pipeline can rely on them."""
    if isinstance(value, str) and value:
        return value
    return None


def _extract_value_field(data: Any) -> str | None:
    """Grab ``value`` style fields regardless of whether the feed gave dicts or objects."""
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


def _coerce_sequence[_SeqItem](
    value: _SeqItem | list[_SeqItem] | tuple[_SeqItem, ...] | None,
) -> list[_SeqItem]:
    """Turn lone values into lists so image scanning code can iterate safely."""
    if value is None:
        return []
    if isinstance(value, list):
        return cast(list[_SeqItem], value)
    if isinstance(value, tuple):
        tuple_value = cast(tuple[_SeqItem, ...], value)
        return list(tuple_value)
    return [value]


def _media_url_from_item(item: Any) -> str:
    """Extract a URL-ish field from common feed structures (dict/object)."""
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
    """Detect boilerplate \"appeared first on\" endings so we can strip them."""
    if not line:
        return False
    low = line.strip().lower()
    if _WORDPRESS_FOOTER_RE.search(low):
        return True
    if low.startswith("the post"):
        return True
    if low.startswith("appeared first on"):
        return True
    return low in {"גיקטיים", "geektime", "appeared first on", "the post"}


_MEDIA_TAGS = (
    "figure",
    "figcaption",
    "img",
    "picture",
    "video",
    "iframe",
    "embed",
    "object",
    "source",
)
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SUBTITLE_TAGS = ("h2", "h3", "h4")
_BODY_BLOCK_TAGS = ("p", "div", "section", "article", *_HEADING_TAGS)


def _remove_footer_lines(lines: Iterable[str]) -> list[str]:
    """Filter out footer-looking lines from plain text snippets."""
    return [line for line in lines if not _looks_like_footer(line)]


def _normalize_for_title_comparison(text: str) -> str:
    """Normalize text so extracted body text can be compared with an entry title.

    Inputs: a title or candidate first body line.
    Outputs: lower-case text with HTML entities decoded, Unicode forms normalized,
    ASCII punctuation removed, and whitespace collapsed.
    Edge cases: Hebrew punctuation and mirrored quote variants are normalized by
    Unicode category rather than by an English-only punctuation list.
    Atomicity or concurrency guarantees: pure function with no shared state.
    """

    decoded_text = _html.unescape(text)
    normalized_text = unicodedata.normalize("NFKC", decoded_text)
    ascii_without_punctuation = normalized_text.translate(_PUNCTUATION_TRANSLATION)
    without_unicode_punctuation = "".join(
        character
        for character in ascii_without_punctuation
        if not unicodedata.category(character).startswith("P")
    )
    return re.sub(r"\s+", " ", without_unicode_punctuation).strip().casefold()


def _matches_entry_title(candidate_text: str, article_title: str | None) -> bool:
    """Return whether a body fragment is the same title already stored on the entry.

    Inputs: candidate text from the start of the extracted body and the entry title.
    Outputs: True only when both normalize to the same non-empty value.
    Edge cases: whitespace, HTML entities, quote variants, and punctuation-only
    differences are ignored so feeds can vary formatting without duplicating speech.
    Atomicity or concurrency guarantees: pure function with no shared state.
    """

    if not candidate_text or not article_title:
        return False
    normalized_candidate_text = _normalize_for_title_comparison(candidate_text)
    normalized_article_title = _normalize_for_title_comparison(article_title)
    return bool(
        normalized_candidate_text
        and normalized_article_title
        and normalized_candidate_text == normalized_article_title
    )


def _strip_leading_duplicate_title_from_text(
    plain_text: str, article_title: str | None
) -> str:
    """Remove the first body line only when it repeats the entry title.

    Inputs: extracted plain article text and the title supplied by the RSS entry.
    Outputs: the original text unless the first non-empty line matches the title.
    Edge cases: preserves the rest of the first paragraph when the duplicate title
    is followed by subtitle/body lines, and preserves all content when there is no
    title match.
    Atomicity or concurrency guarantees: pure function with no shared state.
    """

    if not plain_text or not article_title:
        return plain_text

    lines = plain_text.splitlines()
    first_content_index = next(
        (line_index for line_index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_content_index is None:
        return plain_text

    if not _matches_entry_title(lines[first_content_index], article_title):
        return plain_text

    remaining_lines = lines[:first_content_index] + lines[first_content_index + 1 :]
    return "\n".join(remaining_lines).strip()


def _strip_leading_duplicate_title_from_html(
    html_content: str, article_title: str | None
) -> str:
    """Remove a leading HTML block only when it duplicates the entry title.

    Inputs: normalized or source HTML for an article body and the entry title.
    Outputs: the original HTML unless its first meaningful block contains only the
    duplicated title.
    Edge cases: intentionally does not edit a paragraph that contains title plus
    additional body text, because that could discard real article content.
    Atomicity or concurrency guarantees: pure function with no shared state.
    """

    if not html_content or not article_title:
        return html_content

    soup = BeautifulSoup(html_content, "lxml")
    body = soup.body or soup
    for element in body.find_all(_BODY_BLOCK_TAGS):
        element_text = element.get_text(" ", strip=True)
        if not element_text:
            continue
        if _matches_entry_title(element_text, article_title):
            element.decompose()
            return str(body.decode_contents()).strip()
        return html_content
    return html_content


def _strip_embedded_media(soup: BeautifulSoup) -> None:
    """Remove media/caption tags that would otherwise clutter the spoken summary."""
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
    """Pull the first subtitle-like heading and drop the rest to keep body clean."""
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
    """Accept only HTTP(S) URLs that end in podcast-friendly image extensions."""
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


def html_to_text(
    html_content: str, *, base_url: str | None = None
) -> tuple[str, str, str]:
    """Convert HTML into (text, subtitle, lead_image) so TTS reads natural prose."""
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
    """Clean fetched article text so fallback paragraphs sound human once spoken."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\n ", "\n", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not _URL_ONLY_RE.fullmatch(line)]
    lines = _remove_footer_lines(lines)
    return "\n\n".join(lines).strip()


def fetch_article_text(url: str) -> str:
    """Best-effort fetch of the live article when feed content is too short."""
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
    except Exception:  # noqa: BLE001
        return ""


def text_to_html(text: str) -> str:
    """Rebuild simple HTML paragraphs from the normalized article text."""
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
    """Tiny helper so we know when to refetch a short entry from the site."""
    if not text:
        return 0
    return len(re.findall(r"\w+", text))


def resolve_article_content(
    entry: object,
    link: str | None = None,
    *,
    allow_fetch: bool = False,
    min_words: int = 80,
    article_title: str | None = None,
) -> tuple[str, str, str, str]:
    """Return (plain_text, html_content, subtitle, lead_image_url) for the entry.

    When ``allow_fetch`` is True and the feed-provided content is short, we
    attempt to download the full article using ``trafilatura``. When
    ``article_title`` is provided, a matching first body line is removed so the
    title is not repeated in generated audio or podcast descriptions.
    """

    html_content = _get_entry_content_html(entry)
    plain_text, subtitle, lead_image = html_to_text(html_content, base_url=link or "")
    if _matches_entry_title(subtitle, article_title):
        subtitle = ""
    plain_text = _strip_leading_duplicate_title_from_text(plain_text, article_title)
    normalized_html = text_to_html(plain_text)
    normalized_html = _strip_leading_duplicate_title_from_html(
        normalized_html, article_title
    )

    if allow_fetch and link and _word_count(plain_text) < max(20, min_words):
        fetched_text = fetch_article_text(link)
        if fetched_text:
            plain_text = _strip_leading_duplicate_title_from_text(
                fetched_text, article_title
            )
            normalized_html = text_to_html(plain_text)
            normalized_html = _strip_leading_duplicate_title_from_html(
                normalized_html, article_title
            )
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
