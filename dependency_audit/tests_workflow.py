"""DINIFY-BACKEND'S WIRING: the dependency audit is part of every ``suite`` leg, so the
required ``test`` aggregator cannot go green when the audit blocked, could not complete,
was cancelled or was skipped; and nothing about the audit can authorize a UAT deployment
on its own. Every claim is made against the committed workflow files and, where it is
about behaviour, by EXECUTING their steps (see workflow_harness.py).
"""

import json
import os
import unittest

from dependency_audit.workflow_harness import (command_of, load_workflow, run_aggregator, run_step,
                                               simulate_job, status_swallowers)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = lambda name: os.path.join(ROOT, ".github", "workflows", name)  # noqa: E731
CI = load_workflow(WF("ci.yml"))
AUDIT = load_workflow(WF("audit.yml"))
DEPLOY = load_workflow(WF("deploy-uat.yml"))
SUITE = CI["jobs"]["suite"]["steps"]
SNAPSHOT = "python -m dependency_audit snapshot"
SCAN = "python -m dependency_audit self-test && python -m dependency_audit audit"


def index_of(steps, run):
    for i, s in enumerate(steps):
        if command_of(s) == run:
            return i
    return -1


class TheAuditIsPartOfTheRequiredCheck(unittest.TestCase):
    def test_CONTRACT_the_required_aggregator_keeps_its_name_and_depends_on_the_suite(self):
        # D08 B2.5 added the independent reconstruction to what `test` requires; the suite
        # (which carries the audit) is still required, by the same name, first.
        test = CI["jobs"]["test"]
        self.assertEqual(test["name"], "test")
        self.assertEqual(test["needs"], ["suite", "reconstruct"])
        self.assertEqual(test["if"], "always()")
        self.assertNotIn("continue-on-error", test)

    def test_CONTRACT_the_snapshot_directly_follows_installation(self):
        # Since D08 B2.5 the installation is the certified environment built from the
        # locked files alone; the snapshot still follows it directly.
        install = next(i for i, s in enumerate(SUITE) if s.get("name") == "Install the certified environment from the wheelhouse")
        self.assertEqual(index_of(SUITE, SNAPSHOT), install + 1)

    def test_CONTRACT_the_scan_is_the_last_validation_step_followed_only_by_evidence_retention(self):
        # After it: the evidence retention (always) and, since D08 B2.5, the candidate's
        # packaging and upload — neither validates anything, and neither can run unless
        # every step above succeeded (no `if:`).
        scan = index_of(SUITE, SCAN)
        self.assertGreater(scan, next(i for i, s in enumerate(SUITE) if s.get("name") == "Run tests"))
        after = SUITE[scan + 1:]
        self.assertEqual([s["name"] for s in after], ["Retain the dependency-audit evidence", "Package the certified candidate",
                                                      "Upload the candidate"])
        self.assertEqual(after[0]["if"], "always()")
        self.assertEqual(after[0]["with"]["path"], "dependency_audit/evidence/")
        for step in after[1:]:
            self.assertNotIn("if", step)
            self.assertNotIn("continue-on-error", step)

    def test_CONTRACT_the_pre_existing_gates_are_all_still_present(self):
        names = [s.get("name") for s in SUITE]
        for name in ("Check migrations consistency", "Money-field guard", "Ambient-authority gate", "Tenant-relation ratchet",
                     "Tenant-isolation closure gate", "Run tests", "Dependency-audit evaluator tests"):
            self.assertIn(name, names)

    def test_CONTRACT_neither_audit_step_can_be_skipped_softened_or_rewritten(self):
        for run in (SNAPSHOT, SCAN):
            step = SUITE[index_of(SUITE, run)]
            self.assertNotIn("if", step, run)
            self.assertNotIn("continue-on-error", step, run)
            self.assertNotIn("shell", step, run)
            self.assertEqual(status_swallowers(command_of(step)), [], run)

    def test_CONTRACT_the_audit_runs_on_the_same_pinned_interpreter_the_policy_names(self):
        with open(os.path.join(ROOT, "dependency_audit", "policy.json"), "r", encoding="utf-8") as fh:
            target = json.load(fh)["target"]["python"]
        self.assertEqual(CI["jobs"]["suite"]["strategy"]["matrix"]["python-version"], [target])
        setup = next(s for s in AUDIT["jobs"]["audit"]["steps"] if str(s.get("uses", "")).startswith("actions/setup-python"))
        self.assertEqual(setup["with"]["python-version"], target)


