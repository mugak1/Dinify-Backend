"""THE PREFLIGHT'S RULES, fast (D08 B2.6): which request is honoured, which run is chosen, what
the API must say before a candidate is even opened, how a zip is admitted, what a scanner
process may inherit, how long an answer lasts — and the workflow that runs it, executed.

Each REGRESSION changes ONE fact the way the API, the event or the environment would; each
CONTROL is the case that must keep passing. The heavier end-to-end cases, over real
candidates, are in qualify_preflight.py.
"""

import json
import os
import re
import shutil
import tempfile
import unittest
import zipfile

from dependency_audit import pip_adapter as pa
from dependency_audit.workflow_harness import command_of, load_workflow, run_aggregator, simulate_job, status_swallowers
from release import candidate as cd
from release import preflight as pf
from release import testing as tt
from release.tests_workflow import AGGREGATE, SUITE, execute, step

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = lambda name: os.path.join(ROOT, ".github", "workflows", name)  # noqa: E731
PREFLIGHT = load_workflow(WF("preflight.yml"))
CI = load_workflow(WF("ci.yml"))
TARGET, TREE = "a" * 40, "b" * 40


def codes(problems):
    return sorted({p["code"] for p in problems})


class Request(unittest.TestCase):
    AUTO = {"PREFLIGHT_EVENT": "workflow_run", "GITHUB_REF": cd.MAIN_REF, "PREFLIGHT_EVENT_SHA": TARGET, "PREFLIGHT_EVENT_RUN": "9001",
            "PREFLIGHT_EVENT_ATTEMPT": "2"}
    MANUAL = {"PREFLIGHT_EVENT": "workflow_dispatch", "GITHUB_REF": cd.MAIN_REF, "PREFLIGHT_INPUT_SHA": TARGET}

    def test_CONTROL_automatic_uses_the_triggering_run_attempt_and_commit_and_nothing_else(self):
        req, problems = pf.resolve_request(self.AUTO)
        self.assertEqual((req, problems), ({"source": "automatic", "target": TARGET, "run": "9001", "attempt": "2"}, []))

    def test_CONTROL_manual_takes_an_exact_commit_and_optionally_a_run_and_attempt(self):
        self.assertEqual(pf.resolve_request(self.MANUAL)[0], {"source": "manual", "target": TARGET, "run": None, "attempt": None})
        req, _ = pf.resolve_request(dict(self.MANUAL, PREFLIGHT_INPUT_RUN="9001", PREFLIGHT_INPUT_ATTEMPT="3"))
        self.assertEqual((req["run"], req["attempt"]), ("9001", "3"))

    def test_REGRESSION_MATRIX_malformed_or_hostile_requests_stay_data_and_are_refused(self):
        for env in (dict(self.AUTO, GITHUB_REF="refs/heads/feature"), dict(self.AUTO, PREFLIGHT_EVENT_RUN=""),
                    dict(self.AUTO, PREFLIGHT_EVENT_SHA=TARGET[:12]), dict(self.MANUAL, PREFLIGHT_INPUT_SHA='"; rm -rf / #'),
                    dict(self.MANUAL, PREFLIGHT_INPUT_ATTEMPT="1"), dict(self.MANUAL, PREFLIGHT_INPUT_RUN="$(id)"),
                    dict(self.MANUAL, PREFLIGHT_EVENT="push"), dict(self.MANUAL, PREFLIGHT_INPUT_SHA=TARGET.upper())):
            req, problems = pf.resolve_request(env)
            self.assertIsNone(req, env)
            self.assertEqual(codes(problems), ["request_invalid"], env)

    def run_listing(self, *runs, total=None):
        return {"total_count": len(runs) if total is None else total, "workflow_runs": list(runs)}

    @staticmethod
    def green(run_id, attempt=1, **over):
        return dict({"id": run_id, "run_attempt": attempt, "head_sha": TARGET, "event": "push", "head_branch": "main",
                     "conclusion": "success", "path": cd.WORKFLOW_PATH}, **over)

    def test_CONTROL_run_choice_explicit_latest_and_the_only_listed_run(self):
        base = {"target": TARGET, "source": "manual", "run": None, "attempt": None}
        self.assertEqual(pf.choose_run(dict(base, run="5", attempt="2"))[0], ("5", "2"))
        self.assertEqual(pf.choose_run(dict(base, run="5"), latest={"id": 5, "run_attempt": 3})[0], ("5", "3"))
        listing = self.run_listing(self.green(7, 2), self.green(8, event="pull_request"), self.green(9, head_sha="c" * 40))
        self.assertEqual(pf.choose_run(base, listing=listing), (("7", "2"), []))

    def test_REGRESSION_an_arbitrary_historical_green_run_is_never_chosen(self):
        base = {"target": TARGET, "source": "manual", "run": None, "attempt": None}
        self.assertEqual(codes(pf.choose_run(base, listing=self.run_listing(self.green(7), self.green(8)))[1]), ["certification_ambiguous"])
        self.assertEqual(codes(pf.choose_run(base, listing=self.run_listing())[1]), ["certification_not_found"])
        self.assertEqual(codes(pf.choose_run(base, listing=self.run_listing(self.green(7), total=2))[1]), ["certification_listing_incomplete"])
        self.assertEqual(codes(pf.choose_run(dict(base, run="5"), latest={"id": 6, "run_attempt": 1})[1]), ["certification_unavailable"])


