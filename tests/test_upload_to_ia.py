"""Tests for Internet Archive session setup logging."""

from __future__ import annotations

import hashlib
import importlib
import pathlib
import sys
import types

import requests
from typing import Any

import pytest


@pytest.fixture
def upload_to_ia_module() -> Any:
    """Import upload_to_ia with a stubbed internetarchive dependency.

    Inputs: none.
    Outputs: imported upload_to_ia module object.
    Edge cases: removes any previous upload_to_ia import so each test gets the
    stubbed dependency even when tests are re-run in the same interpreter.
    """

    sys.modules.pop("upload_to_ia", None)
    sys.modules["internetarchive"] = types.SimpleNamespace(get_session=lambda **_: None)
    return importlib.import_module("upload_to_ia")


def test_get_ia_session_uses_env_credentials_without_logging_source(
    monkeypatch: Any, capsys: Any, upload_to_ia_module: Any
) -> None:
    """IA session setup should use env credentials without printing source details.

    Inputs: monkeypatched env vars and get_session wrapper.
    Outputs: None. Asserts the session configuration and stdout contents.
    Edge cases: verifies IA_CONFIG_FILE is cleared before session creation.
    """

    recorded_call: dict[str, object] = {}
    expected_session = object()

    def fake_get_session(
        config: dict[str, object] | None = None,
        config_file: str | None = None,
        debug: bool = False,
        http_adapter_kwargs: dict[str, object] | None = None,
    ) -> object:
        recorded_call["config"] = config
        recorded_call["config_file"] = config_file
        recorded_call["debug"] = debug
        recorded_call["http_adapter_kwargs"] = http_adapter_kwargs
        return expected_session

    monkeypatch.setattr(upload_to_ia_module, "get_session", fake_get_session)
    monkeypatch.setenv("IA_ACCESS_KEY", "test-access-key")
    monkeypatch.setenv("IA_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("IA_CONFIG_FILE", "/tmp/ia.ini")

    actual_session = upload_to_ia_module.get_ia_session()

    assert actual_session is expected_session
    assert recorded_call["config"] == {
        "s3": {"access": "test-access-key", "secret": "test-secret-key"}
    }
    assert recorded_call["config_file"] == ""
    assert "env vars" not in capsys.readouterr().out


def test_get_ia_session_falls_back_without_logging_local_config_source(
    monkeypatch: Any, capsys: Any, upload_to_ia_module: Any
) -> None:
    """IA session setup should fall back cleanly without naming the config source.

    Inputs: monkeypatched empty env and get_session wrapper.
    Outputs: None. Asserts the fallback call shape and stdout contents.
    Edge cases: verifies previous IA_CONFIG_FILE values are removed first.
    """

    recorded_call: dict[str, object] = {}
    expected_session = object()

    def fake_get_session(
        config: dict[str, object] | None = None,
        config_file: str | None = None,
        debug: bool = False,
        http_adapter_kwargs: dict[str, object] | None = None,
    ) -> object:
        recorded_call["config"] = config
        recorded_call["config_file"] = config_file
        recorded_call["debug"] = debug
        recorded_call["http_adapter_kwargs"] = http_adapter_kwargs
        return expected_session

    monkeypatch.setattr(upload_to_ia_module, "get_session", fake_get_session)
    monkeypatch.delenv("IA_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IA_SECRET_KEY", raising=False)
    monkeypatch.setenv("IA_CONFIG_FILE", "/tmp/ia.ini")

    actual_session = upload_to_ia_module.get_ia_session()

    assert actual_session is expected_session
    assert recorded_call["config"] is None
    assert recorded_call["config_file"] == ""
    output = capsys.readouterr().out
    assert "Initializing Internet Archive session" in output
    assert "default IA config" not in output


def test_upload_timeout_configuration(monkeypatch: Any, upload_to_ia_module: Any) -> None:
    monkeypatch.delenv("IA_UPLOAD_TIMEOUT", raising=False)
    assert upload_to_ia_module.ia_upload_request_kwargs()["timeout"] == (15.0, 120.0)

    monkeypatch.setenv("IA_UPLOAD_TIMEOUT", "45")
    assert upload_to_ia_module.ia_upload_request_kwargs()["timeout"] == (15.0, 45.0)

    monkeypatch.setenv("IA_UPLOAD_TIMEOUT", "invalid")
    assert upload_to_ia_module.ia_upload_request_kwargs()["timeout"] == (15.0, 120.0)


def test_timeout_with_matching_remote_checksum_is_success(
    tmp_path: pathlib.Path, upload_to_ia_module: Any
) -> None:
    mp3 = tmp_path / "episode.mp3"
    mp3.write_bytes(b"test audio")
    md5 = hashlib.md5(mp3.read_bytes(), usedforsecurity=False).hexdigest()
    remote_file = types.SimpleNamespace(name="episode.mp3", md5=md5)
    remote_item = types.SimpleNamespace(get_files=lambda: [remote_file])
    session = types.SimpleNamespace(
        get_item=lambda identifier, request_kwargs: remote_item
    )
    attempts = []

    def upload(*args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        raise requests.exceptions.ReadTimeout("upload response lost")

    kwargs = {"timeout": (15.0, 120.0)}
    item = types.SimpleNamespace(upload=upload)
    responses = upload_to_ia_module.upload_with_retries(
        item, {"episode.mp3": str(mp3)}, metadata={}, max_attempts=3,
        request_kwargs=kwargs,
        confirm_upload=lambda: upload_to_ia_module.remote_upload_matches(
            session, "article-id", "episode.mp3", mp3, request_kwargs=kwargs
        ),
    )

    assert len(attempts) == 1
    assert all(response.ok for response in responses)


def test_timeout_with_mismatched_remote_checksum_retries(
    monkeypatch: Any, tmp_path: pathlib.Path, upload_to_ia_module: Any
) -> None:
    mp3 = tmp_path / "episode.mp3"
    mp3.write_bytes(b"new audio")
    remote_file = types.SimpleNamespace(name="episode.mp3", md5="0" * 32)
    remote_item = types.SimpleNamespace(get_files=lambda: [remote_file])
    session = types.SimpleNamespace(
        get_item=lambda identifier, request_kwargs: remote_item
    )
    attempts = []
    success = types.SimpleNamespace(ok=True)

    def upload(*args: Any, **kwargs: Any) -> list[Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.exceptions.ReadTimeout("upload response lost")
        return [success]

    monkeypatch.setattr(upload_to_ia_module, "wait_with_progress", lambda _: None)
    monkeypatch.setattr(upload_to_ia_module, "retry_delay", lambda *_: 0.0)
    kwargs = {"timeout": (15.0, 120.0)}
    responses = upload_to_ia_module.upload_with_retries(
        types.SimpleNamespace(upload=upload),
        {"episode.mp3": str(mp3)}, metadata={}, max_attempts=3,
        request_kwargs=kwargs,
        confirm_upload=lambda: upload_to_ia_module.remote_upload_matches(
            session, "article-id", "episode.mp3", mp3, request_kwargs=kwargs
        ),
    )

    assert len(attempts) == 2
    assert responses == [success]


def test_failed_metadata_check_does_not_hide_upload_timeout(
    monkeypatch: Any, tmp_path: pathlib.Path, upload_to_ia_module: Any
) -> None:
    mp3 = tmp_path / "episode.mp3"
    mp3.write_bytes(b"new audio")
    checks = []

    def get_item(identifier: str, request_kwargs: object) -> None:
        checks.append(1)
        raise requests.exceptions.ReadTimeout("metadata unavailable")

    def upload(*args: Any, **kwargs: Any) -> None:
        raise requests.exceptions.ReadTimeout("upload unavailable")

    monkeypatch.setattr(upload_to_ia_module, "wait_with_progress", lambda _: None)
    monkeypatch.setattr(upload_to_ia_module, "retry_delay", lambda *_: 0.0)
    session = types.SimpleNamespace(get_item=get_item)
    kwargs = {"timeout": (15.0, 120.0)}

    with pytest.raises(requests.exceptions.ReadTimeout, match="upload unavailable"):
        upload_to_ia_module.upload_with_retries(
            types.SimpleNamespace(upload=upload),
            {"episode.mp3": str(mp3)}, metadata={}, max_attempts=2,
            request_kwargs=kwargs,
            confirm_upload=lambda: upload_to_ia_module.remote_upload_matches(
                session, "article-id", "episode.mp3", mp3, request_kwargs=kwargs
            ),
        )

    assert len(checks) == 2