class AFailureReachesTheRequiredCheck(unittest.TestCase):
    """Executed, not asserted."""

    @staticmethod
    def outcome(failing, result="failure"):
        return lambda step, i: result if command_of(step) in failing else "success"

    def test_CONTROL_an_all_green_leg_and_a_successful_matrix_are_green(self):
        self.assertEqual(simulate_job(SUITE, self.outcome(()))[0], "success")
        self.assertEqual(run_aggregator(CI["jobs"]["test"]["steps"][0]["run"], "success"), 0)

    def test_REGRESSION_MATRIX_the_audit_fails_while_the_suite_passes_and_test_is_red(self):
        status, ran = simulate_job(SUITE, self.outcome((SCAN,)))
        self.assertEqual(status, "failure")
        self.assertIn(("Retain the dependency-audit evidence", "success"), ran)
        self.assertNotEqual(run_aggregator(CI["jobs"]["test"]["steps"][0]["run"], status), 0)

    def test_REGRESSION_MATRIX_a_cancelled_or_skipped_leg_never_yields_a_green_test(self):
        self.assertEqual(simulate_job(SUITE, self.outcome((SCAN,), "cancelled"))[0], "cancelled")
        for result in ("failure", "cancelled", "skipped"):
            self.assertNotEqual(run_aggregator(CI["jobs"]["test"]["steps"][0]["run"], result), 0, result)

    def test_REGRESSION_MATRIX_a_refused_snapshot_skips_the_scan_and_the_leg_is_red(self):
        status, ran = simulate_job(SUITE, self.outcome((SNAPSHOT,)))
        self.assertEqual(status, "failure")
        self.assertIn(("Dependency audit — scan the validated inventory and enforce the policy", "skipped"), ran)

    def test_REGRESSION_MATRIX_an_existing_gate_fails_while_the_audit_passes_and_it_stays_red(self):
        for gate in ("python scripts/check_money_fields.py", "python scripts/check_ambient_authority.py"):
            self.assertEqual(simulate_job(SUITE, self.outcome((gate,)))[0], "failure", gate)

    def test_REGRESSION_MATRIX_the_steps_own_shell_propagates_the_audit_exit_status(self):
        script = SUITE[index_of(SUITE, SCAN)]["run"]
        self.assertEqual(run_step(script, 0), 0, "CONTROL")
        self.assertEqual(run_step(script, 1), 1)
        self.assertEqual(run_step(script, 2), 2)

    def test_NEGATIVE_CONTROL_a_pipe_under_the_default_shell_would_hide_the_failure(self):
        # GitHub's default for a `run:` with no `shell:` is `bash -e {0}` — no pipefail.
        self.assertEqual(run_step("python -m dependency_audit audit | tee audit.log\n", 2), 0)
        self.assertNotEqual(run_step("python -m dependency_audit audit | tee audit.log\n", 2, shell=("bash", "-eo", "pipefail")), 0)
        self.assertEqual(status_swallowers("python -m dependency_audit audit | tee audit.log"), ["a pipe or `||`"])


class NothingAboutTheAuditAuthorizesADeployment(unittest.TestCase):
    def test_CONTRACT_deploy_triggers_only_on_the_validation_workflow_and_verifies_ci_yml_by_path(self):
        self.assertEqual(DEPLOY["on"]["workflow_run"]["workflows"], [CI["name"]])
        self.assertEqual(DEPLOY["on"]["workflow_run"]["branches"], ["main"])
        with open(WF("deploy-uat.yml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("actions/workflows/ci.yml/runs", text)
        self.assertNotIn("actions/workflows/audit.yml", text)

    def test_CONTRACT_the_scheduled_audit_is_consumed_by_nothing_and_uses_the_same_evaluator(self):
        self.assertNotEqual(AUDIT["name"], CI["name"])
        self.assertEqual(sorted(AUDIT["on"]), ["schedule", "workflow_dispatch"])
        for name in os.listdir(os.path.join(ROOT, ".github", "workflows")):
            wf = load_workflow(WF(name))
            consumed = ((wf.get("on") or {}).get("workflow_run") or {}).get("workflows") or []
            self.assertNotIn(AUDIT["name"], consumed, name)
            with open(WF(name), "r", encoding="utf-8") as fh:
                self.assertNotIn("pip-audit -r requirements.txt", fh.read(), "%s runs an unbound parallel audit" % name)
        runs = [command_of(s) for s in AUDIT["jobs"]["audit"]["steps"] if command_of(s)]
        self.assertEqual(runs[-2:], [SNAPSHOT, SCAN])
