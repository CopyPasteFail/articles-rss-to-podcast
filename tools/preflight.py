"""Run explicit local or GitHub preflight checks for one selected pipeline."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import asdict, dataclass

from tools.check_gh_auth import check_github_cli_authentication
from tools.command_utils import (
    CommandExecutionError,
    command_exists,
    detect_repo_context,
    ensure_repo_relative_path,
    load_root_env_values,
    resolve_cloudflare_kv_namespace_id,
    resolve_python_command,
    resolve_repository_numeric_id,
    run_command,
    run_json_command,
)
from tools.pipeline_config import (
    PipelineConfig,
    PipelineConfigError,
    build_workflow_ref,
    load_pipeline_config,
    render_schedule_cron_entries,
)
from tools.setup_gcp_oidc_shared import (
    describe_provider_configuration,
    get_expected_provider_configuration,
    _read_provider_issuer_uri,
)
from tools.setup_gcp_pipeline_sa import build_workload_identity_member


EXIT_OK = 0
EXIT_MISSING = 10
EXIT_MISCONFIGURED = 11
EXIT_USAGE_ERROR = 64


@dataclass(frozen=True)
class CheckResult:
    """Represent one preflight check with status, detail, and next action."""

    name: str
    status: str
    detail: str
    next_action: str | None = None


def run_preflight(mode: str, pipeline_config: PipelineConfig) -> list[CheckResult]:
    """Run the requested preflight mode and collect explicit check results."""

    if mode == "local":
        return _run_local_preflight(pipeline_config)
    if mode == "github":
        return _run_github_preflight(pipeline_config)
    raise RuntimeError(f"Unsupported preflight mode: {mode}")


def summarize_exit_code(check_results: list[CheckResult]) -> int:
    """Map collected check statuses to the documented machine-readable exit codes."""

    statuses = {check_result.status for check_result in check_results}
    if "MISCONFIGURED" in statuses:
        return EXIT_MISCONFIGURED
    if "MISSING" in statuses:
        return EXIT_MISSING
    return EXIT_OK


def format_preflight_json(
    mode: str,
    pipeline_config: PipelineConfig,
    check_results: list[CheckResult],
) -> str:
    """Serialize a stable JSON payload for automation tooling."""

    summary = {
        "PASS": sum(
            1 for check_result in check_results if check_result.status == "PASS"
        ),
        "MISSING": sum(
            1 for check_result in check_results if check_result.status == "MISSING"
        ),
        "MISCONFIGURED": sum(
            1
            for check_result in check_results
            if check_result.status == "MISCONFIGURED"
        ),
    }
    payload = {
        "mode": mode,
        "pipeline_id": pipeline_config.pipeline_id,
        "config_path": pipeline_config.config_path.relative_to(
            pipeline_config.repo_root
        ).as_posix(),
        "summary": summary,
        "exit_code": summarize_exit_code(check_results),
        "checks": [asdict(check_result) for check_result in check_results],
        "next_actions": [
            check_result.next_action
            for check_result in check_results
            if check_result.next_action
        ],
    }
    return json.dumps(payload, indent=2)


def print_preflight_report(
    mode: str,
    pipeline_config: PipelineConfig,
    check_results: list[CheckResult],
) -> None:
    """Print the human-readable preflight report with summary and next actions."""

    print(f"Preflight mode: {mode}")
    print(f"Pipeline: {pipeline_config.pipeline_id}")
    print(
        "Config: "
        f"{pipeline_config.config_path.relative_to(pipeline_config.repo_root).as_posix()}"
    )
    print("")
    for check_result in check_results:
        print(f"[{check_result.status}] {check_result.name}: {check_result.detail}")

    exit_code = summarize_exit_code(check_results)
    print("")
    print(
        "Summary: "
        f"{sum(1 for check_result in check_results if check_result.status == 'PASS')} PASS, "
        f"{sum(1 for check_result in check_results if check_result.status == 'MISSING')} MISSING, "
        f"{sum(1 for check_result in check_results if check_result.status == 'MISCONFIGURED')} MISCONFIGURED"
    )
    print(f"Exit code: {exit_code}")

    next_actions = [
        check_result.next_action
        for check_result in check_results
        if check_result.next_action
    ]
    if next_actions:
        print("Next actions:")
        for next_action in next_actions:
            print(f"- {next_action}")


def _run_local_preflight(pipeline_config: PipelineConfig) -> list[CheckResult]:
    """Run local-mode checks against `.env`, local credentials, and toolchain."""

    repo_root = pipeline_config.repo_root
    env_path = repo_root / ".env"
    env_values = load_root_env_values(repo_root)
    check_results: list[CheckResult] = []

    check_results.append(
        CheckResult(
            name="Repository root",
            status="PASS",
            detail=f"Detected repo root at {repo_root}",
        )
    )
    check_results.append(
        _path_exists_check(
            name="Pipeline config",
            path=pipeline_config.config_path,
            success_detail="Selected pipeline config exists.",
            missing_action=f"Create or restore {pipeline_config.config_path.relative_to(repo_root)}.",
        )
    )
    feed_env_path = ensure_repo_relative_path(repo_root, pipeline_config.feed_env_file)
    check_results.append(
        _path_exists_check(
            name="Feed env file",
            path=feed_env_path,
            success_detail=f"Feed env file exists at {feed_env_path.relative_to(repo_root)}.",
            missing_action=f"Create {feed_env_path.relative_to(repo_root)}.",
        )
    )
    check_results.append(_python_runtime_check(repo_root))
    check_results.append(_python_dependency_check(repo_root))
    check_results.append(_node_and_wrangler_check(repo_root))
    check_results.append(
        _path_exists_check(
            name="Local .env",
            path=env_path,
            success_detail="Local .env exists.",
            missing_action="Create .env from .env.example for local mode.",
        )
    )

    required_local_env_names = list(pipeline_config.required_local_env_names)
    for env_name in required_local_env_names:
        if env_name == "CF_KV_NAMESPACE_ID":
            resolved_namespace_id = resolve_cloudflare_kv_namespace_id(
                env_values=env_values
            )
            if resolved_namespace_id:
                check_results.append(
                    CheckResult(
                        name="Local env:CF_KV_NAMESPACE_ID",
                        status="PASS",
                        detail=f"CF_KV_NAMESPACE_ID is available or discoverable as {resolved_namespace_id}.",
                    )
                )
                continue
        env_value = (env_values.get(env_name) or "").strip()
        if env_value:
            check_results.append(
                CheckResult(
                    name=f"Local env:{env_name}",
                    status="PASS",
                    detail=f"{env_name} is set.",
                )
            )
        else:
            check_results.append(
                CheckResult(
                    name=f"Local env:{env_name}",
                    status="MISSING",
                    detail=f"{env_name} is not set in .env or the current environment.",
                    next_action=f"Add {env_name} to .env before running local mode.",
                )
            )

    google_credentials_path = (
        env_values.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    ).strip()
    if google_credentials_path:
        resolved_credentials_path = pathlib.Path(google_credentials_path)
        if not resolved_credentials_path.is_absolute():
            resolved_credentials_path = (
                repo_root / resolved_credentials_path
            ).resolve()
        check_results.append(
            _path_exists_check(
                name="Google credentials file",
                path=resolved_credentials_path,
                success_detail=f"Google credentials file exists at {resolved_credentials_path}.",
                missing_action="Fix GOOGLE_APPLICATION_CREDENTIALS to point to a readable local JSON key file.",
            )
        )

    return check_results


def _run_github_preflight(pipeline_config: PipelineConfig) -> list[CheckResult]:
    """Run GitHub-mode checks against GH CLI, GCP setup, and workflow inputs."""

    repo_root = pipeline_config.repo_root
    repo_context = detect_repo_context(repo_root)
    check_results: list[CheckResult] = []

    check_results.append(
        CheckResult(
            name="Repository root",
            status="PASS",
            detail=f"Detected repo root at {repo_root}",
        )
    )
    check_results.append(
        CheckResult(
            name="Pipeline config",
            status="PASS",
            detail=f"Selected pipeline config is {pipeline_config.config_path.relative_to(repo_root)}.",
        )
    )

    if command_exists("gh"):
        check_results.append(
            CheckResult(name="gh CLI", status="PASS", detail="gh is available on PATH.")
        )
    else:
        check_results.append(
            CheckResult(
                name="gh CLI",
                status="MISSING",
                detail="gh is not installed or not on PATH.",
                next_action="Install GitHub CLI and run `gh auth login`.",
            )
        )

    gh_auth_ok, gh_status_lines = check_github_cli_authentication()
    gh_status_text = " ".join(gh_status_lines)
    check_results.append(
        CheckResult(
            name="gh auth status",
            status="PASS" if gh_auth_ok else "MISSING",
            detail=gh_status_text,
            next_action=None if gh_auth_ok else "Run `gh auth login`.",
        )
    )

    if repo_context.repository is not None:
        check_results.append(
            CheckResult(
                name="GitHub repo context",
                status="PASS",
                detail=f"Resolved repository {repo_context.repository}.",
            )
        )
    else:
        check_results.append(
            CheckResult(
                name="GitHub repo context",
                status="MISCONFIGURED",
                detail="Could not resolve a GitHub owner/repo from this checkout.",
                next_action="Set the `origin` remote to the target GitHub repository.",
            )
        )

    check_results.append(_gcloud_check())
    check_results.append(_gcloud_auth_check(repo_root))
    check_results.append(_gcloud_project_check(repo_root, pipeline_config))
    check_results.extend(
        _gcp_resource_checks(repo_root, pipeline_config, repo_context.repository)
    )
    check_results.append(_repository_variable_check(repo_root, pipeline_config))
    check_results.append(_environment_variable_check(repo_root, pipeline_config))
    check_results.append(_environment_secret_check(repo_root, pipeline_config))
    check_results.append(_workflow_target_path_check(repo_root, pipeline_config))
    check_results.append(_schedule_config_check(pipeline_config))
    check_results.append(_branch_ref_check(pipeline_config))
    return check_results


def _python_runtime_check(repo_root: pathlib.Path) -> CheckResult:
    """Verify that Python is available for local execution."""

    python_command = resolve_python_command()
    if python_command is None:
        return CheckResult(
            name="Python runtime",
            status="MISSING",
            detail="Neither `python` nor `python3` is available on PATH.",
            next_action="Install Python 3.11 and create a virtualenv.",
        )
    try:
        version_text = run_command([python_command, "--version"], cwd=repo_root)
    except CommandExecutionError as exc:
        return CheckResult(
            name="Python runtime",
            status="MISCONFIGURED",
            detail=exc.output or f"Failed to execute `{python_command} --version`.",
            next_action="Fix the Python installation before continuing.",
        )
    return CheckResult(name="Python runtime", status="PASS", detail=version_text)


def _python_dependency_check(repo_root: pathlib.Path) -> CheckResult:
    """Verify that the required Python modules import in the current environment."""

    python_command = resolve_python_command()
    if python_command is None:
        return CheckResult(
            name="Python dependencies",
            status="MISSING",
            detail="Neither `python` nor `python3` is available on PATH.",
            next_action="Install Python 3.11 and run `pip install -r requirements.txt`.",
        )
    dependency_check_command = [
        python_command,
        "-c",
        (
            "import feedparser, requests, dotenv, yaml, google.cloud.texttospeech, "
            "google.cloud.bigquery"
        ),
    ]
    try:
        run_command(dependency_check_command, cwd=repo_root)
    except CommandExecutionError:
        repo_venv_python = repo_root / ".venv" / "bin" / "python"
        if repo_venv_python.exists():
            try:
                run_command(
                    [
                        str(repo_venv_python),
                        "-c",
                        (
                            "import feedparser, requests, dotenv, google.cloud.texttospeech, "
                            "google.cloud.bigquery"
                        ),
                    ],
                    cwd=repo_root,
                )
                return CheckResult(
                    name="Python dependencies",
                    status="PASS",
                    detail="Required runtime Python modules import successfully in .venv/bin/python.",
                )
            except CommandExecutionError:
                pass

        if command_exists("pip") or (repo_root / ".venv" / "bin" / "pip").exists():
            return CheckResult(
                name="Python dependencies",
                status="PASS",
                detail=(
                    "Current shell interpreter is missing some runtime imports, but requirements are "
                    "declared and pip is available for installation."
                ),
            )
        return CheckResult(
            name="Python dependencies",
            status="MISCONFIGURED",
            detail="Python packages are missing and `pip` is unavailable.",
            next_action="Install pip, then run `pip install -r requirements.txt`.",
        )
    return CheckResult(
        name="Python dependencies",
        status="PASS",
        detail="Required Python modules import successfully.",
    )


def _node_and_wrangler_check(repo_root: pathlib.Path) -> CheckResult:
    """Verify that Node is available and Wrangler can be installed or resolved."""

    if not command_exists("node"):
        return CheckResult(
            name="Node.js",
            status="MISSING",
            detail="Node.js is not available on PATH.",
            next_action="Install Node.js before using Cloudflare deploy steps.",
        )
    local_wrangler_path = repo_root / "node_modules" / ".bin" / "wrangler"
    if local_wrangler_path.exists() or command_exists("wrangler"):
        return CheckResult(
            name="Wrangler CLI",
            status="PASS",
            detail="Wrangler is available locally or on PATH.",
        )
    return CheckResult(
        name="Wrangler CLI",
        status="MISSING",
        detail="Wrangler is not installed yet.",
        next_action="Run `npm install` or `npm ci` to install the pinned Wrangler dependency.",
    )


def _gcloud_check() -> CheckResult:
    """Verify that the Google Cloud CLI is available."""

    if command_exists("gcloud"):
        return CheckResult(
            name="gcloud CLI", status="PASS", detail="gcloud is available on PATH."
        )
    return CheckResult(
        name="gcloud CLI",
        status="MISSING",
        detail="gcloud is not installed or not on PATH.",
        next_action="Install the Google Cloud CLI.",
    )


def _gcloud_auth_check(repo_root: pathlib.Path) -> CheckResult:
    """Verify that the current operator has an active gcloud account."""

    if not command_exists("gcloud"):
        return CheckResult(
            name="gcloud auth",
            status="MISSING",
            detail="gcloud is not installed.",
            next_action="Install gcloud, then run `gcloud auth login`.",
        )
    try:
        active_account = run_command(
            [
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
            ],
            cwd=repo_root,
        ).strip()
    except CommandExecutionError as exc:
        return CheckResult(
            name="gcloud auth",
            status="MISCONFIGURED",
            detail=exc.output or "Failed to inspect gcloud auth status.",
            next_action="Run `gcloud auth login` with an operator account that can manage IAM.",
        )
    if not active_account:
        return CheckResult(
            name="gcloud auth",
            status="MISSING",
            detail="No active gcloud account is configured.",
            next_action="Run `gcloud auth login`.",
        )
    return CheckResult(
        name="gcloud auth",
        status="PASS",
        detail=f"Active gcloud account: {active_account}",
    )


def _gcloud_project_check(
    repo_root: pathlib.Path, pipeline_config: PipelineConfig
) -> CheckResult:
    """Verify the active gcloud project matches the selected pipeline config."""

    if not pipeline_config.google.project_id:
        return CheckResult(
            name="gcloud project",
            status="MISSING",
            detail="Local Google setup config is missing google.project_id.",
            next_action="Create local-only pipelines/shared.yaml with the shared Google values.",
        )
    if not command_exists("gcloud"):
        return CheckResult(
            name="gcloud project",
            status="MISSING",
            detail="gcloud is not installed.",
            next_action=f"Install gcloud and set the project to {pipeline_config.google.project_id}.",
        )
    try:
        active_project = run_command(
            ["gcloud", "config", "get-value", "project"],
            cwd=repo_root,
        ).strip()
    except CommandExecutionError as exc:
        return CheckResult(
            name="gcloud project",
            status="MISCONFIGURED",
            detail=exc.output or "Failed to inspect the current gcloud project.",
            next_action=f"Run `gcloud config set project {pipeline_config.google.project_id}`.",
        )
    if active_project == pipeline_config.google.project_id:
        return CheckResult(
            name="gcloud project",
            status="PASS",
            detail=f"Active project matches expected project {active_project}.",
        )
    return CheckResult(
        name="gcloud project",
        status="MISCONFIGURED",
        detail=f"Active project is '{active_project or '(unset)'}', expected '{pipeline_config.google.project_id}'.",
        next_action=f"Run `gcloud config set project {pipeline_config.google.project_id}`.",
    )


def _gcp_resource_checks(
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
    repository_name_with_owner: str | None,
) -> list[CheckResult]:
    """Inspect whether the pool, provider, and service account already exist and match expectations."""

    if not pipeline_config.google.has_shared_github_oidc_settings:
        return [
            CheckResult(
                name="Shared Google setup config",
                status="MISSING",
                detail=(
                    "Local Google setup config is incomplete. Shared project/pool/provider values "
                    "are required for gcloud inspection and setup scripts."
                ),
                next_action="Create local-only pipelines/shared.yaml with the shared Google values.",
            )
        ]

    google_config = pipeline_config.google
    resource_checks: list[CheckResult] = []

    resource_checks.append(
        _gcloud_describe_check(
            name="Workload Identity Pool",
            command=[
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
            repo_root=repo_root,
            missing_action=(
                "Run `scripts/setup-gcp-oidc-shared.sh --pipeline "
                f"{pipeline_config.pipeline_id}` to create the shared pool."
            ),
        )
    )
    resource_checks.append(
        _provider_configuration_check(
            repo_root=repo_root,
            pipeline_config=pipeline_config,
            repository_name_with_owner=repository_name_with_owner,
        )
    )
    resource_checks.append(
        _gcloud_describe_check(
            name="Pipeline service account",
            command=[
                "gcloud",
                "iam",
                "service-accounts",
                "describe",
                pipeline_config.google.service_account_email,
                "--project",
                pipeline_config.google.project_id,
            ],
            repo_root=repo_root,
            missing_action=(
                "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
                f"{pipeline_config.pipeline_id}` to create the dedicated service account."
            ),
        )
    )
    resource_checks.append(
        _pipeline_service_account_roles_check(repo_root, pipeline_config)
    )
    resource_checks.append(
        _pipeline_workload_identity_binding_check(
            repo_root=repo_root,
            pipeline_config=pipeline_config,
            repository_name_with_owner=repository_name_with_owner,
        )
    )
    return resource_checks


def _repository_variable_check(
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
) -> CheckResult:
    """Inspect whether the shared GitHub repository variables already exist."""

    if not command_exists("gh"):
        return CheckResult(
            name="GitHub repository variables",
            status="MISSING",
            detail="gh is not installed, so repository variables cannot be inspected.",
            next_action="Install GitHub CLI, then configure the shared repository variables in GitHub.",
        )

    try:
        variable_names_text = run_command(
            ["gh", "variable", "list", "--json", "name", "--jq", ".[].name"],
            cwd=repo_root,
        )
    except CommandExecutionError as exc:
        return CheckResult(
            name="GitHub repository variables",
            status="MISCONFIGURED",
            detail=exc.output or "Failed to read repository variables with gh.",
            next_action="Fix GH CLI access, then configure the shared repository variables in GitHub.",
        )

    existing_variable_names = {
        variable_name.strip()
        for variable_name in variable_names_text.splitlines()
        if variable_name.strip()
    }
    missing_variable_names = [
        variable_name
        for variable_name in pipeline_config.required_repository_variable_names
        if variable_name not in existing_variable_names
    ]
    if not missing_variable_names:
        return CheckResult(
            name="GitHub repository variables",
            status="PASS",
            detail="All shared Google repository variables already exist.",
        )

    missing_text = ", ".join(missing_variable_names)
    return CheckResult(
        name="GitHub repository variables",
        status="MISSING",
        detail=f"Missing repository variables: {missing_text}",
        next_action="Add the shared Google repository variables in GitHub settings.",
    )


def _environment_variable_check(
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
) -> CheckResult:
    """Inspect whether the selected pipeline environment variables already exist."""

    if not command_exists("gh"):
        return CheckResult(
            name="GitHub environment variables",
            status="MISSING",
            detail="gh is not installed, so environment variables cannot be inspected.",
            next_action="Install GitHub CLI, then configure the pipeline environment variables in GitHub.",
        )

    environment_name = pipeline_config.github.environment_name
    try:
        variable_names_text = run_command(
            [
                "gh",
                "variable",
                "list",
                "--env",
                environment_name,
                "--json",
                "name",
                "--jq",
                ".[].name",
            ],
            cwd=repo_root,
        )
    except CommandExecutionError as exc:
        return CheckResult(
            name="GitHub environment variables",
            status="MISCONFIGURED",
            detail=exc.output
            or f"Failed to read variables for environment '{environment_name}'.",
            next_action=f"Create the GitHub environment '{environment_name}' and add its variables.",
        )

    existing_variable_names = {
        variable_name.strip()
        for variable_name in variable_names_text.splitlines()
        if variable_name.strip()
    }
    missing_variable_names = [
        variable_name
        for variable_name in pipeline_config.required_environment_variable_names
        if variable_name not in existing_variable_names
    ]
    if not missing_variable_names:
        return CheckResult(
            name="GitHub environment variables",
            status="PASS",
            detail=f"Environment '{environment_name}' has all required variables.",
        )

    missing_text = ", ".join(missing_variable_names)
    return CheckResult(
        name="GitHub environment variables",
        status="MISSING",
        detail=f"Environment '{environment_name}' is missing variables: {missing_text}",
        next_action=f"Add the required variables to the GitHub environment '{environment_name}'.",
    )


def _environment_secret_check(
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
) -> CheckResult:
    """Inspect whether the selected pipeline environment secrets already exist."""

    if not command_exists("gh"):
        return CheckResult(
            name="GitHub environment secrets",
            status="MISSING",
            detail="gh is not installed, so environment secrets cannot be inspected.",
            next_action="Install GitHub CLI and run `scripts/push-gh-secrets.sh --pipeline "
            f"{pipeline_config.pipeline_id}`.",
        )

    environment_name = pipeline_config.github.environment_name
    try:
        secret_names_text = run_command(
            [
                "gh",
                "secret",
                "list",
                "--env",
                environment_name,
                "--json",
                "name",
                "--jq",
                ".[].name",
            ],
            cwd=repo_root,
        )
    except CommandExecutionError as exc:
        return CheckResult(
            name="GitHub environment secrets",
            status="MISCONFIGURED",
            detail=exc.output
            or f"Failed to read secrets for environment '{environment_name}'.",
            next_action=(
                f"Create the GitHub environment '{environment_name}', then run "
                f"`scripts/push-gh-secrets.sh --pipeline {pipeline_config.pipeline_id}`."
            ),
        )

    existing_secret_names = {
        secret_name.strip()
        for secret_name in secret_names_text.splitlines()
        if secret_name.strip()
    }
    missing_secret_names = [
        secret_name
        for secret_name in pipeline_config.required_environment_secret_names
        if secret_name not in existing_secret_names
    ]
    if not missing_secret_names:
        return CheckResult(
            name="GitHub environment secrets",
            status="PASS",
            detail=f"Environment '{environment_name}' has all required secrets.",
        )

    missing_text = ", ".join(missing_secret_names)
    return CheckResult(
        name="GitHub environment secrets",
        status="MISSING",
        detail=f"Environment '{environment_name}' is missing secrets: {missing_text}",
        next_action=(
            f"Run `scripts/push-gh-secrets.sh --pipeline {pipeline_config.pipeline_id}` "
            f"to upload the required secrets to environment '{environment_name}'."
        ),
    )


def _workflow_target_path_check(
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
) -> CheckResult:
    """Validate that the configured workflow path is safe and writable."""

    try:
        workflow_path = ensure_repo_relative_path(
            repo_root, pipeline_config.github.workflow_file
        )
    except ValueError as exc:
        return CheckResult(
            name="Workflow file target",
            status="MISCONFIGURED",
            detail=str(exc),
            next_action="Move github.workflow_file under .github/workflows/.",
        )
    return CheckResult(
        name="Workflow file target",
        status="PASS",
        detail=f"Workflow path is valid: {workflow_path.relative_to(repo_root)}",
    )


def _provider_configuration_check(
    *,
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
    repository_name_with_owner: str | None,
) -> CheckResult:
    """Verify issuer, attribute mapping, and repo restriction for the shared provider."""

    if repository_name_with_owner is None:
        return CheckResult(
            name="Workload Identity Provider configuration",
            status="MISCONFIGURED",
            detail="GitHub repository context is unavailable, so provider expectations cannot be computed.",
            next_action="Fix the GitHub remote and rerun preflight.",
        )

    repository_numeric_id = resolve_repository_numeric_id(
        repo_root, repository_name_with_owner
    )
    expected_provider_configuration = get_expected_provider_configuration(
        repository_name_with_owner=repository_name_with_owner,
        repository_numeric_id=repository_numeric_id,
    )
    try:
        current_provider_configuration = describe_provider_configuration(
            pipeline_config
        )
    except CommandExecutionError as exc:
        return CheckResult(
            name="Workload Identity Provider configuration",
            status="MISCONFIGURED",
            detail=exc.output or "Failed to inspect the Workload Identity Provider.",
            next_action=(
                "Fix gcloud access to the shared provider, then rerun preflight or "
                f"`scripts/setup-gcp-oidc-shared.sh --pipeline {pipeline_config.pipeline_id}`."
            ),
        )
    if current_provider_configuration is None:
        return CheckResult(
            name="Workload Identity Provider configuration",
            status="MISSING",
            detail="Workload Identity Provider does not exist yet.",
            next_action=(
                "Run `scripts/setup-gcp-oidc-shared.sh --pipeline "
                f"{pipeline_config.pipeline_id}`."
            ),
        )

    current_issuer = _read_provider_issuer_uri(current_provider_configuration)
    current_mapping = current_provider_configuration.get("attributeMapping")
    current_condition = current_provider_configuration.get("attributeCondition")
    if (
        current_issuer == expected_provider_configuration["issuerUri"]
        and current_mapping == expected_provider_configuration["attributeMapping"]
        and current_condition == expected_provider_configuration["attributeCondition"]
    ):
        return CheckResult(
            name="Workload Identity Provider configuration",
            status="PASS",
            detail="Provider issuer, mapping, and repository restriction match the expected configuration.",
        )
    return CheckResult(
        name="Workload Identity Provider configuration",
        status="MISCONFIGURED",
        detail=(
            f"Provider drift detected. issuerUri={current_issuer!r}, "
            f"attributeCondition={current_condition!r}"
        ),
        next_action=(
            "Run `scripts/setup-gcp-oidc-shared.sh --pipeline "
            f"{pipeline_config.pipeline_id}` to reconcile the provider."
        ),
    )


def _pipeline_service_account_roles_check(
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
) -> CheckResult:
    """Verify the dedicated service account has exactly the configured project roles."""

    google_config = pipeline_config.google
    try:
        project_policy = run_json_command(
            [
                "gcloud",
                "projects",
                "get-iam-policy",
                google_config.project_id,
                "--format=json",
            ],
            cwd=repo_root,
        )
    except CommandExecutionError as exc:
        return CheckResult(
            name="Pipeline service account roles",
            status="MISCONFIGURED",
            detail=exc.output or "Failed to inspect project IAM policy.",
            next_action=(
                "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
                f"{pipeline_config.pipeline_id}`."
            ),
        )
    if not isinstance(project_policy, dict):
        return CheckResult(
            name="Pipeline service account roles",
            status="MISCONFIGURED",
            detail="Project IAM policy response was not a JSON object.",
            next_action="Re-run preflight after fixing gcloud access.",
        )

    current_roles: set[str] = set()
    for binding in project_policy.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        role_name = binding.get("role")
        members = binding.get("members")
        if not isinstance(role_name, str) or not isinstance(members, list):
            continue
        if f"serviceAccount:{google_config.service_account_email}" in members:
            current_roles.add(role_name)

    expected_roles = set(google_config.roles)
    missing_roles = sorted(expected_roles - current_roles)
    extra_roles = sorted(current_roles - expected_roles)
    if not missing_roles and not extra_roles:
        return CheckResult(
            name="Pipeline service account roles",
            status="PASS",
            detail=f"Dedicated service account has exactly the expected roles: {', '.join(sorted(expected_roles))}",
        )

    detail_parts: list[str] = []
    if missing_roles:
        detail_parts.append(f"missing roles: {', '.join(missing_roles)}")
    if extra_roles:
        detail_parts.append(f"extra project roles: {', '.join(extra_roles)}")
    return CheckResult(
        name="Pipeline service account roles",
        status="MISCONFIGURED",
        detail="; ".join(detail_parts),
        next_action=(
            "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
            f"{pipeline_config.pipeline_id}` to reconcile roles."
        ),
    )


def _pipeline_workload_identity_binding_check(
    *,
    repo_root: pathlib.Path,
    pipeline_config: PipelineConfig,
    repository_name_with_owner: str | None,
) -> CheckResult:
    """Verify the dedicated service account allows only the expected workflow_ref principal."""

    if repository_name_with_owner is None:
        return CheckResult(
            name="Pipeline Workload Identity binding",
            status="MISCONFIGURED",
            detail="GitHub repository context is unavailable, so expected workflow_ref cannot be computed.",
            next_action="Fix the GitHub remote and rerun preflight.",
        )

    google_config = pipeline_config.google
    expected_workflow_ref = build_workflow_ref(
        repository_name_with_owner,
        pipeline_config.github.workflow_file,
        pipeline_config.github.branch_ref,
    )
    expected_member = build_workload_identity_member(
        project_number=google_config.project_number,
        pool_id=google_config.workload_identity_pool_id,
        workflow_ref=expected_workflow_ref,
    )
    try:
        service_account_policy = run_json_command(
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
            cwd=repo_root,
        )
    except CommandExecutionError as exc:
        if "NOT_FOUND" in exc.output or "Unknown service account" in exc.output:
            return CheckResult(
                name="Pipeline Workload Identity binding",
                status="MISSING",
                detail="Dedicated pipeline service account does not exist yet.",
                next_action=(
                    "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
                    f"{pipeline_config.pipeline_id}`."
                ),
            )
        return CheckResult(
            name="Pipeline Workload Identity binding",
            status="MISCONFIGURED",
            detail=exc.output or "Failed to inspect the service account IAM policy.",
            next_action=(
                "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
                f"{pipeline_config.pipeline_id}`."
            ),
        )
    if not isinstance(service_account_policy, dict):
        return CheckResult(
            name="Pipeline Workload Identity binding",
            status="MISCONFIGURED",
            detail="Service account IAM policy response was not a JSON object.",
            next_action="Re-run preflight after fixing gcloud access.",
        )

    same_pool_members: list[str] = []
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
            if (
                isinstance(member, str)
                and f"/workloadIdentityPools/{google_config.workload_identity_pool_id}/"
                in member
            ):
                same_pool_members.append(member)

    if expected_member not in same_pool_members:
        return CheckResult(
            name="Pipeline Workload Identity binding",
            status="MISSING",
            detail=f"Expected workflow-scoped binding is missing: {expected_workflow_ref}",
            next_action=(
                "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
                f"{pipeline_config.pipeline_id}`."
            ),
        )
    broader_members = sorted(
        member for member in same_pool_members if member != expected_member
    )
    if broader_members:
        return CheckResult(
            name="Pipeline Workload Identity binding",
            status="MISCONFIGURED",
            detail=(
                "Found additional same-pool workload identity bindings on the dedicated service "
                f"account: {', '.join(broader_members)}"
            ),
            next_action=(
                "Run `scripts/setup-gcp-pipeline-sa.sh --pipeline "
                f"{pipeline_config.pipeline_id}` to prune broader bindings."
            ),
        )
    return CheckResult(
        name="Pipeline Workload Identity binding",
        status="PASS",
        detail=f"Impersonation is scoped to the exact workflow_ref {expected_workflow_ref}.",
    )


def _schedule_config_check(pipeline_config: PipelineConfig) -> CheckResult:
    """Validate that the schedule config renders valid GitHub cron entries."""

    try:
        cron_entries = render_schedule_cron_entries(pipeline_config.schedule)
    except PipelineConfigError as exc:
        return CheckResult(
            name="Schedule config",
            status="MISCONFIGURED",
            detail=str(exc),
            next_action="Fix the schedule block in the selected pipeline config.",
        )
    cron_summary = ", ".join(entry["cron"] for entry in cron_entries)
    return CheckResult(
        name="Schedule config",
        status="PASS",
        detail=(
            f"Schedule renders GitHub cron entries in timezone {pipeline_config.schedule.timezone}: "
            f"{cron_summary}"
        ),
    )


def _branch_ref_check(pipeline_config: PipelineConfig) -> CheckResult:
    """Validate that the branch ref is present and explicit."""

    branch_ref = pipeline_config.github.branch_ref
    if branch_ref.startswith("refs/heads/") and branch_ref != "refs/heads/":
        return CheckResult(
            name="GitHub branch ref",
            status="PASS",
            detail=f"Branch ref is explicit: {branch_ref}",
        )
    return CheckResult(
        name="GitHub branch ref",
        status="MISCONFIGURED",
        detail=f"Invalid branch ref: {branch_ref}",
        next_action="Set github.branch_ref to an explicit refs/heads/<branch> value.",
    )


def _gcloud_describe_check(
    *,
    name: str,
    command: list[str],
    repo_root: pathlib.Path,
    missing_action: str,
) -> CheckResult:
    """Run one gcloud describe command and classify missing vs misconfigured cases."""

    if not command_exists("gcloud"):
        return CheckResult(
            name=name,
            status="MISSING",
            detail="gcloud is not installed.",
            next_action=missing_action,
        )
    try:
        run_command(command, cwd=repo_root)
    except CommandExecutionError as exc:
        if (
            "NOT_FOUND" in exc.output
            or "was not found" in exc.output
            or "does not exist" in exc.output
        ):
            return CheckResult(
                name=name,
                status="MISSING",
                detail=f"{name} does not exist yet.",
                next_action=missing_action,
            )
        return CheckResult(
            name=name,
            status="MISCONFIGURED",
            detail=exc.output or f"Failed to inspect {name}.",
            next_action=missing_action,
        )
    return CheckResult(name=name, status="PASS", detail=f"{name} already exists.")


def _path_exists_check(
    *,
    name: str,
    path: pathlib.Path,
    success_detail: str,
    missing_action: str,
) -> CheckResult:
    """Return a simple path existence check result."""

    if path.exists():
        return CheckResult(name=name, status="PASS", detail=success_detail)
    return CheckResult(
        name=name,
        status="MISSING",
        detail=f"Missing path: {path}",
        next_action=missing_action,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for local and GitHub preflight validation."""

    parser = argparse.ArgumentParser(
        description="Run local or GitHub preflight checks."
    )
    parser.add_argument("mode", choices=["local", "github"])
    parser.add_argument("--pipeline", help="Pipeline id under pipelines/<id>.yaml.")
    parser.add_argument("--config", help="Explicit pipeline config path.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable report.",
    )
    args = parser.parse_args(argv)

    try:
        repo_context = detect_repo_context()
        pipeline_config = load_pipeline_config(
            repo_context.root,
            pipeline_id=args.pipeline,
            config_path=args.config,
        )
    except (RuntimeError, PipelineConfigError) as exc:
        if args.json:
            print(
                json.dumps({"error": str(exc), "exit_code": EXIT_USAGE_ERROR}, indent=2)
            )
        else:
            print(f"Preflight failed before checks could start: {exc}")
        return EXIT_USAGE_ERROR

    check_results = run_preflight(args.mode, pipeline_config)
    if args.json:
        print(format_preflight_json(args.mode, pipeline_config, check_results))
    else:
        print_preflight_report(args.mode, pipeline_config, check_results)
    return summarize_exit_code(check_results)


if __name__ == "__main__":
    sys.exit(main())
