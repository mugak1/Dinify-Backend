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

A line is flagged only when it declares a FloatField AND the field name has an
underscore-token that exactly matches a monetary term. Whole-token matching
(not substring) is deliberate: "coffee" must not match "fee", "multiple" must
not match "tip".

Usage (no DB, no Django import required)::

    python scripts/check_money_fields.py

Exit 0 if clean, 1 if any monetary FloatField is found.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

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

# name = [<dotted.prefix>.]FloatField(  — matches "models.FloatField(" and a
# bare "FloatField(". Anchoring on "name =" means commented-out lines and prose
# mentions of FloatField never match; only real field declarations do.
FLOATFIELD_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(?:[\w.]+\.)?FloatField\s*\(")

MESSAGE = (
    "money must use DecimalField (DRF serialises it as a string; the frontend "
    "wraps in Number()); if the field is genuinely non-monetary, rename it so "
    "no token collides"
)


def iter_models_files(root: Path):
    """Yield every ``models.py`` under *root*, skipping pruned directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk does not descend into these.
        dirnames[:] = [
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")
        ]
        if "models.py" in filenames:
            yield Path(dirpath) / "models.py"


def find_offenders(path: Path):
    """Return [(lineno, field_name, sorted_tokens)] for monetary FloatFields."""
    offenders = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = FLOATFIELD_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        hits = {t for t in name.lower().split("_")} & MONEY_TOKENS
        if hits:
            offenders.append((lineno, name, sorted(hits)))
    return offenders


def main() -> int:
    files = sorted(iter_models_files(REPO_ROOT))
    all_offenders = []
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        for lineno, name, hits in find_offenders(path):
            all_offenders.append((rel, lineno, name, hits))

    if all_offenders:
        print("Money-field guard: FAIL — monetary field(s) declared as FloatField:")
        print()
        for rel, lineno, name, hits in all_offenders:
            tokens = ", ".join(hits)
            print(f"  {rel}:{lineno}: {name} = FloatField (monetary token: {tokens})")
            print(f"      {MESSAGE}")
        print()
        print(f"{len(all_offenders)} offending field(s). Money must use DecimalField.")
        return 1

    print(
        f"Money-field guard: OK — scanned {len(files)} models.py file(s), "
        "no monetary FloatField declarations."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
