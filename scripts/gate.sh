#!/usr/bin/env bash
# gate.sh -- the local gate: lint, tests, public-repo guard. Run before every
# push:
#   bash scripts/gate.sh     (or: make gate)
# Runs every step even when one fails, then exits 1 if any step failed.
# Lint rules live in server/pyproject.toml ([tool.ruff]); RUF100 there also
# fails on a `# noqa` that no longer silences anything.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
failed=()

run() {
  local name="$1"
  shift
  echo "gate: --- $name"
  if "$@"; then
    echo "gate: ok   $name"
  else
    echo "gate: FAIL $name"
    failed+=("$name")
  fi
}

# Prefer the venv when it has the tool, else uv (same order as `make test`).
tool() {
  local bin="$1"
  shift
  if [ -x "$ROOT/server/.venv/bin/$bin" ]; then
    (cd "$ROOT/server" && ".venv/bin/$bin" "$@")
  elif command -v uv >/dev/null 2>&1; then
    (cd "$ROOT/server" && uv run "$bin" "$@")
  else
    echo "gate: ERROR: neither server/.venv/bin/$bin nor uv found. Run 'make install' or install uv." >&2
    return 1
  fi
}

run lint tool ruff check .
run test tool pytest -q
run check-public bash "$ROOT/scripts/check-public.sh"

if [ "${#failed[@]}" -gt 0 ]; then
  echo "gate: FAILED: ${failed[*]}"
  exit 1
fi
echo "gate: all steps passed"
