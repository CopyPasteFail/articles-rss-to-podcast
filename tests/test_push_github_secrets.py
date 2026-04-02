"""Tests for GitHub secret sync guardrails."""

from __future__ import annotations

import pathlib

import pytest

from tools.command_utils import CommandExecutionError
from tools.push_github_secrets import ensure_github_environment_exists


def test_ensure_github_environment_exists_requires_repository_name() -> None:
    """Secret sync should fail clearly when repository context is unavailable."""

    with pytest.raises(
        RuntimeError,
        match="Could not resolve the GitHub repository name from this checkout",
    ):
        ensure_github_environment_exists(
            repository_name_with_owner=None,
            environment_name="geektime-he",
            repo_root=pathlib.Path("/tmp/repo"),
        )


def test_ensure_github_environment_exists_wraps_missing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secret sync should direct operators to the setup helper on missing env."""

    def fake_run_command(
        command: list[str], *, cwd: pathlib.Path, check: bool = True
    ) -> str:
        raise CommandExecutionError(command, 1, "HTTP 404: Not Found")

    monkeypatch.setattr("tools.push_github_secrets.run_command", fake_run_command)

    with pytest.raises(
        RuntimeError,
        match=(
            "GitHub environment 'geektime-he' does not exist or is not readable. "
            "Run `scripts/setup-gh-environment.sh --pipeline geektime-he` first."
        ),
    ):
        ensure_github_environment_exists(
            repository_name_with_owner="CopyPasteFail/articles-rss-to-podcast",
            environment_name="geektime-he",
            repo_root=pathlib.Path("/tmp/repo"),
        )
