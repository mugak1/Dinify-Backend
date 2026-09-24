"""THE ORCHESTRATION: snapshot -> bound scan -> decide, and re-deciding retained evidence.

Each case builds a disposable repository root (the real policy shape and the real
hash-pinned scanner requirements) and drives ``audit()`` with a canned runner that stands
in for ``pip inspect`` and ``pip-audit``. The CLI exposes no way to substitute the scanner.
"""

import json
import os
import shutil
import tempfile
import unittest

from dependency_audit import orchestrate as oc
from dependency_audit import pip_adapter as pa

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = "2026-09-24T12:00:00.000Z"
TARGET = "/fake/target/bin/python"
SCANNER = "/fake/scanner/bin/python"
FACTS = {"python": "3.12.3", "implementation": "CPython", "platform": "linux", "machine": "x86_64", "libc": "glibc 2.39"}

with open(os.path.join(HERE, "scanner-requirements.txt"), "r", encoding="utf-8") as _fh:
    SCANNER_PINS = pa.declared_requirements(_fh.read())


def inspect(pairs):
    return json.dumps({"version": "1", "installed": [{"metadata": {"name": n, "version": v}, "installer": "pip"} for n, v in pairs]})


def audit_json(pairs, vulns=None):
    vulns = vulns or {}
    return json.dumps({"dependencies": [{"name": n, "version": v, "vulns": vulns.get(n, [])} for n, v in pairs], "fixes": []})


class Runner:
    """Answers ``pip inspect`` for the target and the scanner venv, and ``pip-audit`` per
    graph. ``on_scan`` may mutate the world while a scan is "running"."""

    def __init__(self, app=None, answers=None, on_scan=None):
        self.app = list(app or [("django", "5.2.17"), ("pip", "26.2.1"), ("requests", "2.34.2")])
        self.scanner = sorted(SCANNER_PINS.items())
        self.answers = answers or {}
        self.on_scan = on_scan
        self.calls = []

    def __call__(self, command, args, cwd=None, env=None, timeout=None):
        self.calls.append((command, list(args), env))
        ok = {"command": command, "args": args, "status": 0, "signal": None, "timedOut": False, "error": None, "stdout": "", "stderr": "", "durationMs": 1}
        if args[:3] == ["-m", "pip", "inspect"]:
            return dict(ok, stdout=inspect(self.app if command == TARGET else self.scanner))
        if command.endswith("pip-audit"):
            graph = "scanner" if os.path.basename(args[1]).startswith("scanner.") else "application"
            if self.on_scan:
                self.on_scan(self, graph)
            answer = self.answers.get(graph)
            if answer is None:
                answer = {"status": 0, "stdout": audit_json(self.app if graph == "application" else self.scanner)}
            return dict(ok, **answer)
        raise AssertionError("unexpected command %s %s" % (command, args))


def fake_install(root, policy, runner, workdir):
    return SCANNER, [], {"venv": 0, "install": 0}


