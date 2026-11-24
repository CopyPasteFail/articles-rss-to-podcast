"""Tests for GitHub environment setup helpers."""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest
import yaml

from tools.generate_workflow import generate_workflow_yaml
from tools.pipeline_config import (
    GitHubConfig,
    GoogleConfig,
    PipelineConfig,
    PipelineConfigError,
    ScheduleConfig,
    load_pipeline_config,
)
from tools.setup_github_environment import (
    build_environment_variable_values,
    build_repository_variable_values,
)


def build_pipeline_config(
    *, project_id: str | None = "rss-hebrew-podcast-omer"
) -> PipelineConfig:
    """Build a minimal pipeline config for helper tests.

    Inputs: optional project id to simulate complete or incomplete Google setup.
    Outputs: validated-looking PipelineConfig instance for pure helper tests.
    Edge cases: keeps unrelated values small and explicit to simplify assertions.
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
            project_id=project_id,
            project_number="123456789012",
            workload_identity_pool_id="github-actions",
            workload_identity_provider_id="github-provider",
            service_account_id="rss-podcast-geektime-he",
            roles=("roles/serviceusage.serviceUsageConsumer",),
        ),
        failure_email=None,
        config_path=pathlib.Path("/tmp/repo/pipelines/geektime-he.yaml"),
        repo_root=pathlib.Path("/tmp/repo"),
    )


def test_build_repository_variable_values_returns_expected_mapping() -> None:
    """Repository helper should expose the shared Google values with GitHub names."""

    pipeline_config = build_pipeline_config()

    variable_values = build_repository_variable_values(pipeline_config)

    assert variable_values == {
        "GCP_PROJECT_ID": "rss-hebrew-podcast-omer",
        "GCP_PROJECT_NUMBER": "123456789012",
        "GCP_WIF_POOL_ID": "github-actions",
        "GCP_WIF_PROVIDER_ID": "github-provider",
    }


def test_build_repository_variable_values_rejects_missing_google_settings() -> None:
    """Repository helper should fail clearly when shared Google config is absent."""

    pipeline_config = build_pipeline_config(project_id=None)

    with pytest.raises(RuntimeError, match="missing values for: GCP_PROJECT_ID"):
        build_repository_variable_values(pipeline_config)


def test_build_environment_variable_values_uses_explicit_kv_namespace_id() -> None:
    """Environment helper should reuse local values when they are already present."""

    pipeline_config = build_pipeline_config()

    variable_values = build_environment_variable_values(
        pipeline_config,
        env_values={
            "CLOUDFLARE_ACCOUNT_ID": "cloudflare-account-id",
            "CF_PAGES_PROJECT": "rss-podcast-pages",
            "CF_KV_NAMESPACE_ID": "kv-namespace-id",
            "PODCAST_MAX_RETRY_ATTEMPTS": "5",
        },
    )

    assert variable_values == {
        "GCP_SERVICE_ACCOUNT_EMAIL": (
            "rss-podcast-geektime-he@rss-hebrew-podcast-omer.iam.gserviceaccount.com"
        ),
        "CLOUDFLARE_ACCOUNT_ID": "cloudflare-account-id",
        "CF_PAGES_PROJECT": "rss-podcast-pages",
        "CF_KV_NAMESPACE_ID": "kv-namespace-id",
        "IA_ID_PREFIX": "geektime-v2",
        "PODCAST_MAX_RETRY_ATTEMPTS": "5",
    }


def test_build_environment_variable_values_requires_cloudflare_account_id() -> None:
    """Environment helper should fail clearly when CLOUDFLARE_ACCOUNT_ID is missing."""

    pipeline_config = build_pipeline_config()

    with pytest.raises(RuntimeError, match="CLOUDFLARE_ACCOUNT_ID"):
        build_environment_variable_values(
            pipeline_config,
            env_values={
                "CF_PAGES_PROJECT": "rss-podcast-pages",
                "CF_KV_NAMESPACE_ID": "kv-namespace-id",
            },
        )


def test_build_environment_variable_values_requires_cloudflare_pages_project() -> None:
    """Environment helper should fail clearly when CF_PAGES_PROJECT is missing."""

    pipeline_config = build_pipeline_config()

    with pytest.raises(RuntimeError, match="CF_PAGES_PROJECT"):
        build_environment_variable_values(
            pipeline_config,
            env_values={
                "CLOUDFLARE_ACCOUNT_ID": "cloudflare-account-id",
                "CF_KV_NAMESPACE_ID": "kv-namespace-id",
            },
        )


def test_generate_workflow_yaml_exports_cloudflare_account_id() -> None:
    """Generated workflows should expose CLOUDFLARE_ACCOUNT_ID to the runtime job."""

    workflow_yaml = generate_workflow_yaml(build_pipeline_config())

    assert "CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}" in workflow_yaml


def test_build_environment_variable_values_defaults_retry_attempts() -> None:
    """Environment helper should set a stable default retry limit when unset locally."""

    pipeline_config = build_pipeline_config()

    variable_values = build_environment_variable_values(
        pipeline_config,
        env_values={
            "CLOUDFLARE_ACCOUNT_ID": "cloudflare-account-id",
            "CF_PAGES_PROJECT": "rss-podcast-pages",
            "CF_KV_NAMESPACE_ID": "kv-namespace-id",
        },
    )

    assert variable_values["PODCAST_MAX_RETRY_ATTEMPTS"] == "3"


def test_build_environment_variable_values_defaults_ia_id_prefix() -> None:
    """Environment helper should keep IA identifiers stable when unset locally."""

    pipeline_config = build_pipeline_config()

    variable_values = build_environment_variable_values(
        pipeline_config,
        env_values={
            "CLOUDFLARE_ACCOUNT_ID": "cloudflare-account-id",
            "CF_PAGES_PROJECT": "rss-podcast-pages",
            "CF_KV_NAMESPACE_ID": "kv-namespace-id",
        },
    )

    assert variable_values["IA_ID_PREFIX"] == "geektime-v2"


def test_generate_workflow_yaml_exports_retry_attempt_limit() -> None:
    """Generated workflows should expose the retry limit variable to the runtime job."""

    workflow_yaml = generate_workflow_yaml(build_pipeline_config())

    assert (
        "PODCAST_MAX_RETRY_ATTEMPTS: ${{ vars.PODCAST_MAX_RETRY_ATTEMPTS }}"
        in workflow_yaml
    )


def test_generate_workflow_yaml_exports_ia_id_prefix() -> None:
    """Generated workflows should expose IA_ID_PREFIX to the runtime job."""

    workflow_yaml = generate_workflow_yaml(build_pipeline_config())

    assert "IA_ID_PREFIX: ${{ vars.IA_ID_PREFIX }}" in workflow_yaml


def test_generate_workflow_yaml_installs_ffmpeg() -> None:
    """Generated workflows should install ffmpeg before audio generation starts."""

    workflow_yaml = generate_workflow_yaml(build_pipeline_config())

    assert "Install Audio Dependencies" in workflow_yaml
    assert "command -v ffmpeg" in workflow_yaml
    assert "sudo apt-get install -y --no-install-recommends ffmpeg" in workflow_yaml


def test_container_workflow_uses_pinned_runtime_without_install_steps() -> None:
    config = build_pipeline_config()
    image = "ghcr.io/copypastefail/podcast-runtime@sha256:" + "1" * 64
    config = replace(config, github=replace(config.github, runtime_image=image))
    workflow = yaml.safe_load(generate_workflow_yaml(config))
    job = workflow["jobs"]["run-pipeline"]

    assert job["container"]["image"] == image
    assert job["container"]["credentials"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    assert job["defaults"]["run"]["shell"] == "bash"
    assert job["timeout-minutes"] == 60
    assert workflow["permissions"]["packages"] == "read"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    steps = job["steps"]
    assert any(step.get("uses") == "google-github-actions/auth@v3" for step in steps)
    assert not any("setup-python" in step.get("uses", "") for step in steps)
    assert not any("setup-node" in step.get("uses", "") for step in steps)
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    assert not any(
        command in step.get("run", "")
        for step in steps
        for command in ("pip install", "apt-get", "npm ci")
    )


def test_committed_podcast_workflow_matches_generator() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    config = load_pipeline_config(root, pipeline_id="geektime-he")
    assert (root / config.github.workflow_file).read_text(encoding="utf-8") == (
        generate_workflow_yaml(config)
    )


def test_runtime_workflows_do_not_upload_actions_artifacts() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in ("runtime-image.yml", "geektime-he.yml"):
        workflow = yaml.safe_load((root / ".github/workflows" / name).read_text())
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                action = step.get("uses", "")
                assert not action.startswith("actions/upload-artifact@")
                if action.startswith("docker/build-push-action@"):
                    env = workflow.get("env", {}) | step.get("env", {})
                    assert str(env.get("DOCKER_BUILD_RECORD_UPLOAD")).lower() == "false"
                assert "cache-to: type=gha" not in step.get("run", "")


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/owner/runtime:latest",
        "ghcr.io/owner/runtime@sha256:short",
        "ghcr.io/owner/runtime@sha256:" + "G" * 64,
    ],
)
def test_config_rejects_unpinned_runtime_images(
    tmp_path: pathlib.Path, image: str
) -> None:
    pipeline_dir = tmp_path / "pipelines"
    pipeline_dir.mkdir()
    config = {
        "pipeline_id": "example",
        "feed_slug": "example",
        "feed_env_file": "configs/example.env",
        "schedule": {
            "timezone": "UTC",
            "interval_hours": 2,
            "window_start": "00:00",
            "window_end": "24:00",
        },
        "github": {
            "workflow_file": ".github/workflows/example.yml",
            "branch_ref": "refs/heads/main",
            "runtime_image": image,
        },
    }
    (pipeline_dir / "example.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(PipelineConfigError, match="immutable GHCR image"):
        load_pipeline_config(tmp_path, pipeline_id="example")


def test_generate_workflow_yaml_masks_google_auth_metadata() -> None:
    """Generated workflows should mask Google auth metadata before auth runs."""

    workflow_yaml = generate_workflow_yaml(build_pipeline_config())

    assert "Mask Google auth metadata" in workflow_yaml
    assert 'echo "::add-mask::${{ vars.GCP_SERVICE_ACCOUNT_EMAIL }}"' in workflow_yaml
    assert (
        'echo "::add-mask::projects/${{ vars.GCP_PROJECT_NUMBER }}/locations/global/'
        "workloadIdentityPools/${{ vars.GCP_WIF_POOL_ID }}/providers/${{ "
        'vars.GCP_WIF_PROVIDER_ID }}"'
    ) in workflow_yaml
    assert 'echo "::add-mask::${{ github.workspace }}"' in workflow_yaml
