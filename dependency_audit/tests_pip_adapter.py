"""THE pip ADAPTER: what is inventoried, how the scanner is isolated and invoked, and how
every shape of pip-audit answer is read. Scanner answers are fixtures — a synthetic
advisory here is a test input, never a claim about any Dinify dependency.
"""

import json
import os
import tempfile
import unittest

from dependency_audit import core
from dependency_audit import pip_adapter as pa

PACKAGES = [
    {"path": "application:site-packages/django", "name": "django", "version": "5.2.17", "scope": "runtime", "recordSha256": "a"},
    {"path": "application:site-packages/pip", "name": "pip", "version": "26.2.1", "scope": "tooling", "recordSha256": "b"},
]
VULN = {"id": "PYSEC-2026-1", "fix_versions": ["5.2.18"], "aliases": ["CVE-2026-1", "GHSA-aaaa-bbbb-cccc"], "description": "d"}


def report(vulns=None, drop=(), extra=()):
    vulns = vulns or {}
    deps = [{"name": p["name"], "version": p["version"], "vulns": vulns.get(p["name"], [])} for p in PACKAGES if p["name"] not in drop]
    return json.dumps({"dependencies": deps + list(extra), "fixes": []})


def decide(run):
    problems, findings = pa.read_report("application", run, PACKAGES)
    return core.evaluate(problems, findings, [], "2026-09-24T12:00:00Z"), problems


