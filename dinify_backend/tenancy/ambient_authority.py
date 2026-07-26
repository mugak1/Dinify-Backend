"""
Standing gate (TENANT-AUTH-00): no customer-plane module reads ``User.roles`` to
grant platform authority.

Pure, git-free, DB-free source analysis, so it is fully unit-testable and runs
without Django settings — the same shape as ``ratchet.py`` / ``check_money_fields``.

WHAT THIS GUARDS. The customer plane used to recognise a Dinify administrator by
a string in ``User.roles``:

    def is_dinify_admin(user):
        return any(role in dinify_roles for role in user.roles)

...and wired that predicate as a cross-tenant bypass at ~15 call sites — a blanket
write gate, an unrestricted ``model.objects.all()`` write queryset, two "do not
scope this list" sentinels, and three admin-only endpoints. Meanwhile
``account_type`` decided which plane an account could log into. Two discriminators,
one identity: an account that passed customer login carrying a legacy role string
got platform reach on a customer JWT.

The predicates, the constants and the grants are gone. This gate keeps them gone.
Cross-tenant reach on the customer plane comes from a ``DelegationGrant`` — bounded
to one restaurant, one scope, one interval — or from nowhere.

WHAT THIS DOES NOT GUARD. It is a source-text check, not a proof of isolation. It
cannot tell that a *new* predicate spelled differently confers cross-tenant
authority, and it says nothing about tenant scoping generally — that is
``tests_tenant_isolation_closure.py``'s job, bounded by ``ASSURANCE.md``. What it
does prove is that the specific retired mechanism has not been reintroduced by
name, which is how this class of thing usually comes back.

SCOPE. Every ``.py`` file in the repository except:

* ``platform_admin_app/`` — the admin plane. It is *entitled* to platform-identity
  vocabulary; it holds the ``PLATFORM_ONLY_ROLES`` denylist, and it authorises on
  ``account_type`` + ``AdminSession`` + elevation, never on roles.
* ``*/migrations/`` — immutable history. Migration ``0011`` legitimately selects
  accounts BY the legacy role strings; rewriting applied migrations is not a fix.
* test modules — a test must be able to name the strings to assert that holding
  one grants nothing. Tests are not authority.
* this module and its CLI — they must spell the forbidden names to look for them.

Unlike the tenant-relation ratchet there is no baseline to shrink: the tree is
clean today, so the gate is a flat zero-tolerance check, and ``ALLOWLIST`` starts
and should stay empty.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Identifiers that named the retired role-based authority mechanism. Any
# reference — import, call, attribute, assignment — is a violation.
RETIRED_NAMES = frozenset({
    'is_dinify_admin',
    'is_dinify_superuser',
    'is_dinify_staff',
    'dinify_roles',
    'DINIFY_ADMIN',
    'DINIFY_ACCOUNT_MANAGER',
})

# The legacy platform-role values. A bare literal in customer-plane code means
# someone is either granting or hand-rolling platform identity here.
PLATFORM_ROLE_LITERALS = frozenset({'dinify_admin', 'dinify_account_manager'})

# ORM lookups over a ``roles`` column. Flagged ONLY when the value being matched
# is platform-flavoured — ``roles__contains=[DINIFY_ADMIN]`` and
# ``roles__icontains='dinify'`` are how the retired admin CC-list and staff-count
# selected platform users, while ``roles__contains=[RESTAURANT_OWNER]`` is an
# ordinary, correct query against ``RestaurantEmployee.roles``. The distinction is
# the value, not the lookup: the models cannot be told apart from source text, but
# the intent can.
ROLES_ORM_LOOKUP_PREFIX = 'roles__'
PLATFORM_ROLE_VALUE_MARKER = 'dinify'

# Directories never scanned. ``platform_admin_app`` is the admin plane;
# ``migrations`` is immutable history. The rest keep a stray virtualenv or build
# directory out of the scan.
PRUNE_DIRS = frozenset({
    'platform_admin_app', 'migrations', '__pycache__', 'node_modules',
    'site-packages', 'venv', 'env', 'staticfiles', 'media',
})

# This module and its CLI have to spell the forbidden names in order to find them.
SELF_EXEMPT = frozenset({
    'dinify_backend/tenancy/ambient_authority.py',
    'scripts/check_ambient_authority.py',
})

# Deliberate, reviewed exceptions: {'relative/path.py': {'NAME', ...}}.
#
# EMPTY, AND MEANT TO STAY THAT WAY. There is no legitimate customer-plane reason
# to name the retired mechanism. If you are about to add an entry, the thing to
# examine is the code, not this dict.
ALLOWLIST: dict[str, set[str]] = {}


def is_test_module(relative_path: str) -> bool:
    """
    Whether a repo-relative path is a test module (excluded from the scan).

    The repo's convention is ``tests.py`` / ``tests_<thing>.py`` in the app root,
    and this matches exactly that. Deliberately NOT a ``test``-prefix match:
    ``dinify_backend/test_settings.py`` is configuration, not a test module, and
    must stay inside the scan.
    """
    name = relative_path.rsplit('/', 1)[-1]
    return name == 'tests.py' or name.startswith('tests_')


def iter_scanned_files(root: Path):
    """Yield every customer-plane ``.py`` file under *root*, as absolute paths."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith('.')
        ]
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            path = Path(dirpath) / filename
            relative = path.relative_to(root).as_posix()
            if relative in SELF_EXEMPT or is_test_module(relative):
                continue
            yield path


