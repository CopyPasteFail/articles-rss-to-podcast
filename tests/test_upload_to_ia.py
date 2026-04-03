"""Tests for Internet Archive session setup logging."""

from __future__ import annotations

import importlib
import sys
import types
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
