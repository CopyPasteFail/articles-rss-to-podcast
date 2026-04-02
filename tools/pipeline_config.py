"""Pipeline config loading, validation, and workflow schedule rendering."""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import yaml

from tools.command_utils import ensure_repo_relative_path


CONFIG_SEARCH_SUFFIXES = (".yaml", ".yml", ".json")
LOCAL_CONFIG_SUFFIX = ".local"
SHARED_PIPELINE_CONFIG_BASENAME = "shared"
SHARED_GOOGLE_FIELD_NAMES = (
    "project_id",
    "project_number",
    "workload_identity_pool_id",
    "workload_identity_provider_id",
)
MINIMUM_GITHUB_INTERVAL_MINUTES = 5
MAXIMUM_SCHEDULE_WINDOW_MINUTES = 24 * 60
MINIMUM_PIPELINE_GOOGLE_ROLES = ("roles/serviceusage.serviceUsageConsumer",)
REPOSITORY_GOOGLE_VARIABLE_NAMES = (
    "GCP_PROJECT_ID",
    "GCP_PROJECT_NUMBER",
    "GCP_WIF_POOL_ID",
    "GCP_WIF_PROVIDER_ID",
)
PIPELINE_ENVIRONMENT_VARIABLE_NAMES = (
    "GCP_SERVICE_ACCOUNT_EMAIL",
    "CLOUDFLARE_ACCOUNT_ID",
    "CF_PAGES_PROJECT",
    "CF_KV_NAMESPACE_ID",
)
PIPELINE_ENVIRONMENT_SECRET_NAMES = (
    "CLOUDFLARE_API_TOKEN",
    "IA_ACCESS_KEY",
    "IA_SECRET_KEY",
)
FAILURE_EMAIL_SECRET_NAME_DEFAULTS = {
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_username": "SMTP_USERNAME",
    "smtp_password": "SMTP_PASSWORD",  # nosec B105
    "smtp_from": "SMTP_FROM",
}


class PipelineConfigError(ValueError):
    """Raised when a pipeline config cannot be loaded or validated safely."""


@dataclass(frozen=True)
class ScheduleConfig:
    """Validated schedule configuration for one pipeline workflow."""

    timezone: str
    interval_minutes: int
    window_start: str
    window_end: str


@dataclass(frozen=True)
class GitHubConfig:
    """Validated GitHub workflow configuration for one pipeline."""

    workflow_file: str
    branch_ref: str
    environment_name: str


@dataclass(frozen=True)
class GoogleConfig:
    """Validated Google Cloud setup configuration for one pipeline."""

    project_id: str | None
    project_number: str | None
    workload_identity_pool_id: str | None
    workload_identity_provider_id: str | None
    service_account_id: str
    roles: tuple[str, ...]

    @property
    def has_shared_github_oidc_settings(self) -> bool:
        """Return whether shared Google settings are available locally."""

        return all(
            [
                self.project_id,
                self.project_number,
                self.workload_identity_pool_id,
                self.workload_identity_provider_id,
            ]
        )

    @property
    def service_account_email(self) -> str:
        """Return the dedicated service account email for local setup tooling."""

        if not self.project_id:
            raise PipelineConfigError(
                "Local Google setup is missing google.project_id, so the service account email "
                "cannot be derived."
            )
        return f"{self.service_account_id}@{self.project_id}.iam.gserviceaccount.com"

    @property
    def provider_resource_name(self) -> str:
        """Return the full Workload Identity Provider resource name."""

        if (
            not self.project_number
            or not self.workload_identity_pool_id
            or not self.workload_identity_provider_id
        ):
            raise PipelineConfigError(
                "Local Google setup is incomplete. project_number, "
                "workload_identity_pool_id, and workload_identity_provider_id are required."
            )
        return (
            "projects/"
            f"{self.project_number}/locations/global/workloadIdentityPools/"
            f"{self.workload_identity_pool_id}/providers/{self.workload_identity_provider_id}"
        )


