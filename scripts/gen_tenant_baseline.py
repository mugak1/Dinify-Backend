#!/usr/bin/env python3
"""
Regenerate ``dinify_backend/tenancy/baseline.txt`` (TENANT-STRUCT-00).

The baseline is the TEMPORARY legacy-debt list: every writable relational
``serializer::field`` pair not yet classified in a serializer's
``Meta.tenant_relations``. Run once to bootstrap it, and by future migration PRs
ONLY to REMOVE entries that have since been classified. The ratchet
(``scripts/check_tenant_relation_ratchet.py``) forbids additions, so
regenerating this file can never be used to sneak a new relation past
classification — a regenerated file that GREW fails the ratchet.

    python scripts/gen_tenant_baseline.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dinify_backend.test_settings")

import django  # noqa: E402

django.setup()

from dinify_backend.tenancy.discovery import (  # noqa: E402
    discover_all_project_serializers,
    read_classifications,
)
from dinify_backend.tenancy.ratchet import BASELINE_PATH, format_baseline  # noqa: E402


def main() -> int:
    records, unintrospectable = discover_all_project_serializers()
    discovered = {r["key"] for r in records}
    classified = set()
    for cls in {r["serializer"] for r in records}:
        for field_name in read_classifications(cls):
            classified.add(f"{cls.__module__}.{cls.__qualname__}::{field_name}")
    baseline = discovered - classified
    BASELINE_PATH.write_text(format_baseline(baseline), encoding="utf-8")
    serializers = len({key.rsplit("::", 1)[0] for key in baseline})
    print(
        f"Wrote {BASELINE_PATH.relative_to(REPO_ROOT)}: {len(baseline)} entries "
        f"across {serializers} serializers "
        f"({len(discovered)} discovered, {len(classified)} classified)."
    )
    if unintrospectable:
        print(
            f"Un-introspectable ({len(unintrospectable)}): "
            + ", ".join(sorted(unintrospectable))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