class Project:
    def __init__(self, records=None, requirements="Django==5.2.17\nrequests==2.34.2\n"):
        self.root = tempfile.mkdtemp(prefix="dependency-audit-")
        os.makedirs(os.path.join(self.root, "dependency_audit"))
        with open(os.path.join(HERE, "policy.json"), "r", encoding="utf-8") as fh:
            policy = json.load(fh)
        policy["records"] = records or []
        with open(os.path.join(self.root, "dependency_audit", "policy.json"), "w", encoding="utf-8") as fh:
            json.dump(policy, fh)
        shutil.copy(os.path.join(HERE, "scanner-requirements.txt"), os.path.join(self.root, "dependency_audit"))
        with open(os.path.join(self.root, "requirements.txt"), "w", encoding="utf-8") as fh:
            fh.write(requirements)
        self.evidence = os.path.join(self.root, "dependency_audit", "evidence")

    def snapshot(self, runner, facts=FACTS):
        return oc.snapshot(self.root, self.evidence, NOW, runner=runner, python=TARGET, env_facts=facts)

    def audit(self, runner, facts=FACTS, now=NOW):
        return oc.audit(self.root, self.evidence, now, runner=runner, install_scanner=fake_install, python=TARGET, env_facts=facts)

    def reevaluate(self, runner, facts=FACTS):
        return oc.reevaluate(self.root, self.evidence, NOW, runner=runner, python=TARGET, env_facts=facts)

    def read(self, name):
        with open(os.path.join(self.evidence, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.addCleanup(self.p.cleanup)

    def test_CONTROL_a_complete_clean_scan_of_both_graphs_is_within_policy_and_says_what_it_covered(self):
        runner = Runner()
        ok, problems, _ = self.p.snapshot(runner)
        self.assertTrue(ok, problems)
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "within_policy", result["reasons"])
        scans = [c for c in runner.calls if c[0].endswith("pip-audit")]
        self.assertEqual(len(scans), 2)
        for _, args, env in scans:
            self.assertIn("--strict", args)
            self.assertNotIn("PIP_INDEX_URL", env)
        collection = json.loads(self.p.read("collection.json"))
        self.assertEqual(collection["binding"]["repository"], "mugak1/Dinify-Backend")
        self.assertEqual(collection["binding"]["environment"]["python"], "3.12.3")
        self.assertEqual(collection["scanner"]["pinned"], "2.10.1")
        self.assertEqual(collection["graphs"]["application"]["installed"], 3)
        self.assertEqual(collection["graphs"]["scanner"]["installed"], len(SCANNER_PINS))
        self.assertEqual(self.p.read("application.inventory-requirements.txt"), "django==5.2.17\npip==26.2.1\nrequests==2.34.2\n")

    def test_CONTRACT_without_a_snapshot_nothing_is_scanned(self):
        runner = Runner()
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "incomplete")
        self.assertIn("no_snapshot", [r["code"] for r in result["reasons"]])
        self.assertFalse([c for c in runner.calls if c[0].endswith("pip-audit")])

    def test_CONTRACT_a_different_interpreter_is_not_the_validation_target(self):
        ok, problems, _ = self.p.snapshot(Runner(), facts=dict(FACTS, python="3.11.15"))
        self.assertFalse(ok)
        self.assertIn("target_mismatch", [p["code"] for p in problems])

    def test_CONTRACT_a_declared_requirement_that_is_not_installed_refuses_the_snapshot(self):
        ok, problems, _ = self.p.snapshot(Runner(app=[("django", "5.2.17"), ("pip", "26.2.1")]))
        self.assertFalse(ok)
        self.assertIn("missing_declared", [p["code"] for p in problems])

    def test_CONTRACT_an_inventory_that_changed_after_the_snapshot_is_refused_and_not_scanned(self):
        runner = Runner()
        self.p.snapshot(runner)
        runner.app = [("django", "5.2.17"), ("pip", "26.2.1"), ("requests", "2.34.2"), ("stowaway", "0.1")]
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "incomplete")
        self.assertIn("binding_mismatch", [r["code"] for r in result["reasons"]])
        self.assertFalse([c for c in runner.calls if c[0].endswith("pip-audit")])

    def test_CONTRACT_an_inventory_that_changes_WHILE_it_is_scanned_produces_no_clean_result(self):
        def mutate(runner, graph):
            if graph == "application":
                runner.app = [("django", "5.2.18"), ("pip", "26.2.1"), ("requests", "2.34.2")]
        runner = Runner(on_scan=mutate)
        self.p.snapshot(runner)
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "incomplete")
        self.assertIn("inventory_changed_during_audit", [r["code"] for r in result["reasons"]])

    def test_CONTRACT_a_scanner_venv_that_is_not_the_pinned_set_refuses_the_audit_before_any_scan(self):
        runner = Runner()
        runner.scanner = sorted(SCANNER_PINS.items()) + [("extra-thing", "1.0")]
        self.p.snapshot(runner)
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "incomplete")
        self.assertIn("scanner_pin", [r["code"] for r in result["reasons"]])
        self.assertFalse([c for c in runner.calls if c[0].endswith("pip-audit")])

    def test_CONTRACT_a_network_failure_keeps_its_raw_cause_and_fails_the_check(self):
        runner = Runner(answers={"application": {"status": 1, "stdout": "", "stderr": "requests.exceptions.ProxyError: Tunnel connection failed: 403 Forbidden"}})
        self.p.snapshot(runner)
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "incomplete")
        self.assertEqual(result["exitCode"], 2)
        self.assertIn("ProxyError", self.p.read("application.scanner-stderr.txt"))
        self.assertEqual(json.loads(self.p.read("collection.json"))["graphs"]["application"]["run"]["status"], 1)

    def test_CONTRACT_an_advisory_in_the_scanner_venv_is_decided_like_any_tooling_finding(self):
        vuln = {"id": "PYSEC-2026-9", "aliases": [], "fix_versions": [], "description": "d"}
        runner = Runner(answers={"scanner": {"status": 1, "stdout": audit_json(sorted(SCANNER_PINS.items()), {"requests": [vuln]})}})
        self.p.snapshot(runner)
        result = self.p.audit(runner)
        self.assertEqual(result["outcome"], "incomplete", "tooling without a severity cannot be evaluated")
        self.assertEqual(result["findings"][0]["path"], "scanner:site-packages/requests")

    def test_CONTRACT_a_narrow_approved_exception_applies_and_stays_visible_then_lapses(self):
        record = {"id": "EXC-0001", "kind": "exception", "advisory": "PYSEC-2026-1", "aliases": [], "package": "django", "version": "5.2.17",
                  "paths": ["application:site-packages/django"], "scope": "runtime",
                  "applicability": "The affected code path is never reached; verified in the linked review.",
                  "reason": "No fixed release in the 5.2 LTS line yet; tracked in the linked review.", "owner": "Dinify platform owner",
                  "approval": {"by": "Dinify platform owner", "reference": "https://github.com/mugak1/Dinify-Backend/pull/1", "date": "2026-09-20"},
                  "expires": "2026-10-20"}
        p = Project(records=[record])
        self.addCleanup(p.cleanup)
        vuln = {"id": "PYSEC-2026-1", "aliases": ["CVE-2026-1"], "fix_versions": [], "description": "d"}
        runner = Runner(answers={"application": {"status": 1, "stdout": audit_json([("django", "5.2.17"), ("pip", "26.2.1"), ("requests", "2.34.2")], {"django": [vuln]})}})
        p.snapshot(runner)
        result = p.audit(runner)
        self.assertEqual(result["outcome"], "exceptions_only", result["reasons"])
        self.assertEqual(result["exitCode"], 0)
        self.assertIn("PASSES ONLY WITH APPROVED EXCEPTIONS", result["headline"])
        self.assertEqual(p.audit(runner, now="2026-10-21T00:00:00.000Z")["outcome"], "blocking")


