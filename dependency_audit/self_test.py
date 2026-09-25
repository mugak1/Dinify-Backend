"""THE SELF-TEST — run before every real audit, as the repository's other gates run
theirs: an evaluator that silently stopped refusing would pass everything.

Offline and deterministic: every conformance vector decides as recorded; a known-clean
pip-audit report passes (so an always-failing gate cannot pass here); a runtime advisory
blocks; a tooling advisory with pip-audit's absent severity is incomplete; and an empty,
truncated, uncovered or status-contradicting answer, a skipped package and a timeout are
each incomplete. Returns the list of failures; empty means trustworthy.
"""

from __future__ import annotations

import json
import os

from . import core
from . import pip_adapter as pa

CONFORMANCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conformance.json")


def run_conformance(doc):
    failures = []
    for c in doc["cases"]:
        r = core.evaluate(c["incomplete"], c["findings"], c["records"], c["now"])
        refused = sorted(x["id"] for x in r["records"] if x["status"] == "refused")
        errs = []
        if r["outcome"] != c["expect"]["outcome"]:
            errs.append("outcome %s != %s" % (r["outcome"], c["expect"]["outcome"]))
        if r["exitCode"] != c["expect"]["exitCode"]:
            errs.append("exit %s != %s" % (r["exitCode"], c["expect"]["exitCode"]))
        if refused != c["expect"]["refused"]:
            errs.append("refused %s != %s" % (refused, c["expect"]["refused"]))
        for k, v in c["expect"]["counts"].items():
            if r["counts"][k] != v:
                errs.append("%s %s != %s" % (k, r["counts"][k], v))
        if errs:
            failures.append('conformance "%s": %s' % (c["name"], "; ".join(errs)))
    return failures


_PACKAGES = [
    {"path": "application:site-packages/django", "name": "django", "version": "5.2.17", "scope": "runtime", "recordSha256": "a"},
    {"path": "application:site-packages/pip", "name": "pip", "version": "26.2.1", "scope": "tooling", "recordSha256": "b"},
]


def _report(vulns_by_name=None, drop=()):
    vulns_by_name = vulns_by_name or {}
    deps = [{"name": p["name"], "version": p["version"], "vulns": vulns_by_name.get(p["name"], [])}
            for p in _PACKAGES if p["name"] not in drop]
    return json.dumps({"dependencies": deps, "fixes": []})


_VULN = {"id": "PYSEC-2026-0001", "fix_versions": ["9.9.9"], "aliases": ["CVE-2026-0001"], "description": "x"}


def _decide(run):
    problems, findings = pa.read_report("application", run, _PACKAGES)
    return core.evaluate(problems, findings, [], "2026-01-01T00:00:00Z")


def self_test():
    with open(CONFORMANCE, "r", encoding="utf-8") as fh:
        failures = run_conformance(json.load(fh))

    def expect(label, run, outcome, extra=lambda r: True):
        r = _decide(run)
        if r["outcome"] != outcome or not extra(r):
            failures.append('control "%s": got %s %s' % (label, r["outcome"], r["counts"]))

    expect("known-clean report passes", {"status": 0, "stdout": _report()}, "within_policy", lambda r: r["counts"]["findings"] == 0)
    expect("runtime advisory blocks", {"status": 1, "stdout": _report({"django": [_VULN]})}, "blocking")
    expect("tooling advisory without severity is incomplete", {"status": 1, "stdout": _report({"pip": [_VULN]})}, "incomplete")
    expect("empty output is incomplete", {"status": 0, "stdout": ""}, "incomplete")
    expect("truncated output is incomplete", {"status": 1, "stdout": _report()[:25]}, "incomplete")
    expect("an unreported package is incomplete", {"status": 0, "stdout": _report(drop=("pip",))}, "incomplete")
    expect("a skipped package is incomplete", {"status": 1, "stdout": json.dumps({"dependencies": [
        {"name": "django", "version": "5.2.17", "vulns": []}, {"name": "pip", "skip_reason": "could not be audited"}]})}, "incomplete")
    expect("status 0 with findings is incomplete", {"status": 0, "stdout": _report({"django": [_VULN]})}, "incomplete")
    expect("a timeout is incomplete", {"status": None, "timedOut": True, "stdout": ""}, "incomplete")
    return failures
