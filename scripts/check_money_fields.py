#!/usr/bin/env python3
"""Static guard: monetary model fields must be DecimalField, never FloatField.

Money is a silent-correctness footgun when stored as a binary float: 0.1 + 0.2
does not equal 0.3, and DRF serialises a DecimalField as a string the frontend
wraps in Number() — a FloatField quietly breaks both. The repo already mandates
DecimalField for money (CLAUDE.md, "Monetary Fields — CRITICAL"); this script is
the committed backstop that fails the build if anyone reintroduces a money
FloatField. It is a correctness guard, not a style rule.

Scope: files named exactly ``models.py``. Migrations are NEVER scanned — the
historical money FloatFields there (orders_app 0001, restaurants_app 0003, …)
are immutable, already-superseded history.

A declaration is flagged only when it assigns a FloatField AND the field name has
an underscore-token that exactly matches a monetary term. Whole-token matching
(not substring) is deliberate: "coffee" must not match "fee", "multiple" must
not match "tip".

HOW A DECLARATION IS RECOGNISED (D08 B2.3). Each ``models.py`` is parsed with
Python's own ``ast`` — a line matcher missed every spelling that did not put
``name = …FloatField(`` on one physical line. Recognised, anywhere in the module
(class bodies included):

* ``name = FloatField(...)`` and ``name = <any.dotted.prefix>.FloatField(...)``
  (``models.FloatField``, ``dj.FloatField`` after ``from django.db import models
  as dj``, ``django.db.models.FloatField``), whatever the layout — a constructor
  or an assignment split across lines, or wrapped in parentheses, is one AST node;
* the annotated form ``name: T = <…>FloatField(...)``;
* ``a = b = <…>FloatField(...)`` (each name target);
* a same-module alias ``from <module> import FloatField as Alias`` followed by
  ``name = Alias(...)``.

NOT followed, deliberately — this is not dataflow analysis: an alias created by
assignment (``F = models.FloatField``), a subclass of FloatField, a field built by a
helper function, tuple/starred targets, ``add_to_class``/``setattr``, a class merely
NAMED like a Django field (``MyFloatField(...)`` is not ``FloatField``), and anything
outside a file named ``models.py``.

AN INCOMPLETE SCAN IS NEVER CLEAN. A ``models.py`` that cannot be read or parsed,
a directory that cannot be listed, and a scope with no ``models.py`` at all each make
the result INCOMPLETE: "no violation found" is only a statement about files that were
actually analysed. Symlinked DIRECTORIES are not followed, and that is not a gap: a
link's target is either inside the tree, where the walk reaches it at its real path,
or outside it, where it is not repository source.
``django check`` is NOT relied on to report a broken ``models.py``: it imports only
installed apps' models, and this scan covers every ``models.py`` in the tree.

Usage (no DB, no Django import required)::

    python scripts/check_money_fields.py              # self-test, then the scan
    python scripts/check_money_fields.py --self-test  # the self-test alone

The default invocation always runs the self-test first, so a detector that
stopped detecting cannot report a clean tree.

Exit 0 complete and clean · 1 monetary FloatField found · 2 INCOMPLETE (never
clean) · 3 the self-test failed, so no scan was trusted.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_CLEAN, EXIT_VIOLATION, EXIT_INCOMPLETE, EXIT_SELF_TEST = 0, 1, 2, 3

# Monetary terms matched as whole underscore-tokens (NOT substrings).
MONEY_TOKENS = {
    "price", "cost", "amount", "amt", "fee", "total", "subtotal",
    "savings", "discount", "surcharge", "balance", "tax", "refund",
    "charge", "payout", "deposit", "fare", "tip", "gratuity",
}

# Directories we never descend into. "migrations" is the hard requirement;
# the rest keep a stray local virtualenv / build dir from polluting the scan.
PRUNE_DIRS = {
    "migrations", "__pycache__", "node_modules", "site-packages",
    "venv", "env",
}

FIELD_CLASS = "FloatField"

MESSAGE = (
    "money must use DecimalField (DRF serialises it as a string; the frontend "
    "wraps in Number()); if the field is genuinely non-monetary, rename it so "
    "no token collides"
)


class UnanalysableSource(ValueError):
    """A ``models.py`` that could not be parsed — never read as clean."""


@dataclass
class ScanResult:
    files: list = field(default_factory=list)
    offenders: list = field(default_factory=list)  # (rel, lineno, name, tokens)
    incomplete: list = field(default_factory=list)  # human-readable reasons


def money_tokens(name: str) -> list:
    return sorted({t for t in name.lower().split("_")} & MONEY_TOKENS)


def _float_field_aliases(tree) -> set:
    """Local names bound to FloatField by ``from <module> import FloatField as X``."""
    aliases = {FIELD_CLASS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == FIELD_CLASS:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_float_field_call(value, aliases) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return func.id in aliases
    if isinstance(func, ast.Attribute):
        return func.attr == FIELD_CLASS
    return False


def find_offenders_in_source(source, filename: str = "models.py") -> list:
    """Return ``[(lineno, field_name, tokens)]`` for monetary FloatFields.

    ``source`` may be ``bytes`` (so a PEP 263 coding cookie and an undecodable file
    behave exactly as they would for the interpreter) or ``str``. Raises
    ``UnanalysableSource`` rather than guessing at a file it cannot parse.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError) as exc:
        raise UnanalysableSource(f"{filename} does not parse: {exc}") from None

    aliases = _float_field_aliases(tree)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not _is_float_field_call(value, aliases):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                hits = money_tokens(target.id)
                if hits:
                    offenders.append((target.lineno, target.id, hits))
    return sorted(offenders)


