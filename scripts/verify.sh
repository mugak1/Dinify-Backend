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
#   3. money-field guard                 (fails if a monetary model field is a FloatField)
#   4. ambient-authority gate            (fails if the customer plane reads User.roles
#                                         for platform authority)
#   5. tenant-relation ratchet           (fails if the tenant-relation baseline grew)
#   6. tenant-isolation closure gate     (focused adversarial boundary suite, fail-fast;
#                                         includes the delegated-session auth path)
#   7. test                              (full Django test suite)
#
# test_settings falls back to SQLite in-memory for the fast checks, but the
# tenant-isolation closure gate and the full suite include relationship-integrity
# tests that use JSONField `__contains` (Table/MenuItem deletion_blockers), which
# SQLite does NOT support — run those against Postgres (as CI does). Export
# DATABASE_ENGINE=django.db.backends.postgresql plus DATABASE_NAME/USER/PASSWORD/
# HOST/PORT to point at a local Postgres, or VERIFY_SETTINGS=<module> for another
# settings module. CI (Postgres 15) is the authoritative full-suite gate.
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
run_step "money-field guard"    "${PYTHON}" scripts/check_money_fields.py
run_step "ambient-authority gate" "${PYTHON}" scripts/check_ambient_authority.py
run_step "tenant-relation ratchet" "${PYTHON}" scripts/check_tenant_relation_ratchet.py
# Fail-fast adversarial tenant-isolation closure gate (TENANT-ISO-PR6A): the
# focused boundary matrix + the deep capability / relationship / concurrency /
# write-surface suites it builds on. Runs BEFORE the full suite so a broken
# tenant boundary fails early. This does NOT replace the full suite below.
run_step "tenant-isolation closure gate" "${PYTHON}" -m django test \
  dinify_backend.tenancy.tests_tenant_isolation_closure \
  dinify_backend.tenancy.tests_ambient_authority \
  restaurants_app.tests_diner_capability \
  restaurants_app.tests_menu_relationship_integrity \
  restaurants_app.tests_menu_relationships_concurrency \
  restaurants_app.tests_write_surface_tenancy \
  platform_admin_app.tests_delegated_session \
  --settings="${SETTINGS}" --verbosity=2 --timing
run_step "tests"                "${PYTHON}" -m django test --settings="${SETTINGS}" --verbosity=2 --timing

echo
echo "=================================================================="
if [ "${#failures[@]}" -eq 0 ]; then
  echo "All checks passed."
  exit 0
fi
echo "FAILED: ${failures[*]}"
echo "Fix the above before opening a PR."
exit 1
