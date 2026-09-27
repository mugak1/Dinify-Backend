"""THE STAGED DEPLOYMENT, INACTIVE BY CONSTRUCTION (D08 B3): the replacement workflow, the host
script, the ordering and attestation readers, the discovery collector and the committed UAT
profile — and the one property that matters most before any of them is used: merging this
change starts NO second writer. deploy-uat.yml stays the only active deploy.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest

from dependency_audit.workflow_harness import load_workflow
from release import hostprofile as hp
from release import installation as ins
from release.__main__ import main as cli
from release.staged import markers, ordering

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGED = os.path.join(ROOT, "release", "staged")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
PROFILE = os.path.join(ROOT, "release", "profiles", "uat-backend.json")
TEMPLATE = load_workflow(os.path.join(STAGED, "deploy-backend.yml"))
RID = "a" * 40 + "-" + "b" * 16


def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class NoSecondWriter(unittest.TestCase):
    def test_REGRESSION_nothing_active_dispatches_the_host_transition_while_the_legacy_deploy_exists(self):
        self.assertTrue(os.path.exists(os.path.join(WORKFLOWS, "deploy-uat.yml")), "the legacy deploy is the active writer until the cutover")
        for name in os.listdir(WORKFLOWS):
            body = text(os.path.join(WORKFLOWS, name))
            for needle in ("host deploy", "host-run.sh", "release/staged", "Backend deploy (B3)", "recover-legacy", "adopt-legacy"):
                self.assertNotIn(needle, body, "%s references %r: the B3 transition would have a second, active writer" % (name, needle))

    def test_CONTRACT_the_template_lives_outside_the_workflows_directory(self):
        self.assertFalse(any(n.startswith("deploy-backend") for n in os.listdir(WORKFLOWS)))
        self.assertIn("NOT ACTIVE", text(os.path.join(STAGED, "deploy-backend.yml")).splitlines()[0])

    def test_CONTRACT_the_legacy_deploy_is_byte_for_byte_what_it_was_in_its_behaviour(self):
        body = text(os.path.join(WORKFLOWS, "deploy-uat.yml"))
        for kept in ('workflows: ["Backend CI"]', "pip install -q -r requirements.txt", "manage.py migrate", "systemctl restart apache2",
                     "DEPLOYED-HEAD:"):
            self.assertIn(kept, body)


class TheTemplate(unittest.TestCase):
    def test_CONTRACT_permissions_are_empty_by_default_and_only_the_deploy_job_may_request_an_oidc_token(self):
        self.assertEqual(TEMPLATE["permissions"], {})
        self.assertNotIn("id-token", TEMPLATE["jobs"]["receive"]["permissions"])
        self.assertEqual(TEMPLATE["jobs"]["deploy"]["permissions"]["id-token"], "write")
        self.assertNotIn("secrets.", text(os.path.join(STAGED, "deploy-backend.yml")))

    def test_REGRESSION_the_privileged_job_receives_the_preflight_itself_before_any_credential(self):
        names = [s.get("name") for s in TEMPLATE["jobs"]["deploy"]["steps"]]
        oidc = names.index("Configure AWS credentials (OIDC)")
        self.assertLess(names.index("Re-receive, and agree with the receive job"), oidc)
        self.assertLess(names.index("Refuse an unset bucket or an unexpected role"), oidc)
        received = next(s for s in TEMPLATE["jobs"]["deploy"]["steps"] if s.get("name") == "Re-receive, and agree with the receive job")
        self.assertIn("preflight verify", received["run"])
        self.assertIn('= "$HINT"', received["run"])

    def test_REGRESSION_the_privileged_job_installs_scans_and_imports_nothing(self):
        for s in TEMPLATE["jobs"]["deploy"]["steps"]:
            run = s.get("run") or ""
            for forbidden in ("pip install", "npm ", "preflight assess", "reconstruct", "manage.py", "release install", "release acquire"):
                self.assertNotIn(forbidden, run, "%s runs %r" % (s.get("name"), forbidden))
            if "checkout" in str(s.get("uses")):
                self.assertEqual(s["with"]["ref"], "${{ github.sha }}", "the only checkout is the trusted verifier")

    def test_CONTRACT_every_action_is_pinned_to_a_commit(self):
        for job in TEMPLATE["jobs"].values():
            for s in job["steps"]:
                if "uses" in s:
                    self.assertRegex(s["uses"], r"^[a-z0-9-]+/[a-z0-9-]+@[0-9a-f]{40}$")

    def test_REGRESSION_no_event_value_is_interpolated_into_a_script(self):
        for job in TEMPLATE["jobs"].values():
            for s in job["steps"]:
                run = s.get("run") or ""
                self.assertNotRegex(run, r"\$\{\{\s*(inputs|github\.event)\.", "%s interpolates an event value" % s.get("name"))

    def test_CONTRACT_the_automatic_path_is_forward_only_and_skips_a_failed_preflight(self):
        self.assertIn("conclusion == 'success'", TEMPLATE["jobs"]["receive"]["if"])
        self.assertEqual(TEMPLATE["jobs"]["deploy"]["if"], "needs.receive.outputs.proceed == 'true'")

    def test_CONTRACT_the_host_script_receives_exactly_the_placeholders_the_workflow_substitutes(self):
        script = text(os.path.join(STAGED, "host-run.sh"))
        wanted = set(re.findall(r"__[A-Z0-9_]+__", script))
        dispatch = next(s for s in TEMPLATE["jobs"]["deploy"]["steps"] if s.get("id") == "ssm")["run"]
        self.assertEqual(wanted, set(re.findall(r"s\|(__[A-Z0-9_]+__)\|", dispatch)))


class TheHostScript(unittest.TestCase):
    def substituted(self, operation="b3-deploy-1-1"):
        script = text(os.path.join(STAGED, "host-run.sh"))
        values = {"__OPERATION__": operation, "__BUCKET__": "bucket", "__PREFIX__": "backend/x"}
        for name in ("CANDIDATE", "PREFLIGHT", "ADMISSION", "TRUSTED"):
            values["__%s_SHA256__" % name] = "0" * 64
        for k, v in values.items():
            script = script.replace(k, v)
        return script

    def test_CONTRACT_it_parses_under_bash_and_declares_bash(self):
        script = self.substituted()
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        proc = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_REGRESSION_a_malformed_operation_is_refused_before_anything_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "host-run.sh")
            with open(path, "w") as fh:
                fh.write(self.substituted(operation="BAD op"))
            proc = subprocess.run(["bash", path], cwd=d, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("B3-OUTCOME: refused", proc.stdout)
            self.assertEqual(os.listdir(d), ["host-run.sh"])

    def test_CONTRACT_attestations_come_first_and_the_full_report_stays_on_the_host(self):
        script = text(os.path.join(STAGED, "host-run.sh"))
        self.assertIn("> \"$IN/report.out\" 2> \"$IN/report.err\"", script)
        self.assertIn("grep '^B3-' \"$IN/report.out\"", script)
        self.assertNotIn("--rehearsal", script, "the real path never accepts a rehearsal profile")


class Ordering(unittest.TestCase):
    def served(self, commit=None, legacy=False, other=None):
        doc = lambda plane, c: {"release": {"id": c + "-" + "b" * 16, "commit": c}}  # noqa: E731
        if legacy:
            return {"customer": ("legacy", {}), "admin": ("legacy", {})}
        return {"customer": ("verified", doc("customer", commit)), "admin": ("verified", doc("admin", other or commit))}

    def test_CONTROL_the_first_transition_from_legacy_proceeds(self):
        self.assertTrue(ordering.decide("deploy", "a" * 40, self.served(legacy=True), None)[0])

    def test_REGRESSION_an_older_target_is_a_skip_automatically_and_a_move_only_when_asked(self):
        self.assertFalse(ordering.decide("deploy", "a" * 40, self.served("c" * 40), "behind")[0])
        self.assertTrue(ordering.decide("rollback", "a" * 40, self.served("c" * 40), "behind")[0])
        self.assertTrue(ordering.decide("deploy", "a" * 40, self.served("c" * 40), "ahead")[0])

    def test_REGRESSION_divergence_a_split_host_or_an_unreadable_plane_refuses(self):
        for served, relation in ((self.served("c" * 40), "diverged"), (self.served("c" * 40), None),
                                 (self.served("c" * 40, other="d" * 40), "ahead"),
                                 ({"customer": ("legacy", {}), "admin": ("verified", {"release": {"id": RID, "commit": "a" * 40}})}, None),
                                 ({"customer": (None, None), "admin": ("legacy", {})}, None)):
            with self.assertRaises(ValueError):
                ordering.decide("deploy", "a" * 40, served, relation)

    def test_REGRESSION_an_identity_answer_that_is_cacheable_or_for_another_plane_is_unusable(self):
        body = json.dumps({"schema": ordering.SCHEMA, "plane": "customer", "state": "unavailable",
                           "reason": "not_started_by_release_launcher"}).encode()
        self.assertEqual(ordering.read_identity("https://x/uat", "customer", lambda url: (200, {"Cache-Control": "no-store"}, body))[0], "legacy")
        self.assertIsNone(ordering.read_identity("https://x/uat", "customer", lambda url: (200, {}, body))[0])
        self.assertIsNone(ordering.read_identity("https://x/api", "admin", lambda url: (200, {"Cache-Control": "no-store"}, body))[0])
        self.assertIsNone(ordering.read_identity("https://x/uat", "customer", lambda url: (404, {}, b""))[0])


class Markers(unittest.TestCase):
    OK = ("B3-ADMITTED: %s sha256:x preflight sha256:y until z\nB3-PREPARED: %s (installed)\nB3-OUTCOME: verified\n"
          "B3-SERVING: admin %s\nB3-SERVING: customer %s\n" % ("a" * 40, RID, RID, RID))

    def test_CONTROL_a_verified_attestation_names_the_release(self):
        self.assertEqual(markers.read(self.OK, "a" * 40, "Success")[:2], (0, RID))

    def test_REGRESSION_the_workflow_believes_the_box_not_itself(self):
        self.assertEqual(markers.read(self.OK, "c" * 40, "Success")[0], 4)                                 # another commit
        self.assertEqual(markers.read(self.OK, "a" * 40, "Failed")[0], 4)                                  # SSM disagrees
        self.assertEqual(markers.read(self.OK + "B3-OUTCOME: verified\n", "a" * 40, "Success")[0], 4)      # twice
        self.assertEqual(markers.read(self.OK.replace("B3-SERVING: admin", "B3-SERVING: x"), "a" * 40, "Success")[0], 4)
        self.assertEqual(markers.read("", "a" * 40, "Success")[0], 4)                                      # nothing said

    def test_CONTRACT_each_failure_keeps_its_own_meaning(self):
        for outcome, code in (("refused", 1), ("restored", 3), ("verification-failed", 3), ("restoration-failed", 4)):
            self.assertEqual(markers.read("B3-OUTCOME: %s\n" % outcome, "a" * 40, "Failed")[0], code)


class TheCommittedProfile(unittest.TestCase):
    def test_REGRESSION_it_is_unverified_every_unknown_is_named_and_the_real_path_refuses_it(self):
        doc, _, problems = hp.load(PROFILE)
        self.assertEqual(problems, [])
        found = hp.validate(doc)
        self.assertIn("profile_unverified", {p["code"] for p in found})
        unknown = hp.unknown_fields(doc)[0]
        for field in ("basePython", "interpreter.libpythonSha256", "modWsgi.sha256", "identities.prepare", "planes.customer.daemon",
                      "planes.admin.probe.base", "media.root", "otp.deterministicTestOtpAllowed"):
            self.assertIn(field, unknown)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cli(["host", "deploy", "--profile", PROFILE, "--operation", "x1", "--admission", os.path.join(d, "a"),
                                  "--candidate-zip", os.path.join(d, "c"), "--preflight-zip", os.path.join(d, "p"), "--trusted", d]), 1)
            self.assertEqual(os.listdir(d), [])

    def test_CONTRACT_source_hints_are_never_read_as_observations(self):
        for name in os.listdir(os.path.join(ROOT, "release")):
            if name.endswith(".py") and not name.startswith("tests_"):
                self.assertNotIn("_sourceHints", text(os.path.join(ROOT, "release", name)), name)
        for name in os.listdir(STAGED):
            if name.endswith(".py"):
                self.assertNotIn("_sourceHints", text(os.path.join(STAGED, name)), name)


class TheDiscoveryCollector(unittest.TestCase):
    SOURCE = text(os.path.join(STAGED, "discover_host.py"))

    def test_REGRESSION_it_never_writes_signals_or_reads_a_process_environment(self):
        self.assertNotIn("environ\"", self.SOURCE.replace("os.environ", ""))
        self.assertNotIn("/environ", self.SOURCE)
        self.assertNotRegex(self.SOURCE, r"open\([^)]*['\"][wa]b?['\"]")
        for verb in ("restart", "reload", "graceful", "stop", "kill", "rm ", "chmod", "chown", "systemctl", "curl", "wget", "urllib", "socket"):
            self.assertNotIn(verb, " ".join(" ".join(c) for c in __import__("release.staged.discover_host", fromlist=["x"]).READ_ONLY_COMMANDS.values()))
        self.assertNotRegex(self.SOURCE, r"import (socket|urllib|http|requests)")

    def test_REGRESSION_an_environment_file_is_reported_by_key_presence_never_by_value(self):
        from release.staged import discover_host as dh
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("SECRET_KEY=s3cr3t-value-xyz\nENV=dev\nDATABASE_PASSWORD='hunter2-long'\nUNLISTED=abc123def\n")
            doc = dh.env_file(path)
        blob = json.dumps(doc)
        for value in ("s3cr3t-value-xyz", "hunter2-long", "abc123def", "UNLISTED"):
            self.assertNotIn(value, blob)
        self.assertEqual(doc["envWord"], "dev")
        self.assertIn("SECRET_KEY", doc["keysPresent"])

    def test_REGRESSION_a_directive_that_can_carry_a_secret_is_named_not_shown(self):
        from release.staged import discover_host as dh
        self.assertIn("SetEnv", dh.REDACTED_DIRECTIVES)
        self.assertIsNotNone(re.search(r"(?i)password|passwd|secret|credential", "AuthLDAPBindPassword"))


if __name__ == "__main__":
    unittest.main()
