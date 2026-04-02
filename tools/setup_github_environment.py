"""Reconcile GitHub Actions repository and environment variables for one pipeline."""

from __future__ import annotations

import argparse
import sys
from urllib.parse import quote

from tools.check_gh_auth import check_github_cli_authentication
from tools.command_utils import (
    detect_repo_context,
    load_root_env_values,
    resolve_cloudflare_kv_namespace_id,
    run_command,
)
from tools.pipeline_config import (
    PipelineConfig,
    PipelineConfigError,
    load_pipeline_config,
)


def build_repository_variable_values(
    pipeline_config: PipelineConfig,
) -> dict[str, str]:
    """Return the shared GitHub repository variables for one pipeline.

    Inputs: validated pipeline config with local shared Google settings loaded.
    Outputs: mapping of repository variable names to their public-safe values.
    Edge cases: raises a readable error when local shared Google config is missing.
    """

    google_config = pipeline_config.google
    variable_values = {
        "GCP_PROJECT_ID": google_config.project_id or "",
        "GCP_PROJECT_NUMBER": google_config.project_number or "",
        "GCP_WIF_POOL_ID": google_config.workload_identity_pool_id or "",
        "GCP_WIF_PROVIDER_ID": google_config.workload_identity_provider_id or "",
    }
    missing_variable_names = [
        variable_name
        for variable_name, variable_value in variable_values.items()
        if not variable_value.strip()
    ]
    if missing_variable_names:
        missing_text = ", ".join(missing_variable_names)
        raise RuntimeError(
            "Cannot configure shared GitHub repository variables because local shared "
            f"Google config is missing values for: {missing_text}. Create "
            "pipelines/shared.yaml from pipelines/shared.example.yaml or provide the "
            "values through a local overlay."
        )
    return variable_values


