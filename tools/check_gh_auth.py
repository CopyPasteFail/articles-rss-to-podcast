"""Validate GitHub CLI authentication and repository context for this repo."""

from __future__ import annotations

import argparse
import sys

from tools.command_utils import (
    CommandExecutionError,
    command_exists,
    detect_repo_context,
    run_command,
)


def check_github_cli_authentication() -> tuple[bool, list[str]]:
    """Validate `gh` presence, login state, and repository context.

    Inputs: none.
    Outputs: tuple of success flag and human-readable status lines.
    Edge cases: handles missing CLI, missing auth, and non-GitHub repo remotes.
    """

    status_lines: list[str] = []

    if not command_exists("gh"):
        status_lines.append("MISSING: gh CLI is not installed or not on PATH.")
        status_lines.append(
            "Next action: install GitHub CLI, then run `gh auth login`."
        )
        return False, status_lines

    try:
        repo_context = detect_repo_context()
    except RuntimeError as exc:
        status_lines.append(f"MISCONFIGURED: {exc}")
        status_lines.append(
            "Next action: run this command from inside the repository checkout."
        )
        return False, status_lines
    if repo_context.repository is None:
        status_lines.append(
            "MISCONFIGURED: Could not resolve a GitHub repository from this checkout."
        )
        status_lines.append(
            "Next action: verify the `origin` remote points to a GitHub repository."
        )
        return False, status_lines

    try:
        run_command(["gh", "auth", "status"], cwd=repo_context.root)
    except CommandExecutionError as exc:
        status_lines.append("MISSING: `gh auth status` failed.")
        if exc.output:
            status_lines.append(exc.output.strip())
        status_lines.append("Next action: run `gh auth login`.")
        return False, status_lines

    status_lines.append(
        f"PASS: gh auth is active for repository {repo_context.repository}."
    )
    return True, status_lines


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for GitHub CLI auth verification."""

    parser = argparse.ArgumentParser(
        description="Validate GitHub CLI authentication and repository context.",
    )
    parser.parse_args(argv)

    is_valid, status_lines = check_github_cli_authentication()
    for status_line in status_lines:
        print(status_line)
    return 0 if is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