def find_violations_in_source(source: str, relative_path: str) -> list:
    """
    Return ``[(lineno, name, reason)]`` for one module's source.

    Split from the filesystem walk so the meta-test can feed it synthetic source
    and prove the gate actually fires.

    A file that does not parse yields no violations: a syntax error is ``django
    check``'s to report, and guessing at broken source would only produce noise.
    """
    allowed = ALLOWLIST.get(relative_path, set())
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    violations = []

    def flag(lineno, name, reason):
        if name not in allowed:
            violations.append((lineno, name, reason))

    for node in ast.walk(tree):
        # `from x import is_dinify_admin`, including aliased imports.
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in RETIRED_NAMES:
                    flag(
                        node.lineno, alias.name,
                        'imports the retired role-based authority predicate/constant',
                    )
        # Bare references: calls, attribute access, assignment targets.
        elif isinstance(node, ast.Name) and node.id in RETIRED_NAMES:
            flag(
                node.lineno, node.id,
                'references the retired role-based authority predicate/constant',
            )
        elif isinstance(node, ast.Attribute) and node.attr in RETIRED_NAMES:
            flag(
                node.lineno, node.attr,
                'references the retired role-based authority predicate/constant',
            )
        # Bare platform-role string literals.
        elif isinstance(node, ast.Constant) and node.value in PLATFORM_ROLE_LITERALS:
            flag(
                node.lineno, node.value,
                'hard-codes a platform-only role string; platform identity is '
                'account_type on the admin plane',
            )
        # `.filter(roles__contains=[...])` — the lookup arrives as a keyword name.
        # Only platform-flavoured values are a violation (see the constant above).
        elif isinstance(node, ast.keyword) and node.arg:
            if node.arg.startswith(ROLES_ORM_LOOKUP_PREFIX) and _matches_platform_value(node.value):  # noqa: E501
                flag(
                    node.lineno, node.arg,
                    'selects users by a platform role through the ORM; roles '
                    'carries restaurant roles only and is not an authority signal',
                )

    return sorted(set(violations))


def _matches_platform_value(node) -> bool:
    """
    Whether an ORM lookup's value node matches on a platform role.

    True for ``'dinify_admin'``, ``['dinify_admin']`` and the substring form
    ``'dinify'``; False for restaurant roles and for anything non-literal (a
    variable is caught by the name/literal rules wherever it was defined).
    """
    if isinstance(node, ast.Constant):
        return (
            isinstance(node.value, str)
            and PLATFORM_ROLE_VALUE_MARKER in node.value.lower()
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_matches_platform_value(element) for element in node.elts)
    return False


def find_violations(root: Path = REPO_ROOT) -> list:
    """Return ``[(relative_path, lineno, name, reason)]`` across the customer plane."""
    found = []
    for path in sorted(iter_scanned_files(root)):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding='utf-8', errors='replace')
        for lineno, name, reason in find_violations_in_source(source, relative):
            found.append((relative, lineno, name, reason))
    return found


def format_violations(violations) -> list:
    """Human-readable report lines for ``violations`` (empty when clean)."""
    if not violations:
        return []
    lines = [
        'Ambient-authority gate: FAIL — the customer plane must not read '
        'User.roles for platform authority:',
        '',
    ]
    for relative, lineno, name, reason in violations:
        lines.append(f'  {relative}:{lineno}: {name} — {reason}')
    lines.extend([
        '',
        f'{len(violations)} violation(s). Platform staff are identified by '
        'User.account_type and live on the admin plane; a delegated principal is '
        'the only way one reaches tenant data.',
    ])
    return lines