def build_environment_variable_values(
    pipeline_config: PipelineConfig,
    *,
    env_values: dict[str, str],
) -> dict[str, str]:
    """Return the GitHub environment variables for one pipeline.

    Inputs: validated pipeline config and merged local environment values.
    Outputs: mapping of environment variable names to values for the pipeline.
    Edge cases: derives CF_KV_NAMESPACE_ID from Cloudflare when not set explicitly.
    """

    try:
        service_account_email = pipeline_config.google.service_account_email
    except PipelineConfigError as exc:
        raise RuntimeError(
            "Cannot configure GCP_SERVICE_ACCOUNT_EMAIL for GitHub because local "
            "Google setup is incomplete."
        ) from exc

    cloudflare_account_id = (env_values.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    if not cloudflare_account_id:
        raise RuntimeError(
            "Cannot configure GitHub environment variable CLOUDFLARE_ACCOUNT_ID because "
            "it is missing from .env or the current environment."
        )

    cloudflare_pages_project = (env_values.get("CF_PAGES_PROJECT") or "").strip()
    if not cloudflare_pages_project:
        raise RuntimeError(
            "Cannot configure GitHub environment variable CF_PAGES_PROJECT because "
            "it is missing from .env or the current environment."
        )

    kv_namespace_id = resolve_cloudflare_kv_namespace_id(env_values=env_values) or ""
    if not kv_namespace_id:
        raise RuntimeError(
            "Cannot configure GitHub environment variable CF_KV_NAMESPACE_ID because "
            "it is not set and could not be discovered from Cloudflare. Set "
            "CF_KV_NAMESPACE_ID directly or provide CLOUDFLARE_ACCOUNT_ID, "
            "CLOUDFLARE_API_TOKEN, and an optional CF_KV_NAMESPACE_NAME."
        )

    return {
        "GCP_SERVICE_ACCOUNT_EMAIL": service_account_email,
        "CLOUDFLARE_ACCOUNT_ID": cloudflare_account_id,
        "CF_PAGES_PROJECT": cloudflare_pages_project,
        "CF_KV_NAMESPACE_ID": kv_namespace_id,
    }


def ensure_github_environment(
    *,
    repository_name_with_owner: str,
    pipeline_config: PipelineConfig,
    dry_run: bool,
) -> None:
    """Create or reuse the GitHub deployment environment for one pipeline.

    Inputs: repository name, pipeline config, and dry-run flag.
    Outputs: none. Prints the action taken for operator visibility.
    Edge cases: safe to repeat because the GitHub environment API uses idempotent PUT.
    """

    environment_name = pipeline_config.github.environment_name
    api_path = (
        f"repos/{repository_name_with_owner}/environments/"
        f"{quote(environment_name, safe='')}"
    )
    if dry_run:
        print(f"DRY RUN: would ensure GitHub environment {environment_name}")
        return

    run_command(
        ["gh", "api", "--method", "PUT", api_path], cwd=pipeline_config.repo_root
    )
    print(f"PASS: Ensured GitHub environment {environment_name}")


def ensure_repository_variable(
    *,
    pipeline_config: PipelineConfig,
    variable_name: str,
    variable_value: str,
    dry_run: bool,
) -> None:
    """Set one shared GitHub repository variable for GitHub Actions.

    Inputs: pipeline config, variable name, value, and dry-run flag.
    Outputs: none. Prints the action taken for operator visibility.
    Edge cases: safe to repeat because `gh variable set` overwrites in place.
    """

    if dry_run:
        print(f"DRY RUN: would ensure repository variable {variable_name}")
        return

    run_command(
        ["gh", "variable", "set", variable_name, "--body", variable_value],
        cwd=pipeline_config.repo_root,
    )
    print(f"PASS: Ensured repository variable {variable_name}")


def ensure_environment_variable(
    *,
    pipeline_config: PipelineConfig,
    variable_name: str,
    variable_value: str,
    dry_run: bool,
) -> None:
    """Set one GitHub deployment environment variable for the selected pipeline.

    Inputs: pipeline config, variable name, value, and dry-run flag.
    Outputs: none. Prints the action taken for operator visibility.
    Edge cases: assumes the environment already exists and safely overwrites drift.
    """

    environment_name = pipeline_config.github.environment_name
    if dry_run:
        print(
            "DRY RUN: would ensure GitHub environment variable "
            f"{variable_name} in {environment_name}"
        )
        return

    run_command(
        [
            "gh",
            "variable",
            "set",
            variable_name,
            "--env",
            environment_name,
            "--body",
            variable_value,
        ],
        cwd=pipeline_config.repo_root,
    )
    print(
        f"PASS: Ensured GitHub environment variable {variable_name} in {environment_name}"
    )


def setup_github_environment(
    pipeline_config: PipelineConfig,
    *,
    repository_name_with_owner: str,
    dry_run: bool,
) -> None:
    """Reconcile the non-secret GitHub setup required by one pipeline workflow.

    Inputs: validated pipeline config, resolved repository name, and dry-run flag.
    Outputs: none. Applies repo variables, creates the environment, and sets env vars.
    Edge cases: reads public-safe values locally and intentionally leaves secrets to the
    separate secret sync helper.
    """

    env_values = load_root_env_values(pipeline_config.repo_root)
    repository_variable_values = build_repository_variable_values(pipeline_config)
    environment_variable_values = build_environment_variable_values(
        pipeline_config,
        env_values=env_values,
    )

    for variable_name in pipeline_config.required_repository_variable_names:
        ensure_repository_variable(
            pipeline_config=pipeline_config,
            variable_name=variable_name,
            variable_value=repository_variable_values[variable_name],
            dry_run=dry_run,
        )

    ensure_github_environment(
        repository_name_with_owner=repository_name_with_owner,
        pipeline_config=pipeline_config,
        dry_run=dry_run,
    )

    for variable_name in pipeline_config.required_environment_variable_names:
        ensure_environment_variable(
            pipeline_config=pipeline_config,
            variable_name=variable_name,
            variable_value=environment_variable_values[variable_name],
            dry_run=dry_run,
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for idempotent GitHub environment configuration."""

    parser = argparse.ArgumentParser(
        description=(
            "Create or reconcile the shared GitHub repository variables and one "
            "pipeline GitHub environment."
        ),
    )
    parser.add_argument("--pipeline", help="Pipeline id under pipelines/<id>.yaml.")
    parser.add_argument("--config", help="Explicit pipeline config path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions without calling GitHub.",
    )
    args = parser.parse_args(argv)

    is_valid, status_lines = check_github_cli_authentication()
    for status_line in status_lines:
        print(status_line)
    if not is_valid:
        return 1

    repo_context = detect_repo_context()
    if repo_context.repository is None:
        print("Could not resolve the GitHub repository name from this checkout.")
        return 1

    pipeline_config = load_pipeline_config(
        repo_context.root,
        pipeline_id=args.pipeline,
        config_path=args.config,
    )

    try:
        setup_github_environment(
            pipeline_config,
            repository_name_with_owner=repo_context.repository,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1

    print(
        "Completed GitHub variable setup for "
        f"{pipeline_config.pipeline_id} ({pipeline_config.github.environment_name})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