class ReevaluationTests(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.addCleanup(self.p.cleanup)
        self.runner = Runner()
        self.p.snapshot(self.runner)
        self.assertEqual(self.p.audit(self.runner)["outcome"], "within_policy")

    def test_CONTROL_evidence_for_this_checkout_re_decides_to_the_same_outcome(self):
        self.assertEqual(self.p.reevaluate(self.runner)["outcome"], "within_policy")

    def test_CONTRACT_evidence_for_another_inventory_or_environment_is_not_accepted(self):
        self.runner.app = [("django", "5.2.18"), ("pip", "26.2.1"), ("requests", "2.34.2")]
        r = self.p.reevaluate(self.runner)
        self.assertEqual(r["outcome"], "incomplete")
        self.assertIn("evidence_foreign", [x["code"] for x in r["reasons"]])
        self.runner.app = [("django", "5.2.17"), ("pip", "26.2.1"), ("requests", "2.34.2")]
        r = self.p.reevaluate(self.runner, facts=dict(FACTS, machine="aarch64"))
        self.assertIn("evidence_foreign", [x["code"] for x in r["reasons"]])

    def test_CONTRACT_evidence_recorded_for_another_revision_is_not_accepted(self):
        path = os.path.join(self.p.evidence, "collection.json")
        with open(path) as fh:
            c = json.load(fh)
        c["binding"]["revision"] = {"commit": "f" * 40, "tree": "e" * 40}
        with open(path, "w") as fh:
            json.dump(c, fh)
        r = self.p.reevaluate(self.runner)
        self.assertTrue(any(x["code"] == "evidence_foreign" and "revision" in x["detail"] for x in r["reasons"]))

    def test_CONTRACT_raw_output_that_is_not_the_recorded_bytes_is_refused(self):
        with open(os.path.join(self.p.evidence, "application.scanner-stdout.txt"), "a") as fh:
            fh.write(" ")
        r = self.p.reevaluate(self.runner)
        self.assertEqual(r["outcome"], "incomplete")
        self.assertIn("evidence_tampered", [x["code"] for x in r["reasons"]])


class InvalidPolicyTests(unittest.TestCase):
    """An invalid policy is incomplete on every path, never a crash.

    Codex review on Dinify-Frontend#698, which found it in the npm evaluator: the loader
    recorded ``policy_invalid`` and still returned the policy, every caller guarded on a
    truthy policy, and ``capture`` then read ``policy["target"]["python"]`` — so a
    parseable policy with no target, a null target or a non-object one raised out of
    ``snapshot`` and ``reevaluate`` instead of reporting itself. An uncaught exception is
    not one of the four outcomes."""

    SHAPES = {
        "no target": lambda p: p.pop("target"),
        "a null target": lambda p: p.update(target=None),
        "a target that is not an object": lambda p: p.update(target="3.12.3"),
        "a target python that is not a string": lambda p: p.update(target={"python": 3.12}),
        "no scanner": lambda p: p.pop("scanner"),
        "a null scanner": lambda p: p.update(scanner=None),
        "records that are not a list": lambda p: p.update(records={"a": 1}),
    }

    @staticmethod
    def _break(project, mutate):
        path = os.path.join(project.root, "dependency_audit", "policy.json")
        with open(path, "r", encoding="utf-8") as fh:
            policy = json.load(fh)
        mutate(policy)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh)

    def _only_policy_invalid(self, problems, label):
        # Exactly the named problem — not a spurious target_mismatch read off a malformed target.
        self.assertTrue(problems, label)
        self.assertEqual(sorted({x["code"] for x in problems}), ["policy_invalid"], "%s: %s" % (label, problems))

    def _each(self):
        for label, mutate in self.SHAPES.items():
            p = Project()
            self.addCleanup(p.cleanup)
            yield label, mutate, p

    def test_REGRESSION_the_snapshot_reports_it_is_still_written_and_the_audit_after_it_is_incomplete(self):
        for label, mutate, p in self._each():
            with self.subTest(label):
                self._break(p, mutate)
                ok, problems, _ = p.snapshot(Runner())
                self.assertFalse(ok, label)
                self._only_policy_invalid(problems, label)
                self.assertEqual(json.loads(p.read("snapshot.json"))["problems"], problems, "%s: the evidence says why" % label)
                runner = Runner()
                result = p.audit(runner)
                self.assertEqual((result["outcome"], result["exitCode"]), ("incomplete", 2), label)
                self.assertFalse([c for c in runner.calls if c[0].endswith("pip-audit")], "%s: nothing is scanned" % label)

    def test_REGRESSION_a_policy_broken_after_a_valid_snapshot_scans_nothing_and_is_incomplete(self):
        for label, mutate, p in self._each():
            with self.subTest(label):
                ok, _, _ = p.snapshot(Runner())
                self.assertTrue(ok, label)
                self._break(p, mutate)
                runner = Runner()
                result = p.audit(runner)
                self.assertEqual((result["outcome"], result["exitCode"]), ("incomplete", 2), label)
                self._only_policy_invalid(result["reasons"], label)
                self.assertFalse([c for c in runner.calls if c[0].endswith("pip-audit")], "%s: nothing is scanned" % label)

    def test_REGRESSION_re_deciding_retained_evidence_under_an_invalid_policy_is_incomplete(self):
        for label, mutate, p in self._each():
            with self.subTest(label):
                runner = Runner()
                p.snapshot(runner)
                self.assertEqual(p.audit(runner)["outcome"], "within_policy", label)
                self._break(p, mutate)
                result = p.reevaluate(runner)
                self.assertEqual((result["outcome"], result["exitCode"]), ("incomplete", 2), label)
                self._only_policy_invalid(result["reasons"], label)


