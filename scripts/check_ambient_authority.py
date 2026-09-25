#!/usr/bin/env python3
"""Static guard (TENANT-AUTH-00): no ambient role-based admin authority.

The customer plane must not read ``User.roles`` to grant platform authority. A
Dinify administrator used to be defined as "holds 'dinify_admin' in User.roles",
and that predicate was wired as a cross-tenant bypass across the portal — while
``account_type`` separately decided which plane an account could log into. Two
discriminators for one identity is how an ordinary customer JWT ended up able to
carry platform reach.

The predicates and the grants are gone. This script is the committed backstop that
fails the build if they come back by name. Cross-tenant reach on this plane now
comes from a ``DelegationGrant`` or from nowhere.

The scan logic lives in ``dinify_backend.tenancy.ambient_authority`` (git-free,
DB-free, and covered by ``tests_ambient_authority.py``, which proves it actually
fires rather than only that the tree is currently clean). See that module for the
exact scope and the four documented exclusions.

Unlike the tenant-relation ratchet this is NOT a shrinking baseline — the tree is
clean, so it is a flat zero-tolerance check with an empty allowlist. It is also
permanent, not self-terminating: there is no future state in which reintroducing
the mechanism becomes acceptable.

Usage (no DB, no Django settings required)::

    python scripts/check_ambient_authority.py              # self-test, then the scan
    python scripts/check_ambient_authority.py --self-test  # the self-test alone

The default invocation always runs the self-test first, so a detector that stopped
detecting cannot report a clean customer plane.

Exit 0 complete and clean · 1 violation · 2 INCOMPLETE — a module could not be
read or parsed, a directory could not be listed, or nothing was found; never clean ·
3 the self-test failed, so no scan was trusted.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dinify_backend.tenancy.ambient_authority import (  # noqa: E402
    format_violations, scan, self_test,
)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    status = self_test()
    if status != 0 or '--self-test' in argv:
        return status

    result = scan(REPO_ROOT)
    if result.violations:
        for line in format_violations(result.violations):
            print(line)
        for line in result.incomplete:
            print(f'  (the scan was also incomplete: {line})')
        return 1
    if result.incomplete:
        print('Ambient-authority gate: INCOMPLETE — the scan did not cover what it '
              'claims to, so this is NOT a clean result:')
        for line in result.incomplete:
            print(f'  {line}')
        return 2

    print(
        f'Ambient-authority gate: OK — analysed {len(result.files)} customer-plane '
        'module(s) completely, no role-based platform authority.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
