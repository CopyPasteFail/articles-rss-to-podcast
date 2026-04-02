"""Sync pipeline environment secrets from local `.env` into GitHub Actions."""

from __future__ import annotations

import argparse
import sys

from tools.check_gh_auth import check_github_cli_authentication
from tools.command_utils import (
    detect_repo_context,
    load_root_env_values,
    run_command,
)
from tools.pipeline_config import PipelineConfig, load_pipeline_config


def push_github_secrets(
    pipeline_config: PipelineConfig,
    *,
    dry_run: bool,
) -> list[str]:
    """Push the selected pipeline's environment secrets into GitHub Actions.

    Inputs: validated pipeline config and dry-run flag.
    Outputs: list of secret names that were validated or updated.
    Edge cases: refuses to continue when any allowlisted value is missing locally.
    """

    repo_context = detect_repo_context()
    env_values = load_root_env_values(repo_context.root)
    updated_secret_names: list[str] = []
    pending_secret_updates: list[tuple[str, str]] = []
    environment_name = pipeline_config.github.environment_name

    runtime_secret_names = ("CLOUDFLARE_API_TOKEN", "IA_ACCESS_KEY", "IA_SECRET_KEY")
    for secret_name in runtime_secret_names:
        secret_value = (env_values.get(secret_name) or "").strip()
        if not secret_value:
            raise RuntimeError(
                f"Local value for environment secret '{secret_name}' is missing from .env or the current environment."
            )
        pending_secret_updates.append((secret_name, secret_value))

    if pipeline_config.failure_email is not None:
        failure_email_secret_names = pipeline_config.failure_email.required_secret_names
        for secret_name in failure_email_secret_names:
            secret_value = (env_values.get(secret_name) or "").strip()
            if not secret_value:
                raise RuntimeError(
                    f"Local value for failure email secret '{secret_name}' is missing from .env or the current environment."
                )
            pending_secret_updates.append((secret_name, secret_value))

    for github_secret_name, secret_value in pending_secret_updates:
        if dry_run:
            print(
                "DRY RUN: would update GitHub environment secret "
                f"{github_secret_name} in {environment_name}"
            )
        else:
            run_command(
                [
                    "gh",
                    "secret",
                    "set",
                    github_secret_name,
                    "--env",
                    environment_name,
                    "--body",
                    secret_value,
                ],
                cwd=repo_context.root,
            )
            print(f"UPDATED: {github_secret_name} in {environment_name}")
        updated_secret_names.append(github_secret_name)

    return updated_secret_names


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for safe GitHub secret synchronization."""

    parser = argparse.ArgumentParser(
        description="Push pipeline-specific GitHub Actions environment secrets from local .env.",
    )
    parser.add_argument("--pipeline", help="Pipeline id under pipelines/<id>.yaml.")
    parser.add_argument("--config", help="Explicit pipeline config path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which secrets would be updated without calling `gh secret set`.",
    )
    args = parser.parse_args(argv)

    is_valid, status_lines = check_github_cli_authentication()
    for status_line in status_lines:
        print(status_line)
    if not is_valid:
        return 1

    repo_context = detect_repo_context()
    pipeline_config = load_pipeline_config(
        repo_context.root,
        pipeline_id=args.pipeline,
        config_path=args.config,
    )

    root_env_path = repo_context.root / ".env"
    if not root_env_path.exists():
        print(f"Missing required local env file: {root_env_path}")
        return 1

    forbidden_google_keys = {"GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GHA_CREDS_PATH"}
    overlapping_names = forbidden_google_keys.intersection(
        set(pipeline_config.required_environment_secret_names)
    )
    if overlapping_names:
        overlap_text = ", ".join(sorted(overlapping_names))
        raise RuntimeError(
            "GitHub secret allowlist contains forbidden Google credential names: "
            f"{overlap_text}"
        )

    try:
        updated_secret_names = push_github_secrets(
            pipeline_config,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1
    if not updated_secret_names:
        print("No GitHub secrets were selected for upload.")
        return 1
    print(
        "Completed GitHub environment secret sync for "
        f"{pipeline_config.pipeline_id} ({pipeline_config.github.environment_name}): "
        f"{', '.join(updated_secret_names)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
