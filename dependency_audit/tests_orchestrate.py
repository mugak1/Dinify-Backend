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
