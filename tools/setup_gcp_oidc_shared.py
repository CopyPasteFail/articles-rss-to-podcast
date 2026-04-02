"""Create or reconcile the shared GCP Workload Identity pool and provider."""

from __future__ import annotations

import argparse
import sys

from tools.command_utils import (
    CommandExecutionError,
    command_exists,
    detect_repo_context,
    resolve_repository_numeric_id,
    run_command,
    run_json_command,
)
from tools.pipeline_config import PipelineConfig, load_pipeline_config


EXPECTED_PROVIDER_ISSUER_URI = "https://token.actions.githubusercontent.com"
PROVIDER_ATTRIBUTE_MAPPING = {
    "google.subject": "assertion.sub",
    "attribute.actor": "assertion.actor",
    "attribute.repository": "assertion.repository",
    "attribute.repository_id": "assertion.repository_id",
    "attribute.repository_owner": "assertion.repository_owner",
    "attribute.repository_owner_id": "assertion.repository_owner_id",
    "attribute.ref": "assertion.ref",
    "attribute.workflow_ref": "assertion.workflow_ref",
}


def _read_provider_issuer_uri(provider_configuration: dict[str, object]) -> str | None:
    """Return the provider issuer URI from either flat or nested gcloud JSON.

    Inputs: raw `gcloud ... providers describe --format=json` payload.
    Outputs: issuer URI string when present, else None.
    Edge cases: `gcloud` nests the OIDC issuer under `oidc.issuerUri` for OIDC providers.
    """

    issuer_uri = provider_configuration.get("issuerUri")
    if isinstance(issuer_uri, str) and issuer_uri:
        return issuer_uri

    oidc_configuration = provider_configuration.get("oidc")
    if not isinstance(oidc_configuration, dict):
        return None

    nested_issuer_uri = oidc_configuration.get("issuerUri")
    if isinstance(nested_issuer_uri, str) and nested_issuer_uri:
        return nested_issuer_uri
    return None


def _format_attribute_mapping(attribute_mapping: dict[str, str]) -> str:
    """Render the provider attribute mapping as the gcloud CLI expects it."""

    return ",".join(
        f"{mapping_key}={mapping_value}"
        for mapping_key, mapping_value in attribute_mapping.items()
    )


def _is_not_found_error(command_error: CommandExecutionError) -> bool:
    """Return whether a gcloud error represents a missing resource."""

    return (
        "NOT_FOUND" in command_error.output
        or "was not found" in command_error.output
        or "does not exist" in command_error.output
    )


def build_expected_provider_condition(
    *,
    repository_name_with_owner: str,
    repository_numeric_id: str | None,
) -> str:
    """Build the shared provider condition for this repository.

    Inputs: repository owner/name and optional numeric repository id.
    Outputs: CEL expression that admits only branch tokens from this repository.
    Edge cases: falls back to repository name if the numeric id cannot be resolved.
    """

    if repository_numeric_id:
        return (
            f"assertion.repository_id == '{repository_numeric_id}' && "
            "assertion.ref_type == 'branch'"
        )
    return (
        f"assertion.repository == '{repository_name_with_owner}' && "
        "assertion.ref_type == 'branch'"
    )


def get_expected_provider_configuration(
    *,
    repository_name_with_owner: str,
    repository_numeric_id: str | None,
) -> dict[str, object]:
    """Return the expected provider issuer, mapping, and condition."""

    return {
        "issuerUri": EXPECTED_PROVIDER_ISSUER_URI,
        "attributeMapping": PROVIDER_ATTRIBUTE_MAPPING,
        "attributeCondition": build_expected_provider_condition(
            repository_name_with_owner=repository_name_with_owner,
            repository_numeric_id=repository_numeric_id,
        ),
    }


def describe_provider_configuration(
    pipeline_config: PipelineConfig,
) -> dict[str, object] | None:
    """Describe the current provider configuration, or return None if it is missing."""

    google_config = pipeline_config.google
    try:
        payload = run_json_command(
            [
                "gcloud",
                "iam",
                "workload-identity-pools",
                "providers",
                "describe",
                google_config.workload_identity_provider_id,
                "--project",
                google_config.project_id,
                "--location",
                "global",
                "--workload-identity-pool",
                google_config.workload_identity_pool_id,
                "--format=json",
            ],
            cwd=pipeline_config.repo_root,
        )
    except CommandExecutionError as exc:
        if _is_not_found_error(exc):
            return None
        raise
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Workload Identity Provider description payload.")
    return payload


def diff_provider_configuration(
    *,
    current_provider_configuration: dict[str, object],
    expected_provider_configuration: dict[str, object],
) -> list[str]:
    """Return human-readable drift descriptions for the provider configuration."""

    drift_messages: list[str] = []
    current_issuer_uri = _read_provider_issuer_uri(current_provider_configuration)
    if current_issuer_uri != expected_provider_configuration["issuerUri"]:
        drift_messages.append(
            "issuerUri differs "
            f"(current={current_issuer_uri}, "
            f"expected={expected_provider_configuration['issuerUri']})"
        )
    if (
        current_provider_configuration.get("attributeMapping")
        != expected_provider_configuration["attributeMapping"]
    ):
        drift_messages.append(
            "attributeMapping differs from the expected GitHub claim mapping."
        )
    if (
        current_provider_configuration.get("attributeCondition")
        != expected_provider_configuration["attributeCondition"]
    ):
        drift_messages.append(
            "attributeCondition differs "
            f"(current={current_provider_configuration.get('attributeCondition')}, "
            f"expected={expected_provider_configuration['attributeCondition']})"
        )
    return drift_messages


