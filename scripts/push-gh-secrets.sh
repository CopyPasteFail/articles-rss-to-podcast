#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
VENV_PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
MISSING_DEPENDENCY_MODULE="yaml"
BOOTSTRAP_COMMAND="python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt"

PYTHON_BIN=${PYTHON_BIN:-}
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$VENV_PYTHON_BIN" ]]; then
    PYTHON_BIN="$VENV_PYTHON_BIN"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
  else
    echo "Missing Python interpreter. Set PYTHON_BIN or install python3." >&2
    exit 1
  fi
fi

cd "$REPO_ROOT"

if ! "$PYTHON_BIN" -c "import ${MISSING_DEPENDENCY_MODULE}" >/dev/null 2>&1; then
  echo "Missing Python dependency '${MISSING_DEPENDENCY_MODULE}' for $PYTHON_BIN." >&2
  echo "Bootstrap the repo environment first:" >&2
  echo "  cd \"$REPO_ROOT\" && $BOOTSTRAP_COMMAND" >&2
  exit 1
fi

exec "$PYTHON_BIN" -m tools.push_github_secrets "$@"