class Selection(unittest.TestCase):
    """What the API must state about the certifying run BEFORE anything is opened."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.trusted = os.path.join(cls._tmp.name, "trusted")
        tt.init_repo(cls.trusted, {"release/__init__.py": "", "dependency_audit/__init__.py": "", "requirements.txt": "x==1\n"})
        cls.cz, cls.rz = os.path.join(cls._tmp.name, "c.zip"), os.path.join(cls._tmp.name, "r.zip")
        for path in (cls.cz, cls.rz):
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("x", "x")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def select(self, attempt="1", **edit):
        facts = tempfile.mkdtemp(dir=self._tmp.name)
        tt.write_facts(facts, TARGET, TREE, self.trusted, self.cz, self.rz, attempt=attempt, edit=edit)
        with open(os.path.join(facts, pf.FACT_FILES["choice"]), "r", encoding="utf-8") as fh:
            return pf.select(facts, json.load(fh))

    def job(self, name, **over):
        def fn(doc):
            for j in doc["jobs"]:
                if j["name"] == name:
                    j.update(over)
        return fn

    def test_CONTROL_a_complete_successful_push_to_main_selects_both_artifacts_by_id_and_digest(self):
        selection, problems = self.select()
        self.assertEqual(problems, [])
        self.assertEqual((selection["commit"], selection["tree"], selection["run"]["id"], selection["run"]["attempt"]), (TARGET, TREE, tt.CI_RUN, "1"))
        self.assertEqual([j["name"] for j in selection["jobs"]], list(pf.REQUIRED_JOBS))
        self.assertEqual((selection["candidate"]["id"], selection["candidate"]["name"]), ("501", "backend-candidate-%s-1" % tt.CI_RUN))
        self.assertEqual((selection["reconstruction"]["id"], selection["reconstruction"]["name"]), ("502", "backend-reconstruction-%s-1" % tt.CI_RUN))
        self.assertTrue(selection["candidate"]["digest"].startswith("sha256:"))

    def test_REGRESSION_MATRIX_the_candidate_exists_but_its_reconstruction_or_aggregate_did_not_succeed(self):
        for name in ("reconstruct", "test"):
            for status, conclusion in (("completed", "failure"), ("completed", "skipped"), ("completed", "cancelled"), ("in_progress", None)):
                selection, problems = self.select(jobs=self.job(name, status=status, conclusion=conclusion))
                self.assertIsNone(selection)
                self.assertIn("certification_job_not_successful", codes(problems), (name, status, conclusion))

    def test_REGRESSION_a_required_job_missing_from_the_attempt_is_refused(self):
        for name in pf.REQUIRED_JOBS:
            drop = lambda doc, n=name: {"total_count": 2, "jobs": [j for j in doc["jobs"] if j["name"] != n]}  # noqa: E731
            self.assertEqual(codes(self.select(jobs=drop)[1]), ["certification_job_missing"], name)

    def test_REGRESSION_a_partial_re_run_cannot_borrow_another_attempts_success(self):
        # Attempt 2's listing carries the suite leg (and so the candidate) from attempt 1.
        selection, problems = self.select(attempt="2", jobs=self.job("suite (3.12.3)", run_attempt=1))
        self.assertIsNone(selection)
        self.assertIn("certification_mixed_attempt", codes(problems))
        self.assertIn("re-run ALL jobs", " ".join(p["detail"] for p in problems))

    def test_REGRESSION_MATRIX_the_run_is_not_a_successful_push_to_main_of_ci_yml_for_this_commit(self):
        cases = {
            "certification_not_successful": {"run": lambda r: r.update(conclusion="failure")},
            "certification_wrong_event": {"run": lambda r: r.update(event="pull_request", head_branch="feature")},
            "certification_wrong_workflow": {"run": lambda r: r.update(workflow_id=1, path=".github/workflows/impostor.yml")},
            "certification_wrong_repository": {"run": lambda r: r.update(head_repository={"full_name": "someone/Dinify-Backend"})},
            "certification_wrong_commit": {"run": lambda r: r.update(head_sha="c" * 40)},
            "certification_wrong_run": {"run": lambda r: r.update(run_attempt=2)},
            "not_on_main": {"compare": lambda c: c.update(status="diverged", merge_base_commit={"sha": "d" * 40})},
            "commit_unreadable": {"commit": lambda c: c.update(sha="e" * 40)},
        }
        for code, edit in cases.items():
            selection, problems = self.select(**edit)
            self.assertIsNone(selection, code)
            self.assertIn(code, codes(problems), code)

    def test_REGRESSION_the_same_display_name_is_not_the_workflow(self):
        selection, problems = self.select(workflow=lambda w: w.update(path=".github/workflows/other.yml"))
        self.assertEqual(codes(problems), ["certification_wrong_workflow"])

    def test_REGRESSION_MATRIX_the_artifact_listing_must_name_exactly_this_attempts_candidate(self):
        def only(names):
            return lambda doc: {"total_count": len([a for a in doc["artifacts"] if a["name"] in names]),
                                "artifacts": [a for a in doc["artifacts"] if a["name"] in names]}

        def rename(old, new):
            return lambda doc: [a.update(name=new) for a in doc["artifacts"] if a["name"] == old] and None

        cand, recon = "backend-candidate-%s-1" % tt.CI_RUN, "backend-reconstruction-%s-1" % tt.CI_RUN
        self.assertIn("certification_listing_incomplete", codes(self.select(artifacts=lambda d: d.update(total_count=9))[1]))
        self.assertIn("certification_no_candidate", codes(self.select(artifacts=only([recon]))[1]))
        self.assertIn("certification_no_reconstruction", codes(self.select(artifacts=only([cand]))[1]))
        other = self.select(artifacts=rename(cand, "backend-candidate-%s-2" % tt.CI_RUN))[1]
        self.assertIn("other attempts", " ".join(p["detail"] for p in other))
        self.assertIn("certification_ambiguous", codes(self.select(artifacts=lambda d: d.update(
            total_count=4, artifacts=d["artifacts"] + [dict(d["artifacts"][0], id=777)]))[1]))
        self.assertIn("certification_expired", codes(self.select(artifacts=lambda d: d["artifacts"][0].update(expired=True))[1]))
        self.assertIn("certification_listing_incomplete", codes(self.select(artifacts=lambda d: d["artifacts"][0].update(digest=None))[1]))
        self.assertIn("certification_wrong_run", codes(self.select(artifacts=lambda d: d["artifacts"][1]["workflow_run"].update(id=1))[1]))
        self.assertIn("certification_contradictory", codes(self.select(artifacts=lambda d: d.update(
            total_count=4, artifacts=d["artifacts"] + [dict(d["artifacts"][0], id=778, name="backend-candidate-nonpromotable-%s-1" % tt.CI_RUN)]))[1]))

    def test_REGRESSION_an_unreadable_fact_is_refused_never_defaulted(self):
        facts = tempfile.mkdtemp(dir=self._tmp.name)
        tt.write_facts(facts, TARGET, TREE, self.trusted, self.cz, self.rz)
        with open(os.path.join(facts, pf.FACT_FILES["jobs"]), "w", encoding="utf-8") as fh:
            fh.write("[]")
        with open(os.path.join(facts, pf.FACT_FILES["choice"]), "r", encoding="utf-8") as fh:
            self.assertEqual(codes(pf.select(facts, json.load(fh))[1]), ["facts_unreadable"])


class Unpacking(unittest.TestCase):
    """A zip is admitted by the listing's digest BEFORE a single member is written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def make(self, members, symlink=None):
        path = os.path.join(self.tmp, "a.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members:
                zf.writestr(name, data)
            if symlink:
                info = zipfile.ZipInfo(symlink)
                info.external_attr = (0o120777 << 16)
                zf.writestr(info, "/etc/passwd")
        with open(path, "rb") as fh:
            return path, "sha256:" + __import__("hashlib").sha256(fh.read()).hexdigest()

    def test_CONTROL_a_well_formed_zip_with_its_listed_digest_unpacks(self):
        path, digest = self.make([("record.json", "{}"), ("wheelhouse/a.whl", "x")])
        dest = os.path.join(self.tmp, "out")
        self.assertEqual(pf.unpack(path, digest, dest), [])
        self.assertEqual(sorted(os.listdir(dest)), ["record.json", "wheelhouse"])

    def test_REGRESSION_a_digest_mismatch_extracts_nothing(self):
        path, _ = self.make([("record.json", "{}")])
        dest = os.path.join(self.tmp, "out")
        self.assertEqual(codes(pf.unpack(path, "sha256:" + "0" * 64, dest)), ["artifact_digest_mismatch"])
        self.assertFalse(os.path.exists(dest))

    def test_REGRESSION_MATRIX_unsafe_members_are_refused_even_under_the_right_digest(self):
        for members, link in (([("../escape", "x")], None), ([("/abs", "x")], None), ([("a\\b", "x")], None),
                              ([("./a", "x")], None), ([("a", "x"), ("a", "y")], None), ([("ok", "x")], "link"),
                              ([("a", "x"), ("a/b", "y")], None)):
            path, digest = self.make(members, symlink=link)
            dest = os.path.join(self.tmp, "out")
            self.assertEqual(codes(pf.unpack(path, digest, dest)), ["artifact_unsafe"], (members, link))
            self.assertFalse(os.path.exists(dest), members)


class TimeAndEnvironment(unittest.TestCase):
    def test_CONTROL_the_window_is_24_hours_from_the_start_of_the_evaluation(self):
        self.assertEqual(pf._iso(pf.deadline_ms("2026-09-26T13:00:00.000Z", [])), "2026-09-27T13:00:00.000Z")

    def test_REGRESSION_an_applied_record_lapsing_inside_the_window_cuts_it_short(self):
        self.assertEqual(pf._iso(pf.deadline_ms("2026-09-26T13:00:00.000Z", [{"id": "x", "expires": "2026-09-27"}])), "2026-09-27T00:00:00.000Z")
        self.assertEqual(pf.deadline_ms("2026-09-26T13:00:00.000Z", [{"id": "x", "expires": "not-a-date"}]), -1)
        self.assertIsNone(pf.deadline_ms("yesterday", []))

    def test_REGRESSION_the_receiving_margin_can_be_raised_but_never_lowered(self):
        from release.__main__ import main
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        argv = ["preflight", "verify", "--facts", os.path.join(work, "none"), "--work", os.path.join(work, "w"),
                "--evaluation-run", "7", "--evaluation-attempt", "1", "--margin-minutes"]
        for lowered in ("0", "29"):
            self.assertEqual(main(argv + [lowered]), 64, lowered)
        # CONTROL: a larger margin is accepted as a request and fails later, on the absent facts.
        self.assertEqual(main(argv + ["45"]), 1)

    def test_REGRESSION_runner_authority_is_removed_from_the_process_and_named_not_logged(self):
        environ = {"PATH": "/bin", "GITHUB_OUTPUT": "/o", "GITHUB_ENV": "/e", "GITHUB_PATH": "/p", "GITHUB_STEP_SUMMARY": "/s",
                   "GH_TOKEN": "t", "ACTIONS_RUNTIME_TOKEN": "t", "ACTIONS_ID_TOKEN_REQUEST_URL": "u", "NPM_TOKEN": "t", "GITHUB_SHA": "x"}
        removed = pf.withhold_runner_authority(environ)
        self.assertEqual(sorted(environ), ["GITHUB_SHA", "PATH"])
        self.assertEqual(sorted(removed), ["ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN", "GH_TOKEN", "GITHUB_ENV", "GITHUB_OUTPUT",
                                           "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "NPM_TOKEN"])

    def test_REGRESSION_every_scanner_process_gets_pip_configuration_disabled_and_no_authority(self):
        seen = []
        run = pf.scanner_runner(lambda command, args, cwd=None, env=None, timeout=None: seen.append(env) or {})
        hostile = {"PIP_INDEX_URL": "http://evil", "PIP_FIND_LINKS": "/tmp/x", "GITHUB_OUTPUT": "/o", "GH_TOKEN": "t", "HOME": "/h",
                   "PIP_CONFIG_FILE": "/tmp/pip.conf"}
        from unittest import mock
        with mock.patch.dict(os.environ, hostile):
            run("python", ["-m", "venv", "x"])
            run("python", ["-m", "pip", "install"], env=dict(os.environ))
        for env in seen:
            self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
            for key in ("GITHUB_OUTPUT", "GH_TOKEN"):
                self.assertNotIn(key, env)
        self.assertNotIn("PIP_INDEX_URL", seen[0])
        self.assertEqual(seen[0]["HOME"], "/h")

    def test_CONTROL_the_b21_ci_scanner_environment_is_unchanged_by_this_delivery(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": "/o"}):
            env, _ = pa.scrubbed_environment()
        self.assertNotIn("PIP_CONFIG_FILE", env)
        self.assertEqual(env.get("GITHUB_OUTPUT"), "/o", "B2.1's CI audit keeps its documented environment handling")


def job_steps(job):
    return PREFLIGHT["jobs"][job]["steps"]


def named(job, prefix):
    return next(s for s in job_steps(job) if s["name"].startswith(prefix))


class TheWorkflowDeploysNothingAndHoldsTheTokenApartFromTheScanner(unittest.TestCase):
    def test_CONTRACT_triggers_follow_backend_ci_on_main_or_an_explicit_request(self):
        self.assertEqual(PREFLIGHT["on"]["workflow_run"], {"workflows": [CI["name"]], "types": ["completed"], "branches": ["main"]})
        self.assertEqual(sorted(PREFLIGHT["on"]["workflow_dispatch"]["inputs"]), ["ci_run_attempt", "ci_run_id", "sha"])
        self.assertNotIn("push", PREFLIGHT["on"])
        self.assertNotIn("pull_request", PREFLIGHT["on"])
        cond = PREFLIGHT["jobs"]["assess"]["if"]
        for fragment in ("github.event.workflow_run.conclusion == 'success'", "github.event.workflow_run.event == 'push'",
                         "github.event.workflow_run.head_branch == 'main'", "github.event_name == 'workflow_dispatch'"):
            self.assertIn(fragment, cond)

    def test_CONTRACT_read_only_permissions_and_no_deployment_capability_anywhere(self):
        self.assertEqual(PREFLIGHT["permissions"], {})
        self.assertEqual(sorted(PREFLIGHT["jobs"]), ["assess", "verify"])
        for name, job in PREFLIGHT["jobs"].items():
            self.assertEqual(job["permissions"], {"contents": "read", "actions": "read"}, name)
            self.assertNotIn("environment", job)
            for s in job["steps"]:
                self.assertNotIn("continue-on-error", s)
                text = json.dumps(s).lower()
                for word in ("aws", "ssm", "send-command", "s3 ", "id-token", "secrets.", "deploy", "oidc"):
                    self.assertNotIn(word, text, (name, s.get("name")))
        with open(WF("preflight.yml"), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("secrets.", text)
        self.assertNotIn("id-token: write", text)
        self.assertEqual(PREFLIGHT["jobs"]["verify"]["needs"], ["assess"])

    def test_CONTRACT_the_token_reaches_only_the_facts_steps_and_never_the_scanner_or_the_receiving_check(self):
        for job in ("assess", "verify"):
            for s in job_steps(job):
                env = s.get("env") or {}
                if s["name"] == "Read the facts and fetch the artifacts":
                    self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")
                    self.assertIn("preflight facts", s["run"])
                else:
                    self.assertNotIn("GH_TOKEN", env, s["name"])
                    self.assertNotIn("github.token", json.dumps(s), s["name"])
        self.assertIn("preflight assess", named("assess", "Assess")["run"])
        self.assertIn("preflight verify", named("verify", "Receive")["run"])

    def test_CONTRACT_the_verifier_is_this_workflows_own_revision_with_no_persisted_credential(self):
        for job in ("assess", "verify"):
            checkout = job_steps(job)[0]
            self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertIs(checkout["with"]["persist-credentials"], False)
            self.assertEqual(checkout["with"]["sparse-checkout"].split(), list(pf.TRUSTED_TREES))
            assertion = job_steps(job)[1]["run"]
            self.assertIn("extraheader", assertion)
            self.assertIn('= "$GITHUB_SHA"', assertion)
            self.assertEqual(job_steps(job)[2]["with"]["python-version"], "3.12.3")

    def test_CONTRACT_every_action_is_pinned_by_commit_not_by_a_moving_tag(self):
        # A tag is a pointer its owner can move; the verifier's own tooling must not be able
        # to change underneath a reviewed workflow.
        pinned = re.compile(r"^actions/(checkout|setup-python|upload-artifact)@[0-9a-f]{40}$")
        seen = [s["uses"] for job in ("assess", "verify") for s in job_steps(job) if "uses" in s]
        self.assertEqual(len(seen), 5)
        for uses in seen:
            self.assertRegex(uses, pinned)

    def test_CONTRACT_no_event_value_is_interpolated_into_a_script_and_nothing_swallows_a_status(self):
        for job in ("assess", "verify"):
            for s in job_steps(job):
                if "run" in s:
                    self.assertNotIn("${{", s["run"], s["name"])
                    if s["name"] != "Assert the verifier is the workflow revision and holds no credential":
                        self.assertEqual(status_swallowers(command_of(s)), [], s["name"])
                    for line in s["run"].splitlines():
                        if "-m release" in line:
                            self.assertIn("python -B -m release", line)

    def test_CONTRACT_the_result_is_retained_whether_accepted_or_refused_under_its_run_and_attempt(self):
        retain = named("assess", "Retain")
        upload = next(s for s in SUITE if s.get("name") == "Upload the candidate")
        self.assertEqual(retain["uses"], upload["uses"], "the reviewed upload-artifact pin")
        self.assertEqual(retain["with"]["name"], "backend-preflight-${{ github.run_id }}-${{ github.run_attempt }}")
        self.assertEqual(retain["if"], "always() && (steps.assess.outcome == 'success' || steps.assess.outcome == 'failure')")
        self.assertEqual(retain["with"]["if-no-files-found"], "error")

    def test_REGRESSION_the_committed_step_text_carries_each_verifier_status_under_the_runner_shell(self):
        for job, prefix in (("assess", "Read the facts"), ("assess", "Assess"), ("verify", "Read the facts"), ("verify", "Receive")):
            script = named(job, prefix)["run"]
            for status in (0, 1, 2):
                self.assertEqual(execute(script, status)[0], status, (job, prefix, status))

    def test_REGRESSION_a_failed_or_incomplete_assessment_fails_the_job_but_its_result_is_retained(self):
        steps = job_steps("assess")
        for outcome in ("failure", "success"):
            status, ran = "success", []
            for s in steps:
                if s["name"].startswith("Retain"):
                    ran.append((s["name"], "success" if dict(ran).get("Assess the retained candidate now") in ("success", "failure") else "skipped"))
                    continue
                result = outcome if s.get("id") == "assess" else "success"
                ran.append((s["name"], result if status == "success" else "skipped"))
                if result == "failure":
                    status = "failure"
            self.assertEqual(status, "failure" if outcome == "failure" else "success")
            self.assertIn(("Retain the preflight result", "success"), ran)

    def test_CONTRACT_the_live_deployment_consumes_nothing_from_the_preflight(self):
        with open(WF("deploy-uat.yml"), "r", encoding="utf-8") as fh:
            deploy = fh.read()
        for word in ("preflight", "Backend Candidate Preflight", "backend-preflight", "backend-candidate"):
            self.assertNotIn(word, deploy)
        self.assertIn("pip install -q -r requirements.txt", deploy, "the live install is independent of the candidate and the preflight")


class ThePreflightIsPartOfTheRequiredCheck(unittest.TestCase):
    def test_CONTRACT_the_required_jobs_are_ci_ymls_own_names(self):
        legs = ["suite (%s)" % v for v in CI["jobs"]["suite"]["strategy"]["matrix"]["python-version"]]
        self.assertEqual(list(pf.REQUIRED_JOBS), legs + ["reconstruct", CI["jobs"]["test"]["name"]])
        self.assertEqual(CI["jobs"]["test"]["needs"], ["suite", "reconstruct"])

    def test_CONTRACT_the_required_release_tests_step_discovers_the_preflight_suites(self):
        run = step("release-tests")["run"]
        found = set()
        for pattern in ("tests_*.py", "qualify_*.py"):
            self.assertIn('-p "%s"' % pattern, run)
            suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "release"), pattern=pattern, top_level_dir=ROOT)
            stack = [suite]
            while stack:
                item = stack.pop()
                if isinstance(item, unittest.TestSuite):
                    stack.extend(item)
                else:
                    found.add(type(item).__module__)
        self.assertIn("release.tests_preflight", found)
        self.assertIn("release.qualify_preflight", found)

    def test_REGRESSION_a_failing_preflight_test_keeps_the_required_check_red_while_everything_else_passes(self):
        status, ran = simulate_job(SUITE, lambda s, i: "failure" if s.get("id") == "release-tests" else "success")
        self.assertEqual(status, "failure")
        self.assertIn(("Upload the candidate", "skipped"), ran)
        self.assertNotEqual(run_aggregator(AGGREGATE, {"suite": status, "reconstruct": "skipped"}), 0)
        self.assertEqual(execute(step("release-tests")["run"], 1)[0], 1)


if __name__ == "__main__":
    unittest.main()
