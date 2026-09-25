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

AN INCOMPLETE SCAN IS NEVER CLEAN (D08 B2.3). A module that cannot be read or parsed
is not analysed, so it cannot be called clean: it used to return no violations on the
theory that a syntax error is ``django check``'s to report. It is not — ``django
check`` never imported 46 of the 244 modules this gate scanned at d4aacbd (``wsgi.py``,
``wsgi_admin.py``, ``settings_admin.py``, ``urls_admin.py``, management commands,
``restaurants_app/controllers/lifecycle.py`` among them), so a module that reintroduced
the retired predicate AND failed to parse passed both. Now: an unparseable or
unreadable module, a directory that cannot be listed, and a scope with no module at
all each make the scan INCOMPLETE, which the CLI reports and exits 2 on. Symlinked
directories are not followed, which is not a gap: a link's target is either inside
the tree, where the walk reaches it at its real path, or outside it, where it is not
repository source.
"""
from __future__ import annotations

import ast
import os
import tempfile
from dataclasses import dataclass, field
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


class UnanalysableSource(ValueError):
    """A module that could not be parsed. Never read as clean."""


class IncompleteScan(RuntimeError):
    """The scan did not cover its whole scope; its violation list proves nothing."""


@dataclass
class ScanResult:
    files: list = field(default_factory=list)       # repo-relative paths analysed
    violations: list = field(default_factory=list)  # (relative_path, lineno, name, reason)
    incomplete: list = field(default_factory=list)  # human-readable reasons


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


def iter_scanned_files(root: Path, incomplete: list | None = None, walk=os.walk):
    """Yield every customer-plane ``.py`` file under *root*, as absolute paths.

    A directory that cannot be listed is appended to ``incomplete`` rather than
    silently skipped (``os.walk`` ignores listing errors unless told otherwise).
    """
    incomplete = [] if incomplete is None else incomplete

    def on_error(exc):
        where = getattr(exc, 'filename', None) or '?'
        incomplete.append(f'could not list {where}: {exc.strerror or exc}')

    for dirpath, dirnames, filenames in walk(root, onerror=on_error):
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

    ``source`` may be ``bytes`` (so a coding cookie and an undecodable file behave as
    they would for the interpreter) or ``str``. A module that does not parse raises
    ``UnanalysableSource``: it has not been analysed, so it cannot be reported clean,
    and no other enforcing check is guaranteed to import it (see the module docstring).
    """
    allowed = ALLOWLIST.get(relative_path, set())
    try:
        tree = ast.parse(source, filename=relative_path)
    except (SyntaxError, ValueError) as exc:
        raise UnanalysableSource(f'{relative_path} does not parse: {exc}') from None

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


def scan(root: Path = REPO_ROOT, walk=os.walk) -> ScanResult:
    """Analyse the customer plane, recording anything that could not be analysed."""
    result = ScanResult()
    for path in sorted(iter_scanned_files(root, result.incomplete, walk=walk)):
        relative = path.relative_to(root).as_posix()
        result.files.append(relative)
        try:
            source = path.read_bytes()
        except OSError as exc:
            result.incomplete.append(f'{relative} could not be read: {exc.strerror or exc}')
            continue
        try:
            found = find_violations_in_source(source, relative)
        except UnanalysableSource as exc:
            result.incomplete.append(f'{relative} could not be analysed: {exc}')
            continue
        for lineno, name, reason in found:
            result.violations.append((relative, lineno, name, reason))
    if not result.files and not result.incomplete:
        result.incomplete.append(
            'no customer-plane module was found — an empty scope is not a clean one')
    return result


def find_violations(root: Path = REPO_ROOT) -> list:
    """Return ``[(relative_path, lineno, name, reason)]`` across the customer plane.

    Raises ``IncompleteScan`` when any in-scope module could not be analysed, so a
    caller asserting "no violations" can never be satisfied by a partial scan.
    """
    result = scan(root)
    if result.incomplete:
        raise IncompleteScan('; '.join(result.incomplete))
    return result.violations


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


