"""THE WIRING: the candidate is produced inside the required check, only after every
required step succeeded, and the independent reconstruction is part of what ``test``
requires. Claims about behaviour are made by EXECUTING the committed step text under the
runner's own shell (``bash -e``, no pipefail) and GitHub's step sequencing
(dependency_audit/workflow_harness.py) — not by reading it.

And the boundary this delivery does NOT cross: the live deployment is untouched.
"""

import json
import os
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


if __name__ == "__main__":
    unittest.main()
