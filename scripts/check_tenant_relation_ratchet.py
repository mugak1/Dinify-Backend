#!/usr/bin/env python3
"""
Ratchet guard CLI (TENANT-STRUCT-00): the tenant-relation baseline may only SHRINK.

``dinify_backend/tenancy/baseline.txt`` lists writable relational
``serializer::field`` pairs not yet classified in a serializer's
``Meta.tenant_relations`` — temporary legacy debt from the ``fields='__all__'``
era. The tenancy meta-test forbids leaving a discovered relation *neither*
classified *nor* baselined; this guard closes the remaining escape hatch — adding
a NEW relation to the baseline instead of classifying it — by comparing the
committed baseline against the SAME file on the base branch and failing on any
addition.

The comparison logic lives in ``dinify_backend/tenancy/git_ratchet.check_ratchet``
(so it is covered by real git integration tests). It FAILS CLOSED in CI: if this
runs in CI and cannot read the base baseline, it exits non-zero — a ratchet that
passes when it can't do its job silently protects nothing.

TEMPORARY / SELF-TERMINATING: when ``baseline.txt`` reaches ZERO entries, DELETE
the baseline file, this script, and its ci.yml / verify.sh steps — the meta-test
alone then enforces that every writable relation is classified, nowhere left to
hide. Do not let this become permanent CI furniture.

Usage (no DB, no Django settings needed)::

    python scripts/check_tenant_relation_ratchet.py

Exit 0 if no additions (or bootstrap / local-skip), 1 on additions or a
can't-read-base failure in CI.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dinify_backend.tenancy.git_ratchet import check_ratchet, in_ci  # noqa: E402
from dinify_backend.tenancy.ratchet import BASELINE_PATH  # noqa: E402

BASELINE_REL = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()


def main() -> int:
    base_ref = os.environ.get("GITHUB_BASE_REF") or "main"
    code, lines = check_ratchet(REPO_ROOT, BASELINE_REL, base_ref, in_ci())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
