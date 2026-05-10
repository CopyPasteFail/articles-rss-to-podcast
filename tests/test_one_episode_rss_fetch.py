from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

import one_episode


class FakeResponse:
    def __init__(self, content: bytes = b"<rss><channel></channel></rss>") -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def test_parse_rss_source_fetches_http_with_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    parsed_result = SimpleNamespace(entries=[])

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(b"<rss><channel><title>Feed</title></channel></rss>")

    def fake_parse(payload: bytes) -> object:
        assert payload == b"<rss><channel><title>Feed</title></channel></rss>"
        return parsed_result

    monkeypatch.setattr(one_episode.feedparser, "parse", fake_parse)

    parsed_source = one_episode._parse_rss_source(
        "https://example.com/feed.xml",
        http_get=fake_get,
    )

    assert parsed_source["parsed"] is parsed_result
    assert (
        parsed_source["payload"] == b"<rss><channel><title>Feed</title></channel></rss>"
    )
    assert calls == [
        {
            "url": "https://example.com/feed.xml",
            "headers": {
                "User-Agent": one_episode.RSS_HTTP_USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            },
            "timeout": (
                one_episode.RSS_DEBUG_HTTP_CONNECT_TIMEOUT_S,
                one_episode.RSS_DEBUG_HTTP_READ_TIMEOUT_S,
            ),
        }
    ]


def test_parse_rss_source_timeout_fails_before_feedparser_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        raise requests.Timeout("slow feed")

    def fail_parse(payload: object) -> object:
        raise AssertionError("feedparser.parse should not run after HTTP timeout")

    monkeypatch.setattr(one_episode.feedparser, "parse", fail_parse)

    with pytest.raises(SystemExit, match="RSS fetch timed out before parsing"):
        one_episode._parse_rss_source(
            "https://example.com/feed.xml",
            http_get=fake_get,
        )


def test_select_entry_falls_back_to_wordpress_posts_api_after_rss_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_entry = SimpleNamespace(
        title="Fallback title",
        link="https://example.com/fallback-title",
        id="101",
        author="Example Author",
        summary="Fallback article body",
        description="Fallback article body",
        published_parsed=(2026, 5, 9, 6, 30, 0, 0, 0, 0),
    )

    monkeypatch.setattr(one_episode, "RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setattr(
        one_episode,
        "WORDPRESS_POSTS_API_URL",
        "https://example.com/wp-json/wp/v2/posts",
    )

    def fail_parse(rss_url: str) -> object:
        raise SystemExit("RSS fetch timed out before parsing")

    monkeypatch.setattr(one_episode, "_parse_rss_source", fail_parse)
    monkeypatch.setattr(
        one_episode,
        "_fetch_entries_from_wordpress_posts_api",
        lambda wordpress_posts_api_url: [fallback_entry],
    )
    monkeypatch.setattr(
        one_episode,
        "resolve_article_content",
        lambda entry, link, allow_fetch: (
            entry.summary,
            "<p>Fallback article body</p>",
            "",
            "",
        ),
    )

    selected = one_episode.select_entry()

    assert selected["title"] == "Fallback title"
    assert selected["link"] == "https://example.com/fallback-title"
    assert selected["article_text"] == "Fallback article body"


def test_select_entry_does_not_swallow_rss_timeout_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(one_episode, "RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setattr(one_episode, "WORDPRESS_POSTS_API_URL", "")

    def fail_parse(rss_url: str) -> object:
        raise SystemExit("RSS fetch timed out before parsing")

    monkeypatch.setattr(one_episode, "_parse_rss_source", fail_parse)

    with pytest.raises(SystemExit, match="RSS fetch timed out before parsing"):
        one_episode.select_entry()
