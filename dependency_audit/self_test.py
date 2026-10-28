"""THE SELF-TEST — run before every real audit, as the repository's other gates run
theirs: an evaluator that silently stopped refusing would pass everything.

Offline and deterministic: every conformance vector decides as recorded (the dated
obligation's deadlines, and the ABSENCE of every deferral field where nothing was
deferred, included); a known-clean pip-audit report passes (so an always-failing gate
cannot pass here); a runtime advisory blocks; a tooling advisory with pip-audit's absent
severity is incomplete; and an empty, truncated, uncovered or status-contradicting answer,
a skipped package and a timeout are each incomplete. Returns the list of failures; empty
means trustworthy.
"""

from __future__ import annotations

import json
import os

from . import core
from . import pip_adapter as pa

CONFORMANCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conformance.json")


def run_conformance(doc, decide=None):
    """Every vector, decided by ``decide`` (the evaluator; a parameter only so a test can
    hand it a mutant and see the comparison refuse it). Beyond outcome, exit code, refused
    records and counts, the dated obligation is held to what the vector records — the same
    assertions tests_core.py makes, so a build whose unit suite never ran still cannot pass
    the gate with them broken: the deferred findings' deadlines are exactly the vector's
    ``deadlines`` (absent: none); ``counts.deferred`` is PRESENT only when something was
    deferred (an emitted ``deferred: 0`` would make every result certified before A-1
    unreproducible); no finding that was not deferred carries a ``deadline``; and the
    ``deferred_finding`` reason appears exactly when something was deferred. A count the
    result lacks is a reported failure, never a KeyError.
    """
    decide = decide or core.evaluate
    failures = []
    for c in doc["cases"]:
        r = decide(c["incomplete"], c["findings"], c["records"], c["now"])
        counts = r.get("counts") if isinstance(r.get("counts"), dict) else {}
        findings = r.get("findings") if isinstance(r.get("findings"), list) else []
        reasons = r.get("reasons") if isinstance(r.get("reasons"), list) else []
        refused = sorted(x.get("id") for x in r.get("records", []) if x.get("status") == "refused")
        errs = []
        if r.get("outcome") != c["expect"]["outcome"]:
            errs.append("outcome %s != %s" % (r.get("outcome"), c["expect"]["outcome"]))
        if r.get("exitCode") != c["expect"]["exitCode"]:
            errs.append("exit %s != %s" % (r.get("exitCode"), c["expect"]["exitCode"]))
        if refused != c["expect"]["refused"]:
            errs.append("refused %s != %s" % (refused, c["expect"]["refused"]))
        for k, v in c["expect"]["counts"].items():
            if counts.get(k) != v:
                errs.append("%s %s != %s" % (k, counts.get(k), v))
        deadlines = c["expect"].get("deadlines", [])
        deferred = sorted(str(f.get("deadline")) for f in findings if f.get("class") == "deferred")
        if deferred != deadlines:
            errs.append("deadlines %s != %s" % (deferred, deadlines))
        if ("deferred" in counts) != (len(deadlines) > 0):
            errs.append("counts.deferred is %s, %d deferral(s) expected"
                        % ("present (%s)" % counts.get("deferred") if "deferred" in counts else "absent", len(deadlines)))
        if any(f.get("class") != "deferred" and "deadline" in f for f in findings):
            errs.append("a finding that was not deferred carries a deadline")
        if any(x.get("code") == "deferred_finding" for x in reasons) != (len(deadlines) > 0):
            errs.append("the deferred_finding reason is %s" % ("missing" if deadlines else "present with nothing deferred"))
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
