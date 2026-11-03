#!/usr/bin/env bash
# Every check in one command: lint, format, types, tests. Extra args go to pytest.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff check"
uv run ruff check .
echo "== ruff format --check"
uv run ruff format --check .
echo "== mypy"
uv run mypy src
echo "== pytest"
uv run pytest "$@"