class InventoryTests(unittest.TestCase):
    def inspect(self, items):
        return json.dumps({"version": "1", "pip_version": "26.2.1", "installed": items})

    def test_CONTROL_pip_inspect_is_read_with_per_package_integrity(self):
        with tempfile.TemporaryDirectory() as d:
            dist = os.path.join(d, "Django-5.2.17.dist-info")
            os.makedirs(dist)
            with open(os.path.join(dist, "RECORD"), "w") as fh:
                fh.write("django/__init__.py,sha256=x,1\n")
            packages, problems = pa.parse_inspect(self.inspect([
                {"metadata": {"name": "Django", "version": "5.2.17"}, "metadata_location": dist, "installer": "pip"},
                {"metadata": {"name": "typing_extensions", "version": "4.13.2"}, "installer": "pip"},
            ]), "application")
        self.assertEqual(problems, [])
        self.assertEqual([p["name"] for p in packages], ["django", "typing-extensions"])
        self.assertEqual(packages[0]["path"], "application:site-packages/django")
        self.assertRegex(packages[0]["recordSha256"], r"^[0-9a-f]{64}$")

    def test_CONTRACT_an_empty_or_unreadable_inventory_is_refused(self):
        self.assertEqual([p["code"] for p in pa.parse_inspect(self.inspect([]), "application")[1]], ["empty_inventory"])
        self.assertEqual([p["code"] for p in pa.parse_inspect("not json", "application")[1]], ["inventory_unreadable"])
        self.assertEqual([p["code"] for p in pa.parse_inspect(json.dumps({"x": 1}), "application")[1]], ["inventory_unreadable"])

    def test_CONTRACT_a_package_no_index_advisory_can_describe_is_refused(self):
        _, problems = pa.parse_inspect(self.inspect([{"metadata": {"name": "local", "version": "0.1"}, "direct_url": {"url": "file:///x"}}]), "application")
        self.assertEqual([p["code"] for p in problems], ["not_from_index"])

    def test_CONTRACT_declared_requirements_must_be_installed_at_their_pins(self):
        declared = pa.declared_requirements("# c\nDjango==5.2.17\nrequests==2.34.2 ; python_version > '3'\npillow\n")
        self.assertEqual(declared, {"django": "5.2.17", "requests": "2.34.2", "pillow": None})
        codes = [p["code"] for p in pa.check_declared(PACKAGES, declared, "application")]
        self.assertEqual(codes, ["missing_declared", "missing_declared"])
        codes = [p["code"] for p in pa.check_declared(PACKAGES, {"django": "5.2.16"}, "application")]
        self.assertEqual(codes, ["requirement_not_satisfied"])

    def test_CONTRACT_the_installer_tools_are_tooling_everything_else_runtime(self):
        self.assertEqual(pa.application_scope({"name": "pip"}), "tooling")
        self.assertEqual(pa.application_scope({"name": "cffi"}), "runtime")
        self.assertEqual(pa.tooling_scope({"name": "requests"}), "tooling")

    def test_CONTRACT_the_scan_asks_about_exactly_the_inventory(self):
        self.assertEqual(pa.pins_text(PACKAGES), "django==5.2.17\npip==26.2.1\n")
        args = pa.scanner_args("/r.txt", "/c", 30)
        for flag in ("--no-deps", "--disable-pip", "--strict"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--format") + 1], "json")
        self.assertEqual(args[args.index("--vulnerability-service") + 1], "pypi")
        self.assertNotIn("--ignore-vuln", args)

    def test_REGRESSION_inherited_pip_and_interpreter_configuration_never_reaches_install_or_scan(self):
        env, removed = pa.scrubbed_environment({"PATH": "/bin", "PIP_INDEX_URL": "https://evil.example/", "PIP_AUDIT_VULNERABILITY_SERVICE": "osv",
                                                "PYTHONPATH": "/inject", "PIP_NO_BINARY": ":all:", "VIRTUAL_ENV": "/v", "HOME": "/h"})
        for key in ("PIP_INDEX_URL", "PIP_AUDIT_VULNERABILITY_SERVICE", "PYTHONPATH", "PIP_NO_BINARY", "VIRTUAL_ENV"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(removed, sorted(["PIP_INDEX_URL", "PIP_AUDIT_VULNERABILITY_SERVICE", "PYTHONPATH", "PIP_NO_BINARY", "VIRTUAL_ENV"]))


class ReportTests(unittest.TestCase):
    def test_CONTROL_a_complete_clean_report_is_within_policy_with_zero_findings(self):
        r, _ = decide({"status": 0, "stdout": report()})
        self.assertEqual(r["outcome"], "within_policy")
        self.assertEqual(r["counts"]["findings"], 0)

    def test_CONTRACT_an_applicable_runtime_advisory_blocks_and_duplicates_collapse(self):
        r, _ = decide({"status": 1, "stdout": report({"django": [VULN, dict(VULN, fix_versions=["5.2.18.0"])]})})
        self.assertEqual(r["outcome"], "blocking")
        self.assertEqual(r["counts"]["findings"], 1, "PyPI can list one advisory twice; it is one finding")
        f = r["findings"][0]
        self.assertEqual((f["advisory"], f["scope"], f["severity"]), ("PYSEC-2026-1", "runtime", "unknown"))
        self.assertEqual(f["aliases"], ["CVE-2026-1", "GHSA-aaaa-bbbb-cccc"])

    def test_CONTRACT_an_advisory_on_pip_cannot_be_evaluated_without_a_severity(self):
        r, _ = decide({"status": 1, "stdout": report({"pip": [VULN]})})
        self.assertEqual(r["outcome"], "incomplete")

    INCOMPLETE = [
        ("a timeout", {"status": None, "timedOut": True, "stdout": ""}, "scanner_timeout"),
        ("a scanner that could not start", {"status": None, "error": "FileNotFoundError", "stdout": ""}, "scanner_error"),
        ("a signal", {"status": None, "signal": "signal 9", "stdout": ""}, "scanner_signal"),
        ("an unexpected status", {"status": 2, "stdout": report()}, "scanner_status"),
        ("empty output (a network failure prints a traceback to stderr and nothing here)", {"status": 1, "stdout": "", "stderr": "requests.exceptions.ProxyError: ..."}, "scanner_empty"),
        ("non-JSON output", {"status": 1, "stdout": "Traceback (most recent call last):"}, "scanner_unparseable"),
        ("truncated JSON", {"status": 1, "stdout": report()[:30]}, "scanner_unparseable"),
        ("a body with no dependency list", {"status": 0, "stdout": json.dumps({"fixes": []})}, "scanner_shape"),
        ("an empty dependency list", {"status": 0, "stdout": json.dumps({"dependencies": [], "fixes": []})}, "coverage_empty"),
        ("a skipped package", {"status": 1, "stdout": json.dumps({"dependencies": [{"name": "django", "version": "5.2.17", "vulns": []}, {"name": "pip", "skip_reason": "not on PyPI"}]})}, "coverage_skipped"),
        ("an inventory package the scanner never reported", {"status": 0, "stdout": report(drop=("pip",))}, "coverage_mismatch"),
        ("a reported version that is not the inventory's", {"status": 0, "stdout": json.dumps({"dependencies": [{"name": "django", "version": "4.0", "vulns": []}, {"name": "pip", "version": "26.2.1", "vulns": []}]})}, "coverage_mismatch"),
        ("a package outside the inventory", {"status": 0, "stdout": report(extra=[{"name": "ghost", "version": "1", "vulns": []}])}, "coverage_unknown_node"),
        ("exit 0 contradicting listed findings", {"status": 0, "stdout": report({"django": [VULN]})}, "scanner_inconsistent"),
        ("exit 1 with nothing to explain it", {"status": 1, "stdout": report()}, "scanner_status"),
    ]

    def test_CONTRACT_every_unusable_answer_is_incomplete_and_fails_the_required_check(self):
        for label, run, code in self.INCOMPLETE:
            with self.subTest(label):
                r, problems = decide(run)
                self.assertIn(code, [p["code"] for p in problems])
                self.assertEqual(r["outcome"], "incomplete")
                self.assertEqual(r["exitCode"], 2)
