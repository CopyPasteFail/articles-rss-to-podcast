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
