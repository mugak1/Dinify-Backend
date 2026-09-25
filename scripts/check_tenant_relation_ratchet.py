#!/usr/bin/env python3
"""
Ratchet guard CLI (TENANT-STRUCT-00): the tenant-relation baseline may only SHRINK.

``dinify_backend/tenancy/baseline.txt`` lists writable relational
``serializer::field`` pairs not yet classified in a serializer's
``Meta.tenant_relations`` — temporary legacy debt from the ``fields='__all__'``
era. The tenancy meta-test forbids leaving a discovered relation *neither*
classified *nor* baselined; this guard closes the remaining escape hatch — adding
a NEW relation to the baseline instead of classifying it — by comparing the
committed baseline against the SAME file at the comparison BASE and failing on any
addition.

The base is event-aware (``git_ratchet.resolve_base_ref``): a pull request compares
against its target branch (``GITHUB_BASE_REF``); a PUSH compares against the commit
BEFORE the push (``github.event.before`` via ``GITHUB_EVENT_BEFORE`` — set in
ci.yml), NOT the branch tip, which on a push already contains the new commit and
would hide the addition; locally it falls back to ``main``. Under branch protection
(PR-only merges to ``main``) the PR comparison is the authoritative gate; the push
comparison is defense-in-depth for direct pushes.

The comparison logic lives in ``dinify_backend/tenancy/git_ratchet.check_ratchet``
(so it is covered by real git integration tests). It FAILS CLOSED in CI: if this
runs in CI and cannot read the base baseline, it exits non-zero — a ratchet that
passes when it can't do its job silently protects nothing.

TEMPORARY / SELF-TERMINATING: when ``baseline.txt`` reaches ZERO entries, DELETE
the baseline file, this script, and its ci.yml / verify.sh steps — the meta-test
alone then enforces that every writable relation is classified, nowhere left to
hide. Do not let this become permanent CI furniture.

Usage (no DB, no Django settings needed)::

    python scripts/check_tenant_relation_ratchet.py              # self-test, then the check
    python scripts/check_tenant_relation_ratchet.py --self-test  # the self-test alone

The default invocation always runs the pure self-test first, so a comparator that
stopped detecting additions cannot report a clean baseline.

Exit 0 compared-and-clean, or a LABELLED bootstrap / local skip (the output says
``NO COMPARISON PERFORMED``); 1 on additions; 2 INCOMPLETE — in CI, any state in which
nothing was compared (an unreadable base, a base that is the commit under test, a push
with no pre-push commit, a moved baseline), and anywhere, a missing baseline file; 3 the
self-test failed, so no comparison was trusted.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dinify_backend.tenancy.git_ratchet import (  # noqa: E402
    check_ratchet, in_ci, resolve_base_ref, self_test,
)
from dinify_backend.tenancy.ratchet import BASELINE_PATH  # noqa: E402

BASELINE_REL = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    status = self_test()
    if status != 0 or "--self-test" in argv:
        return status
    base_ref = resolve_base_ref(os.environ)
    code, lines = check_ratchet(REPO_ROOT, BASELINE_REL, base_ref, in_ci())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
