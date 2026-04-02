"""Tests for pipeline retry exhaustion behavior."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import types

import pytest

content_utils_stub = types.ModuleType("content_utils")
content_utils_stub.resolve_article_content = lambda *args, **kwargs: ("", "", "", "")
content_utils_stub.text_to_html = lambda text: text
sys.modules.setdefault("content_utils", content_utils_stub)

pipeline = __import__("pipeline")


def test_should_skip_failed_entry_only_when_retry_is_exhausted() -> None:
    """Scheduled runs should only skip entries after retry exhaustion.

    Inputs: entry states for non-exhausted and exhausted failures.
    Outputs: False for active retries and True once retries are exhausted.
    Edge cases: manual retry mode remains covered by the helper itself.
    """

    assert (
        pipeline._should_skip_failed_entry(
            {
                pipeline.FAILED_ENTRY_PUB_UTC_KEY: "2026-04-03T00:00:00+00:00",
                pipeline.FAILED_ENTRY_ATTEMPT_COUNT_KEY: 2,
                pipeline.FAILED_ENTRY_MAX_ATTEMPTS_KEY: 3,
                pipeline.FAILED_ENTRY_RETRY_EXHAUSTED_KEY: False,
            },
            "2026-04-03T00:00:00+00:00",
            False,
        )
        is False
    )
    assert (
        pipeline._should_skip_failed_entry(
            {
                pipeline.FAILED_ENTRY_PUB_UTC_KEY: "2026-04-03T00:00:00+00:00",
                pipeline.FAILED_ENTRY_ATTEMPT_COUNT_KEY: 3,
                pipeline.FAILED_ENTRY_MAX_ATTEMPTS_KEY: 3,
                pipeline.FAILED_ENTRY_RETRY_EXHAUSTED_KEY: True,
            },
            "2026-04-03T00:00:00+00:00",
            False,
        )
        is True
    )


def test_main_fails_when_entry_reaches_retry_limit(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    """The run that exhausts retries should fail the workflow.

    Inputs: one entry already failed twice with max retries set to three.
    Outputs: SystemExit after the third failed attempt is recorded.
    Edge cases: persists state through mocked KV writes without requiring external services.
    """

    credentials_path = tmp_path / "gcp.json"
    credentials_path.write_text("{}", encoding="utf-8")
    state: dict[str, object] = {
        "items": {
            "tts-default-abc123": {
                pipeline.FAILED_ENTRY_PUB_UTC_KEY: "2026-04-03T10:00:00+00:00",
                pipeline.FAILED_ENTRY_ATTEMPT_COUNT_KEY: 2,
                pipeline.FAILED_ENTRY_MAX_ATTEMPTS_KEY: 3,
                pipeline.FAILED_ENTRY_RETRY_EXHAUSTED_KEY: False,
            }
        },
        "usage": {"cumulative_characters": 0},
        "pending_deploy": False,
    }

    monkeypatch.setenv("RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))
    monkeypatch.setenv("PODCAST_MAX_RETRY_ATTEMPTS", "3")
    monkeypatch.setattr(pipeline, "RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setattr(pipeline, "ensure_kv_namespace_id", lambda: None)
    monkeypatch.setattr(pipeline, "kv_get", lambda key: state)
    monkeypatch.setattr(pipeline, "kv_put", lambda key, data: True)
    monkeypatch.setattr(pipeline, "kv_put_or_die", lambda key, data: None)
    monkeypatch.setattr(pipeline, "ia_has_episode_http", lambda identifier: False)
    monkeypatch.setattr(
        pipeline,
        "ia_identifier_for_link",
        lambda link: "tts-default-abc123",
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_entries_from_rss",
        lambda: [
            {
                "article_title": "Example",
                "article_link": "https://example.com/article",
                "article_summary": "Summary",
                "article_summary_html": "<p>Summary</p>",
                "article_subtitle": "",
                "article_pub_utc": "2026-04-03T10:00:00+00:00",
                "article_image_url": "",
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "_validate_google_credentials_access",
        lambda: None,
    )
    monkeypatch.setattr(
        pipeline.shutil,
        "which",
        lambda command_name: f"/usr/bin/{command_name}",
    )

    def fake_sh(*args: object, env=None, cwd=None) -> str:
        raise subprocess.CalledProcessError(1, [str(arg) for arg in args], "tts failed")

    monkeypatch.setattr(pipeline, "sh", fake_sh)

    try:
        pipeline.main()
    except SystemExit as exc:
        assert str(exc) == (
            "Retry limit reached for 1 entry; manual retry required for exhausted failures."
        )
    else:
        raise AssertionError("Expected SystemExit when retries are exhausted")


def test_main_skips_retry_exhausted_entry_on_scheduled_run(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    """Scheduled runs should skip exhausted entries and not fail repeatedly.

    Inputs: one entry already marked retry exhausted for the same publication timestamp.
    Outputs: normal return from main without reattempting synthesis.
    Edge cases: confirms exhausted entries are not counted as attempted work.
    """

    credentials_path = tmp_path / "gcp.json"
    credentials_path.write_text("{}", encoding="utf-8")
    state: dict[str, object] = {
        "items": {
            "tts-default-abc123": {
                pipeline.FAILED_ENTRY_PUB_UTC_KEY: "2026-04-03T10:00:00+00:00",
                pipeline.FAILED_ENTRY_ATTEMPT_COUNT_KEY: 3,
                pipeline.FAILED_ENTRY_MAX_ATTEMPTS_KEY: 3,
                pipeline.FAILED_ENTRY_RETRY_EXHAUSTED_KEY: True,
                pipeline.FAILED_ENTRY_AT_UTC_KEY: "2026-04-03T12:00:00+00:00",
                pipeline.FAILED_ENTRY_STEP_KEY: pipeline.FAILURE_STEP_GENERATE_AUDIO,
            }
        },
        "usage": {"cumulative_characters": 0},
        "pending_deploy": False,
    }

    monkeypatch.setenv("RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))
    monkeypatch.delenv("PODCAST_RETRY_FAILED", raising=False)
    monkeypatch.setattr(pipeline, "RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setattr(pipeline, "ensure_kv_namespace_id", lambda: None)
    monkeypatch.setattr(pipeline, "kv_get", lambda key: state)
    monkeypatch.setattr(pipeline, "kv_put_or_die", lambda key, data: None)
    monkeypatch.setattr(pipeline, "ia_has_episode_http", lambda identifier: False)
    monkeypatch.setattr(
        pipeline,
        "ia_identifier_for_link",
        lambda link: "tts-default-abc123",
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_entries_from_rss",
        lambda: [
            {
                "article_title": "Example",
                "article_link": "https://example.com/article",
                "article_summary": "Summary",
                "article_summary_html": "<p>Summary</p>",
                "article_subtitle": "",
                "article_pub_utc": "2026-04-03T10:00:00+00:00",
                "article_image_url": "",
            }
        ],
    )

    def fail_if_called(*args: object, env=None, cwd=None) -> str:
        raise AssertionError("Exhausted entry should not be retried on scheduled runs")

    monkeypatch.setattr(pipeline, "sh", fail_if_called)

    pipeline.main()


def test_main_fails_fast_when_ffprobe_is_missing(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    """Missing local audio tooling should fail the run before entry retries begin.

    Inputs: one pending entry plus a PATH lookup that lacks ffprobe.
    Outputs: SystemExit describing the missing binary.
    Edge cases: leaves retry bookkeeping untouched because the environment is not ready.
    """

    credentials_path = tmp_path / "gcp.json"
    credentials_path.write_text("{}", encoding="utf-8")
    state: dict[str, object] = {
        "items": {},
        "usage": {"cumulative_characters": 0},
        "pending_deploy": False,
    }

    monkeypatch.setenv("RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))
    monkeypatch.setattr(pipeline, "RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setattr(pipeline, "ensure_kv_namespace_id", lambda: None)
    monkeypatch.setattr(pipeline, "kv_get", lambda key: state)
    monkeypatch.setattr(pipeline, "ia_has_episode_http", lambda identifier: False)
    monkeypatch.setattr(
        pipeline,
        "fetch_entries_from_rss",
        lambda: [
            {
                "article_title": "Example",
                "article_link": "https://example.com/article",
                "article_summary": "Summary",
                "article_summary_html": "<p>Summary</p>",
                "article_subtitle": "",
                "article_pub_utc": "2026-04-03T10:00:00+00:00",
                "article_image_url": "",
            }
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "_validate_google_credentials_access",
        lambda: None,
    )
    monkeypatch.setattr(
        pipeline.shutil,
        "which",
        lambda command_name: (
            f"/usr/bin/{command_name}" if command_name == "ffmpeg" else None
        ),
    )

    with pytest.raises(
        SystemExit,
        match="missing required system binary 'ffprobe' on PATH",
    ):
        pipeline.main()


def test_main_fails_fast_when_google_credentials_are_unusable(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    """Broken Google auth should fail as a sanity error instead of an entry retry.

    Inputs: one pending entry plus a mocked credential refresh failure.
    Outputs: SystemExit describing the credential sanity failure.
    Edge cases: keeps the test local by mocking the credential refresh helper directly.
    """

    credentials_path = tmp_path / "gcp.json"
    credentials_path.write_text("{}", encoding="utf-8")
    state: dict[str, object] = {
        "items": {},
        "usage": {"cumulative_characters": 0},
        "pending_deploy": False,
    }

    monkeypatch.setenv("RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))
    monkeypatch.setattr(pipeline, "RSS_URL", "https://example.com/feed.xml")
    monkeypatch.setattr(pipeline, "ensure_kv_namespace_id", lambda: None)
    monkeypatch.setattr(pipeline, "kv_get", lambda key: state)
    monkeypatch.setattr(pipeline, "ia_has_episode_http", lambda identifier: False)
    monkeypatch.setattr(
        pipeline,
        "fetch_entries_from_rss",
        lambda: [
            {
                "article_title": "Example",
                "article_link": "https://example.com/article",
                "article_summary": "Summary",
                "article_summary_html": "<p>Summary</p>",
                "article_subtitle": "",
                "article_pub_utc": "2026-04-03T10:00:00+00:00",
                "article_image_url": "",
            }
        ],
    )
    monkeypatch.setattr(
        pipeline.shutil,
        "which",
        lambda command_name: f"/usr/bin/{command_name}",
    )

    def fail_google_credentials_refresh() -> None:
        raise RuntimeError("Unable to acquire impersonated credentials")

    monkeypatch.setattr(
        pipeline,
        "_validate_google_credentials_access",
        fail_google_credentials_refresh,
    )

    with pytest.raises(
        SystemExit,
        match="Google credential sanity check failed: Unable to acquire impersonated credentials",
    ):
        pipeline.main()
