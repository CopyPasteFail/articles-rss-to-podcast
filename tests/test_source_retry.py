"""Source failures should stop early without hiding errors or multiplying retries."""

from __future__ import annotations

import json

import pytest
import requests

import pipeline


def response(status: int, body: bytes = b"", **headers: str) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    result.headers.update(headers)
    result.url = "https://example.com/source"
    return result


@pytest.mark.parametrize("status", [400, 401, 403, 404, 501])
def test_permanent_source_failure_is_not_retried(status: int, monkeypatch) -> None:
    calls: list[str] = []

    def get(url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(status)

    def no_sleep(delay: float) -> None:
        raise AssertionError("Permanent source failures must not sleep")

    monkeypatch.setattr(pipeline.time, "sleep", no_sleep)
    payload, error = pipeline._fetch_rss_payload(
        "https://example.com/feed", http_get=get
    )
    assert payload is None
    assert error is not None and str(status) in error
    assert calls == ["https://example.com/feed"]


@pytest.mark.parametrize("status", [429, 503, 522])
def test_transient_source_failure_recovers_on_one_retry(
    status: int, monkeypatch
) -> None:
    replies = iter([response(status, **{"Retry-After": "2"}), response(200, b"<rss/>")])
    sleeps: list[float] = []
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    payload, error = pipeline._fetch_rss_payload(
        "https://example.com/feed", http_get=lambda *args, **kwargs: next(replies)
    )
    assert (payload, error) == (b"<rss/>", None)
    assert sleeps == [2.0]


def test_source_timeout_is_tried_twice_at_most(monkeypatch) -> None:
    calls: list[object] = []
    sleeps: list[float] = []

    def get(url: str, **kwargs: object) -> requests.Response:
        calls.append(kwargs["timeout"])
        raise requests.Timeout("slow origin")

    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    payload, error = pipeline._fetch_rss_payload(
        "https://example.com/feed", http_get=get
    )
    assert payload is None
    assert error is not None and "timed out" in error
    assert calls == [(5.0, 15.0), (5.0, 15.0)]
    assert sleeps == [1.0]


@pytest.mark.parametrize("retry_after", ["120", "Tue, 24 Nov 2026 22:02:00 GMT"])
def test_long_retry_after_is_not_shortened_into_an_early_retry(
    retry_after: str, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline.time, "time", lambda: 1795557600.0)
    calls: list[str] = []

    def get(url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(429, **{"Retry-After": retry_after})

    pipeline._fetch_rss_payload("https://example.com/feed", http_get=get)
    assert calls == ["https://example.com/feed"]


def test_both_sources_blocked_fail_before_state_access(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def get(url: str, **kwargs: object) -> requests.Response:
        calls.append(url)
        return response(403)

    def fail_state_access() -> None:
        raise AssertionError("State must not be accessed when sources are unavailable")

    monkeypatch.setattr(pipeline, "RSS_URL", "https://example.com/feed")
    monkeypatch.setattr(
        pipeline, "WORDPRESS_POSTS_API_URL", "https://example.com/posts"
    )
    monkeypatch.setattr(pipeline.requests, "get", get)
    monkeypatch.setattr(pipeline, "ensure_kv_namespace_id", fail_state_access)
    with pytest.raises(SystemExit, match="fallback.*403"):
        pipeline.main()
    assert calls == [
        "https://example.com/feed",
        "https://example.com/posts?_embed=1&per_page=20",
    ]
    output = capsys.readouterr().out
    assert "source RSS feed 'https://example.com/feed': HTTP 403" in output


@pytest.mark.parametrize("rss_body", [b"<html>Blocked</html>", b"invalid feed"])
def test_invalid_rss_uses_wordpress_fallback(rss_body: bytes, monkeypatch) -> None:
    post = {
        "title": {"rendered": "Test article"},
        "content": {"rendered": "<p>Full text</p>"},
        "link": "https://example.com/article",
        "date_gmt": "2026-11-24T12:00:00",
    }
    replies = iter(
        [response(200, rss_body), response(200, json.dumps([post]).encode())]
    )
    monkeypatch.setattr(pipeline, "RSS_URL", "https://example.com/feed")
    monkeypatch.setattr(
        pipeline, "WORDPRESS_POSTS_API_URL", "https://example.com/posts"
    )
    monkeypatch.setattr(pipeline.requests, "get", lambda *a, **kw: next(replies))
    assert pipeline.fetch_entries_from_rss()[0]["article_link"] == post["link"]
