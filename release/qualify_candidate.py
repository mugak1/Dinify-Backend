"""PRODUCER -> CONSUMER, end to end: the real ``python -B -m release`` CLIs certify a
synthetic project laid out like this repository (the real release/ and dependency_audit/
code, a real git repository, a real offline environment, audit evidence in the real
formats), and the real consumer accepts the candidate only as the candidate it expected.

Each regression changes ONE fact — an identity, a byte, a file, an outcome — and names
the refusal it must produce and the boundary that produces it. Offline.
"""

import json
import os
import shutil
import tarfile
import tempfile
import unittest

from release import candidate as cd
from release import consumer as cs
from release import sourcetree as st
from release import testing as tt

MAIN_RUN, PR_RUN, OTHER_RUN = "9001", "9002", "9003"


def codes(problems):
    return sorted({p["code"] for p in problems})


def tree_hashes(root):
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            path = os.path.join(dirpath, f)
            out[os.path.relpath(path, root)] = (cd.file_sha256(path), os.stat(path).st_mtime_ns)
    return out


class Produced(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.state = tt.produce(cls._tmp.name)
        cls.commit, cls.tree = cls.state["commit"], cls.state["tree"]
        cls.main = os.path.join(cls._tmp.name, "candidate-main")
        cls.pr = os.path.join(cls._tmp.name, "candidate-pr")
        cls.other = os.path.join(cls._tmp.name, "candidate-other-run")
        for out, env in ((cls.main, tt.ci_env(cls.commit, run_id=MAIN_RUN)),
                         (cls.pr, tt.ci_env(cls.commit, event="pull_request", ref="refs/pull/5/merge", run_id=PR_RUN)),
                         (cls.other, tt.ci_env(cls.commit, run_id=OTHER_RUN))):
            proc = tt.package(cls.state, out, env=env)
            if proc.returncode != 0:
                raise RuntimeError("fixture packaging refused: %s %s" % (proc.stdout, proc.stderr))
        cls.stub = os.path.join(cls._tmp.name, "startup_stand_in.py")
        with open(cls.stub, "w", encoding="utf-8") as fh:
            fh.write(tt.STARTUP_STAND_IN)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def expect(self, **over):
        e = {"repository": cd.REPOSITORY, "commit": self.commit, "tree": self.tree, "runId": MAIN_RUN, "runAttempt": "1",
             "event": "push", "ref": cd.MAIN_REF, "artifact": "backend-candidate-%s-1" % MAIN_RUN, "workflowPath": cd.WORKFLOW_PATH,
             "local": False}
        e.update(over)
        return e

    def copy(self, source=None):
        d = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "candidate")
        shutil.copytree(source or self.main, d)
        return d

    @staticmethod
    def edit_record(candidate, fn):
        path = os.path.join(candidate, cd.RECORD)
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
        fn(record)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def write_archive(self, files):
        """A well-formed archive of ``files`` — what a forger who controls the bytes but not
        the consumer's git would produce."""
        path = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "source.tar")
        with tarfile.open(path, "w") as tf:
            for name, (mode, data) in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o755 if mode == "100755" else 0o644
                tf.addfile(info, __import__("io").BytesIO(data))
        return path

    def refused(self, candidate, expect=None):
        state, problems = cs.verify(candidate, expect or self.expect())
        self.assertIsNone(state)
        return codes(problems)