def find_offenders(path: Path):
    """Offenders in one ``models.py`` (raises ``OSError`` / ``UnanalysableSource``)."""
    return find_offenders_in_source(path.read_bytes(), filename=str(path))


def iter_models_files(root: Path, incomplete: list | None = None, walk=os.walk):
    """Yield every ``models.py`` under *root*, skipping pruned directories.

    A directory that cannot be listed is appended to ``incomplete`` rather than
    silently skipped — ``os.walk`` ignores listing errors unless told otherwise.
    """
    incomplete = [] if incomplete is None else incomplete

    def on_error(exc):
        where = getattr(exc, "filename", None) or "?"
        incomplete.append(f"could not list {where}: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in walk(root, onerror=on_error):
        # Prune in place so os.walk does not descend into these.
        dirnames[:] = [
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")
        ]
        if "models.py" in filenames:
            yield Path(dirpath) / "models.py"


def scan(root: Path = REPO_ROOT, walk=os.walk) -> ScanResult:
    result = ScanResult()
    for path in sorted(iter_models_files(root, result.incomplete, walk=walk)):
        rel = path.relative_to(root).as_posix()
        result.files.append(rel)
        try:
            found = find_offenders(path)
        except OSError as exc:
            result.incomplete.append(f"{rel} could not be read: {exc.strerror or exc}")
            continue
        except UnanalysableSource as exc:
            result.incomplete.append(f"{rel} could not be analysed: {exc}".replace(str(path), rel))
            continue
        for lineno, name, hits in found:
            result.offenders.append((rel, lineno, name, hits))
    if not result.files and not result.incomplete:
        result.incomplete.append("no models.py was found — an empty scope is not a clean one")
    return result


# ---------------------------------------------------------------------------
# Self-test: the detector must fire on every spelling it claims to recognise,
# stay quiet on the ones it must not, and refuse what it cannot analyse.
# ---------------------------------------------------------------------------

_POSITIVE = {
    "bare FloatField": "from django.db.models import FloatField\nclass A:\n    price = FloatField()\n",
    "dotted models.FloatField": "from django.db import models\nclass A(models.Model):\n    total_amount = models.FloatField(default=0)\n",
    "a fully dotted import": "import django.db.models\nclass A:\n    unit_cost = django.db.models.FloatField()\n",
    "an aliased module": "from django.db import models as dj\nclass A:\n    service_fee = dj.FloatField()\n",
    "a multiline constructor": "class A:\n    refund_total = models.FloatField(\n        default=0,\n        null=True,\n    )\n",
    "a parenthesised multiline assignment": "class A:\n    tip_amount = (\n        models.FloatField(default=0)\n    )\n",
    "an annotated assignment": "class A:\n    tax: float = models.FloatField()\n",
    "a chained assignment": "class A:\n    ok = price = models.FloatField()\n",
    "a same-module import alias": "from django.db.models import FloatField as Number\nclass A:\n    balance = Number()\n",
}

_NEGATIVE = {
    "a DecimalField for money": "class A:\n    price = models.DecimalField(max_digits=12, decimal_places=2)\n",
    "a non-monetary FloatField": "class A:\n    rating = models.FloatField()\n    latitude = models.FloatField()\n",
    "a substring that is not a token": "class A:\n    coffee_rating = models.FloatField()\n    multiple = models.FloatField()\n",
    "prose and comments": "# price = models.FloatField()  -- never do this\nclass A:\n    '''total = models.FloatField() is forbidden'''\n",
    "a class merely named like the field": "class A:\n    price = MyFloatField()\n",
}


def self_test(log=print) -> int:
    failures = []
    for label, source in _POSITIVE.items():
        try:
            if not find_offenders_in_source(source):
                failures.append(f"did not flag {label}")
        except UnanalysableSource as exc:
            failures.append(f"could not parse the {label} case: {exc}")
    for label, source in _NEGATIVE.items():
        try:
            if find_offenders_in_source(source):
                failures.append(f"flagged {label}")
        except UnanalysableSource as exc:
            failures.append(f"could not parse the {label} case: {exc}")
    try:
        find_offenders_in_source("class A:\n    price = (models.FloatField(\n")
        failures.append("an unparseable models.py was analysed instead of refused")
    except UnanalysableSource:
        pass

    # Discovery, on a real temporary tree: nested models.py found, migrations pruned,
    # an empty scope and a listing error are incomplete.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "app" / "sub").mkdir(parents=True)
        (root / "app" / "migrations").mkdir()
        (root / "app" / "sub" / "models.py").write_text(_POSITIVE["dotted models.FloatField"])
        (root / "app" / "migrations" / "models.py").write_text(_POSITIVE["bare FloatField"])
        found = scan(root)
        if [o[0] for o in found.offenders] != ["app/sub/models.py"] or found.incomplete:
            failures.append(f"discovery: expected one offender in app/sub/models.py, got {found}")
        (root / "app" / "sub" / "models.py").write_text("class A(\n")
        if not scan(root).incomplete:
            failures.append("discovery: an unparseable models.py did not make the scan incomplete")
        empty = Path(tmp) / "empty"
        empty.mkdir()
        if not scan(empty).incomplete:
            failures.append("discovery: an empty scope was not incomplete")

        def failing_walk(top, onerror=None):
            onerror(PermissionError(13, "Permission denied", str(Path(top) / "locked")))
            yield str(top), [], []
        if not scan(root, walk=failing_walk).incomplete:
            failures.append("discovery: a listing error was silently skipped")

    if failures:
        for line in failures:
            log(f"  self-test FAIL: {line}")
        log(f"Money-field guard self-test: {len(failures)} case(s) failed — the detector "
            "cannot be trusted, so no scan was run.")
        return EXIT_SELF_TEST
    log(f"Money-field guard self-test: OK — {len(_POSITIVE)} spellings flagged, "
        f"{len(_NEGATIVE)} controls quiet, unparseable and empty scopes refused.")
    return EXIT_CLEAN


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    status = self_test()
    if status != EXIT_CLEAN or "--self-test" in argv:
        return status

    result = scan(REPO_ROOT)
    if result.offenders:
        print("Money-field guard: FAIL — monetary field(s) declared as FloatField:")
        print()
        for rel, lineno, name, hits in result.offenders:
            tokens = ", ".join(hits)
            print(f"  {rel}:{lineno}: {name} = FloatField (monetary token: {tokens})")
            print(f"      {MESSAGE}")
        print()
        print(f"{len(result.offenders)} offending field(s). Money must use DecimalField.")
        for line in result.incomplete:
            print(f"  (the scan was also incomplete: {line})")
        return EXIT_VIOLATION

    if result.incomplete:
        print("Money-field guard: INCOMPLETE — the scan did not cover what it claims to, "
              "so this is NOT a clean result:")
        for line in result.incomplete:
            print(f"  {line}")
        return EXIT_INCOMPLETE

    print(
        f"Money-field guard: OK — analysed {len(result.files)} models.py file(s) "
        "completely, no monetary FloatField declarations."
    )
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
