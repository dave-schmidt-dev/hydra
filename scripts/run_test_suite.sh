#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
PYTEST="$ROOT/.venv/bin/pytest"

cd "$ROOT"

MODE="default"
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --fast)
      MODE="fast"
      PYTEST_ARGS+=(-m "not integration and not e2e")
      ;;
    --integration)
      MODE="integration"
      PYTEST_ARGS+=(-m "integration")
      ;;
    --e2e)
      MODE="e2e"
      PYTEST_ARGS+=(-m "e2e")
      ;;
    *)
      PYTEST_ARGS+=("$arg")
      ;;
  esac
done

if [[ ! -x "$PYTEST" ]]; then
  echo "pytest not found at $PYTEST. Run 'uv sync' first." >&2
  exit 1
fi

echo "Running tests in mode: $MODE"
exec "$PYTEST" "${PYTEST_ARGS[@]}"