class TheCandidateIsWhatWasCertified(Produced):
    def test_CONTROL_a_push_to_main_is_recorded_promotable_and_verifies(self):
        with open(os.path.join(self.main, cd.RECORD), "r", encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual((record["commit"], record["tree"], record["eligibility"]["promotable"], record["artifact"]["name"]),
                         (self.commit, self.tree, True, "backend-candidate-%s-1" % MAIN_RUN))
        self.assertEqual(sorted(record["validation"]["requiredSteps"]), sorted(cd.REQUIRED_STEPS))
        self.assertEqual(sorted(os.listdir(self.main)), sorted([cd.RECORD, cd.SOURCE, cd.WHEELHOUSE, cd.EVIDENCE]))
        state, problems = cs.verify(self.main, self.expect())
        self.assertEqual(problems, [])
        self.assertEqual(sorted(p["name"] for p in record["environment"]["packages"]), ["demo-app", "demo-lib", "pip"])

    def test_CONTROL_a_pull_request_candidate_is_named_and_recorded_non_promotable(self):
        with open(os.path.join(self.pr, cd.RECORD), "r", encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertFalse(record["eligibility"]["promotable"])
        self.assertIn("merge preview", record["eligibility"]["reason"])
        self.assertEqual(record["artifact"]["name"], "backend-candidate-nonpromotable-%s-1" % PR_RUN)
        expect = self.expect(runId=PR_RUN, event="pull_request", ref="refs/pull/5/merge", artifact=record["artifact"]["name"])
        self.assertEqual(cs.verify(self.pr, expect)[1], [])

    def test_CONTROL_an_offline_reconstruction_rebuilds_the_certified_environment_and_touches_nothing(self):
        before = tree_hashes(self.main)
        work = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "work")
        report, problems = cs.reconstruct(self.main, work, self.expect(), tt.NOW, startup=self.stub)
        self.assertEqual(problems, [])
        with open(os.path.join(self.main, cd.RECORD), "r", encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertTrue(report["environment"]["matchesRecord"])
        self.assertEqual(report["environment"]["digest"], record["environment"]["digest"])
        self.assertEqual(report["candidate"]["createdAt"], record["createdAt"], "the certification's own time is reproduced")
        self.assertNotEqual(report["reconstructedAt"], record["createdAt"] + "x")
        self.assertEqual(tree_hashes(self.main), before, "the candidate was not modified")
        self.assertEqual(st.archive_tree(st.read_archive(os.path.join(self.main, cd.SOURCE))[0]), self.tree)


class TheWrongCandidateIsRefusedAtTheConsumer(Produced):
    def test_REGRESSION_wrong_commit_run_attempt_event_or_artifact(self):
        for over in ({"commit": "0" * 40}, {"runId": "1"}, {"runAttempt": "2"}, {"event": "pull_request"},
                     {"ref": "refs/heads/feature"}, {"artifact": "backend-candidate-1-1"}, {"repository": "mugak1/Dinify-Admin"}):
            self.assertEqual(self.refused(self.main, self.expect(**over)), ["identity_mismatch"], over)

    def test_REGRESSION_the_same_commit_from_another_run_is_not_the_expected_candidate(self):
        self.assertEqual(cs.verify(self.other, self.expect(runId=OTHER_RUN, artifact="backend-candidate-%s-1" % OTHER_RUN))[1], [])
        self.assertEqual(self.refused(self.other), ["identity_mismatch"])

    def test_REGRESSION_a_pull_request_candidate_relabelled_promotable_is_refused(self):
        c = self.copy(self.pr)
        self.edit_record(c, lambda r: (r["eligibility"].update(promotable=True),
                                       r["artifact"].update(name="backend-candidate-%s-1" % PR_RUN)))
        expect = self.expect(runId=PR_RUN, event="pull_request", ref="refs/pull/5/merge", artifact="backend-candidate-%s-1" % PR_RUN)
        self.assertIn("eligibility_mismatch", self.refused(c, expect))

    def test_REGRESSION_a_self_consistent_source_of_another_commit_is_refused_against_the_expected_tree(self):
        c = self.copy()
        files, _ = st.read_archive(os.path.join(c, cd.SOURCE))
        files["app.py"] = ("100644", b"print('substituted')\n")
        other = self.write_archive(files)
        shutil.copy(other, os.path.join(c, cd.SOURCE))
        forged = st.archive_tree(files)
        self.edit_record(c, lambda r: (r.update(tree=forged), r["source"]["archive"].update(sha256=cd.file_sha256(other), size=os.path.getsize(other)),
                                       r["source"].update(contentSha256=st.listing_digest(files), tree=forged)))
        self.assertEqual(self.refused(c), ["identity_mismatch"], "the record's tree is not the expected one")
        self.assertEqual(self.refused(c, self.expect(tree=forged)), ["evidence_foreign"],
                         "even when the consumer is fooled about the tree, the audit evidence names the real one")

    def test_REGRESSION_a_substituted_archive_described_consistently_under_the_expected_tree_is_refused_by_recomputation(self):
        # The forger keeps the record's commit and tree EXACTLY as expected and rewrites only
        # what describes the archive's bytes. The identity check passes (the record names the
        # right tree), the audit evidence is genuinely this commit's, the lock and the wheels
        # are untouched. Only recomputing the tree from the archive can tell — and without it
        # the substituted code is what the reconstruction would extract and start.
        c = self.copy()
        files, _ = st.read_archive(os.path.join(c, cd.SOURCE))
        victim = next(p for p in sorted(files) if p.endswith(".py") and files[p][1])
        files[victim] = (files[victim][0], files[victim][1] + b"\nimport os; os.system('true')\n")
        other = self.write_archive(files)
        shutil.copy(other, os.path.join(c, cd.SOURCE))
        self.edit_record(c, lambda r: (r["source"]["archive"].update(sha256=cd.file_sha256(other), size=os.path.getsize(other)),
                                       r["source"].update(contentSha256=st.listing_digest(files), files=len(files))))
        with open(os.path.join(c, cd.RECORD), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tree"], self.tree, "the premise: the record still names the expected tree")
        self.assertEqual(self.refused(c), ["source_mismatch"])
        work = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "work")
        _, problems = cs.reconstruct(c, work, self.expect(), tt.NOW, startup=self.stub)
        self.assertEqual(codes(problems), ["source_mismatch"])
        self.assertFalse(os.path.exists(os.path.join(work, "source")), "nothing was extracted")

    def test_REGRESSION_a_record_claiming_another_certified_environment_is_refused_after_the_rebuild(self):
        # Everything the consumer can check BEFORE running anything holds; the record simply
        # describes a different environment from the one its own wheels rebuild. The rebuilt
        # environment is compared, and nothing is started.
        c = self.copy()
        self.edit_record(c, lambda r: r["environment"].update(digest="0" * 64))
        self.assertEqual(cs.verify(c, self.expect())[1], [], "the premise: nothing before the rebuild can see it")
        work = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "work")
        report, problems = cs.reconstruct(c, work, self.expect(), tt.NOW, startup=self.stub)
        self.assertEqual(codes(problems), ["environment_mismatch"])
        self.assertFalse(report["environment"]["matchesRecord"])
        self.assertNotIn("startup", report, "a mismatched environment is never started")

    def test_REGRESSION_a_poisoned_source_archive_is_refused_before_extraction(self):
        c = self.copy()
        with tarfile.open(os.path.join(c, cd.SOURCE), "a") as tf:
            link = tarfile.TarInfo("zz-link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tf.addfile(link)
        size, sha = os.path.getsize(os.path.join(c, cd.SOURCE)), cd.file_sha256(os.path.join(c, cd.SOURCE))
        self.edit_record(c, lambda r: r["source"]["archive"].update(sha256=sha, size=size))
        self.assertEqual(self.refused(c), ["archive_unsafe"])

    def test_REGRESSION_changed_wheel_bytes_with_a_consistently_rewritten_record_are_refused_against_the_lock(self):
        c = self.copy()
        lib = next(n for n in os.listdir(os.path.join(c, cd.WHEELHOUSE)) if n.startswith("demo_lib"))
        tt.make_wheel(os.path.join(c, cd.WHEELHOUSE), tt.LIB, "1.0", modules={"demo_lib/__init__.py": "EVIL = 1\n"})
        path = os.path.join(c, cd.WHEELHOUSE, lib)

        def rewrite(r):
            for e in r["wheelhouse"]["files"]:
                if e["filename"] == lib:
                    e.update(sha256=cd.file_sha256(path), size=os.path.getsize(path))
            r["wheelhouse"]["digest"] = cd.listing_digest(r["wheelhouse"]["files"])
        self.edit_record(c, rewrite)
        self.assertEqual(self.refused(c), ["artifact_mismatch"])

    def test_REGRESSION_a_wheel_absent_from_the_candidate_is_refused_whatever_else_is_available(self):
        c = self.copy()
        os.remove(os.path.join(c, cd.WHEELHOUSE, next(n for n in os.listdir(os.path.join(c, cd.WHEELHOUSE)) if n.startswith("demo_lib"))))
        self.assertEqual(self.refused(c), ["missing_artifact"])

    def test_REGRESSION_unexpected_container_contents(self):
        cases = {
            "extra root file": lambda c: tt.write(c, "notes.txt", "x"),
            "extra wheel": lambda c: shutil.copy(os.path.join(c, cd.WHEELHOUSE, sorted(os.listdir(os.path.join(c, cd.WHEELHOUSE)))[0]),
                                                 os.path.join(c, cd.WHEELHOUSE, "demo_extra-1.0-py3-none-any.whl")),
            "environment file": lambda c: tt.write(c, ".env", "SECRET_KEY=never-read"),
            "link in the evidence": lambda c: (os.remove(os.path.join(c, cd.EVIDENCE, "result.json")),
                                               os.symlink("/etc/hostname", os.path.join(c, cd.EVIDENCE, "result.json"))),
            "missing source": lambda c: os.remove(os.path.join(c, cd.SOURCE)),
            "nested directory": lambda c: os.makedirs(os.path.join(c, cd.WHEELHOUSE, "sub")),
        }
        for label, fn in cases.items():
            c = self.copy()
            fn(c)
            self.assertTrue(set(self.refused(c)) & {"container_unexpected", "container_incomplete", "unexpected_artifact"}, label)

    def test_REGRESSION_an_unreadable_or_unknown_record_is_refused(self):
        for fn in (lambda r: r.update(schema="dinify.backend.candidate/2"), lambda r: r.update(extra=True), lambda r: r.pop("audit")):
            c = self.copy()
            self.edit_record(c, fn)
            self.assertTrue(set(self.refused(c)) & {"record_unsupported", "record_invalid"})

    def test_REGRESSION_missing_truncated_or_inconsistent_audit_evidence_is_refused(self):
        c = self.copy()
        os.remove(os.path.join(c, cd.EVIDENCE, "collection.json"))
        self.assertEqual(self.refused(c), ["evidence_incomplete"])

        c = self.copy()
        raw = os.path.join(c, cd.EVIDENCE, "application.scanner-stdout.txt")
        with open(raw, "r+", encoding="utf-8") as fh:
            text = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(text[: len(text) // 2])
        self.assertEqual(self.refused(c), ["evidence_mismatch", "evidence_tampered"])

        c = self.copy()
        raw = os.path.join(c, cd.EVIDENCE, "application.scanner-stdout.txt")
        with open(raw, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        report["dependencies"][0]["vulns"] = [{"id": "PYSEC-2026-9999", "aliases": [], "description": "synthetic"}]
        text = json.dumps(report)
        with open(raw, "w", encoding="utf-8") as fh:
            fh.write(text)
        coll = os.path.join(c, cd.EVIDENCE, "collection.json")
        with open(coll, "r", encoding="utf-8") as fh:
            collection = json.load(fh)
        collection["graphs"]["application"]["run"].update(stdoutSha256=tt.lf.sha256(text), status=1)
        with open(coll, "w", encoding="utf-8") as fh:
            json.dump(collection, fh)

        def relist(r):
            files = [{"filename": n, "sha256": cd.file_sha256(os.path.join(c, cd.EVIDENCE, n)),
                      "size": os.path.getsize(os.path.join(c, cd.EVIDENCE, n))} for n in sorted(os.listdir(os.path.join(c, cd.EVIDENCE)))]
            r["audit"]["evidence"].update(files=files, digest=cd.listing_digest(files))
        self.edit_record(c, relist)
        self.assertEqual(self.refused(c), ["audit_not_passed", "evidence_inconsistent"],
                         "result.json still says within policy; the raw output it came from does not")

    def test_REGRESSION_an_environment_file_above_the_reconstruction_stops_it_unread(self):
        parent = tempfile.mkdtemp(dir=self._tmp.name)
        tt.write(parent, ".env", "SECRET_KEY=must-never-be-read-9f3c\n")
        report, problems = cs.reconstruct(self.main, os.path.join(parent, "work"), self.expect(), tt.NOW, startup=self.stub)
        self.assertEqual(codes(problems), ["environment_file_nearby"])
        self.assertNotIn("must-never-be-read", json.dumps([report, problems]))
        self.assertFalse(os.path.exists(os.path.join(parent, "work", "venv")), "nothing was built or started")


class TheProducerRefusesToCertify(unittest.TestCase):
    """Destructive producer cases, on a production of their own."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.state = tt.produce(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def package(self, **ci):
        out = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "candidate")
        proc = tt.package(self.state, out, env=tt.ci_env(self.state["commit"], **ci))
        return proc, out

    def assertRefused(self, proc, out, code):
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(code, proc.stderr)
        self.assertFalse(os.path.exists(out), "no partial candidate is left behind")

    def test_CONTROL_the_same_production_packages_cleanly(self):
        proc, out = self.package()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, cd.RECORD)))

    def test_REGRESSION_a_required_step_that_did_not_succeed_blocks_certification(self):
        for outcome in ("failure", "cancelled", "skipped"):
            proc, out = self.package(outcomes={"suite": outcome})
            self.assertRefused(proc, out, "required_step_not_passed")

    def test_REGRESSION_unreadable_step_outcomes_are_a_named_refusal_not_a_crash(self):
        for bad in ("{not json", "[]", '"success"'):
            env = tt.ci_env(self.state["commit"])
            env["STEP_OUTCOMES"] = bad
            out = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "candidate")
            proc = tt.package(self.state, out, env=env)
            self.assertRefused(proc, out, "step_outcomes_unreadable")
            self.assertNotIn("Traceback", proc.stderr, bad)
        out = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "candidate")
        proc = tt.cli(self.state["root"], self.state["python"], "package", "--local", "--step-outcomes", os.path.join(self._tmp.name, "absent.json"),
                      "--wheelhouse", self.state["wheelhouse"], "--venv", self.state["venv"], "--evidence", self.state["evidence"],
                      "--before", self.state["before"], "--out", out)
        self.assertRefused(proc, out, "step_outcomes_unreadable")
        self.assertNotIn("Traceback", proc.stderr)

    def test_REGRESSION_the_run_must_be_for_the_commit_that_was_checked_out(self):
        out = os.path.join(tempfile.mkdtemp(dir=self._tmp.name), "candidate")
        env = tt.ci_env("1" * 40)
        proc = tt.package(self.state, out, env=env)
        self.assertRefused(proc, out, "wrong_commit")

    def test_REGRESSION_a_source_change_after_validation_blocks_certification(self):
        path = os.path.join(self.state["root"], "app.py")
        with open(path, "rb") as fh:
            original = fh.read()
        try:
            with open(path, "wb") as fh:
                fh.write(b"print('changed after the tests ran')\n")
            self.assertRefused(*self.package(), "source_changed")
        finally:
            with open(path, "wb") as fh:
                fh.write(original)

    def test_REGRESSION_an_unapproved_generated_module_blocks_certification(self):
        path = tt.write(self.state["root"], "generated_settings.py", "DEBUG = True\n")
        try:
            self.assertRefused(*self.package(), "unapproved_file")
        finally:
            os.remove(path)

    def test_REGRESSION_missing_or_blocking_audit_evidence_blocks_certification(self):
        evidence = self.state["evidence"]
        saved = os.path.join(self._tmp.name, "evidence-saved")
        shutil.copytree(evidence, saved)
        try:
            os.remove(os.path.join(evidence, "scanner.scanner-stderr.txt"))
            self.assertRefused(*self.package(), "evidence_incomplete")
            shutil.rmtree(evidence)
            shutil.copytree(saved, evidence)
            tt.write_evidence(self.state["root"], self.state["python"],
                              vulns={"demo-lib": [{"id": "PYSEC-2026-0001", "aliases": [], "description": "synthetic"}]})
            self.assertRefused(*self.package(), "audit_not_passed")
        finally:
            shutil.rmtree(evidence, ignore_errors=True)
            shutil.copytree(saved, evidence)

    def test_REGRESSION_an_inventory_change_after_the_snapshot_blocks_certification(self):
        extras = tempfile.mkdtemp(dir=self._tmp.name)
        tt.make_wheel(extras, tt.WIN, "1.0")
        python = self.state["python"]
        install = tt.run([python, "-I", "-m", "pip", "install", "--no-index", "--find-links", extras, "--no-deps", "demo-win"],
                         env=tt.clean_env({"PIP_CONFIG_FILE": os.devnull}))
        self.assertEqual(install.returncode, 0, install.stderr)
        try:
            proc, out = self.package()
            self.assertRefused(proc, out, "unexpected_package")
            self.assertIn("audit_evidence_inconsistent", proc.stderr, "the audit no longer describes this environment either")
        finally:
            tt.run([python, "-I", "-m", "pip", "uninstall", "-y", "demo-win"], env=tt.clean_env({"PIP_CONFIG_FILE": os.devnull}))

    def test_REGRESSION_a_forbidden_file_in_the_tree_blocks_certification_and_is_named_not_shown(self):
        tmp = tempfile.mkdtemp(dir=self._tmp.name)
        state = tt.produce(tmp, extra_files={"deploy/.env": "SECRET_KEY=never-in-a-log-7b1e\n"})
        out = os.path.join(tmp, "candidate")
        proc = tt.package(state, out, env=tt.ci_env(state["commit"]))
        self.assertRefused(proc, out, "forbidden_path")
        self.assertIn("deploy/.env", proc.stderr)
        self.assertNotIn("never-in-a-log", proc.stdout + proc.stderr)

    def test_CONTROL_eligibility_follows_the_run_never_the_request(self):
        for ci, promotable in (({}, True), ({"event": "pull_request", "ref": "refs/pull/1/merge"}, False),
                               ({"ref": "refs/heads/feature"}, False), ({"event": "workflow_dispatch"}, False),
                               ({"repository": "someone/fork"}, False)):
            proc, out = self.package(**ci)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(out, cd.RECORD), "r", encoding="utf-8") as fh:
                record = json.load(fh)
            self.assertIs(record["eligibility"]["promotable"], promotable, ci)
            self.assertEqual(record["artifact"]["name"].startswith("backend-candidate-nonpromotable-"), not promotable, ci)


if __name__ == "__main__":
    unittest.main()
