#!/bin/bash
set -euo pipefail

# Only run inside Claude Code on the web (remote) sessions. On a local machine the
# developer manages their own environment, so do nothing.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Job 1 — refresh origin/main so any feature branch is cut from the real, up-to-date
# main, not a stale freshly-cloned local ref. Updates only the remote-tracking ref:
# never touches local `main` or the working tree, so it is safe with uncommitted work.
# Tolerate transient network errors so a flaky fetch never blocks the session.
git fetch origin main --quiet 2>/dev/null || true

# Job 2 — install Python deps only when missing, so warm/cached containers skip the
# ~30s install. "django importable" is the marker (the pip analog of node_modules).
# Installs into the container's python3 (root + global site-packages writable), matching
# CI's no-venv approach so python3 / scripts/verify.sh work in every later shell.
# --ignore-installed: the base image ships some deps as Debian packages without RECORD
# files (e.g. PyJWT) that pip cannot uninstall; installing the pinned versions over them
# (they shadow the Debian ones via the higher-precedence site-packages) sidesteps the
# "Cannot uninstall ..." failures a plain install hits on a cold container.
if ! python3 -c "import django" >/dev/null 2>&1; then
  python3 -m pip install --ignore-installed -r requirements.txt
fi

# The base image's system `cryptography` (Debian) is missing its CFFI backend
# (`_cffi_backend`), which makes PyJWT's `import jwt` panic — that would break the
# SimpleJWT auth tests `scripts/verify.sh` runs. Supply the missing backend. Gated by
# its own import check (no-op once present) and non-fatal so a hiccup here never blocks
# session start — the core deps above are what matter.
if ! python3 -c "import _cffi_backend" >/dev/null 2>&1; then
  python3 -m pip install --ignore-installed cffi || true
fi
