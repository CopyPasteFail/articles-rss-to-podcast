"""Shared command and repository helpers used by the automation CLIs."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass

import requests
from dotenv import dotenv_values


ROOT_MARKERS = (".git", "pyproject.toml", "README.md")


class CommandExecutionError(RuntimeError):
    """Wrap subprocess failures with the exact command and captured output."""

    def __init__(self, command: list[str], exit_code: int, output: str) -> None:
        message = (
            f"Command failed with exit code {exit_code}: {' '.join(command)}\n"
            f"{output.strip()}"
        )
        super().__init__(message)
        self.command = command
        self.exit_code = exit_code
        self.output = output


@dataclass(frozen=True)
class RepoContext:
    """Describe the local repository root and resolved GitHub repository name."""

    root: pathlib.Path
    repository: str | None


def command_exists(command_name: str) -> bool:
    """Return whether an executable can be found on the current PATH."""

    return shutil.which(command_name) is not None


def resolve_python_command() -> str | None:
    """Return the preferred Python executable name for this environment."""

    if command_exists("python"):
        return "python"
    if command_exists("python3"):
        return "python3"
    return None


def run_command(
    command: list[str],
    *,
    cwd: pathlib.Path | None = None,
    check: bool = True,
) -> str:
    """Run a command and return stdout while preserving stderr for diagnostics.

    Inputs: a command list, optional cwd, and whether failures should raise.
    Outputs: stripped stdout text, or combined output in the raised exception.
    Edge cases: commands that only write to stderr still return useful output.
    """
    if not command:
        raise ValueError("Command list must contain at least one argument.")

    executable = command[0]
    if os.path.isabs(executable):
        executable_path = pathlib.Path(executable)
        if not executable_path.exists():
            raise FileNotFoundError(f"Command executable does not exist: {executable}")
        if not os.access(executable_path, os.X_OK):
            raise PermissionError(f"Command executable is not executable: {executable}")
        normalized_command = [str(executable_path), *command[1:]]
    else:
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise FileNotFoundError(
                f"Command executable not found on PATH: {executable}"
            )
        normalized_command = [resolved_executable, *command[1:]]

    completed = subprocess.run(
        normalized_command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )  # nosec B603
    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    ).strip()
    if check and completed.returncode != 0:
        raise CommandExecutionError(normalized_command, completed.returncode, output)
    return output


def run_json_command(
    command: list[str],
    *,
    cwd: pathlib.Path | None = None,
) -> object:
    """Run a command and parse its stdout as JSON.

    Inputs: command list and optional cwd.
    Outputs: parsed JSON payload from stdout.
    Edge cases: raises when the command output is not valid JSON.
    """

    output = run_command(command, cwd=cwd)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Command returned invalid JSON: {' '.join(command)}"
        ) from exc


def detect_repo_root(start_path: pathlib.Path | None = None) -> pathlib.Path | None:
    """Walk upward until a repository root marker is found.

    Inputs: an optional starting path. Defaults to the current working directory.
    Outputs: the detected repository root path or None when detection fails.
    Edge cases: supports being called from nested subdirectories within the repo.
    """

    current_path = (start_path or pathlib.Path.cwd()).resolve()
    search_paths = [current_path, *current_path.parents]
    for candidate_path in search_paths:
        if all((candidate_path / marker).exists() for marker in ROOT_MARKERS):
            return candidate_path
    return None


def parse_repository_from_remote(remote_url: str) -> str | None:
    """Convert a GitHub remote URL into the canonical owner/repo string."""

    cleaned_remote_url = remote_url.strip()
    if not cleaned_remote_url:
        return None

    if cleaned_remote_url.startswith("git@github.com:"):
        repository_path = cleaned_remote_url.split(":", 1)[1]
    elif cleaned_remote_url.startswith("https://github.com/"):
        repository_path = cleaned_remote_url.split("https://github.com/", 1)[1]
    else:
        return None

    repository_path = repository_path.removesuffix(".git").strip("/")
    if repository_path.count("/") != 1:
        return None
    return repository_path


def resolve_repository_name_with_owner(repo_root: pathlib.Path) -> str | None:
    """Resolve the GitHub owner/repo using GH CLI first, then git remote fallback.

    Inputs: repo root path.
    Outputs: owner/repo string or None when no GitHub context can be resolved.
    Edge cases: works when GH CLI is not installed by falling back to `git remote`.
    """

    if command_exists("gh"):
        try:
            repository = run_command(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "--jq",
                    ".nameWithOwner",
                ],
                cwd=repo_root,
            ).strip()
            if repository:
                return repository
        except CommandExecutionError:
            pass

    try:
        remote_url = run_command(["git", "remote", "get-url", "origin"], cwd=repo_root)
    except CommandExecutionError:
        return None
    return parse_repository_from_remote(remote_url)


def resolve_repository_numeric_id(
    repo_root: pathlib.Path,
    repository_name_with_owner: str,
) -> str | None:
    """Resolve the GitHub repository's numeric id when GH CLI access is available."""

    if not command_exists("gh"):
        return None
    try:
        repository_id = run_command(
            ["gh", "api", f"repos/{repository_name_with_owner}", "--jq", ".id"],
            cwd=repo_root,
        ).strip()
    except CommandExecutionError:
        return None
    return repository_id or None


def detect_repo_context(start_path: pathlib.Path | None = None) -> RepoContext:
    """Resolve the repo root and best-effort GitHub repository context."""

    repo_root = detect_repo_root(start_path)
    if repo_root is None:
        raise RuntimeError(
            "Could not detect the repository root from the current directory."
        )
    repository = resolve_repository_name_with_owner(repo_root)
    return RepoContext(root=repo_root, repository=repository)


def load_root_env_values(repo_root: pathlib.Path) -> dict[str, str]:
    """Load the repo `.env` file and overlay current process environment values.

    Inputs: repo root path.
    Outputs: merged environment values where process env overrides `.env`.
    Edge cases: missing `.env` returns only current process environment values.
    """

    env_path = repo_root / ".env"
    file_values = {
        key: value
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }
    merged_values = file_values.copy()
    for key, value in os.environ.items():
        merged_values[key] = value
    return merged_values


def ensure_repo_relative_path(
    repo_root: pathlib.Path, relative_path: str
) -> pathlib.Path:
    """Resolve a repo-relative path and reject paths that escape the repository."""

    resolved_path = (repo_root / relative_path).resolve()
    if not resolved_path.is_relative_to(repo_root.resolve()):
        raise ValueError(f"Path '{relative_path}' escapes the repository root.")
    return resolved_path


def resolve_cloudflare_kv_namespace_id(
    *,
    env_values: dict[str, str],
) -> str | None:
    """Resolve the Cloudflare KV namespace id from env or Cloudflare's API."""

    explicit_namespace_id = (env_values.get("CF_KV_NAMESPACE_ID") or "").strip()
    if explicit_namespace_id:
        return explicit_namespace_id

    account_id = (env_values.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    api_token = (env_values.get("CLOUDFLARE_API_TOKEN") or "").strip()
    namespace_name = (
        env_values.get("CF_KV_NAMESPACE_NAME") or "tts-podcast-state"
    ).strip()
    if not account_id or not api_token or not namespace_name:
        return None

    try:
        response = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    results = payload.get("result")
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("title") == namespace_name and isinstance(result.get("id"), str):
            return result["id"]
    return None
