"""THE PREFLIGHT, END TO END, over REAL candidates (D08 B2.6): the real producer certifies a
synthetic project laid out like this repository, the real consumer reconstructs it and
writes a real report, and the real preflight selects, verifies, queries and receives — with
only the SCANNER'S PROCESSES answered by a stand-in (release/testing.py::FakeScanner), so no
advisory service is contacted. The genuine query against PyPI is recorded in
release/README.md, not repeated here.

Each REGRESSION changes one fact — a job, a byte, a report, an advisory, a clock, a policy —
and names the refusal it must produce. The CONTROLs are the cases that must keep passing.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dependency_audit import orchestrate as oc
from release import candidate as cd
from release import consumer as cs
from release import preflight as pf
from release import testing as tt

EVAL = {"runId": tt.EVAL_RUN, "runAttempt": "1"}
RECEIVED_AT = "2026-09-26T13:40:00.000Z"
SYNTHETIC_RECORD = {
    "id": "SYNTHETIC-FIXTURE-1", "kind": "exception", "advisory": "GHSA-demo-0001", "aliases": [], "package": "demo-lib", "version": "1.0",
    "paths": ["application:site-packages/demo-lib"], "scope": "runtime",
    "applicability": "SYNTHETIC FIXTURE: demo-lib is a test wheel with no real advisory",
    "reason": "SYNTHETIC FIXTURE: exercises how a certification-time exception ages",
    "owner": "release/qualify_preflight.py", "expires": "2026-09-27",
    "approval": {"by": "nobody — a synthetic fixture, not an approval", "reference": "https://github.com/mugak1/Dinify-Backend/pull/1", "date": "2026-09-20"},
}


def codes(problems):
    return sorted({p["code"] for p in problems})


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def clone(source, destination, edit=None):
    """A second checkout of the fixture repository, optionally with one committed change —
    the verifier as it stands on main at another moment."""
    tt.run(tt.GIT + ["clone", "-q", source, destination])
    if edit:
        edit(destination)
        tt.git(destination, "add", "-A")
        tt.git(destination, "commit", "-qm", "moved")
    return destination


def set_policy_records(root, records):
    path = os.path.join(root, "dependency_audit", "policy.json")
    with open(path, "r", encoding="utf-8") as fh:
        policy = json.load(fh)
    policy["records"] = records
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, indent=2)


def certify(tmp, name, state, run_id=tt.CI_RUN, attempt="1", stub=None, recon_edit=None, env=None):
    """Package, reconstruct and zip one candidate as CI and upload-artifact would."""
    cand = os.path.join(tmp, name)
    proc = tt.package(state, cand, env=dict(tt.ci_env(state["commit"], run_id=run_id, attempt=attempt), **(env or {})))
    if proc.returncode != 0:
        raise RuntimeError("fixture packaging refused: %s %s" % (proc.stdout, proc.stderr))
    expect = {"repository": cd.REPOSITORY, "commit": state["commit"], "tree": state["tree"], "runId": run_id, "runAttempt": attempt,
              "event": "push", "ref": cd.MAIN_REF, "artifact": pf.candidate_name(run_id, attempt), "workflowPath": cd.WORKFLOW_PATH, "local": False}
    report, problems = cs.reconstruct(cand, os.path.join(tmp, name + "-rebuild"), expect, tt.NOW, startup=stub)
    if problems:
        raise RuntimeError("fixture reconstruction refused: %s" % problems)
    report = dict(report, problems=problems)
    if recon_edit:
        recon_edit(report)
    recon = os.path.join(tmp, name + "-report")
    os.makedirs(recon)
    with open(os.path.join(recon, pf.RECONSTRUCTION_FILE), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    cz, rz = os.path.join(tmp, name + ".zip"), os.path.join(tmp, name + "-report.zip")
    tt.zip_dir(cand, cz)
    tt.zip_dir(recon, rz)
    return {"dir": cand, "zip": cz, "reportZip": rz}


_SHARED = {}


def shared():
    """ONE produced project and ONE certified candidate for every class below (producing is
    the slow part); each test copies what it changes."""
    if not _SHARED:
        tmp = tempfile.TemporaryDirectory()
        stub = os.path.join(tmp.name, "startup_stand_in.py")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(tt.STARTUP_STAND_IN)
        state = tt.produce(tmp.name)
        _SHARED.update(tmp=tmp, stub=stub, state=state, main=certify(tmp.name, "main", state, stub=stub))
    return _SHARED


def tearDownModule():
    if _SHARED:
        _SHARED["tmp"].cleanup()


def day(offset, clock="00:00:00.000"):
    """An instant relative to TODAY (UTC), so a fixture involving a dated record does not
    start failing on some later calendar date."""
    import datetime as dt
    return "%sT%sZ" % ((dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=offset)).isoformat(), clock)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = shared()
        cls._tmp, cls.stub, cls.state, cls.main = s["tmp"], s["stub"], s["state"], s["main"]
        cls.trusted = cls.state["root"]

    def scratch(self):
        return tempfile.mkdtemp(dir=self._tmp.name)

    def facts(self, candidate=None, trusted=None, main_root=None, **edit):
        candidate = candidate or self.main
        d = os.path.join(self.scratch(), "facts")
        tt.write_facts(d, self.state["commit"], self.state["tree"], trusted or self.trusted, candidate["zip"], candidate["reportZip"],
                       main_root=main_root, edit=edit)
        return d

    def assess(self, facts, trusted=None, scanner=None, clock=None):
        trusted = trusted or self.trusted
        scanner = scanner or tt.FakeScanner(trusted)
        base = self.scratch()
        out, work = os.path.join(base, "out"), os.path.join(base, "work")
        revision = tt.git(trusted, "rev-parse", "HEAD")
        doc = pf.assess(trusted, facts, out, work, dict(EVAL, revision=revision), clock or tt.Clock(), runner=scanner)
        return doc, out, work, scanner

    def receive(self, facts, out, trusted=None, now=RECEIVED_AT, margin=pf.RECEIVING_MARGIN_MINUTES, **edit):
        tt.write_evaluation(facts, trusted or self.trusted, out, edit=edit)
        return pf.receive(trusted or self.trusted, facts, os.path.join(self.scratch(), "received"), EVAL, now, margin_minutes=margin)

    @staticmethod
    def rewrite(out, fn):
        path = os.path.join(out, pf.PREFLIGHT_DOC)
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        fn(doc)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)


class TheControl(Fixture):
    def test_CONTROL_a_complete_certification_is_assessed_now_and_received_without_deploying_anything(self):
        before = sha(self.main["zip"])
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": "/runner/output", "GH_TOKEN": "a-read-token", "PIP_INDEX_URL": "http://elsewhere"}):
            doc, out, _, scanner = self.assess(self.facts())
        self.assertEqual((doc["decision"], doc["problems"]), ("accepted", []))
        self.assertEqual(pf.exit_code(doc), 0)
        self.assertEqual(doc["scope"], pf.SCOPE)
        self.assertIs(doc["scope"]["deploymentAuthorized"], False)
        a = doc["assessment"]
        self.assertEqual(a["outcome"], "within_policy")
        self.assertEqual(a["graphs"]["application"]["observation"], "retained-inventory")
        self.assertEqual(a["graphs"]["scanner"]["observation"], "installed-now")
        with open(os.path.join(out, "application.inventory-requirements.txt"), "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "demo-app==1.0\ndemo-lib==1.0\npip==%s\n" % tt.entry(tt.bundled_pip())["version"])
        self.assertEqual(doc["candidate"]["originalAudit"]["outcome"], "within_policy")
        self.assertEqual(doc["deadline"], "2026-09-27T13:00:00.000Z")
        # Every scanner process: pip configuration disabled, no runner file, no token, no inherited PIP_*.
        self.assertTrue(scanner.calls)
        for call in scanner.calls:
            self.assertEqual(call["env"]["PIP_CONFIG_FILE"], os.devnull)
            for key in ("GITHUB_OUTPUT", "GH_TOKEN", "PIP_INDEX_URL"):
                self.assertNotIn(key, call["env"])
        audits = [c for c in scanner.calls if c["command"].endswith("pip-audit")]
        self.assertEqual(len(audits), 2)
        for call in audits:
            for flag in ("--no-deps", "--disable-pip", "--strict"):
                self.assertIn(flag, call["args"])
        self.assertNotEqual(*[c["args"][c["args"].index("--cache-dir") + 1] for c in audits])
        admitted, problems = self.receive(self.facts(), out)
        self.assertEqual(problems, [])
        self.assertEqual((admitted["commit"], admitted["candidateArtifactId"], admitted["outcome"], admitted["deploymentAuthorized"]),
                         (self.state["commit"], "501", "within_policy", False))
        self.assertEqual(sha(self.main["zip"]), before, "the candidate is read, never changed")


class EligibilityIsEstablishedBeforeAnythingIsOpened(Fixture):
    def test_REGRESSION_MATRIX_a_candidate_without_a_complete_successful_certification_is_never_opened(self):
        def job(name, **over):
            return lambda doc: [j.update(over) for j in doc["jobs"] if j["name"] == name] and None
        cases = {
            "reconstruction failed": ({"jobs": job("reconstruct", conclusion="failure")}, "certification_job_not_successful"),
            "aggregate still running": ({"jobs": job("test", status="in_progress", conclusion=None)}, "certification_job_not_successful"),
            "a pull request": ({"run": lambda r: r.update(event="pull_request")}, "certification_wrong_event"),
            "another workflow named alike": ({"run": lambda r: r.update(workflow_id=5)}, "certification_wrong_workflow"),
            "a partial listing": ({"artifacts": lambda d: d.update(total_count=7)}, "certification_listing_incomplete"),
            "another attempt's candidate": ({"jobs": job("suite (3.12.3)", run_attempt=2)}, "certification_mixed_attempt"),
        }
        for label, (edit, code) in cases.items():
            doc, _, work, scanner = self.assess(self.facts(**edit))
            self.assertEqual(doc["decision"], "refused", label)
            self.assertIn(code, codes(doc["problems"]), label)
            self.assertIsNone(doc["assessment"], label)
            self.assertEqual(scanner.calls, [], "%s: no scanner process was started" % label)
            self.assertFalse(os.path.exists(os.path.join(work, "candidate")), "%s: the candidate was not even unpacked" % label)
            self.assertEqual(pf.exit_code(doc), 1)

    def test_REGRESSION_a_verifier_or_policy_that_moved_on_main_needs_a_new_evaluation_not_this_one(self):
        moved = clone(self.trusted, os.path.join(self.scratch(), "main-now"), edit=lambda r: set_policy_records(r, [SYNTHETIC_RECORD]))
        doc, _, _, scanner = self.assess(self.facts(main_root=moved))
        self.assertEqual(codes(doc["problems"]), ["evaluator_not_current"])
        self.assertEqual(scanner.calls, [])


class TheBytesAreTheCertifiedBytes(Fixture):
    def test_REGRESSION_a_zip_that_is_not_the_listed_artifact_is_refused_before_extraction(self):
        other = certify(self.scratch(), "other-run", self.state, run_id="9004", stub=self.stub)
        facts = self.facts(artifacts=lambda d: d["artifacts"][0].update(digest="sha256:" + sha(other["zip"])))
        doc, _, work, scanner = self.assess(facts)
        self.assertEqual(codes(doc["problems"]), ["artifact_digest_mismatch"])
        self.assertFalse(os.path.exists(os.path.join(work, "candidate")))
        self.assertEqual(scanner.calls, [])

    def test_REGRESSION_a_candidate_from_another_run_uploaded_under_this_ones_listing_is_refused_by_the_consumer(self):
        other = certify(self.scratch(), "other-run", self.state, run_id="9004", stub=self.stub)
        doc, _, _, scanner = self.assess(self.facts(candidate={"zip": other["zip"], "reportZip": self.main["reportZip"]}))
        self.assertEqual(codes(doc["problems"]), ["identity_mismatch"])
        self.assertEqual(scanner.calls, [])

    def test_REGRESSION_changed_wheel_bytes_are_refused_against_the_lock_inside_the_verified_source(self):
        d = os.path.join(self.scratch(), "tampered")
        shutil.copytree(self.main["dir"], d)
        wheel = next(f for f in os.listdir(os.path.join(d, cd.WHEELHOUSE)) if f.startswith("demo_lib"))
        with open(os.path.join(d, cd.WHEELHOUSE, wheel), "ab") as fh:
            fh.write(b"\0")
        z = os.path.join(self.scratch(), "tampered.zip")
        tt.zip_dir(d, z)
        doc, _, _, scanner = self.assess(self.facts(candidate={"zip": z, "reportZip": self.main["reportZip"]}))
        self.assertIn("artifact_mismatch", codes(doc["problems"]))
        self.assertEqual(scanner.calls, [])

    def test_REGRESSION_the_queried_inventory_must_be_the_one_the_record_certified(self):
        # Defence in depth: the consumer has already bound the snapshot to the record before
        # this runs, so an authentic artifact cannot reach it unbound; the rule is pinned directly.
        with open(os.path.join(self.main["dir"], cd.RECORD), "r", encoding="utf-8") as fh:
            record = json.load(fh)
        packages, problems = pf.retained_inventory(self.main["dir"], record)
        self.assertEqual((problems, [p["name"] for p in packages]), ([], ["demo-app", "demo-lib", "pip"]))
        self.assertEqual([p["scope"] for p in packages], ["runtime", "runtime", "tooling"], "scoped by the TRUSTED rule")
        record["environment"]["auditInventorySha256"] = "0" * 64
        self.assertEqual(codes(pf.retained_inventory(self.main["dir"], record)[1]), ["inventory_unbound"])

    def test_REGRESSION_MATRIX_a_reconstruction_about_another_record_or_that_did_not_succeed_is_refused(self):
        cases = {"reconstruction_foreign": lambda r: r["candidate"].update(recordSha256="0" * 64),
                 "reconstruction_failed": lambda r: r.update(problems=[{"code": "startup_failed", "detail": "x"}])}
        for code, edit in cases.items():
            bad = certify(self.scratch(), "recon-" + code, self.state, stub=self.stub, recon_edit=edit)
            doc, _, _, scanner = self.assess(self.facts(candidate={"zip": self.main["zip"], "reportZip": bad["reportZip"]}))
            self.assertIn(code, codes(doc["problems"]), code)
            self.assertEqual(scanner.calls, [], code)


class TheQuestionIsAskedNow(Fixture):
    def test_REGRESSION_a_new_blocking_advisory_refuses_unchanged_candidate_bytes(self):
        before = sha(self.main["zip"])
        scanner = tt.FakeScanner(self.trusted, vulns={"application": {"demo-lib": ["GHSA-new-0001"]}})
        doc, out, _, _ = self.assess(self.facts(), scanner=scanner)
        self.assertEqual((doc["decision"], doc["assessment"]["outcome"], pf.exit_code(doc)), ("blocking", "blocking", 1))
        self.assertEqual(doc["candidate"]["originalAudit"]["outcome"], "within_policy", "the certification's own answer is kept, not rewritten")
        self.assertEqual(sha(self.main["zip"]), before)
        self.assertEqual(codes(self.receive(self.facts(), out)[1]), ["preflight_not_accepted"])

    def test_REGRESSION_MATRIX_a_scanner_that_could_not_answer_is_incomplete_never_clean(self):
        answers = {"empty body, exit 1": lambda g: {"status": 1, "stdout": ""},
                   "timed out": lambda g: {"status": None, "timedOut": True},
                   "killed": lambda g: {"status": None, "signal": "signal 9"},
                   "a package skipped": lambda g: {"status": 0, "stdout": json.dumps({"dependencies": [{"name": "demo-app", "version": "1.0", "skip_reason": "x"}]})}}
        for label, answer in answers.items():
            doc, _, _, _ = self.assess(self.facts(), scanner=tt.FakeScanner(self.trusted, audit=answer))
            self.assertEqual((doc["decision"], pf.exit_code(doc)), ("incomplete", 2), label)
        doc, _, _, _ = self.assess(self.facts(), scanner=tt.FakeScanner(self.trusted, install_status=1))
        self.assertEqual((doc["decision"], pf.exit_code(doc)), ("incomplete", 2), "the scanner could not be installed")
        self.assertIn("scanner_install_failed", [r["code"] for r in doc["assessment"]["reasons"]])


class TheReceivingSideTrustsNothingItCannotReproduce(Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base = tempfile.mkdtemp(dir=cls._tmp.name)
        facts = os.path.join(base, "facts")
        tt.write_facts(facts, cls.state["commit"], cls.state["tree"], cls.trusted, cls.main["zip"], cls.main["reportZip"])
        cls.out = os.path.join(base, "out")
        pf.assess(cls.trusted, facts, cls.out, os.path.join(base, "work"), dict(EVAL, revision=tt.git(cls.trusted, "rev-parse", "HEAD")),
                  tt.Clock(), runner=tt.FakeScanner(cls.trusted))

    def edited(self, fn=None, files=None):
        d = os.path.join(self.scratch(), "out")
        shutil.copytree(self.out, d)
        if fn:
            self.rewrite(d, fn)
        for name, data in (files or {}).items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(data)
        return d

    def test_CONTROL_the_unmodified_result_is_received_and_a_small_consistent_shift_inside_githubs_bounds_is_tolerated(self):
        self.assertEqual(self.receive(self.facts(), self.edited())[1], [])
        self.assertEqual(self.receive(self.facts(), self.edited(self.shifted(1)))[1], [])

    @staticmethod
    def shifted(minutes):
        """Every instant moved together and the deadline recomputed: a consistent re-dating."""
        def fn(d):
            move = lambda t: pf._iso(pf._ms(t) + minutes * 60000)  # noqa: E731
            d["startedAt"] = move(d["startedAt"])
            a = d["assessment"]
            a["finishedAt"], a["decidedAt"] = move(a["finishedAt"]), move(a["decidedAt"])
            for g in a["graphs"].values():
                g["queryStartedAt"], g["queryFinishedAt"] = move(g["queryStartedAt"]), move(g["queryFinishedAt"])
            d["deadline"] = pf._iso(pf.deadline_ms(d["startedAt"], a["recordsApplied"]))
        return fn

    def test_REGRESSION_MATRIX_a_re_dated_or_rewritten_result_is_refused_even_when_its_listing_matches(self):
        # Received well after every claimed instant, so only GitHub's upload time can refuse it.
        late = "2026-09-26T15:00:00.000Z"
        self.assertEqual(codes(self.receive(self.facts(), self.edited(self.shifted(50)), now=late)[1]), ["preflight_time_invalid"],
                         "re-dated after its own upload")
        cases = {
            "re-dated before its evaluation began": (self.shifted(-180), {}, "preflight_time_invalid"),
            "out of order": (lambda d: d.update(startedAt="2026-09-26T13:30:00.000Z"), {}, "preflight_time_invalid"),
            "a stretched deadline": (lambda d: d.update(deadline="2026-09-28T13:00:00.000Z"), {}, "preflight_time_invalid"),
            "an outcome the raw output does not support": (lambda d: d["assessment"]["counts"].update(findings=3), {}, "preflight_unreproducible"),
            "raw output replaced": (None, {"application.scanner-stdout.txt": '{"dependencies": []}'}, "preflight_raw_mismatch"),
            "another candidate named": (lambda d: d["candidate"].update(recordSha256="0" * 64), {}, "preflight_wrong_candidate"),
            "another certification named": (lambda d: d["certification"]["run"].update(attempt="2"), {}, "preflight_wrong_certification"),
            "claims deployment": (lambda d: d["scope"].update(deploymentAuthorized=True), {}, "preflight_scope_invalid"),
            "refused, relabelled": (lambda d: d.update(decision="refused"), {}, "preflight_not_accepted"),
        }
        for label, (fn, files, code) in cases.items():
            self.assertIn(code, codes(self.receive(self.facts(), self.edited(fn, files))[1]), label)

    def test_REGRESSION_a_query_that_did_not_cover_exactly_the_retained_inventory_or_the_scanner_is_refused(self):
        def drop_pip(d):
            g = d["assessment"]["graphs"]["application"]
            g["packages"] = [p for p in g["packages"] if p["name"] != "pip"]
        reqs = "demo-app==1.0\ndemo-lib==1.0\n"
        stdout = json.dumps({"dependencies": [{"name": "demo-app", "version": "1.0", "vulns": []}, {"name": "demo-lib", "version": "1.0", "vulns": []}], "fixes": []})

        def narrow(d):
            drop_pip(d)
            g = d["assessment"]["graphs"]["application"]
            g["run"]["stdoutSha256"] = hashlib.sha256(stdout.encode()).hexdigest()
            g["requirementsSha256"] = hashlib.sha256(reqs.encode()).hexdigest()
        problems = self.receive(self.facts(), self.edited(narrow, {"application.inventory-requirements.txt": reqs,
                                                                  "application.scanner-stdout.txt": stdout}))[1]
        self.assertEqual(codes(problems), ["preflight_wrong_inventory"])

        def other_scanner(d):
            d["assessment"]["graphs"]["scanner"]["packages"][0]["version"] = "0.0.1"
        self.assertIn("preflight_wrong_scanner", codes(self.receive(self.facts(), self.edited(other_scanner))[1]))
        self.assertIn("preflight_wrong_inventory", codes(self.receive(self.facts(), self.edited(
            lambda d: d["assessment"]["graphs"]["application"].update(observation="installed-now")))[1]))

    def test_REGRESSION_a_policy_or_verifier_that_changed_since_the_assessment_is_refused_not_reused(self):
        moved = clone(self.trusted, os.path.join(self.scratch(), "later"), edit=lambda r: set_policy_records(r, [SYNTHETIC_RECORD]))
        problems = self.receive(self.facts(trusted=moved), self.edited(), trusted=moved)[1]
        self.assertIn("preflight_evaluator_changed", codes(problems))
        self.assertIn("preflight_policy_changed", codes(problems))

    def test_REGRESSION_an_assessment_that_expired_while_queued_is_refused(self):
        for now, margin in (("2026-09-27T12:40:00.000Z", 30), ("2026-09-27T13:00:01.000Z", 0)):
            self.assertEqual(codes(self.receive(self.facts(), self.edited(), now=now, margin=margin)[1]), ["preflight_expired"], now)
        self.assertEqual(self.receive(self.facts(), self.edited(), now="2026-09-27T12:20:00.000Z")[1], [], "CONTROL: inside the margin")

    def test_REGRESSION_the_result_must_be_the_one_this_evaluation_listed(self):
        problems = self.receive(self.facts(), self.edited(), evaluationRun=lambda r: r.update(path=".github/workflows/ci.yml"))[1]
        self.assertEqual(codes(problems), ["preflight_wrong_evaluation"])
        facts = self.facts()
        tt.write_evaluation(facts, self.trusted, self.edited())
        self.assertEqual(codes(pf.receive(self.trusted, facts, os.path.join(self.scratch(), "r"), EVAL, RECEIVED_AT,
                                          expect_preflight={"id": "602", "digest": "sha256:" + "0" * 64})[1]), ["preflight_wrong_evaluation"])


class ATrustedPolicyDecidesAndACandidateCannotChooseIt(unittest.TestCase):
    """The candidate carries its own policy (with a certification-time exception) and a
    booby-trapped copy of the verifier. Neither is read as authority, and neither runs.
    Dates are relative to TODAY: the record is approved yesterday and lapses at 00:00 UTC
    the day after tomorrow, so the fixture means the same thing whenever it runs."""

    PRODUCING = {"DINIFY_FIXTURE_PRODUCER": "1"}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.stub = os.path.join(cls._tmp.name, "stub.py")
        with open(cls.stub, "w", encoding="utf-8") as fh:
            fh.write(tt.STARTUP_STAND_IN)
        cls.marker = os.path.join(cls._tmp.name, "CANDIDATE-CODE-RAN")
        # The trap fires whenever the candidate's copy is imported — except by the fixture's
        # own producer, which legitimately runs this checkout to certify it.
        trap = "import os, pathlib\nif os.environ.get('DINIFY_FIXTURE_PRODUCER') != '1':\n    pathlib.Path(%r).write_text('ran')\n" % cls.marker
        record = dict(SYNTHETIC_RECORD, expires=day(2)[:10], approval=dict(SYNTHETIC_RECORD["approval"], date=day(-1)[:10]))
        cls.record, cls.certified_at = record, day(0, "00:00:30.000")
        with open(os.path.join(tt.REPO, "dependency_audit", "policy.json"), "r", encoding="utf-8") as fh:
            policy = json.load(fh)
        policy["target"]["python"] = tt.target()["python"]
        policy["records"] = [record]
        p = tt.project(cls._tmp.name, extra_files={"dependency_audit/policy.json": json.dumps(policy, indent=2) + "\n"})
        for module in ("preflight.py", "__init__.py"):
            with open(os.path.join(p["root"], "release", module), "a", encoding="utf-8") as fh:
                fh.write("\n" + trap)
        tt.git(p["root"], "add", "-A")
        tt.git(p["root"], "commit", "-qm", "the candidate's own policy, and a trap in its verifier")
        p["commit"], p["tree"] = tt.git(p["root"], "rev-parse", "HEAD"), tt.git(p["root"], "rev-parse", "HEAD^{tree}")
        work = os.path.join(cls._tmp.name, "work")
        os.makedirs(work)
        base = tt.base_python()
        for args in (["observe", "--out", os.path.join(work, "before.json")],
                     ["install", "--wheelhouse", p["wheelhouse"], "--venv", os.path.join(work, "venv"), "--work", os.path.join(work, "install")]):
            proc = tt.cli(p["root"], base, *args, env=cls.PRODUCING)
            if proc.returncode != 0:
                raise RuntimeError("%s: %s" % (proc.stdout, proc.stderr))
        venv_python = os.path.join(work, "venv", "bin", "python")
        # Certification: the advisory existed, and the candidate's own policy excepted it.
        evidence = tt.write_evidence(p["root"], venv_python, vulns={"demo-lib": [{"id": "GHSA-demo-0001", "aliases": [], "description": "synthetic"}]}, now=cls.certified_at)
        result = oc.reevaluate(p["root"], evidence, cls.certified_at, python=venv_python)
        assert result["outcome"] == "exceptions_only", result["reasons"]
        oc._write_json(os.path.join(evidence, "result.json"), result)
        cls.state = dict(p, work=work, venv=os.path.join(work, "venv"), python=venv_python, before=os.path.join(work, "before.json"), evidence=evidence)
        cls.cand = certify(cls._tmp.name, "exc", cls.state, stub=cls.stub, env=cls.PRODUCING)
        # The TRUSTED verifier: the same code without the trap, and a policy with no record.
        cls.clean = clone(p["root"], os.path.join(cls._tmp.name, "trusted"), edit=lambda r: (
            shutil.copy(os.path.join(tt.REPO, "release", "preflight.py"), os.path.join(r, "release", "preflight.py")),
            shutil.copy(os.path.join(tt.REPO, "release", "__init__.py"), os.path.join(r, "release", "__init__.py")),
            set_policy_records(r, [])))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_assessment(self, trusted, clock):
        base = tempfile.mkdtemp(dir=self._tmp.name)
        facts = os.path.join(base, "facts")
        tt.write_facts(facts, self.state["commit"], self.state["tree"], trusted, self.cand["zip"], self.cand["reportZip"])
        scanner = tt.FakeScanner(trusted, vulns={"application": {"demo-lib": ["GHSA-demo-0001"]}})
        doc = pf.assess(trusted, facts, os.path.join(base, "out"), os.path.join(base, "work"),
                        dict(EVAL, revision=tt.git(trusted, "rev-parse", "HEAD")), clock, runner=scanner)
        return doc, facts, os.path.join(base, "out")

    def test_REGRESSION_the_certification_time_exception_does_not_survive_into_the_fresh_decision(self):
        doc, _, _ = self.run_assessment(self.clean, tt.Clock(start=day(0, "01:00:00.000")))
        self.assertEqual(doc["problems"], [], "the historical certification is still verified, under the policy it carried")
        self.assertEqual(doc["candidate"]["originalAudit"]["outcome"], "exceptions_only")
        self.assertEqual(doc["assessment"]["outcome"], "blocking")
        with open(os.path.join(self.clean, "dependency_audit", "policy.json"), "rb") as fh:
            self.assertEqual(doc["evaluator"]["policySha256"], hashlib.sha256(fh.read()).hexdigest())
        self.assertFalse(os.path.exists(self.marker), "no module of the candidate was imported or run")

    def test_CONTROL_the_trap_is_armed(self):
        proc = subprocess.run([tt.base_python(), "-B", "-c", "import release"], cwd=self.state["root"], env=tt.clean_env(), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(self.marker), "importing the candidate's copy DOES run it — which is why the preflight never does")
        os.remove(self.marker)

    def test_REGRESSION_an_exception_that_lapsed_under_the_current_policy_blocks_while_the_history_stays_readable(self):
        # The same record, still in the trusted policy, has lapsed by the time of this preflight.
        doc, _, _ = self.run_assessment(self.state["root"], tt.Clock(start=day(2, "01:00:00.000")))
        self.assertEqual(doc["problems"], [])
        self.assertEqual((doc["candidate"]["originalAudit"]["outcome"], doc["assessment"]["outcome"]), ("exceptions_only", "blocking"))
        self.assertIn("record_refused", [r["code"] for r in doc["assessment"]["reasons"]])

    def test_REGRESSION_an_applied_record_lapsing_while_the_result_is_queued_expires_it(self):
        doc, facts, out = self.run_assessment(self.state["root"], tt.Clock(start=day(1, "12:00:00.000")))
        self.assertEqual((doc["decision"], doc["assessment"]["outcome"]), ("accepted", "exceptions_only"))
        self.assertEqual(doc["assessment"]["recordsApplied"], [{"id": self.record["id"], "kind": "exception", "expires": self.record["expires"]}])
        self.assertEqual(doc["deadline"], day(2), "the lapse cuts the 24-hour window short")
        tt.write_evaluation(facts, self.state["root"], out, started=day(1, "12:00:00"), created=day(1, "12:30:00"))
        recv = lambda now: pf.receive(self.state["root"], facts, tempfile.mkdtemp(dir=self._tmp.name), EVAL, now)[1]  # noqa: E731
        self.assertEqual(recv(day(1, "14:00:00.000")), [], "CONTROL")
        self.assertEqual(codes(recv(day(1, "23:45:00.000"))), ["preflight_expired"])


class TheScannerBootstrapIgnoresAmbientPipConfiguration(unittest.TestCase):
    """REAL pip, REAL venv: a pip.conf offering a find-links location is honoured by B2.1's
    CI scanner install (``--isolated`` still reads site configuration — recorded, and left
    unchanged there) and refused by the preflight's, which sets ``PIP_CONFIG_FILE=/dev/null``.
    Offline: the index is behind a dead proxy, so only configuration could satisfy it."""

    def test_REGRESSION_configuration_can_satisfy_the_ci_install_but_not_the_preflights(self):
        with tempfile.TemporaryDirectory() as d:
            offer = os.path.join(d, "offer")
            os.makedirs(offer)
            wheel = tt.make_wheel(offer, "demo-scanner", "1.0")
            root = os.path.join(d, "trusted")
            os.makedirs(os.path.join(root, "dependency_audit"))
            with open(os.path.join(root, "dependency_audit", "scanner-requirements.txt"), "w", encoding="utf-8") as fh:
                fh.write("demo-scanner==1.0 \\\n    --hash=sha256:%s\n" % sha(wheel))
            policy = {"scanner": {"requirements": "dependency_audit/scanner-requirements.txt", "index": "https://pypi.org/simple/", "timeoutSeconds": 120}}
            dead = {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}

            def planting(outer):
                def run(command, args, cwd=None, env=None, timeout=None):
                    result = outer(command, args, cwd=cwd, env=dict(env or {}, **dead), timeout=timeout)
                    if list(args[:2]) == ["-m", "venv"]:
                        with open(os.path.join(args[2], "pip.conf"), "w", encoding="utf-8") as fh:
                            fh.write("[global]\nfind-links = %s\n" % offer)
                    return result
                return run

            results = {}
            for label, runner in (("ci", planting(oc.spawn_runner)), ("preflight", pf.scanner_runner(planting(oc.spawn_runner)))):
                work = os.path.join(d, label)
                os.makedirs(work)
                with mock.patch.object(sys, "executable", tt.base_python()):
                    _, problems, summary = oc.default_install_scanner(root, policy, runner, work)
                results[label] = (summary.get("install"), codes(problems))
            self.assertEqual(results["ci"], (0, []), "NEGATIVE CONTROL: B2.1's install takes the configured offer")
            self.assertNotEqual(results["preflight"][0], 0)
            self.assertEqual(results["preflight"][1], ["scanner_install_failed"])


class TheCommandsRunThroughTheirOwnFacts(Fixture):
    """The real CLIs: ``facts`` through a stub ``gh`` serving the API's answers, and
    ``verify`` receiving a result exactly as the workflow's receiving job does."""

    def stub_gh(self, facts, extra=None):
        d = self.scratch()
        served = {}
        base = "/repos/%s" % cd.REPOSITORY
        run, attempt, target = tt.CI_RUN, "1", self.state["commit"]
        with open(os.path.join(facts, pf.FACT_FILES["main"]), "r", encoding="utf-8") as fh:
            main = json.load(fh)
        with open(os.path.join(facts, pf.FACT_FILES["evaluatorCommit"]), "r", encoding="utf-8") as fh:
            evaluator = json.load(fh)
        revision = evaluator["sha"]
        for key, endpoint in (("workflow", "%s/actions/workflows/ci.yml" % base), ("run", "%s/actions/runs/%s/attempts/%s" % (base, run, attempt)),
                              ("jobs", "%s/actions/runs/%s/attempts/%s/jobs?per_page=100" % (base, run, attempt)),
                              ("artifacts", "%s/actions/runs/%s/artifacts?per_page=100" % (base, run)), ("commit", "%s/git/commits/%s" % (base, target)),
                              ("main", "%s/commits/main" % base), ("evaluatorCommit", "%s/git/commits/%s" % (base, revision)),
                              ("compare", "%s/compare/%s...%s?per_page=1" % (base, target, main["sha"])),
                              ("mainTree", "%s/git/trees/%s" % (base, main["commit"]["tree"]["sha"])),
                              ("evaluatorTree", "%s/git/trees/%s" % (base, evaluator["tree"]["sha"])),
                              ("evaluationRun", "%s/actions/runs/%s/attempts/1" % (base, tt.EVAL_RUN)),
                              ("evaluationArtifacts", "%s/actions/runs/%s/artifacts?per_page=100" % (base, tt.EVAL_RUN))):
            served[endpoint] = os.path.join(facts, pf.FACT_FILES[key])
        for label, artifact in (("candidate", "501"), ("reconstruction", "502"), ("preflight", "601")):
            served["%s/actions/artifacts/%s/zip" % (base, artifact)] = os.path.join(facts, pf.ZIPS, label + ".zip")
        served.update(extra or {})
        with open(os.path.join(d, "served.json"), "w", encoding="utf-8") as fh:
            json.dump(served, fh)
        gh = os.path.join(d, "gh")
        with open(gh, "w", encoding="utf-8") as fh:
            fh.write("#!%s\nimport json, os, sys\nserved = json.load(open(%r))\nwith open(%r, 'a') as log:\n"
                     "    log.write(json.dumps({'endpoint': sys.argv[-1], 'token': bool(os.environ.get('GH_TOKEN'))}) + '\\n')\n"
                     "if sys.argv[-1] not in served:\n    sys.stderr.write('HTTP 404: Not Found\\n'); sys.exit(1)\n"
                     "sys.stdout.buffer.write(open(served[sys.argv[-1]], 'rb').read())\n" % (sys.executable, os.path.join(d, "served.json"), os.path.join(d, "calls.log")))
        os.chmod(gh, 0o755)
        return gh, os.path.join(d, "calls.log"), revision

    def cli(self, *args, env=None):
        return subprocess.run([tt.base_python(), "-B", "-m", "release", "preflight"] + list(args), cwd=self.trusted,
                              env=tt.clean_env(dict(env or {}, GITHUB_REF=cd.MAIN_REF)), capture_output=True, text=True, timeout=300)

    def fetched_evaluation(self):
        """A real assessment, served through a stub `gh` and fetched by the real `facts`
        command as the verify job fetches it. The receiving command reads the REAL clock (it
        has no override), so the evaluation is dated an hour ago."""
        start = pf._iso(pf._ms(pf.now_iso()) - 3600 * 1000)
        served_facts = self.facts()
        _, out, _, _ = self.assess(served_facts, clock=tt.Clock(start=start))
        found = tt.write_evaluation(served_facts, self.trusted, out, started=start[:19] + "Z", created=pf._iso(pf._ms(start) + 600 * 1000)[:19] + "Z")
        gh, calls, revision = self.stub_gh(served_facts)
        event = {"PREFLIGHT_EVENT": "workflow_run", "PREFLIGHT_EVENT_RUN": tt.CI_RUN, "PREFLIGHT_EVENT_ATTEMPT": "1",
                 "PREFLIGHT_EVENT_SHA": self.state["commit"], "GH_TOKEN": "read-only"}
        fetched = os.path.join(self.scratch(), "facts")
        proc = self.cli("facts", "--out", fetched, "--revision", revision, "--evaluation-run", tt.EVAL_RUN, "--evaluation-attempt", "1", "--gh", gh, env=event)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return served_facts, fetched, found, calls

    def test_REGRESSION_the_verify_job_receives_with_the_hint_exactly_as_upload_artifact_emits_it(self):
        # actions/upload-artifact's `artifact-digest` output is BARE hex; the REST listing says
        # `sha256:<hex>`. The workflow forwards the former, so the receiving check must read
        # both as one digest — or every valid result is refused. A hint that names another
        # artifact, in either spelling, is still refused, and a malformed one is not a hint.
        _, fetched, found, _ = self.fetched_evaluation()
        bare = found["digest"][len("sha256:"):]
        def verify(*hint):
            return self.cli("verify", "--facts", fetched, "--work", os.path.join(self.scratch(), "w"), "--evaluation-run", tt.EVAL_RUN,
                            "--evaluation-attempt", "1", *hint)
        for spelling in (bare, found["digest"]):
            proc = verify("--expect-preflight-id", found["id"], "--expect-preflight-digest", spelling)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for other in ("0" * 64, "sha256:" + "0" * 64):
            proc = verify("--expect-preflight-id", found["id"], "--expect-preflight-digest", other)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("preflight_wrong_evaluation", proc.stdout + proc.stderr)
        proc = verify("--expect-preflight-id", "602", "--expect-preflight-digest", bare)
        self.assertIn("preflight_wrong_evaluation", proc.stdout + proc.stderr)
        for malformed in (("--expect-preflight-id", found["id"], "--expect-preflight-digest", "sha256:" + bare[:10]),
                          ("--expect-preflight-id", found["id"], "--expect-preflight-digest", ""),
                          ("--expect-preflight-id", "", "--expect-preflight-digest", bare),
                          ("--expect-preflight-id", "abc", "--expect-preflight-digest", bare),
                          ("--expect-preflight-digest", bare)):
            proc = verify(*malformed)
            self.assertEqual(proc.returncode, 1, malformed)
            self.assertIn("request_invalid", proc.stdout + proc.stderr, malformed)

    def test_CONTROL_facts_then_verify_through_the_real_commands(self):
        served_facts, fetched, _, calls = self.fetched_evaluation()
        for label in ("candidate", "reconstruction", "preflight"):
            self.assertEqual(sha(os.path.join(fetched, pf.ZIPS, label + ".zip")), sha(os.path.join(served_facts, pf.ZIPS, label + ".zip")))
        outputs = os.path.join(self.scratch(), "outputs")
        proc = self.cli("verify", "--facts", fetched, "--work", os.path.join(self.scratch(), "w"), "--evaluation-run", tt.EVAL_RUN,
                        "--evaluation-attempt", "1", env={"GITHUB_OUTPUT": outputs})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("deploymentAuthorized false", proc.stdout)
        with open(outputs, "r", encoding="utf-8") as fh:
            written = dict(line.split("=", 1) for line in fh.read().splitlines())
        self.assertEqual((written["commit"], written["candidateArtifactId"]), (self.state["commit"], "501"))
        with open(calls, "r", encoding="utf-8") as fh:
            self.assertTrue(all(json.loads(line)["token"] for line in fh))

    def test_REGRESSION_a_hostile_or_ambiguous_request_is_refused_before_any_api_read(self):
        gh, calls, revision = self.stub_gh(self.facts())
        for env in ({"PREFLIGHT_EVENT": "workflow_dispatch", "PREFLIGHT_INPUT_SHA": "$(id)"},
                    {"PREFLIGHT_EVENT": "pull_request_target", "PREFLIGHT_INPUT_SHA": self.state["commit"]}):
            proc = self.cli("facts", "--out", os.path.join(self.scratch(), "f"), "--revision", revision, "--gh", gh, env=env)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("request_invalid", proc.stderr)
        self.assertFalse(os.path.exists(calls))
        proc = self.cli("facts", "--out", os.path.join(self.scratch(), "f"), "--revision", revision, "--gh", gh,
                        env={"PREFLIGHT_EVENT": "workflow_dispatch", "PREFLIGHT_INPUT_SHA": "f" * 40})
        self.assertEqual(proc.returncode, 1, "an unanswered API read is a failure, not an empty fact")
        self.assertIn("facts_unavailable", proc.stderr)

    def test_CONTRACT_the_assessment_command_has_no_clock_or_scanner_override(self):
        proc = self.cli("assess", "--help")
        for word in ("--now", "--clock", "--scanner", "--runner", "--policy"):
            self.assertNotIn(word, proc.stdout)


if __name__ == "__main__":
    unittest.main()
