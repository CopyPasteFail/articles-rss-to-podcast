"""Create or reconcile one pipeline-specific Google service account and bindings."""

from __future__ import annotations

import argparse
import sys

from tools.command_utils import (
    CommandExecutionError,
    command_exists,
    detect_repo_context,
    run_command,
    run_json_command,
)
from tools.pipeline_config import build_workflow_ref, load_pipeline_config


def build_workload_identity_member(
    *,
    project_number: str,
    pool_id: str,
    workflow_ref: str,
) -> str:
    """Return the exact principalSet member for one workflow file and branch."""

    return (
        "principalSet://iam.googleapis.com/projects/"
        f"{project_number}/locations/global/workloadIdentityPools/"
        f"{pool_id}/attribute.workflow_ref/{workflow_ref}"
    )


def get_project_roles_for_service_account(
    *,
    pipeline_config,
) -> set[str]:
    """Return project-level roles currently granted to the pipeline service account."""

    google_config = pipeline_config.google
    policy = run_json_command(
        [
            "gcloud",
            "projects",
            "get-iam-policy",
            google_config.project_id,
            "--format=json",
        ],
        cwd=pipeline_config.repo_root,
    )
    if not isinstance(policy, dict):
        raise RuntimeError("Unexpected project IAM policy payload.")

    current_roles: set[str] = set()
    for binding in policy.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        role_name = binding.get("role")
        members = binding.get("members")
        if not isinstance(role_name, str) or not isinstance(members, list):
            continue
        if f"serviceAccount:{google_config.service_account_email}" in members:
            current_roles.add(role_name)
    return current_roles


def remove_unexpected_project_roles(
    *,
    pipeline_config,
    expected_roles: set[str],
) -> None:
    """Remove broader project-level roles from the dedicated pipeline service account."""

    google_config = pipeline_config.google
    current_roles = get_project_roles_for_service_account(
        pipeline_config=pipeline_config
    )
    unexpected_roles = sorted(current_roles - expected_roles)
    for role_name in unexpected_roles:
        run_command(
            [
                "gcloud",
                "projects",
                "remove-iam-policy-binding",
                google_config.project_id,
                "--member",
                f"serviceAccount:{google_config.service_account_email}",
                "--role",
                role_name,
                "--quiet",
            ],
            cwd=pipeline_config.repo_root,
        )
        print(
            "REMOVED: Unexpected project-level role from pipeline service account "
            f"{role_name}"
        )


def get_service_account_policy(
    *,
    pipeline_config,
) -> dict[str, object]:
    """Return the IAM policy attached to the dedicated pipeline service account."""

    google_config = pipeline_config.google
    policy = run_json_command(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "get-iam-policy",
            google_config.service_account_email,
            "--project",
            google_config.project_id,
            "--format=json",
        ],
        cwd=pipeline_config.repo_root,
    )
    if not isinstance(policy, dict):
        raise RuntimeError("Unexpected service account IAM policy payload.")
    return policy


def remove_unexpected_workload_identity_members(
    *,
    pipeline_config,
    expected_member: str,
) -> None:
    """Remove broader same-pool workload identity members from the service account."""

    google_config = pipeline_config.google
    service_account_policy = get_service_account_policy(pipeline_config=pipeline_config)
    pool_principal_prefix = (
        "principal://iam.googleapis.com/projects/"
        f"{google_config.project_number}/locations/global/workloadIdentityPools/"
        f"{google_config.workload_identity_pool_id}/"
    )
    pool_principal_set_prefix = (
        "principalSet://iam.googleapis.com/projects/"
        f"{google_config.project_number}/locations/global/workloadIdentityPools/"
        f"{google_config.workload_identity_pool_id}/"
    )
    for binding in service_account_policy.get("bindings", []):
        if (
            not isinstance(binding, dict)
            or binding.get("role") != "roles/iam.workloadIdentityUser"
        ):
            continue
        members = binding.get("members")
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, str):
                continue
            if member == expected_member:
                continue
            if member.startswith(pool_principal_prefix) or member.startswith(
                pool_principal_set_prefix
            ):
                run_command(
                    [
                        "gcloud",
                        "iam",
                        "service-accounts",
                        "remove-iam-policy-binding",
                        google_config.service_account_email,
                        "--project",
                        google_config.project_id,
                        "--role",
                        "roles/iam.workloadIdentityUser",
                        "--member",
                        member,
                        "--quiet",
                    ],
                    cwd=pipeline_config.repo_root,
                )
                print(
                    "REMOVED: Broader same-pool workload identity binding from dedicated "
                    f"service account: {member}"
                )


