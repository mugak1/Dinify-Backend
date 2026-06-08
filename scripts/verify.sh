#!/usr/bin/env bash
#
# scripts/verify.sh — local pre-PR verification for the Dinify backend.
#
# Runs the same checks as CI (.github/workflows/ci.yml), in the same order,
# against dinify_backend.test_settings. This script is the single committed
# source of truth for these checks — the /dinify-check command defers to it.
# If a command changes, change it here (and CI).
#
#   1. django check
#   2. makemigrations --check --dry-run  (fails if a model changed w/o a migration)
#   3. test                              (full Django test suite)
#
# test_settings falls back to SQLite in-memory, so no local Postgres is
# needed. Export DATABASE_ENGINE/NAME/USER/... to run against another database,
# or VERIFY_SETTINGS=<module> to use a different settings module.
#
# This is a manual, post-change pre-PR gate — run it after making changes and
# paste the output into the PR. It is intentionally NOT wired as a hook.
#
#   ./scripts/verify.sh
#
# Every step runs even if an earlier one fails, so you see all problems at
# once; the script exits non-zero if any step failed. Assumes dependencies are
# installed (pip install -r requirements.txt).

set -uo pipefail

# Run from the repo root regardless of where the script is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Always pass --settings explicitly so a stray DJANGO_SETTINGS_MODULE in the
# shell can't hijack the run (e.g. point it at production settings).
SETTINGS="${VERIFY_SETTINGS:-dinify_backend.test_settings}"

# Prefer `python`, fall back to `python3`; override with PYTHON=... if needed.
PYTHON="${PYTHON:-}"
if [ -z "${PYTHON}" ]; then
  if command -v python >/dev/null 2>&1; then PYTHON=python; else PYTHON=python3; fi
fi

failures=()

run_step() {
  local label="$1"; shift
  echo
  echo "=================================================================="
  echo ">>> ${label}"
  echo "=================================================================="
  if "$@"; then
    echo "--- ${label}: PASS"
  else
    echo "--- ${label}: FAIL"
    failures+=("${label}")
  fi
}

run_step "django check"         "${PYTHON}" -m django check --settings="${SETTINGS}"
run_step "makemigrations check" "${PYTHON}" -m django makemigrations --check --dry-run --settings="${SETTINGS}"
run_step "tests"                "${PYTHON}" -m django test --settings="${SETTINGS}" --verbosity=2

echo
echo "=================================================================="
if [ "${#failures[@]}" -eq 0 ]; then
  echo "All checks passed."
  exit 0
fi
echo "FAILED: ${failures[*]}"
echo "Fix the above before opening a PR."
exit 1
