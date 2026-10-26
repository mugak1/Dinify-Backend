"""THE WIRING: the candidate is produced inside the required check, only after every
required step succeeded, and the independent reconstruction is part of what ``test``
requires. Claims about behaviour are made by EXECUTING the committed step text under the
runner's own shell (``bash -e``, no pipefail) and GitHub's step sequencing
(dependency_audit/workflow_harness.py) — not by reading it.

And the boundary this delivery does NOT cross: the live deployment is untouched.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest

from dependency_audit.workflow_harness import command_of, load_workflow, run_aggregator, simulate_job, status_swallowers
from release import candidate as cd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = lambda name: os.path.join(ROOT, ".github", "workflows", name)  # noqa: E731
CI = load_workflow(WF("ci.yml"))
DEPLOY = load_workflow(WF("deploy-uat.yml"))
SUITE = CI["jobs"]["suite"]["steps"]
RECONSTRUCT = CI["jobs"]["reconstruct"]
AGGREGATE = CI["jobs"]["test"]["steps"][0]["run"]


def step(step_id, steps=SUITE):
    return next(s for s in steps if s.get("id") == step_id)


def execute(script, stub_exit):
    """Run a step's committed text under ``bash -e`` with ``python`` stubbed to exit
    ``stub_exit`` and the runner's files present. Returns (status, GITHUB_PATH contents)."""
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "python")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit %d\n" % stub_exit)
        os.chmod(stub, 0o755)
        env = {"PATH": "%s:/usr/bin:/bin" % d, "HOME": d, "RUNNER_TEMP": d}
        for key in ("GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_ENV"):
            env[key] = os.path.join(d, key.lower())
            open(env[key], "w").close()
        with open(os.path.join(d, "step.sh"), "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run(["bash", "-e", os.path.join(d, "step.sh")], cwd=d, env=env, capture_output=True, text=True, timeout=30, check=False)
        with open(env["GITHUB_PATH"], "r", encoding="utf-8") as fh:
            return proc.returncode, fh.read()


class TheCandidateIsProducedInsideTheRequiredCheck(unittest.TestCase):
    def test_CONTRACT_every_validating_step_is_required_by_id_and_the_list_cannot_drift(self):
        ids = [s["id"] for s in SUITE if s.get("id")]
        self.assertEqual(ids[-1], "package")
        self.assertEqual(ids[:-1], list(cd.REQUIRED_STEPS), "a new step with an id must be added to REQUIRED_STEPS, in order")

    def test_CONTRACT_the_only_installation_is_the_certified_environment(self):
        with open(WF("ci.yml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("pip install -r requirements.txt", text)
        self.assertNotIn("--upgrade pip", text)
        setup = next(s for s in SUITE if str(s.get("uses", "")).startswith("actions/setup-python"))
        self.assertNotIn("cache", setup["with"], "nothing installs from an index, so nothing may be cached")
        self.assertEqual(CI["jobs"]["suite"]["runs-on"], "ubuntu-24.04")
        install = command_of(step("install"))
        self.assertIn('python -B -m release install', install)
        self.assertIn('>> "$GITHUB_PATH"', install)
        self.assertEqual(SUITE.index(step("snapshot")), SUITE.index(step("install")) + 1)
        self.assertEqual(SUITE.index(step("interpreter")), SUITE.index(step("snapshot")) + 1)

    def test_CONTRACT_the_producer_steps_are_unconditional_bytecode_free_and_expression_free(self):
        for step_id in ("lock", "observe", "acquire", "install", "interpreter", "release-tests", "package"):
            s = step(step_id)
            for key in ("if", "continue-on-error", "shell"):
                self.assertNotIn(key, s, step_id)
            self.assertNotIn("${{", s["run"], "%s: context reaches the script through the environment, never inline" % step_id)
            for line in s["run"].splitlines():
                if "-m release" in line:
                    self.assertIn("python -B -m release", line, step_id)
        package = step("package")
        self.assertEqual(package["env"]["STEP_OUTCOMES"], "${{ toJSON(steps) }}")
        self.assertEqual(status_swallowers(command_of(package)), [])

    def test_CONTRACT_both_release_test_patterns_run_and_neither_is_django_discoverable(self):
        run = step("release-tests")["run"]
        self.assertIn('-p "tests_*.py"', run)
        self.assertIn('-p "qualify_*.py"', run)
        self.assertEqual(status_swallowers(run), [])
        heavy = sorted(f for f in os.listdir(os.path.join(ROOT, "release")) if f.startswith("qualify_"))
        self.assertEqual(heavy, ["qualify_candidate.py", "qualify_environment.py", "qualify_preflight.py"])
        self.assertFalse([f for f in heavy if f.startswith("test")], "the Django runner discovers test*.py")

    def test_CONTRACT_the_upload_carries_exactly_the_packaged_candidate(self):
        upload = SUITE[SUITE.index(step("package")) + 1]
        self.assertTrue(upload["uses"].startswith("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"))
        self.assertEqual(upload["with"]["name"], "${{ steps.package.outputs.artifact }}")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertNotIn("if", upload)
        self.assertEqual(CI["jobs"]["suite"]["outputs"]["candidate"], "${{ steps.package.outputs.artifact }}")


class AFailureNeverYieldsACandidateOrAGreenCheck(unittest.TestCase):
    @staticmethod
    def outcome(failing, result="failure"):
        return lambda s, i: result if s.get("id") in failing else "success"

    def test_CONTROL_an_all_green_leg_packages_and_uploads(self):
        status, ran = simulate_job(SUITE, self.outcome(()))
        self.assertEqual(status, "success")
        self.assertIn(("Upload the candidate", "success"), ran)
        self.assertEqual(run_aggregator(AGGREGATE, "success"), 0)

    def test_REGRESSION_MATRIX_any_required_step_failing_skips_packaging_and_upload_but_keeps_evidence(self):
        for failing in cd.REQUIRED_STEPS:
            status, ran = simulate_job(SUITE, self.outcome((failing,)))
            self.assertEqual(status, "failure", failing)
            self.assertIn(("Package the certified candidate", "skipped"), ran, failing)
            self.assertIn(("Upload the candidate", "skipped"), ran, failing)
            self.assertIn(("Retain the dependency-audit evidence", "success"), ran, "evidence survives a failure (%s)" % failing)
            self.assertNotEqual(run_aggregator(AGGREGATE, {"suite": status, "reconstruct": "skipped"}), 0, failing)

    def test_REGRESSION_packaging_that_refuses_fails_the_leg_and_retained_evidence_cannot_rescue_it(self):
        status, ran = simulate_job(SUITE, self.outcome(("package",)))
        self.assertEqual(status, "failure")
        self.assertIn(("Upload the candidate", "skipped"), ran)

    def test_REGRESSION_MATRIX_other_suites_pass_while_the_reconstruction_fails_and_test_stays_red(self):
        for result in ("failure", "cancelled", "skipped"):
            self.assertNotEqual(run_aggregator(AGGREGATE, {"suite": "success", "reconstruct": result}), 0, result)
        self.assertEqual(run_aggregator(AGGREGATE, {"suite": "success", "reconstruct": "success"}), 0, "CONTROL")

    def test_REGRESSION_the_committed_step_text_carries_the_producer_status_under_the_runner_shell(self):
        for step_id in ("lock", "observe", "acquire", "interpreter", "release-tests", "package"):
            self.assertEqual(execute(step(step_id)["run"], 0)[0], 0, step_id)
            self.assertEqual(execute(step(step_id)["run"], 1)[0], 1, step_id)
        status, path = execute(step("install")["run"], 1)
        self.assertEqual((status, path), (1, ""), "a refused install never puts its environment on PATH")
        status, path = execute(step("install")["run"], 0)
        self.assertEqual(status, 0)
        self.assertTrue(path.strip().endswith("/certified-venv/bin"))
        self.assertEqual(execute(RECONSTRUCT["steps"][3]["run"], 1)[0], 1)

    def test_NEGATIVE_CONTROL_a_pipe_would_have_hidden_a_refusal(self):
        self.assertEqual(execute("python -B -m release package | tee package.log\n", 1)[0], 0)


class TheReceivingSideIsIndependentAndUnprivileged(unittest.TestCase):
    def test_CONTRACT_the_reconstruction_is_a_separate_unprivileged_job_that_test_requires(self):
        self.assertEqual(RECONSTRUCT["needs"], ["suite"])
        self.assertEqual(RECONSTRUCT["permissions"], {"contents": "read"})
        self.assertEqual(RECONSTRUCT["runs-on"], "ubuntu-24.04")
        self.assertEqual(CI["jobs"]["test"]["needs"], ["suite", "reconstruct"])
        self.assertEqual(CI["jobs"]["test"]["if"], "always()")
        with open(WF("ci.yml"), "r", encoding="utf-8") as fh:
            self.assertNotIn("secrets.", fh.read())

    def test_CONTRACT_it_runs_its_own_consumer_and_receives_the_candidate_as_data(self):
        checkout, setup, receive, rebuild, retain = RECONSTRUCT["steps"]
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertEqual(checkout["with"]["sparse-checkout"].split(), ["release", "dependency_audit"])
        self.assertEqual(setup["with"]["python-version"], "3.12.3")
        self.assertTrue(receive["uses"].startswith("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"))
        self.assertEqual((receive["with"]["name"], receive["with"]["digest-mismatch"]), ("${{ needs.suite.outputs.candidate }}", "error"))
        run = rebuild["run"]
        self.assertNotIn("${{", run)
        for expectation in ('--expect-commit "$GITHUB_SHA"', '--expect-run-id "$GITHUB_RUN_ID"', '--expect-run-attempt "$GITHUB_RUN_ATTEMPT"',
                            '--expect-event "$GITHUB_EVENT_NAME"', '--expect-ref "$GITHUB_REF"', '--expect-artifact "$RECEIVED_ARTIFACT"'):
            self.assertIn(expectation, run)
        self.assertNotIn("if", rebuild)
        self.assertEqual(retain["if"], "always()")
        for s in [RECONSTRUCT] + RECONSTRUCT["steps"]:
            self.assertNotIn("continue-on-error", s, "a reconstruction that fails must fail its job")
        self.assertEqual(status_swallowers(run), [])


class TheLiveDeploymentIsUntouched(unittest.TestCase):
    def test_CONTRACT_a_branch_or_pull_request_run_cannot_deploy(self):
        self.assertEqual(DEPLOY["on"]["workflow_run"]["workflows"], [CI["name"]])
        self.assertEqual(DEPLOY["on"]["workflow_run"]["branches"], ["main"])
        self.assertIn("workflow_run.conclusion == 'success'", DEPLOY["jobs"]["deploy"]["if"])

    def test_CONTRACT_the_deployment_still_installs_from_requirements_and_consumes_no_candidate(self):
        with open(WF("deploy-uat.yml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('pip install -q -r requirements.txt', text, "the live path has NOT acquired the candidate's exact-package guarantee")
        for word in ("backend-candidate", "python-lock", "release reconstruct", "download-artifact"):
            self.assertNotIn(word, text)

    def test_CONTRACT_requirements_txt_remains_the_direct_input_contract_the_deploy_reads(self):
        with open(os.path.join(ROOT, "requirements.txt"), "r", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l and not l.startswith("#")]
        self.assertTrue(all("==" in l for l in lines))
        with open(os.path.join(ROOT, "release", "python-lock.json"), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["directInputs"]["path"], "requirements.txt")



# ─────────────────────────────────────────────────────────────────────────────────────
# THE WINDOW JOB (CI-signal C3). The development server runs only inside the owner's AWS
# schedule. ``window`` decides PROCEED / DEFER / FAIL before anything else runs, holds only
# ``actions: write``, and a deferred run cancels itself and can never conclude green. The
# mixin is applied to the live deploy here and to the staged B3 workflow in tests_staged.py.
# ─────────────────────────────────────────────────────────────────────────────────────
SELF_TEST = "Self-test the window decision"
DECIDE = "Decide PROCEED, DEFER or FAIL"
ENFORCE = "Enforce the decision (a deferred run cancels itself)"
REAL_DATE = "/usr/bin/date" if os.path.exists("/usr/bin/date") else "/bin/date"
CURL_SILENT = "printf '000'; exit 28"


def _epoch(iso):
    import datetime
    return str(int(datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()))


def run_window_step(job, name, env=None, stubs=None, at=None):
    """Execute a window step's committed text under ``bash -e`` (the runner's default shell)
    with the job's env, stub tools first on PATH, and optionally a fixed clock. Returns
    (status, stdout, stderr, outputs, summary, calls)."""
    with tempfile.TemporaryDirectory() as d:
        bindir = os.path.join(d, "bin")
        os.mkdir(bindir)
        tools = {"sleep": "exit 0"}
        if at:
            tools["date"] = 'if [ "$*" = "-u +%%s" ]; then echo %s; else exec %s "$@"; fi' % (_epoch(at), REAL_DATE)
        tools.update(stubs or {})
        calls = os.path.join(d, "calls.log")
        open(calls, "w").close()
        for tool, body in tools.items():
            with open(os.path.join(bindir, tool), "w", encoding="utf-8") as fh:
                fh.write('#!/bin/bash\necho "%s $*" >> "%s"\n%s\n' % (tool, calls, body))
            os.chmod(os.path.join(bindir, tool), 0o755)
        files = {k: os.path.join(d, k.lower()) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
        for f in files.values():
            open(f, "w").close()
        full = {"PATH": "%s:/usr/bin:/bin" % bindir, "HOME": d, "GITHUB_RUN_ID": "4242", "GITHUB_REPOSITORY": "mugak1/Dinify-Backend"}
        full.update({k: job["env"][k] for k in ("DEV_SERVER_WINDOW", "DEV_SERVER_WINDOW_END_MARGIN_MINUTES", "WINDOW_PROBE_URL", "WINDOW_DECISION_SH")})
        full.update(files)
        full.update(env or {})
        script = os.path.join(d, "step.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(next(s for s in job["steps"] if s.get("name") == name)["run"])
        proc = subprocess.run(["bash", "-e", script], cwd=d, env=full, capture_output=True, text=True, timeout=60, check=False)
        with open(files["GITHUB_OUTPUT"], encoding="utf-8") as fh:
            outputs = dict(l.split("=", 1) for l in fh.read().splitlines() if "=" in l)
        with open(files["GITHUB_STEP_SUMMARY"], encoding="utf-8") as fh:
            summary = fh.read()
        with open(calls, encoding="utf-8") as fh:
            called = fh.read().splitlines()
        return proc.returncode, proc.stdout, proc.stderr, outputs, summary, called


class WindowJobContract:
    """Mixin. Concrete classes set WORKFLOW (parsed), WORKFLOW_TEXT, DEPLOYING (job names
    that must need ``window``), FIRST_GATED (the job whose event gate ``window`` copies),
    TITLE (the workflow's display name) and PRE_OIDC (job, step) pairs that must still
    precede the credential."""

    def job(self):
        return self.WORKFLOW["jobs"]["window"]

    def test_CONTRACT_it_holds_exactly_actions_write_and_runs_no_action_checkout_or_package_tool(self):
        job = self.job()
        self.assertEqual(job["permissions"], {"actions": "write"})
        self.assertNotIn("needs", job)
        for s in job["steps"]:
            self.assertNotIn("uses", s, s.get("name"))
            self.assertNotIn("${{", s["run"], "%s interpolates into run:" % s.get("name"))
            self.assertNotRegex(s["run"], r"\bpip\b|\bnpm\b|\bgit\b|\bpython\b|checkout", s.get("name"))
        self.assertNotRegex(json.dumps(job), r"secrets\.|id-token")
        for name, other in self.WORKFLOW["jobs"].items():
            if name != "window":
                self.assertNotEqual((other.get("permissions") or {}).get("actions"), "write", name)

    def test_CONTRACT_it_is_first_gated_like_the_job_it_precedes_and_every_deploying_job_needs_it(self):
        self.assertEqual(list(self.WORKFLOW["jobs"])[0], "window")
        self.assertEqual(self.job()["if"], self.WORKFLOW["jobs"][self.FIRST_GATED]["if"])
        for name in self.DEPLOYING:
            needs = self.WORKFLOW["jobs"][name]["needs"]
            self.assertIn("window", [needs] if isinstance(needs, str) else needs, name)

    def test_CONTRACT_the_existing_pre_oidc_steps_still_precede_the_credential(self):
        for job_name, step_name in self.PRE_OIDC:
            names = [s.get("name") for s in self.WORKFLOW["jobs"][job_name]["steps"]]
            self.assertIn(step_name, names)
            if "Configure AWS credentials (OIDC)" in names:   # otherwise the job holds no credential at all
                self.assertLess(names.index(step_name), names.index("Configure AWS credentials (OIDC)"), step_name)

    def test_CONTRACT_the_window_is_written_once_as_london_wall_time_with_a_20_minute_margin(self):
        env = self.job()["env"]
        self.assertEqual(env["DEV_SERVER_WINDOW"], "11:00–23:00 Europe/London")
        self.assertEqual(env["DEV_SERVER_WINDOW_END_MARGIN_MINUTES"], "20")
        self.assertEqual(self.WORKFLOW_TEXT.count("11:00–23:00 Europe/London"), 1)
        self.assertIn('TZ="$zone" date -d', env["WINDOW_DECISION_SH"])
        self.assertNotRegex(env["WINDOW_DECISION_SH"], r"\+0[0-9]:?00|UTC[+-]|TZ=UTC")

    def test_CONTRACT_the_defer_path_cannot_reach_a_zero_exit(self):
        job = self.job()
        self.assertEqual([s["name"] for s in job["steps"]], [SELF_TEST, DECIDE, ENFORCE])
        for s in [job] + job["steps"]:
            self.assertNotIn("continue-on-error", s)
        for s in job["steps"]:
            self.assertNotIn("if", s, s["name"])
        enforce = next(s for s in job["steps"] if s["name"] == ENFORCE)["run"]
        self.assertEqual([l.strip() for l in enforce.splitlines() if re.search(r"\bexit 0\b", l)], ["PROCEED) exit 0 ;;"])
        self.assertTrue(enforce.rstrip().endswith("exit 1"))
        self.assertNotRegex(next(s for s in job["steps"] if s["name"] == DECIDE)["run"], r"\bexit 0\b")
        for out in ("failure", "cancelled"):
            self.assertNotEqual(simulate_job(job["steps"], lambda s, i, out=out: out if s["name"] == ENFORCE else "success")[0], "success")

    # ---- executed ----
    def test_CONTROL_the_self_test_passes_under_the_real_tz_database_including_both_dst_days(self):
        status, out, err, _, _, _ = run_window_step(self.job(), SELF_TEST)
        self.assertEqual(status, 0, out + err)
        self.assertIn("window self-test: all cases passed", out)
        for stamp in ("2026-03-29 02:30 BST", "2026-03-29 11:30 BST", "2026-10-25 01:30 BST", "2026-10-25 01:30 GMT",
                      "2026-10-25 22:30 GMT", "2026-10-26 22:45 GMT"):
            self.assertIn("at %s:" % stamp, out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("ok ")]), 22)

    def test_NEGATIVE_CONTROL_a_broken_decision_fails_the_self_test(self):
        lib = self.job()["env"]["WINDOW_DECISION_SH"]
        for old, new in (('TZ="$zone" date -d', "TZ=UTC date -d"), ('[ "$minute" -lt "$end" ]', '[ "$minute" -le "$end" ]'),
                         ("end=$((end - 10#${margin}))", "end=$((end - 0))"), ("WINDOW_DECISION=PROCEED", "WINDOW_DECISION=DEFER")):
            self.assertIn(old, lib)
            self.assertEqual(run_window_step(self.job(), SELF_TEST, env={"WINDOW_DECISION_SH": lib.replace(old, new)})[0], 1, new)

    def test_CONTROL_an_answering_origin_proceeds_off_hours(self):
        status, _, err, outputs, summary, _ = run_window_step(self.job(), DECIDE, env=self.auto(), at="2026-10-27T03:00:00Z",
                                                              stubs={"curl": "printf '502'"})
        self.assertEqual((status, outputs.get("decision"), summary), (0, "PROCEED", ""), err)

    def test_REGRESSION_silence_inside_the_window_fails_red_after_three_attempts(self):
        status, _, err, outputs, _, calls = run_window_step(self.job(), DECIDE, env=self.auto(), at="2026-10-26T14:00:00Z",
                                                            stubs={"curl": CURL_SILENT})
        self.assertEqual((status, outputs.get("decision")), (1, "FAIL"))
        self.assertEqual(len([c for c in calls if c.startswith("curl ")]), 3)
        self.assertIn("a real outage", err)

    def test_CONTRACT_silence_off_hours_defers_with_the_target_the_london_time_and_the_recovery(self):
        target = "c3" * 20
        status, _, err, outputs, summary, _ = run_window_step(self.job(), DECIDE, env=self.auto(target), at="2026-10-26T23:30:00Z",
                                                              stubs={"curl": CURL_SILENT})
        self.assertEqual((status, outputs.get("decision")), (0, "DEFER"), err)
        self.assertIn("- Target SHA: `%s`" % target, summary)
        self.assertIn("- London time: 2026-10-26 23:30 GMT", summary)
        self.assertIn("The development server is scheduled off (11:00–23:00 Europe/London). Re-run all jobs at or after 11:00 London, "
                      "or dispatch %s with sha=%s." % (self.TITLE, target), summary)

    def test_CONTRACT_a_manual_run_is_deferred_the_same_way(self):
        target = "d4" * 20
        _, _, _, outputs, summary, _ = run_window_step(self.job(), DECIDE, at="2026-10-26T10:00:00Z", stubs={"curl": CURL_SILENT},
                                                       env={"EVENT_NAME": "workflow_dispatch", "RUN_SHA": "", "INPUT_SHA": target})
        self.assertEqual(outputs.get("decision"), "DEFER")
        self.assertIn("sha=%s." % target, summary)

    @staticmethod
    def gh(cancel=0, view="echo 'in_progress/'"):
        return 'case "$1 $2" in "run cancel") exit %d ;; "run view") %s ;; *) echo "unexpected gh $*" >&2; exit 99 ;; esac' % (cancel, view)

    def enforce(self, decision, gh=None):
        return run_window_step(self.job(), ENFORCE, env={"DECISION": decision, "GH_TOKEN": "x"}, stubs={"gh": gh or self.gh()})

    def test_CONTROL_proceed_continues_and_cancels_nothing(self):
        status, _, _, _, _, calls = self.enforce("PROCEED")
        self.assertEqual(status, 0)
        self.assertFalse([c for c in calls if c.startswith("gh ")])

    def test_CONTRACT_defer_cancels_its_own_run_waits_and_fails_red_if_the_cancel_never_lands(self):
        status, _, _, _, _, calls = self.enforce("DEFER")
        self.assertEqual(status, 1)
        gh = [c for c in calls if c.startswith("gh ")]
        self.assertEqual(gh[0], "gh run cancel 4242 --repo mugak1/Dinify-Backend")
        self.assertEqual(len([c for c in gh if c.startswith("gh run view 4242")]), 24)
        self.assertEqual(calls.count("sleep 5"), 24)

    def test_REGRESSION_MATRIX_defer_never_exits_zero_and_no_other_decision_does_either(self):
        for gh in (self.gh(cancel=1), self.gh(view="echo 'completed/cancelled'"), self.gh(view="exit 1")):
            self.assertEqual(self.enforce("DEFER", gh)[0], 1, gh)
        for decision in ("FAIL", "", "proceed", "PROCEED ", "DEFER\nPROCEED"):
            self.assertNotEqual(self.enforce(decision)[0], 0, repr(decision))

    @staticmethod
    def auto(target="c3" * 20):
        return {"EVENT_NAME": "workflow_run", "RUN_SHA": target, "INPUT_SHA": ""}


with open(WF("deploy-uat.yml"), "r", encoding="utf-8") as _fh:
    _DEPLOY_TEXT = _fh.read()


class TheLiveDeployDefersOffHours(WindowJobContract, unittest.TestCase):
    WORKFLOW = DEPLOY
    WORKFLOW_TEXT = _DEPLOY_TEXT
    DEPLOYING = ("deploy",)
    FIRST_GATED = "deploy"
    TITLE = "Deploy UAT Backend"
    PRE_OIDC = (("deploy", "Resolve and validate deploy target"), ("deploy", "Verify dispatched SHA passed Backend CI on main"))

    def test_CONTRACT_the_probe_is_the_customer_plane_health_route_for_reachability_only(self):
        self.assertEqual(self.job()["env"]["WINDOW_PROBE_URL"], "https://api-test.dinifyapp.com/uat/api/v1/health/")
        self.assertEqual(self.job()["runs-on"], DEPLOY["jobs"]["deploy"]["runs-on"])


if __name__ == "__main__":
    unittest.main()
