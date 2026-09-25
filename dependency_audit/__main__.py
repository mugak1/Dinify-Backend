"""dependency_audit — the CLI. See dependency_audit/README.md.

    python -m dependency_audit snapshot    offline; right after `pip install -r requirements.txt`
    python -m dependency_audit audit       NETWORK; scans the snapshotted inventory
    python -m dependency_audit evaluate    offline; re-decides retained evidence
    python -m dependency_audit self-test   offline; proves the evaluator can pass and refuse

Exit status: 0 within policy (or only approved exceptions) · 1 blocking ·
2 incomplete/unavailable · 64 usage. There is deliberately no option to swap the scanner,
lower a threshold, skip a graph or tolerate a failed scan.

Run it with the interpreter whose environment is being validated: the target inventory is
read with THIS interpreter's own `pip inspect`.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

from . import orchestrate
from .self_test import self_test

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE = os.path.join(ROOT, "dependency_audit", "evidence")


def main(argv):
    if len(argv) != 1:
        print("usage: python -m dependency_audit <snapshot|audit|evaluate|self-test>", file=sys.stderr)
        return 64
    command = argv[0]
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if command == "snapshot":
        ok, problems, doc = orchestrate.snapshot(ROOT, EVIDENCE, now)
        b = doc["binding"]
        print("inventory snapshot: %s installed" % b["application"]["installed"])
        print("  requirements sha256 %s" % b["application"]["requirementsSha256"])
        print("  inventory sha256    %s" % b["application"]["inventorySha256"])
        print("  revision            %s" % ((b.get("revision") or {}).get("commit") or "(not a git checkout)"))
        print("  environment         %s" % b["environment"])
        if not ok:
            for p in problems:
                print("  ✗ %s: %s" % (p["code"], p["detail"]), file=sys.stderr)
            print("SNAPSHOT REFUSED — this environment cannot serve as audit evidence.", file=sys.stderr)
            return 2
        return 0
    if command == "audit":
        result = orchestrate.audit(ROOT, EVIDENCE, now)
        print(orchestrate.render_summary(result))
        print("evidence: %s" % EVIDENCE)
        orchestrate.publish_summary(result)
        return result["exitCode"]
    if command == "evaluate":
        result = orchestrate.reevaluate(ROOT, EVIDENCE, now)
        print(orchestrate.render_summary(result))
        return result["exitCode"]
    if command == "self-test":
        failures = self_test()
        if failures:
            for f in failures:
                print("  ✗ %s" % f, file=sys.stderr)
            print("dependency-audit self-test FAILED — the evaluator cannot be trusted.", file=sys.stderr)
            return 2
        print("dependency-audit self-test: ok (a clean control passes; blocking, incomplete and refused-record controls behave)")
        return 0
    print("usage: python -m dependency_audit <snapshot|audit|evaluate|self-test>", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
