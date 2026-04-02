"""Tests for required Google API setup and preflight checks."""

from __future__ import annotations

import pathlib

from tools.pipeline_config import GitHubConfig, GoogleConfig, PipelineConfig, ScheduleConfig
from tools.preflight import _required_google_services_check
from tools.setup_gcp_oidc_shared import (
    ensure_required_google_services_enabled,
    get_missing_required_google_service_names,
)


def build_pipeline_config() -> PipelineConfig:
    """Build a minimal pipeline config for Google setup helper tests.

    Inputs: none.
    Outputs: deterministic PipelineConfig with shared OIDC settings populated.
    Edge cases: keeps paths and ids explicit so command assertions stay readable.
    """

    return PipelineConfig(
        pipeline_id="geektime-he",
        feed_slug="geektime",
        feed_env_file="configs/geektime.env",
        schedule=ScheduleConfig(
            timezone="Asia/Jerusalem",
            interval_minutes=120,
            window_start="07:00",
            window_end="24:00",
        ),
        github=GitHubConfig(
            workflow_file=".github/workflows/geektime-he.yml",
            branch_ref="refs/heads/main",
            environment_name="geektime-he",
        ),
        google=GoogleConfig(
            project_id="rss-hebrew-podcast-omer",
            project_number="665807615238",
            workload_identity_pool_id="github-actions",
            workload_identity_provider_id="github-provider",
            service_account_id="rss-podcast-geektime-he",
            roles=("roles/serviceusage.serviceUsageConsumer",),
        ),
        failure_email=None,
        config_path=pathlib.Path("/tmp/repo/pipelines/geektime-he.yaml"),
        repo_root=pathlib.Path("/tmp/repo"),
    )


def test_get_missing_required_google_service_names_returns_missing_services(
    monkeypatch,
) -> None:
    """Missing-service helper should report APIs absent from the enabled list.

    Inputs: mocked `gcloud services list` output containing unrelated APIs only.
    Outputs: missing required API names in sorted order.
    Edge cases: ignores blank lines in the mocked command output.
    """

    pipeline_config = build_pipeline_config()

    def fake_run_command(command: list[str], *, cwd: pathlib.Path, check: bool = True) -> str:
        assert command == [
            "gcloud",
            "services",
            "list",
            "--enabled",
            "--project",
            "rss-hebrew-podcast-omer",
            "--format=value(config.name)",
        ]
        assert cwd == pathlib.Path("/tmp/repo")
        assert check is True
        return "iam.googleapis.com\n\ncloudresourcemanager.googleapis.com\n"

    monkeypatch.setattr("tools.setup_gcp_oidc_shared.run_command", fake_run_command)

    missing_service_names = get_missing_required_google_service_names(
        pipeline_config=pipeline_config
    )

    assert missing_service_names == ["iamcredentials.googleapis.com"]


def test_ensure_required_google_services_enabled_enables_missing_services(
    monkeypatch,
) -> None:
    """Setup helper should enable missing APIs before continuing.

    Inputs: mocked service listing without `iamcredentials.googleapis.com`.
    Outputs: one explicit `gcloud services enable` call for the missing API.
    Edge cases: preserves project scoping on both inspect and enable commands.
    """

    pipeline_config = build_pipeline_config()
    observed_commands: list[list[str]] = []

    def fake_run_command(command: list[str], *, cwd: pathlib.Path, check: bool = True) -> str:
        observed_commands.append(command)
        assert cwd == pathlib.Path("/tmp/repo")
        assert check is True
        if command[:3] == ["gcloud", "services", "list"]:
            return "iam.googleapis.com\n"
        if command[:3] == ["gcloud", "services", "enable"]:
            return ""
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("tools.setup_gcp_oidc_shared.run_command", fake_run_command)

    ensure_required_google_services_enabled(pipeline_config=pipeline_config)

    assert observed_commands == [
        [
            "gcloud",
            "services",
            "list",
            "--enabled",
            "--project",
            "rss-hebrew-podcast-omer",
            "--format=value(config.name)",
        ],
        [
            "gcloud",
            "services",
            "enable",
            "iamcredentials.googleapis.com",
            "--project",
            "rss-hebrew-podcast-omer",
        ],
    ]


def test_required_google_services_check_reports_missing_api(monkeypatch) -> None:
    """GitHub preflight should surface the missing IAM Credentials API clearly.

    Inputs: mocked helper response with one missing Google API.
    Outputs: a `MISSING` CheckResult with the setup command as next action.
    Edge cases: keeps the message explicit enough to explain the later TTS failure.
    """

    pipeline_config = build_pipeline_config()
    monkeypatch.setattr(
        "tools.preflight.get_missing_required_google_service_names",
        lambda *, pipeline_config: ["iamcredentials.googleapis.com"],
    )

    check_result = _required_google_services_check(pipeline_config=pipeline_config)

    assert check_result.status == "MISSING"
    assert check_result.detail == (
        "Missing enabled Google APIs: iamcredentials.googleapis.com"
    )
    assert check_result.next_action == (
        "Run `scripts/setup-gcp-oidc-shared.sh --pipeline geektime-he` "
        "to enable the missing APIs."
    )
