#!/usr/bin/env python3
"""Thin wrapper for one-pipeline GitHub workflow generation."""

from __future__ import annotations

import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    """Run the workflow generator from the repository root."""

    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))
    from tools.generate_workflow import main as generate_workflow_main

    return generate_workflow_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