@dataclass(frozen=True)
class FailureEmailConfig:
    """Optional failure email transport configuration for one pipeline."""

    transport: str
    recipients: tuple[str, ...]
    subject_prefix: str
    smtp_host_secret_name: str
    smtp_port_secret_name: str
    smtp_username_secret_name: str
    smtp_password_secret_name: str
    smtp_from_secret_name: str

    @property
    def required_secret_names(self) -> tuple[str, ...]:
        """Return the environment secret names required by the failure email step."""

        return (
            self.smtp_host_secret_name,
            self.smtp_port_secret_name,
            self.smtp_username_secret_name,
            self.smtp_password_secret_name,
            self.smtp_from_secret_name,
        )


@dataclass(frozen=True)
class PipelineConfig:
    """Validated, repo-aware configuration for one pipeline definition."""

    pipeline_id: str
    feed_slug: str
    feed_env_file: str
    schedule: ScheduleConfig
    github: GitHubConfig
    google: GoogleConfig
    failure_email: FailureEmailConfig | None
    config_path: pathlib.Path
    repo_root: pathlib.Path

    @property
    def required_local_env_names(self) -> tuple[str, ...]:
        """Return the local env vars needed for local pipeline runs."""

        return (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_API_TOKEN",
            "CF_PAGES_PROJECT",
            "CF_KV_NAMESPACE_ID",
            "IA_ACCESS_KEY",
            "IA_SECRET_KEY",
        )

    @property
    def required_repository_variable_names(self) -> tuple[str, ...]:
        """Return the shared repository variable names used by GitHub Actions."""

        return REPOSITORY_GOOGLE_VARIABLE_NAMES

    @property
    def required_environment_variable_names(self) -> tuple[str, ...]:
        """Return the environment-level variable names used by GitHub Actions."""

        return PIPELINE_ENVIRONMENT_VARIABLE_NAMES

    @property
    def required_environment_secret_names(self) -> tuple[str, ...]:
        """Return the environment-level secret names used by GitHub Actions."""

        secret_names = list(PIPELINE_ENVIRONMENT_SECRET_NAMES)
        if self.failure_email is not None:
            secret_names.extend(self.failure_email.required_secret_names)
        return tuple(secret_names)


def load_pipeline_config(
    repo_root: pathlib.Path,
    *,
    pipeline_id: str | None = None,
    config_path: str | pathlib.Path | None = None,
) -> PipelineConfig:
    """Load and validate a single pipeline config from YAML or JSON.

    Inputs: repo root plus either a pipeline id or an explicit config path.
    Outputs: a validated PipelineConfig object with repo-relative paths attached.
    Edge cases: applies optional local-only overrides from `*.local.yaml` files.
    """

    resolved_config_path = _resolve_config_path(
        repo_root, pipeline_id=pipeline_id, config_path=config_path
    )
    raw_config = _read_config_document(resolved_config_path)
    local_pipeline_override = _load_local_overlay_for_config_path(resolved_config_path)
    shared_config = _load_shared_pipeline_config(repo_root)

    merged_config = _merge_mapping_documents(raw_config, local_pipeline_override)
    merged_config = _merge_pipeline_with_shared_google_config(
        pipeline_config=merged_config,
        shared_config=shared_config,
    )
    return _validate_pipeline_config(
        repo_root=repo_root,
        raw_config=merged_config,
        config_path=resolved_config_path,
        requested_pipeline_id=pipeline_id,
    )


