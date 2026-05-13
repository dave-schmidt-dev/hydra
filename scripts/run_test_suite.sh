#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
PYTEST="$ROOT/.venv/bin/pytest"

cd "$ROOT"

MODE="default"
RUN_UI=0
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
    --ui)
      RUN_UI=1
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

echo "Running Python tests in mode: $MODE"
"$PYTEST" "${PYTEST_ARGS[@]}"

if [[ "$RUN_UI" -eq 1 ]]; then
  echo "Running Playwright UI tests"
  pnpm --dir ui_tests test
fi
