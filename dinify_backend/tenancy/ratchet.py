"""
Pure ratchet / classification set-logic (TENANT-STRUCT-00) — git-free and
DB-free, so it is fully unit-testable. The git side (obtaining the base
baseline) lives in ``scripts/check_tenant_relation_ratchet.py`` and is covered
by its own git integration tests; introspection lives in ``discovery.py``.
"""
from pathlib import Path

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.txt"

_HEADER = [
    "# Tenant-relation classification baseline (TENANT-STRUCT-00) — TEMPORARY.",
    "#",
    "# Legacy debt: writable relational (serializer::field) pairs not yet",
    "# classified in a serializer's Meta.tenant_relations. This file may only",
    "# SHRINK. A NEW writable relation cannot be added here — it must be classified",
    "# (SameTenant / ServerDerived / GlobalRelation). Entries are removed as",
    "# domains migrate. When this file reaches ZERO entries, DELETE it, the ratchet",
    "# script (scripts/check_tenant_relation_ratchet.py), and its ci.yml/verify.sh",
    "# steps — the meta-test alone then enforces everything.",
    "#",
    "# Regenerate (only to REMOVE migrated entries): python scripts/gen_tenant_baseline.py",
    "",
]


def detect_additions(base_keys, current_keys):
    """Keys present in ``current`` but not in ``base`` — the ratchet forbids
    these (the baseline may only shrink)."""
    return set(current_keys) - set(base_keys)


def classification_violations(discovered_keys, classified_keys, baselined_keys):
    """
    Every discovered writable relational field must be EITHER classified
    (Meta.tenant_relations) XOR baselined — never neither, never both — and no
    classified/baselined key may refer to a field that isn't a discovered writable
    relation. Returns a sorted list of human-readable violations (empty == clean).
    """
    discovered = set(discovered_keys)
    classified = set(classified_keys)
    baselined = set(baselined_keys)
    violations = []

    for key in sorted(discovered - classified - baselined):
        violations.append(
            f"{key}: writable relation is neither classified in Meta.tenant_relations "
            f"nor in the baseline. Classify it (SameTenant/ServerDerived/GlobalRelation) "
            f"— a new relation cannot be added to the baseline."
        )
    for key in sorted(classified & baselined):
        violations.append(
            f"{key}: appears in BOTH Meta.tenant_relations and the baseline. "
            f"Remove it from the baseline — it is classified."
        )
    for key in sorted(baselined - discovered):
        violations.append(
            f"{key}: baseline entry no longer maps to a discovered writable relation "
            f"(serializer/field removed or made read_only). Remove it (baseline shrinks)."
        )
    for key in sorted(classified - discovered):
        violations.append(
            f"{key}: classified in Meta.tenant_relations but is not a discovered "
            f"writable relational field (typo / wrong name / read_only)."
        )
    return violations


def load_baseline(path=BASELINE_PATH):
    """Parse the baseline file into a set of keys (blank/comment lines skipped)."""
    text = Path(path).read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def format_baseline(keys):
    """Render a sorted baseline file (header comment + one key per line)."""
    return "\n".join(_HEADER + sorted(set(keys))) + "\n"