def ensure_pipeline_service_account(
    *,
    repository_name_with_owner: str,
    pipeline_config,
) -> None:
    """Create or update one pipeline service account, roles, and WIF binding.

    Inputs: repository name and validated pipeline config.
    Outputs: None. Mutates Google Cloud IAM through `gcloud`.
    Edge cases: reuses existing service accounts and reconciles drift to the exact workflow binding.
    """

    google_config = pipeline_config.google
    workflow_ref = build_workflow_ref(
        repository_name_with_owner,
        pipeline_config.github.workflow_file,
        pipeline_config.github.branch_ref,
    )
    workload_identity_member = build_workload_identity_member(
        project_number=google_config.project_number,
        pool_id=google_config.workload_identity_pool_id,
        workflow_ref=workflow_ref,
    )

    run_command(
        ["gcloud", "config", "set", "project", google_config.project_id],
        cwd=pipeline_config.repo_root,
    )

    service_account_exists = True
    try:
        run_command(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "describe",
                google_config.service_account_email,
                "--project",
                google_config.project_id,
            ],
            cwd=pipeline_config.repo_root,
        )
    except CommandExecutionError:
        service_account_exists = False

    if service_account_exists:
        print(f"PASS: Reusing service account {google_config.service_account_email}")
    else:
        run_command(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "create",
                google_config.service_account_id,
                "--project",
                google_config.project_id,
                "--display-name",
                pipeline_config.pipeline_id,
            ],
            cwd=pipeline_config.repo_root,
        )
        print(f"CREATED: Service account {google_config.service_account_email}")

    for role_name in google_config.roles:
        run_command(
            [
                "gcloud",
                "projects",
                "add-iam-policy-binding",
                google_config.project_id,
                "--member",
                f"serviceAccount:{google_config.service_account_email}",
                "--role",
                role_name,
                "--quiet",
            ],
            cwd=pipeline_config.repo_root,
        )
        print(
            "PASS: Ensured pipeline service account role "
            f"{role_name} on {google_config.project_id}"
        )

    remove_unexpected_project_roles(
        pipeline_config=pipeline_config,
        expected_roles=set(google_config.roles),
    )

    run_command(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            google_config.service_account_email,
            "--project",
            google_config.project_id,
            "--role",
            "roles/iam.workloadIdentityUser",
            "--member",
            workload_identity_member,
        ],
        cwd=pipeline_config.repo_root,
    )

    remove_unexpected_workload_identity_members(
        pipeline_config=pipeline_config,
        expected_member=workload_identity_member,
    )

    print(
        "PASS: Ensured exact workflow-scoped Workload Identity binding for "
        f"{workflow_ref}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for one pipeline service account setup."""

    parser = argparse.ArgumentParser(
        description="Create or reuse one dedicated pipeline service account and its WIF binding.",
    )
    parser.add_argument("--pipeline", help="Pipeline id under pipelines/<id>.yaml.")
    parser.add_argument("--config", help="Explicit pipeline config path.")
    args = parser.parse_args(argv)

    if not command_exists("gcloud"):
        print("Missing required dependency: gcloud")
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
    ensure_pipeline_service_account(
        repository_name_with_owner=repo_context.repository,
        pipeline_config=pipeline_config,
    )
    print(
        "Pipeline Google service account setup complete for "
        f"{pipeline_config.pipeline_id}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
