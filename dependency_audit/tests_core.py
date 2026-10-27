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
CONFORMANCE_SHA256 = "6365e880c55153f5ce50ca144434cb42d5c937be48218445da36cefb8ffb9653"

# INERTNESS, BYTE FOR BYTE. The first 41 vectors are the ones that existed before the dated
# obligation (A-1). These are the sha256 digests of the FULL result each of them produced on
# the evaluator before A-1 (Dinify-Backend e50429d), serialized as compact JSON of
# ``{"headline": ..., **result}``. The JavaScript suites in Dinify-Frontend and Dinify-Admin
# pin the SAME digests: on that evaluator the two languages already produced identical
# bytes. A rule that leaks a ``deferred: 0``, a ``deadline`` or a new reason into a result
# that deferred nothing changes one of these, and every consumer that re-decides a retained
# result and compares it exactly (release/preflight.py among them) would then refuse
# evidence certified before A-1.
PRE_DEFERRAL_RESULTS_SHA256 = (
    "4659842661c49f472e5a5c7d92c94a8c36f582c6bf8932efc1f2d2758fe96e0b",
    "6916a116d26094bbaed646964a89651c7e95fdaa447e6650bdb58ec5762fb317",
    "58800523fd48a82c100d205a31a2467901366a17b97cfac02b705b02df6f5a8d",
    "753c81c544689f6a08984f6de9ff68f23aeb7110a8c2b9620cc1f0034bf786b6",
    "b509a94a6527e2b2a50c153d36d1e0120d452f5d9adf564ec785cc6461ec94aa",
    "1e7ca1e20263e798b74a267adf4e0d961b9d51f706739c54a7230f7fc3e28c84",
    "60e8d37cb9c930907e3f4863014a84b3e2510c41f163af99dee5f95404797f41",
    "81f8b5181f02deaf00ff482b82b15710ce22e1f01e87a6bd2a1c928ac095ba05",
    "f346869124d1fbcf1e5eaea551cd869b6807860a8622bdec16cb04d4e82f0270",
    "b7550aea88cf644329a3418a90750497e0c7b2afbb9af46e417d9cd544553d18",
    "a44afe6c338013d7bcd418156e22a478bdc930b4351b238372408ffcaab42b24",
    "d64f763cbb044dd93b92b6cf96f6210b4cdd578e83a13deddf058cea8d04de98",
    "1c971aa6c17d5684c6f52c415fcec44b9b98b58de3a522b40eeeacc28ef01084",
    "f2c69a6fd3a3d05f0453d485f9a22cfa82e93fdd5dbddb7124a48c38ff97413e",
    "9e73cdf5efb50cef32a02f1b695a2d3c07d3ee6bf05d42b81a985466c7380f81",
    "9e73cdf5efb50cef32a02f1b695a2d3c07d3ee6bf05d42b81a985466c7380f81",
    "10aecfd1145c6c3e410b1cbc303edc88e4b83f9ee89629f2d4fdcb71a7d5f738",
    "000fcd469c65dfa828d1ebd60c9fa8af8ea91c13f29658bb179aaf1699f5deda",
    "9e73cdf5efb50cef32a02f1b695a2d3c07d3ee6bf05d42b81a985466c7380f81",
    "9ec5fb47733893902f9daf22ac904ae45ae7735b00b779b7f2acc1736ac3cd9b",
    "cba27976d3289b2d1de73111919a1c25399d052d57cadd9eafd6de1f2715a693",
    "d3df95160f55c844f9dd7235dd97374516f9461b2a2b107cae99a059be949a1c",
    "2ef9a6584f47fb41da84cf753388942fd69a6119dc22a4366c75d77d7d05ac04",
    "a39ecf8a06aa43d6db1e9b81ced45c017c365455d7b13308ffc54c1170bafd8d",
    "5717ec5fae6f990087afd9e77b1b74e71a181ce5e55cfea71f3fff0aded0e889",
    "cba5065d76f52441dc09a316ef992445e2a22436f6bf9494c7f557926d330a77",
    "046aa97db05267a9c748085b6f9f16f7d46126d9e301e8679cbe704b3dbc8df1",
    "046aa97db05267a9c748085b6f9f16f7d46126d9e301e8679cbe704b3dbc8df1",
    "0ab928879d43eb4e05fdad2a5fbc19d61b188201f1258b557b578143abeaa9e3",
    "f961b35afd062902eb6a25125428f250832c1eecde295ab2ed3ddcb05e88ff5f",
    "743af8f7e61e3fd2aedf920bb18b16a61f4b4c86b31ddc3f0b762d6670dec598",
    "60ca9fd7e6ae8b04ac3487013076a045020371dc1f1f665c67fcea71eaa425e0",
    "091f0778a7161d382b5f341aba831ff66996158545fdde7d3a9c4809d94d3e9a",
    "2bf366a1b1c7478f78d6228643e000f67e3a4c1b08c7bebc5857ac2399ba1aad",
    "ff3a3f19f49b463bbcce8e5a6b36fd0630442f33569b203bebbcea742ef78a4d",
    "ba4cc172e5dfc668afdc6b78deb43eb6d257b2dbf4f914d039967e5be64e3067",
    "e84392eb163a85b592b3ee1c29a520a9a67cf49946af4522a0b67df6567ab7af",
    "e45b7fb77f068fe13975744cde9ac647b2f63746eba51587afe0b5f4c9ac5963",
    "76b5f3506d48ff55e83d09f91a5d971afc1355a5a1dc5c92c4f1786c6dd4fa05",
    "e80bb106e9e7b657983eae22e8eda94182aa66683cd85793432a14e7579b5411",
    "f90fedf8401cec088aa7f0827841bbc139c1a0ed922c17734793eee03726e8e7",
)


