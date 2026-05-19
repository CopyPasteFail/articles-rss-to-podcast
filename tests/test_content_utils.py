"""Tests for article content extraction and duplicate title cleanup."""

from __future__ import annotations

from types import SimpleNamespace

from content_utils import resolve_article_content


def build_entry(html_content: str) -> SimpleNamespace:
    """Return a minimal feedparser-like entry with rich HTML content.

    Inputs: HTML content that would normally come from ``entry.content[0].value``.
    Outputs: a namespace matching the subset consumed by ``resolve_article_content``.
    Edge cases: keeps the test independent of feedparser's concrete data types.
    Atomicity or concurrency guarantees: constructs only in-memory objects.
    """

    return SimpleNamespace(content=[{"value": html_content}])


def test_resolve_article_content_removes_leading_duplicate_title() -> None:
    """Remove a first body paragraph that repeats the article title.

    Inputs: HTML content shaped like recent Geektime entries where the title is
    emitted as the first paragraph of the body.
    Outputs: plain text and HTML that start with the subtitle/body, not the title.
    Edge cases: verifies both sidecar text and RSS-description HTML inputs.
    Atomicity or concurrency guarantees: pure local transformation.
    """

    article_title = "איך אילון מאסק הפסיד בשעתיים את קרב חייו?"
    entry = build_entry(
        "<p>איך אילון מאסק הפסיד בשעתיים את קרב חייו?</p>"
        "<p>חבר המושבעים דחה את הטענות של מאסק</p>"
        "<p>אתמול היה סוג של רכבת הרים.</p>"
    )

    plain_text, html_content, subtitle, lead_image_url = resolve_article_content(
        entry,
        "https://www.geektime.co.il/example/",
        article_title=article_title,
    )

    assert plain_text == (
        "חבר המושבעים דחה את הטענות של מאסק\nאתמול היה סוג של רכבת הרים."
    )
    assert article_title not in html_content
    assert "חבר המושבעים דחה את הטענות של מאסק" in html_content
    assert subtitle == ""
    assert lead_image_url == ""


def test_resolve_article_content_keeps_distinct_leading_summary() -> None:
    """Keep a first body paragraph when it is not the article title.

    Inputs: HTML content whose first paragraph is a real dek/summary.
    Outputs: unchanged text beginning with that summary.
    Edge cases: prevents over-eager cleanup from deleting legitimate content.
    Atomicity or concurrency guarantees: pure local transformation.
    """

    article_title = "כותרת הכתבה"
    leading_summary = "זו פתיחה אמיתית שאינה חוזרת על הכותרת"
    entry = build_entry(f"<p>{leading_summary}</p><p>גוף הכתבה ממשיך כאן.</p>")

    plain_text, html_content, _subtitle, _lead_image_url = resolve_article_content(
        entry,
        "https://www.geektime.co.il/example/",
        article_title=article_title,
    )

    assert plain_text.startswith(leading_summary)
    assert leading_summary in html_content


def test_resolve_article_content_matches_title_with_punctuation_variants() -> None:
    """Treat punctuation-only title formatting differences as duplicates.

    Inputs: an entry title with a question mark and body text with the same words
    but no final punctuation.
    Outputs: body text with the duplicated first line removed.
    Edge cases: mirrors feeds that vary punctuation between title and content.
    Atomicity or concurrency guarantees: pure local transformation.
    """

    article_title = "יש תאריך ל-WWDC 2026?"
    entry = build_entry(
        "<p>יש תאריך ל-WWDC 2026</p><p>אפל הודיעה על תאריכי הכנס.</p>"
    )

    plain_text, html_content, _subtitle, _lead_image_url = resolve_article_content(
        entry,
        "https://www.geektime.co.il/example/",
        article_title=article_title,
    )

    assert plain_text == "אפל הודיעה על תאריכי הכנס."
    assert "WWDC" not in html_content


def test_resolve_article_content_drops_duplicate_title_subtitle() -> None:
    """Remove a subtitle heading when it repeats the article title.

    Inputs: HTML where the first subtitle-like heading duplicates the entry title.
    Outputs: plain body text with an empty subtitle field.
    Edge cases: avoids a title -> subtitle repeat in generated SSML and RSS text.
    Atomicity or concurrency guarantees: pure local transformation.
    """

    article_title = "כותרת שחוזרת ככותרת משנה"
    entry = build_entry(
        "<h2>כותרת שחוזרת ככותרת משנה</h2><p>גוף הכתבה מתחיל כאן.</p>"
    )

    plain_text, _html_content, subtitle, _lead_image_url = resolve_article_content(
        entry,
        "https://www.geektime.co.il/example/",
        article_title=article_title,
    )

    assert plain_text == "גוף הכתבה מתחיל כאן."
    assert subtitle == ""
