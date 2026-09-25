"""THE POLICY CORE, against the cross-ecosystem oracle and on its own terms.

conformance.json is byte-identical in Dinify-Frontend, Dinify-Admin and Dinify-Backend;
the JavaScript evaluator in the other two is tested against the same cases, and the digest
below is pinned in all three suites. Editing the vectors in one repository fails that
suite until the change is made deliberately everywhere — which is how two implementations
in two languages are held to ONE policy without giving a pull-request job access to
another repository.
"""

import hashlib
import json
import os
import unittest

from dependency_audit import core
from dependency_audit.self_test import run_conformance, self_test

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "conformance.json"), "rb") as _fh:
    BYTES = _fh.read()
VECTORS = json.loads(BYTES.decode("utf-8"))

# Pinned identically in the three repositories. Change it only with the vectors, everywhere.
CONFORMANCE_SHA256 = "7a9d09e3acbede18578d64ce86d5f93c361601648dcc979a68f8066b58e865bf"


class ConformanceVectorTests(unittest.TestCase):
    def test_CONTRACT_the_vectors_are_the_pinned_shared_bytes(self):
        self.assertEqual(hashlib.sha256(BYTES).hexdigest(), CONFORMANCE_SHA256)
        self.assertEqual(VECTORS["schema"], "dinify.dependency-audit.conformance/v1")

    def test_every_vector(self):
        for c in VECTORS["cases"]:
            with self.subTest(vector=c["name"]):
                r = core.evaluate(c["incomplete"], c["findings"], c["records"], c["now"])
                self.assertEqual(r["outcome"], c["expect"]["outcome"], r["reasons"])
                self.assertEqual(r["exitCode"], c["expect"]["exitCode"])
                self.assertEqual(sorted(x["id"] for x in r["records"] if x["status"] == "refused"), c["expect"]["refused"])
                for k, v in c["expect"]["counts"].items():
                    self.assertEqual(r["counts"][k], v, k)

    def test_CONTROL_the_vectors_cover_all_four_outcomes(self):
        self.assertEqual(sorted({c["expect"]["outcome"] for c in VECTORS["cases"]}),
                         ["blocking", "exceptions_only", "incomplete", "within_policy"])

    def test_CONTRACT_the_self_test_passes_and_an_always_pass_or_always_fail_core_would_not(self):
        self.assertEqual(run_conformance(VECTORS), [])
        self.assertEqual(self_test(), [])
        for outcome in ("within_policy", "blocking"):
            mutated = {"cases": [dict(c, expect=dict(c["expect"], outcome=outcome)) for c in VECTORS["cases"]]}
            self.assertTrue(run_conformance(mutated), outcome)


class RuleTests(unittest.TestCase):
    def test_CONTRACT_high_critical_block_everywhere_runtime_always_blocks_tooling_lower_is_triage(self):
        for scope in ("runtime", "tooling", "unknown"):
            self.assertEqual(core.classify({"scope": scope, "severity": "critical"}), "blocking")
            self.assertEqual(core.classify({"scope": scope, "severity": "high"}), "blocking")
        for severity in ("moderate", "low", "info", "unknown", "weird"):
            self.assertEqual(core.classify({"scope": "runtime", "severity": severity}), "blocking")
        for severity in ("moderate", "low", "info"):
            self.assertEqual(core.classify({"scope": "tooling", "severity": severity}), "triage")
        self.assertEqual(core.classify({"scope": "tooling", "severity": "unknown"}), "unresolved")

    def test_CONTRACT_pip_audit_findings_carry_no_severity_and_are_never_read_as_low(self):
        runtime = {"advisory": "PYSEC-2026-1", "aliases": [], "package": "django", "version": "5.2.17",
                   "path": "application:site-packages/django", "scope": "runtime", "severity": "unknown"}
        tooling = dict(runtime, package="pip", path="application:site-packages/pip", scope="tooling")
        self.assertEqual(core.evaluate([], [runtime], [], "2026-01-01T00:00:00Z")["outcome"], "blocking")
        self.assertEqual(core.evaluate([], [tooling], [], "2026-01-01T00:00:00Z")["outcome"], "incomplete")

    def test_CONTRACT_exit_codes(self):
        self.assertEqual([core.exit_code_for(o) for o in core.OUTCOMES], [0, 0, 1, 2])
        self.assertEqual(core.exit_code_for("something-new"), 2)

    def test_CONTRACT_triage_is_never_worded_as_zero_findings_and_incomplete_says_so(self):
        f = {"advisory": "GHSA-1111-2222-3333", "aliases": [], "package": "t", "version": "1.0.0",
             "path": "application:site-packages/t", "scope": "tooling", "severity": "moderate"}
        r = core.evaluate([], [f], [], "2026-01-01T00:00:00Z")
        self.assertIn("REQUIRE TRIAGE (not zero findings)", core.headline(r))
        r = core.evaluate([{"code": "scanner_timeout", "detail": "x"}], [], [], "2026-01-01T00:00:00Z")
        self.assertTrue(core.headline(r).startswith("AUDIT UNAVAILABLE OR INCOMPLETE — NOT A CLEAN RESULT"))

    def test_CONTRACT_a_missing_decision_time_is_incomplete(self):
        self.assertEqual(core.evaluate([], [], [], "not-a-time")["outcome"], "incomplete")

    def test_CONTRACT_dates_are_calendar_dates_and_records_lapse_at_midnight_utc(self):
        self.assertIsNone(core.parse_date("2026-02-30"))
        self.assertIsNone(core.parse_date("2026-2-3"))
        self.assertIsNotNone(core.parse_date("2028-02-29"))
        base = {"id": "EXC-1", "kind": "exception", "advisory": "GHSA-aaaa-bbbb-cccc", "aliases": [], "package": "a",
                "version": "1.0.0", "paths": ["application:site-packages/a"], "scope": "runtime", "applicability": "x" * 20,
                "reason": "y" * 20, "owner": "o",
                "approval": {"by": "o", "reference": "https://github.com/mugak1/Dinify-Backend/pull/1", "date": "2026-09-01"},
                "expires": "2026-10-01"}
        self.assertEqual(core.validate_record(base, core.parse_instant("2026-09-30T23:59:59Z")), [])
        self.assertTrue(any("expired" in p for p in core.validate_record(base, core.parse_instant("2026-10-01T00:00:00Z"))))

    def test_CONTRACT_no_record_is_pre_approved_by_this_change(self):
        with open(os.path.join(HERE, "policy.json"), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["records"], [])