def _result_digest(r):
    text = json.dumps(dict({"headline": core.headline(r)}, **r), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
                # The deadlines a vector records (absent: none), and nothing deferral-shaped otherwise.
                deadlines = c["expect"].get("deadlines", [])
                self.assertEqual(sorted(f["deadline"] for f in r["findings"] if f["class"] == "deferred"), deadlines)
                self.assertEqual("deferred" in r["counts"], len(deadlines) > 0)
                self.assertFalse(any(f["class"] != "deferred" and "deadline" in f for f in r["findings"]))
                self.assertEqual(any(x["code"] == "deferred_finding" for x in r["reasons"]), len(deadlines) > 0)

    def test_CONTROL_the_vectors_cover_all_five_outcomes(self):
        self.assertEqual(sorted({c["expect"]["outcome"] for c in VECTORS["cases"]}), sorted(core.OUTCOMES))
        self.assertEqual(sorted({c["expect"]["outcome"] for c in VECTORS["cases"]}),
                         ["blocking", "exceptions_only", "incomplete", "obligations_pending", "within_policy"])

    def test_CONTRACT_inertness_every_vector_that_predates_the_dated_obligation_decides_byte_identically(self):
        self.assertEqual(len(PRE_DEFERRAL_RESULTS_SHA256), 41)
        for i, c in enumerate(VECTORS["cases"][:41]):
            with self.subTest(vector=c["name"]):
                r = core.evaluate(c["incomplete"], c["findings"], c["records"], c["now"])
                self.assertEqual(_result_digest(r), PRE_DEFERRAL_RESULTS_SHA256[i])

    def test_CONTRACT_the_deferral_vectors_are_appended_so_the_41_keep_their_positions(self):
        self.assertEqual(len(VECTORS["cases"]), 61)
        self.assertFalse(any(c["name"].startswith("deferral:") for c in VECTORS["cases"][:41]))
        self.assertTrue(all(c["name"].startswith("deferral:") for c in VECTORS["cases"][41:]))

    def test_CONTRACT_the_self_test_passes_and_an_always_pass_or_always_fail_core_would_not(self):
        self.assertEqual(run_conformance(VECTORS), [])
        self.assertEqual(self_test(), [])
        for outcome in core.OUTCOMES:
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
        self.assertEqual(core.OUTCOMES, ("within_policy", "exceptions_only", "obligations_pending", "blocking", "incomplete"))
        self.assertEqual([core.exit_code_for(o) for o in core.OUTCOMES], [0, 0, 0, 1, 2])
        self.assertEqual(core.exit_code_for("something-new"), 2)

    def test_CONTRACT_the_deferral_window_is_7_days_for_high_and_critical_has_none(self):
        self.assertEqual(core.HIGH_DEFERRAL_DAYS, 7)
        inherited = {"scope": "tooling", "severity": "high", "provenance": "inherited", "reportableSince": "2026-09-30"}
        self.assertEqual(core.deferral_deadline(inherited, core.parse_instant("2026-10-06T23:59:59.999000+00:00")), "2026-10-07")
        self.assertIsNone(core.deferral_deadline(inherited, core.parse_instant("2026-10-07T00:00:00Z")))
        for severity in ("critical", "moderate", "low", "info", "unknown"):
            self.assertIsNone(core.deferral_deadline(dict(inherited, severity=severity), core.parse_instant("2026-10-01T00:00:00Z")), severity)
        self.assertIsNone(core.deferral_deadline(dict(inherited, scope="runtime"), core.parse_instant("2026-10-01T00:00:00Z")))
        self.assertIsNone(core.deferral_deadline(inherited, None), "no decision time, no deferral")
        # A deadline that cannot be written as a calendar date is not established: it blocks.
        self.assertIsNone(core.deferral_deadline(dict(inherited, reportableSince="9999-12-30"), core.parse_instant("9999-12-31T00:00:00Z")))

    def test_CONTRACT_obligations_pending_passes_but_is_never_worded_as_clean(self):
        def finding(advisory, since):
            return {"advisory": advisory, "aliases": [], "package": "tool", "version": "1.0.0", "path": "application:site-packages/" + advisory,
                    "scope": "tooling", "severity": "high", "provenance": "inherited", "reportableSince": since}
        r = core.evaluate([], [finding("GHSA-aaaa-0000-0001", "2026-10-02"), finding("GHSA-aaaa-0000-0002", "2026-09-30")], [], "2026-10-03T00:00:00Z")
        self.assertEqual((r["outcome"], r["exitCode"]), ("obligations_pending", 0))
        self.assertTrue(core.headline(r).startswith("AUDIT COMPLETE — PASSES WITH DATED OBLIGATIONS OUTSTANDING: "))
        self.assertIn("2 inherited high tooling finding(s) DEFERRED under a dated obligation (blocks from 00:00 UTC on 2026-10-07 at the earliest)", core.headline(r))
        self.assertNotIn("WITHIN POLICY", core.headline(r))

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
