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

    python scripts/check_ambient_authority.py

Exit 0 if clean, 1 if any violation is found.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dinify_backend.tenancy.ambient_authority import (  # noqa: E402
    find_violations, format_violations, iter_scanned_files,
)


def main() -> int:
    violations = find_violations(REPO_ROOT)
    if violations:
        for line in format_violations(violations):
            print(line)
        return 1

    scanned = sum(1 for _ in iter_scanned_files(REPO_ROOT))
    print(
        f'Ambient-authority gate: OK — scanned {scanned} customer-plane module(s), '
        'no role-based platform authority.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
