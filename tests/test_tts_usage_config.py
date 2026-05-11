from __future__ import annotations

import importlib
import sys
import types

import pytest


class FakeBigQueryClient:
    def __init__(self, project=None):
        self.project = project

    def close(self):
        return None


def import_tts_usage(monkeypatch):
    fake_bigquery = types.SimpleNamespace(Client=FakeBigQueryClient)
    fake_table = types.SimpleNamespace(Row=dict)
    fake_google_cloud = types.SimpleNamespace(bigquery=fake_bigquery)
    fake_google = types.SimpleNamespace(cloud=fake_google_cloud)

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery.table", fake_table)
    sys.modules.pop("tts_usage", None)
    return importlib.import_module("tts_usage")


def test_tts_usage_requires_billing_export_table(monkeypatch):
    monkeypatch.delenv("BILLING_EXPORT_TABLE", raising=False)
    module = import_tts_usage(monkeypatch)

    with pytest.raises(RuntimeError, match="BILLING_EXPORT_TABLE is not set"):
        module._render_sql()


def test_tts_usage_uses_configured_billing_project(monkeypatch):
    monkeypatch.setenv("BILLING_EXPORT_TABLE", "billing-project.dataset.table")
    monkeypatch.setenv("TTS_BILLING_PROJECT_ID", "billing-runner-project")
    module = import_tts_usage(monkeypatch)

    assert module._billing_project_id() == "billing-runner-project"
    assert "`billing-project.dataset.table`" in module._render_sql()