def render_schedule_cron_entries(schedule: ScheduleConfig) -> list[dict[str, str]]:
    """Render workflow `schedule` entries from a local-time window and interval.

    Inputs: validated schedule config.
    Outputs: list of objects with `cron` and `timezone` keys for workflow YAML.
    Edge cases: supports `24:00` as the end of day and windows not evenly divisible by the interval.
    """

    start_minutes = _parse_clock_minutes(schedule.window_start, allow_end_of_day=False)
    end_minutes = _parse_clock_minutes(schedule.window_end, allow_end_of_day=True)
    if end_minutes <= start_minutes:
        raise PipelineConfigError(
            "schedule.window_end must be later than schedule.window_start within the same day."
        )

    interval_minutes = schedule.interval_minutes
    if interval_minutes < MINIMUM_GITHUB_INTERVAL_MINUTES:
        raise PipelineConfigError(
            "Schedule interval is shorter than GitHub Actions supports. "
            f"Expected at least {MINIMUM_GITHUB_INTERVAL_MINUTES} minutes."
        )

    run_times: list[tuple[int, int]] = []
    current_minutes = start_minutes
    while current_minutes < end_minutes:
        hour_of_day = (current_minutes // 60) % 24
        minute_of_hour = current_minutes % 60
        run_times.append((hour_of_day, minute_of_hour))
        current_minutes += interval_minutes

    if not run_times:
        raise PipelineConfigError(
            "Schedule window does not produce any workflow run times."
        )

    grouped_hours_by_minute: dict[int, list[int]] = {}
    for hour_of_day, minute_of_hour in run_times:
        grouped_hours_by_minute.setdefault(minute_of_hour, [])
        if hour_of_day not in grouped_hours_by_minute[minute_of_hour]:
            grouped_hours_by_minute[minute_of_hour].append(hour_of_day)

    cron_entries: list[dict[str, str]] = []
    for minute_of_hour in sorted(grouped_hours_by_minute):
        hours = ",".join(
            str(hour) for hour in sorted(grouped_hours_by_minute[minute_of_hour])
        )
        cron_entries.append(
            {
                "cron": f"{minute_of_hour} {hours} * * *",
                "timezone": schedule.timezone,
            }
        )
    return cron_entries


def validate_branch_ref(branch_ref: str) -> None:
    """Validate that a branch ref is explicit and branch-scoped."""

    if not branch_ref.startswith("refs/heads/"):
        raise PipelineConfigError(
            f"GitHub branch_ref must start with 'refs/heads/'. Received: {branch_ref}"
        )
    if branch_ref == "refs/heads/":
        raise PipelineConfigError("GitHub branch_ref is missing the branch name.")


def build_github_subject(repository: str, branch_ref: str) -> str:
    """Return the expected GitHub OIDC subject for a repository branch."""

    return f"repo:{repository}:ref:{branch_ref}"


def build_workflow_ref(repository: str, workflow_file: str, branch_ref: str) -> str:
    """Return the expected GitHub OIDC workflow_ref claim for one workflow file."""

    return f"{repository}/{workflow_file}@{branch_ref}"


def _resolve_config_path(
    repo_root: pathlib.Path,
    *,
    pipeline_id: str | None,
    config_path: str | pathlib.Path | None,
) -> pathlib.Path:
    """Resolve exactly one config file path from either `--pipeline` or `--config`."""

    if config_path is not None:
        resolved_path = pathlib.Path(config_path)
        if not resolved_path.is_absolute():
            resolved_path = (repo_root / resolved_path).resolve()
        if not resolved_path.exists():
            raise PipelineConfigError(
                f"Pipeline config does not exist: {resolved_path}"
            )
        return resolved_path

    if not pipeline_id:
        raise PipelineConfigError("A pipeline id or explicit config path is required.")

    candidate_paths = [
        repo_root / "pipelines" / f"{pipeline_id}{suffix}"
        for suffix in CONFIG_SEARCH_SUFFIXES
    ]
    existing_paths = [
        candidate_path for candidate_path in candidate_paths if candidate_path.exists()
    ]
    if not existing_paths:
        expected_paths = ", ".join(
            str(path.relative_to(repo_root)) for path in candidate_paths
        )
        raise PipelineConfigError(
            f"No pipeline config found for '{pipeline_id}'. Checked: {expected_paths}"
        )
    if len(existing_paths) > 1:
        matched_paths = ", ".join(
            str(path.relative_to(repo_root)) for path in existing_paths
        )
        raise PipelineConfigError(
            f"Multiple config files match pipeline '{pipeline_id}'. Remove the ambiguity: {matched_paths}"
        )
    return existing_paths[0].resolve()


def _read_config_document(config_path: pathlib.Path) -> dict[str, object]:
    """Parse a YAML or JSON config file into a mapping."""

    suffix = config_path.suffix.lower()
    document_text = config_path.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        parsed_document = yaml.safe_load(document_text)
    elif suffix == ".json":
        parsed_document = json.loads(document_text)
    else:
        raise PipelineConfigError(
            f"Unsupported config format '{suffix}'. Use YAML or JSON."
        )
    if not isinstance(parsed_document, dict):
        raise PipelineConfigError(
            f"Pipeline config must contain a top-level mapping: {config_path}"
        )
    return parsed_document


def _load_local_overlay_for_config_path(
    config_path: pathlib.Path,
) -> dict[str, object] | None:
    """Load the optional `*.local.*` overlay that belongs to one base config file."""

    base_name = config_path.stem
    if base_name.endswith(LOCAL_CONFIG_SUFFIX):
        return None

    candidate_paths = [
        config_path.with_name(f"{base_name}{LOCAL_CONFIG_SUFFIX}{suffix}")
        for suffix in CONFIG_SEARCH_SUFFIXES
    ]
    existing_paths = [
        candidate_path for candidate_path in candidate_paths if candidate_path.exists()
    ]
    if not existing_paths:
        return None
    if len(existing_paths) > 1:
        matched_paths = ", ".join(str(path) for path in existing_paths)
        raise PipelineConfigError(
            "Multiple local override config files match the supported names. "
            f"Remove the ambiguity: {matched_paths}"
        )
    return _read_config_document(existing_paths[0].resolve())


def _load_shared_pipeline_config(repo_root: pathlib.Path) -> dict[str, object] | None:
    """Load the optional shared pipeline config that carries local Google settings.

    Inputs: repository root.
    Outputs: parsed mapping from `pipelines/shared.*`, or `None` when no shared file exists.
    Edge cases: also applies an optional `pipelines/shared.local.*` overlay.
    """

    candidate_paths = [
        repo_root / "pipelines" / f"{SHARED_PIPELINE_CONFIG_BASENAME}{suffix}"
        for suffix in CONFIG_SEARCH_SUFFIXES
    ]
    existing_paths = [
        candidate_path for candidate_path in candidate_paths if candidate_path.exists()
    ]
    if not existing_paths:
        return None
    if len(existing_paths) > 1:
        matched_paths = ", ".join(
            str(path.relative_to(repo_root)) for path in existing_paths
        )
        raise PipelineConfigError(
            "Multiple shared pipeline config files match the supported names. "
            f"Remove the ambiguity: {matched_paths}"
        )

    shared_config_path = existing_paths[0].resolve()
    shared_config = _read_config_document(shared_config_path)
    local_shared_override = _load_local_overlay_for_config_path(shared_config_path)
    return _merge_mapping_documents(shared_config, local_shared_override)


def _merge_pipeline_with_shared_google_config(
    *,
    pipeline_config: dict[str, object],
    shared_config: dict[str, object] | None,
) -> dict[str, object]:
    """Merge shared Google settings into one pipeline config with pipeline-local precedence."""

    merged_config = dict(pipeline_config)
    pipeline_google_config = _read_optional_mapping(pipeline_config, "google")
    shared_google_config = _read_shared_google_mapping(shared_config)

    if pipeline_google_config is None and not shared_google_config:
        return merged_config

    merged_google_config: dict[str, object] = {}
    for field_name in SHARED_GOOGLE_FIELD_NAMES:
        if field_name in shared_google_config:
            merged_google_config[field_name] = shared_google_config[field_name]
    if pipeline_google_config is not None:
        merged_google_config = _merge_mapping_documents(
            merged_google_config, pipeline_google_config
        )
    merged_config["google"] = merged_google_config
    return merged_config


def _read_shared_google_mapping(
    shared_config: dict[str, object] | None,
) -> dict[str, object]:
    """Extract the shared Google mapping from the optional shared config document."""

    if shared_config is None:
        return {}
    shared_google_config = _read_optional_mapping(shared_config, "google")
    if shared_google_config is None:
        return {}
    return dict(shared_google_config)


def _merge_mapping_documents(
    base_mapping: dict[str, object] | None,
    override_mapping: dict[str, object] | None,
) -> dict[str, object]:
    """Merge nested mapping documents recursively with override precedence."""

    if base_mapping is None:
        return dict(override_mapping or {})
    if override_mapping is None:
        return dict(base_mapping)

    merged_mapping: dict[str, object] = dict(base_mapping)
    for key, override_value in override_mapping.items():
        existing_value = merged_mapping.get(key)
        if isinstance(existing_value, dict) and isinstance(override_value, dict):
            merged_mapping[key] = _merge_mapping_documents(
                existing_value, override_value
            )
        else:
            merged_mapping[key] = override_value
    return merged_mapping


def _validate_pipeline_config(
    *,
    repo_root: pathlib.Path,
    raw_config: dict[str, object],
    config_path: pathlib.Path,
    requested_pipeline_id: str | None,
) -> PipelineConfig:
    """Validate every required config field and return a typed config object."""

    pipeline_id = _read_required_string(raw_config, "pipeline_id")
    if requested_pipeline_id and pipeline_id != requested_pipeline_id:
        raise PipelineConfigError(
            f"Pipeline id mismatch. Requested '{requested_pipeline_id}' but config declares '{pipeline_id}'."
        )

    feed_slug = _read_required_string(raw_config, "feed_slug")
    feed_env_file = _read_required_string(raw_config, "feed_env_file")
    ensure_repo_relative_path(repo_root, feed_env_file)

    schedule = _validate_schedule_config(_read_required_mapping(raw_config, "schedule"))
    github = _validate_github_config(
        repo_root,
        pipeline_id,
        _read_required_mapping(raw_config, "github"),
    )
    google = _validate_google_config(
        pipeline_id,
        _read_optional_mapping(raw_config, "google") or {},
    )
    failure_email = _validate_failure_email_config(raw_config.get("failure_email"))

    return PipelineConfig(
        pipeline_id=pipeline_id,
        feed_slug=feed_slug,
        feed_env_file=feed_env_file,
        schedule=schedule,
        github=github,
        google=google,
        failure_email=failure_email,
        config_path=config_path,
        repo_root=repo_root,
    )


def _validate_schedule_config(raw_schedule: dict[str, object]) -> ScheduleConfig:
    """Validate the schedule block including timezone and run window semantics."""

    timezone = _read_required_string(raw_schedule, "timezone")
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise PipelineConfigError(
            f"Invalid schedule.timezone '{timezone}': {exc}"
        ) from exc

    interval_minutes_value = _read_schedule_interval_minutes(raw_schedule)

    window_start = _read_required_string(raw_schedule, "window_start")
    window_end = _read_required_string(raw_schedule, "window_end")
    start_minutes = _parse_clock_minutes(window_start, allow_end_of_day=False)
    end_minutes = _parse_clock_minutes(window_end, allow_end_of_day=True)
    if end_minutes <= start_minutes:
        raise PipelineConfigError(
            "schedule.window_end must be later than schedule.window_start within the same day."
        )
    if end_minutes - start_minutes > MAXIMUM_SCHEDULE_WINDOW_MINUTES:
        raise PipelineConfigError("schedule window cannot exceed 24 hours.")

    schedule = ScheduleConfig(
        timezone=timezone,
        interval_minutes=interval_minutes_value,
        window_start=window_start,
        window_end=window_end,
    )
    render_schedule_cron_entries(schedule)
    return schedule


def _validate_github_config(
    repo_root: pathlib.Path,
    pipeline_id: str,
    raw_github: dict[str, object],
) -> GitHubConfig:
    """Validate GitHub workflow file placement and branch ref semantics."""

    workflow_file = _read_required_string(raw_github, "workflow_file")
    branch_ref = _read_required_string(raw_github, "branch_ref")
    validate_branch_ref(branch_ref)

    workflow_path = ensure_repo_relative_path(repo_root, workflow_file)
    workflow_path_string = workflow_path.relative_to(repo_root).as_posix()
    if not workflow_path_string.startswith(".github/workflows/"):
        raise PipelineConfigError(
            "github.workflow_file must live under .github/workflows/."
        )
    if workflow_path.suffix.lower() not in {".yml", ".yaml"}:
        raise PipelineConfigError(
            "github.workflow_file must use a .yml or .yaml extension."
        )

    environment_name = _read_optional_string(raw_github, "environment")
    if environment_name is None:
        environment_name = pipeline_id
    if not environment_name.strip():
        raise PipelineConfigError(
            "github.environment must be a non-empty string when provided."
        )

    return GitHubConfig(
        workflow_file=workflow_path_string,
        branch_ref=branch_ref,
        environment_name=environment_name.strip(),
    )


def _read_schedule_interval_minutes(raw_schedule: dict[str, object]) -> int:
    """Read exactly one schedule interval field and normalize it to minutes."""

    raw_interval_hours = raw_schedule.get("interval_hours")
    raw_interval_minutes = raw_schedule.get("interval_minutes")

    if raw_interval_hours is not None and raw_interval_minutes is not None:
        raise PipelineConfigError(
            "Specify only one of schedule.interval_hours or schedule.interval_minutes."
        )
    if raw_interval_minutes is not None:
        if not isinstance(raw_interval_minutes, int) or raw_interval_minutes <= 0:
            raise PipelineConfigError(
                "schedule.interval_minutes must be a positive integer."
            )
        return raw_interval_minutes
    if raw_interval_hours is not None:
        if not isinstance(raw_interval_hours, int) or raw_interval_hours <= 0:
            raise PipelineConfigError(
                "schedule.interval_hours must be a positive integer."
            )
        return raw_interval_hours * 60
    raise PipelineConfigError(
        "One of schedule.interval_hours or schedule.interval_minutes is required."
    )


def _validate_google_config(
    pipeline_id: str,
    raw_google: dict[str, object],
) -> GoogleConfig:
    """Validate optional local Google setup config for one pipeline."""

    project_id = _read_optional_string(raw_google, "project_id")
    project_number = _read_optional_string(raw_google, "project_number")
    workload_identity_pool_id = _read_optional_string(
        raw_google, "workload_identity_pool_id"
    )
    workload_identity_provider_id = _read_optional_string(
        raw_google, "workload_identity_provider_id"
    )
    service_account_id = _read_optional_string(raw_google, "service_account_id")

    if project_number is not None and not project_number.isdigit():
        raise PipelineConfigError("google.project_number must contain only digits.")

    shared_field_values = [
        project_id,
        project_number,
        workload_identity_pool_id,
        workload_identity_provider_id,
    ]
    if any(shared_field_values) and not all(shared_field_values):
        raise PipelineConfigError(
            "Local Google setup must define all shared google fields together: "
            "project_id, project_number, workload_identity_pool_id, and "
            "workload_identity_provider_id."
        )

    raw_service_account_email = _read_optional_string(
        raw_google, "service_account_email"
    )
    if raw_service_account_email is not None:
        raise PipelineConfigError(
            "google.service_account_email is no longer allowed in tracked config. "
            "Use google.service_account_id for local setup overrides or the "
            "GitHub environment variable GCP_SERVICE_ACCOUNT_EMAIL for workflows."
        )

    if service_account_id is None:
        service_account_id = _build_default_service_account_id(pipeline_id)

    roles = _read_optional_string_list(raw_google, "roles")
    if not roles:
        roles = list(MINIMUM_PIPELINE_GOOGLE_ROLES)
    for role_name in roles:
        if not role_name.startswith("roles/"):
            raise PipelineConfigError(
                f"Google IAM roles must start with 'roles/'. Received: {role_name}"
            )
        if role_name not in MINIMUM_PIPELINE_GOOGLE_ROLES:
            allowed_roles_text = ", ".join(MINIMUM_PIPELINE_GOOGLE_ROLES)
            raise PipelineConfigError(
                "google.roles contains a role that is broader than the current least-privilege "
                f"pipeline model. Allowed roles: {allowed_roles_text}. Received: {role_name}"
            )

    return GoogleConfig(
        project_id=project_id,
        project_number=project_number,
        workload_identity_pool_id=workload_identity_pool_id,
        workload_identity_provider_id=workload_identity_provider_id,
        service_account_id=service_account_id,
        roles=tuple(roles),
    )


def _validate_failure_email_config(
    raw_failure_email: object,
) -> FailureEmailConfig | None:
    """Validate the optional failure email block when one is configured."""

    if raw_failure_email is None:
        return None
    if not isinstance(raw_failure_email, dict):
        raise PipelineConfigError("failure_email must be a mapping when provided.")

    transport = _read_required_string(raw_failure_email, "transport")
    recipients = tuple(_read_required_string_list(raw_failure_email, "recipients"))
    if not recipients:
        raise PipelineConfigError(
            "failure_email.recipients must contain at least one email."
        )
    subject_prefix = _read_required_string(raw_failure_email, "subject_prefix")

    raw_secret_names = _read_optional_mapping(raw_failure_email, "secret_names") or {}
    smtp_host_secret_name = _read_secret_name_override(raw_secret_names, "smtp_host")
    smtp_port_secret_name = _read_secret_name_override(raw_secret_names, "smtp_port")
    smtp_username_secret_name = _read_secret_name_override(
        raw_secret_names, "smtp_username"
    )
    smtp_password_secret_name = _read_secret_name_override(
        raw_secret_names, "smtp_password"
    )
    smtp_from_secret_name = _read_secret_name_override(raw_secret_names, "smtp_from")

    return FailureEmailConfig(
        transport=transport,
        recipients=recipients,
        subject_prefix=subject_prefix,
        smtp_host_secret_name=smtp_host_secret_name,
        smtp_port_secret_name=smtp_port_secret_name,
        smtp_username_secret_name=smtp_username_secret_name,
        smtp_password_secret_name=smtp_password_secret_name,
        smtp_from_secret_name=smtp_from_secret_name,
    )


def _read_secret_name_override(
    raw_secret_names: dict[str, object], field_name: str
) -> str:
    """Read one optional secret name override or fall back to the default env secret name."""

    override_value = raw_secret_names.get(field_name)
    if override_value is None:
        return FAILURE_EMAIL_SECRET_NAME_DEFAULTS[field_name]
    if not isinstance(override_value, str) or not override_value.strip():
        raise PipelineConfigError(
            f"failure_email.secret_names.{field_name} must be a non-empty string."
        )
    secret_name = override_value.strip()
    _validate_secret_name(secret_name, f"failure_email.secret_names.{field_name}")
    return secret_name


def _build_default_service_account_id(pipeline_id: str) -> str:
    """Build the default dedicated service account id for one pipeline."""

    normalized_pipeline_id = re.sub(r"[^a-z0-9-]", "-", pipeline_id.lower())
    normalized_pipeline_id = re.sub(r"-{2,}", "-", normalized_pipeline_id).strip("-")
    if not normalized_pipeline_id:
        raise PipelineConfigError(
            "pipeline_id must contain letters or digits so a service account id can be derived."
        )
    return f"rss-podcast-{normalized_pipeline_id}"


def _read_required_mapping(
    raw_mapping: dict[str, object], field_name: str
) -> dict[str, object]:
    """Read one required nested mapping field."""

    raw_value = raw_mapping.get(field_name)
    if not isinstance(raw_value, dict):
        raise PipelineConfigError(f"{field_name} must be a mapping.")
    return raw_value


def _read_optional_mapping(
    raw_mapping: dict[str, object],
    field_name: str,
) -> dict[str, object] | None:
    """Read one optional nested mapping field."""

    raw_value = raw_mapping.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise PipelineConfigError(f"{field_name} must be a mapping when provided.")
    return raw_value


def _read_required_string(raw_mapping: dict[str, object], field_name: str) -> str:
    """Read one required non-empty string field."""

    raw_value = raw_mapping.get(field_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise PipelineConfigError(f"{field_name} must be a non-empty string.")
    return raw_value.strip()


def _read_optional_string(
    raw_mapping: dict[str, object], field_name: str
) -> str | None:
    """Read one optional non-empty string field."""

    raw_value = raw_mapping.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise PipelineConfigError(
            f"{field_name} must be a non-empty string when provided."
        )
    return raw_value.strip()


def _read_required_string_list(
    raw_mapping: dict[str, object], field_name: str
) -> list[str]:
    """Read one required list of non-empty strings."""

    raw_value = raw_mapping.get(field_name)
    if not isinstance(raw_value, list) or not raw_value:
        raise PipelineConfigError(f"{field_name} must be a non-empty list of strings.")
    cleaned_values: list[str] = []
    for index, list_value in enumerate(raw_value):
        if not isinstance(list_value, str) or not list_value.strip():
            raise PipelineConfigError(
                f"{field_name}[{index}] must be a non-empty string."
            )
        cleaned_values.append(list_value.strip())
    return cleaned_values


def _read_optional_string_list(
    raw_mapping: dict[str, object], field_name: str
) -> list[str]:
    """Read one optional list of non-empty strings."""

    raw_value = raw_mapping.get(field_name)
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise PipelineConfigError(
            f"{field_name} must be a list of strings when provided."
        )
    cleaned_values: list[str] = []
    for index, list_value in enumerate(raw_value):
        if not isinstance(list_value, str) or not list_value.strip():
            raise PipelineConfigError(
                f"{field_name}[{index}] must be a non-empty string."
            )
        cleaned_values.append(list_value.strip())
    return cleaned_values


def _validate_secret_name(secret_name: str, field_name: str) -> None:
    """Validate that one GitHub environment secret name is uppercase and explicit."""

    if not secret_name.replace("_", "").isalnum() or secret_name != secret_name.upper():
        raise PipelineConfigError(
            f"{field_name} must be uppercase alphanumeric with underscores. Received: {secret_name}"
        )


def _parse_clock_minutes(clock_value: str, *, allow_end_of_day: bool) -> int:
    """Parse an `HH:MM` clock string into minutes since midnight."""

    if not isinstance(clock_value, str) or ":" not in clock_value:
        raise PipelineConfigError(
            f"Invalid time value '{clock_value}'. Expected HH:MM."
        )
    hour_text, minute_text = clock_value.split(":", 1)
    if not hour_text.isdigit() or not minute_text.isdigit():
        raise PipelineConfigError(
            f"Invalid time value '{clock_value}'. Expected HH:MM."
        )

    hour_value = int(hour_text)
    minute_value = int(minute_text)
    if minute_value < 0 or minute_value > 59:
        raise PipelineConfigError(f"Invalid minute in time '{clock_value}'.")
    if allow_end_of_day and hour_value == 24 and minute_value == 0:
        return 24 * 60
    if hour_value < 0 or hour_value > 23:
        raise PipelineConfigError(f"Invalid hour in time '{clock_value}'.")
    return hour_value * 60 + minute_value