def ensure_shared_oidc_resources(
    pipeline_config: PipelineConfig,
    repository_name_with_owner: str,
    repository_numeric_id: str | None,
) -> None:
    """Create or update the shared pool and provider for this repository.

    Inputs: validated pipeline config, repository owner/name, and optional numeric repo id.
    Outputs: None. Mutates Google Cloud resources through `gcloud`.
    Edge cases: reuses the pool/provider when they already match the expected configuration.
    """

    google_config = pipeline_config.google
    expected_provider_configuration = get_expected_provider_configuration(
        repository_name_with_owner=repository_name_with_owner,
        repository_numeric_id=repository_numeric_id,
    )

    run_command(
        ["gcloud", "config", "set", "project", google_config.project_id],
        cwd=pipeline_config.repo_root,
    )

    pool_exists = True
    try:
        run_command(
            [
                "gcloud",
                "iam",
                "workload-identity-pools",
                "describe",
                google_config.workload_identity_pool_id,
                "--project",
                google_config.project_id,
                "--location",
                "global",
            ],
            cwd=pipeline_config.repo_root,
        )
    except CommandExecutionError:
        pool_exists = False

    if pool_exists:
        print(
            "PASS: Reusing Workload Identity Pool "
            f"{google_config.workload_identity_pool_id}."
        )
    else:
        run_command(
            [
                "gcloud",
                "iam",
                "workload-identity-pools",
                "create",
                google_config.workload_identity_pool_id,
                "--project",
                google_config.project_id,
                "--location",
                "global",
                "--display-name",
                "GitHub Actions Pool",
            ],
            cwd=pipeline_config.repo_root,
        )
        print(
            f"CREATED: Workload Identity Pool {google_config.workload_identity_pool_id}"
        )

    current_provider_configuration = describe_provider_configuration(pipeline_config)
    provider_exists = current_provider_configuration is not None
    provider_drift_messages = (
        diff_provider_configuration(
            current_provider_configuration=current_provider_configuration,
            expected_provider_configuration=expected_provider_configuration,
        )
        if current_provider_configuration is not None
        else []
    )

    provider_command = [
        "gcloud",
        "iam",
        "workload-identity-pools",
        "providers",
        "update-oidc" if provider_exists else "create-oidc",
        google_config.workload_identity_provider_id,
        "--project",
        google_config.project_id,
        "--location",
        "global",
        "--workload-identity-pool",
        google_config.workload_identity_pool_id,
        "--display-name",
        "GitHub Actions Provider",
        "--issuer-uri",
        str(expected_provider_configuration["issuerUri"]),
        "--attribute-mapping",
        _format_attribute_mapping(PROVIDER_ATTRIBUTE_MAPPING),
        "--attribute-condition",
        str(expected_provider_configuration["attributeCondition"]),
    ]

    if provider_exists and not provider_drift_messages:
        print(
            "PASS: Workload Identity Provider already matches the expected configuration."
        )
        return

    if provider_drift_messages:
        print("UPDATING: Workload Identity Provider drift detected:")
        for drift_message in provider_drift_messages:
            print(f"  - {drift_message}")

    run_command(provider_command, cwd=pipeline_config.repo_root)

    reconciled_provider_configuration = describe_provider_configuration(pipeline_config)
    if reconciled_provider_configuration is None:
        raise RuntimeError(
            "Provider update completed but the provider can no longer be described."
        )
    remaining_drift_messages = diff_provider_configuration(
        current_provider_configuration=reconciled_provider_configuration,
        expected_provider_configuration=expected_provider_configuration,
    )
    if remaining_drift_messages:
        remaining_drift_text = "\n".join(
            f"- {message}" for message in remaining_drift_messages
        )
        raise RuntimeError(
            "Provider update completed but drift remains according to a fresh describe:\n"
            f"{remaining_drift_text}"
        )

    if provider_exists:
        print(
            "PASS: Reconciled Workload Identity Provider "
            f"{google_config.workload_identity_provider_id}"
        )
    else:
        print(
            "CREATED: Workload Identity Provider "
            f"{google_config.workload_identity_provider_id}"
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for shared Google OIDC setup."""

    parser = argparse.ArgumentParser(
        description="Create or reuse the shared Google Workload Identity pool and provider.",
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

    repository_numeric_id = resolve_repository_numeric_id(
        repo_context.root,
        repo_context.repository,
    )
    if repository_numeric_id:
        print(
            f"Using GitHub repository id {repository_numeric_id} for provider restriction."
        )
    else:
        print(
            "GitHub repository id could not be resolved; falling back to repository name "
            "for provider restriction."
        )

    pipeline_config = load_pipeline_config(
        repo_context.root,
        pipeline_id=args.pipeline,
        config_path=args.config,
    )
    ensure_shared_oidc_resources(
        pipeline_config,
        repo_context.repository,
        repository_numeric_id,
    )
    print(
        f"Shared Google OIDC setup complete for repository {repo_context.repository}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