# ---------------------------------------------------------------------------
# Self-test: run by the CLI before every scan, so a detector that stopped
# detecting cannot report a clean customer plane.
# ---------------------------------------------------------------------------

_LEGACY = 'dinify' + '_admin'  # assembled so this module's own source stays readable

_FIRES = {
    'an imported predicate': 'from users_app.controllers.permissions_check import is_dinify_admin\n',
    'an aliased import': 'from x import is_dinify_admin as allowed\n',
    'a called predicate': 'def gate(user):\n    return is_dinify_admin(user)\n',
    'an attribute reference': 'def gate(user):\n    return permissions.is_dinify_superuser(user)\n',
    'a retired constant': 'ROLE = DINIFY_ACCOUNT_MANAGER\n',
    'a platform-role literal': f'ADMIN_ROLES = [{_LEGACY!r}]\n',
    'a platform-role ORM lookup': f'q = User.objects.filter(roles__contains=[{_LEGACY!r}])\n',
    'a substring roles lookup': "q = User.objects.filter(roles__icontains='dinify')\n",
}
_QUIET = {
    'a restaurant-role ORM lookup': "q = RestaurantEmployee.objects.filter(roles__contains=['owner'])\n",
    'unrelated source': 'def add(a, b):\n    return a + b\n',
    'the name in prose only': '# is_dinify_admin was retired; see TENANT-AUTH-00\n',
}


def self_test(log=print) -> int:
    """Return 0 when every case behaves, 3 otherwise (printing each failure)."""
    failures = []
    for label, source in _FIRES.items():
        if not find_violations_in_source(source, 'some_app/module.py'):
            failures.append(f'did not flag {label}')
    for label, source in _QUIET.items():
        if find_violations_in_source(source, 'some_app/module.py'):
            failures.append(f'flagged {label}')
    try:
        find_violations_in_source('def is_dinify_admin(:\n', 'some_app/broken.py')
        failures.append('an unparseable module was analysed instead of refused')
    except UnanalysableSource:
        pass
    if ALLOWLIST:
        failures.append('the allowlist is not empty')
    for path, excluded in (('users_app/tests.py', True), ('app/tests_x.py', True),
                           ('dinify_backend/test_settings.py', False),
                           ('app/latest_thing.py', False)):
        if is_test_module(path) is not excluded:
            failures.append(f'test-module classification is wrong for {path}')

    # Discovery on a real temporary tree: the documented exclusions hold, an
    # unparseable module and an empty scope are incomplete, a listing error is reported.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative, text in (
            ('some_app/views.py', _FIRES['an imported predicate']),
            ('platform_admin_app/services.py', _FIRES['an imported predicate']),
            ('some_app/migrations/0011_flip.py', _FIRES['a platform-role literal']),
            ('some_app/tests_boundary.py', _FIRES['a platform-role literal']),
            ('scripts/tool.py', _QUIET['unrelated source']),
        ):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text, encoding='utf-8')
        found = scan(root)
        if {v[0] for v in found.violations} != {'some_app/views.py'} or found.incomplete:
            failures.append(f'discovery: expected exactly some_app/views.py, got {found}')
        (root / 'scripts/tool.py').write_text('def (:\n', encoding='utf-8')
        if not scan(root).incomplete:
            failures.append('discovery: an unparseable module did not make the scan incomplete')
        empty = root / 'nothing'
        empty.mkdir()
        if not scan(empty).incomplete:
            failures.append('discovery: an empty scope was not incomplete')

        def failing_walk(top, onerror=None):
            onerror(PermissionError(13, 'Permission denied', str(Path(top) / 'locked')))
            yield str(top), [], []
        if not scan(root, walk=failing_walk).incomplete:
            failures.append('discovery: a listing error was silently skipped')

    if failures:
        for line in failures:
            log(f'  self-test FAIL: {line}')
        log(f'Ambient-authority gate self-test: {len(failures)} case(s) failed — the '
            'detector cannot be trusted, so no scan was run.')
        return 3
    log(f'Ambient-authority gate self-test: OK — {len(_FIRES)} forms flagged, '
        f'{len(_QUIET)} controls quiet, exclusions, unparseable and empty scopes checked.')
    return 0