class MalformedEvidenceTests(unittest.TestCase):
    """Retained evidence that is not a document is incomplete, never clean.

    Codex review on mugak1/Dinify-Backend#339: evidence that PARSES is not yet evidence.
    ``reevaluate`` guarded on truthy documents, so a collection.json or snapshot.json holding
    ``{}``, ``[]``, ``null``, ``0``, ``""`` or ``false`` skipped every binding and graph
    check and re-decided to within policy, exit 0 — a clean verdict about evidence that does
    not exist — and a non-empty list raised AttributeError."""

    SHAPES = {
        "an empty object": {}, "an empty list": [], "null": None, "zero": 0, "an empty string": "",
        "false": False, "a list": [1, 2], "an object with another schema": {"schema": "dinify.dependency-audit.something-else/v1"},
    }

    def _prepared(self):
        p = Project()
        self.addCleanup(p.cleanup)
        runner = Runner()
        p.snapshot(runner)
        self.assertEqual(p.audit(runner)["outcome"], "within_policy")
        return p, runner

    def _write(self, p, name, value):
        with open(os.path.join(p.evidence, name), "w", encoding="utf-8") as fh:
            json.dump(value, fh)

    def test_REGRESSION_re_deciding_a_collection_or_snapshot_that_is_not_a_document_is_incomplete(self):
        for label, value in self.SHAPES.items():
            for name in ("collection.json", "snapshot.json"):
                with self.subTest(label=label, file=name):
                    p, runner = self._prepared()
                    self._write(p, name, value)
                    r = p.reevaluate(runner)
                    self.assertEqual((r["outcome"], r["exitCode"]), ("incomplete", 2), r["reasons"])
                    self.assertTrue(any(x["code"] == "evidence_unreadable" and x["detail"].startswith(name) for x in r["reasons"]), r["reasons"])

    def test_REGRESSION_a_malformed_graph_record_is_incomplete_not_a_crash(self):
        def graphs_list(c): c["graphs"] = []
        def graph_string(c): c["graphs"]["application"] = "recorded"
        def run_empty(c): c["graphs"]["application"]["run"] = {}
        def run_string(c): c["graphs"]["scanner"]["run"] = "recorded"
        def packages_junk(c): c["graphs"]["scanner"]["packages"] = [1]
        def packages_missing(c): c["graphs"]["scanner"].pop("packages")
        for mutate in (graphs_list, graph_string, run_empty, run_string, packages_junk, packages_missing):
            with self.subTest(mutate.__name__):
                p, runner = self._prepared()
                c = json.loads(p.read("collection.json"))
                mutate(c)
                self._write(p, "collection.json", c)
                r = p.reevaluate(runner)
                self.assertEqual((r["outcome"], r["exitCode"]), ("incomplete", 2), r["reasons"])

    def test_REGRESSION_the_audit_refuses_a_snapshot_that_is_not_a_document_and_scans_nothing(self):
        shapes = dict(self.SHAPES, **{"problems that are not a list": "problems-string", "a problem that is not an object": "problems-null"})
        for label, value in shapes.items():
            with self.subTest(label):
                p = Project()
                self.addCleanup(p.cleanup)
                runner = Runner()
                p.snapshot(runner)
                good = json.loads(p.read("snapshot.json"))
                doc = dict(good, problems="none") if value == "problems-string" else dict(good, problems=[None]) if value == "problems-null" else value
                self._write(p, "snapshot.json", doc)
                runner = Runner()
                r = p.audit(runner)
                self.assertEqual((r["outcome"], r["exitCode"]), ("incomplete", 2), r["reasons"])
                self.assertIn("snapshot_unreadable", [x["code"] for x in r["reasons"]])
                self.assertFalse([c for c in runner.calls if c[0].endswith("pip-audit")], "nothing is scanned")
