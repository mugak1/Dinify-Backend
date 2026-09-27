"""THE INSTALLATION AND THE TRANSITION'S RULES, fast (D08 B3): the profile, the admission, the
runtime files, the include each plane is switched with, the migration decision, the host lock
and the journal, and the switch/restore sequence driven through transport doubles.

Each REGRESSION breaks ONE fact the way a host, a candidate or an operator could; each CONTROL
is the case that must keep passing. The real Apache/mod_wsgi/PostgreSQL rehearsal is recorded
in release/README.md -> "The installation" (it needs root and a disposable host).
"""

import copy
import fcntl
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from release import hostprofile as hp
from release import installation as ins
from release import transition as tr

RID = "a" * 40 + "-" + "b" * 16
OLD = "c" * 40 + "-" + "d" * 16


def codes(problems):
    return sorted({p["code"] for p in problems})


def profile(tmp=None, **over):
    tmp = tmp or "/srv/x"
    plane = lambda daemon, mount: {"daemon": daemon, "mount": mount, "config": "/etc/dinify/%s.env" % daemon, "processes": 2, "threads": 4,  # noqa: E731
                                   "shutdownTimeout": 5, "passAuthorization": True, "staticUrl": mount + "/static",
                                   "probe": {"base": "http://h.invalid" + mount, "connectTo": "127.0.0.1"}}
    doc = {"schema": hp.SCHEMA, "kind": "live", "status": "verified",
           "observation": {"collectedAt": "2026-09-27T00:00:00Z", "collectorSha256": "1" * 64, "reportSha256": "2" * 64,
                           "reviewedIn": "mugak1/Dinify-Backend#1"},
           "releaseRoot": tmp + "/releases", "stateDir": tmp + "/state", "lockPath": tmp + "/lock/host.lock", "basePython": "/usr/bin/python3.12",
           "interpreter": {"python": "3.12.3", "soabi": "cpython-312-x86_64-linux-gnu", "libpython": "/lib/libpython3.12.so.1.0",
                           "libpythonSha256": "3" * 64},
           "modWsgi": {"module": "/usr/lib/apache2/modules/mod_wsgi.so", "sha256": "4" * 64, "version": "5.0.0"},
           "identities": {"prepare": "dinify-prep", "runtime": "www-data", "runtimeGroup": "www-data", "migrate": "dinify-migrate"},
           "apache": {"ctl": "/usr/sbin/apachectl", "includeDir": tmp + "/apache", "reload": "graceful", "drainSeconds": 5},
           "planes": {"customer": plane("dinify-customer", "/uat"), "admin": plane("dinify-admin", "/api")},
           "media": {"root": "/srv/media", "url": "/uat/media"}, "otp": {"deterministicTestOtpAllowed": False, "reason": None},
           "minFreeBytes": 1 << 30}
    for key, value in over.items():
        *parents, leaf = key.split("__")
        node = doc
        for part in parents:
            node = node[part]
        node[leaf] = value
    return doc


class Profile(unittest.TestCase):
    def test_CONTROL_a_complete_verified_live_profile_is_valid(self):
        self.assertEqual(hp.validate(profile()), [])

    def test_REGRESSION_every_unknown_field_is_named_and_the_live_path_refuses_it(self):
        doc = profile(status="unverified", basePython=None, interpreter__libpythonSha256=None, observation=None)
        found = hp.validate(doc)
        self.assertIn("profile_unknown", codes(found))
        self.assertIn("profile_unverified", codes(found))
        detail = [p["detail"] for p in found if p["code"] == "profile_unknown"][0]
        self.assertIn("basePython", detail)
        self.assertIn("interpreter.libpythonSha256", detail)

    def test_REGRESSION_not_serving_static_or_media_is_false_and_never_the_same_as_unknown(self):
        # false: this Apache deliberately serves nothing there, so no Alias is written
        doc = profile(planes__admin__staticUrl=False, media__url=False)
        self.assertEqual(hp.validate(doc), [])
        admin = tr.render_include(doc, "admin", RID, "op-1").decode()
        customer = tr.render_include(doc, "customer", RID, "op-1").decode()
        self.assertNotIn("/static/", admin)
        self.assertNotIn("/srv/media", customer)
        # null: nobody has looked, which the live path refuses by name
        found = hp.validate(profile(planes__admin__staticUrl=None, media__url=None))
        detail = [p["detail"] for p in found if p["code"] == "profile_unknown"][0]
        self.assertIn("planes.admin.staticUrl", detail)
        self.assertIn("media.url", detail)
        # anything else is neither
        self.assertIn("profile_invalid", codes(hp.validate(profile(media__url=True))))
        self.assertIn("profile_invalid", codes(hp.validate(profile(planes__customer__staticUrl="static"))))

    def test_REGRESSION_a_rehearsal_profile_never_drives_the_live_path_and_a_live_one_never_needs_the_flag(self):
        self.assertEqual(codes(hp.validate(profile(kind="rehearsal"))), ["profile_rehearsal_only"])
        self.assertEqual(hp.validate(profile(kind="rehearsal"), rehearsal=True), [])
        self.assertIn("profile_invalid", codes(hp.validate(profile(), rehearsal=True)))

    def test_REGRESSION_the_preparer_may_not_be_the_identity_that_serves_or_migrates(self):
        for key in ("runtime", "migrate"):
            doc = profile()
            doc["identities"]["prepare"] = doc["identities"][key]
            self.assertIn("profile_invalid", codes(hp.validate(doc)), key)
        self.assertIn("profile_invalid", codes(hp.validate(profile(identities__runtime="root"))))

    def test_REGRESSION_media_and_configuration_must_live_outside_every_release(self):
        self.assertIn("profile_invalid", codes(hp.validate(profile(media__root="/srv/x/releases/media"))))
        doc = profile()
        doc["planes"]["admin"]["config"] = "/srv/x/releases/admin.env"
        self.assertIn("profile_invalid", codes(hp.validate(doc)))

    def test_REGRESSION_allowing_the_deterministic_otp_needs_a_stated_reason(self):
        self.assertIn("profile_invalid", codes(hp.validate(profile(otp__deterministicTestOtpAllowed=True))))
        self.assertEqual(hp.validate(profile(otp={"deterministicTestOtpAllowed": True, "reason": "pre-launch UAT keeps ENV=dev on purpose"})), [])

    def test_REGRESSION_shell_unsafe_paths_and_non_loopback_probes_are_refused(self):
        self.assertIn("profile_invalid", codes(hp.validate(profile(releaseRoot="/srv/x y"))))
        doc = profile()
        doc["planes"]["customer"]["probe"]["connectTo"] = "10.0.0.5"
        self.assertIn("profile_invalid", codes(hp.validate(doc)))
        doc = profile()
        doc["planes"]["admin"]["daemon"] = doc["planes"]["customer"]["daemon"]
        self.assertIn("profile_invalid", codes(hp.validate(doc)))

    def test_REGRESSION_an_environment_file_above_the_release_root_is_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a", "releases"))
            self.assertEqual(hp.env_files_above(os.path.join(tmp, "a", "releases")), [])
            open(os.path.join(tmp, "a", ".env"), "w").close()
            self.assertEqual(hp.env_files_above(os.path.join(tmp, "a", "releases")), [os.path.join(tmp, "a", ".env")])


ADMISSION = {"schema": ins.ADMISSION_SCHEMA, "commit": "a" * 40, "tree": "b" * 40, "ciRun": "36275901270", "ciAttempt": "1",
             "candidateArtifactId": "10917740297", "candidateDigest": "sha256:" + "c" * 64, "recordSha256": "d" * 64,
             "environmentDigest": "e" * 64, "preflightArtifactId": "10917139266", "preflightDigest": "sha256:" + "f" * 64,
             "preflightSha256": "0" * 64, "outcome": "within_policy", "deadline": "2026-09-27T22:35:15.028Z", "deadlineEpoch": 1790548515,
             "deploymentAuthorized": False}


class Admission(unittest.TestCase):
    def test_CONTROL_the_receiving_check_s_values_are_the_admission(self):
        self.assertEqual(ins.validate_admission(ADMISSION), [])

    def test_REGRESSION_an_admission_claiming_authority_or_carrying_anything_else_is_refused(self):
        self.assertTrue(ins.validate_admission(dict(ADMISSION, deploymentAuthorized=True)))
        self.assertTrue(ins.validate_admission(dict(ADMISSION, token="x")))
        self.assertTrue(ins.validate_admission({k: v for k, v in ADMISSION.items() if k != "deadlineEpoch"}))
        self.assertTrue(ins.validate_admission(dict(ADMISSION, commit="a" * 39)))
        self.assertTrue(ins.validate_admission(dict(ADMISSION, outcome="blocking")))

    def test_REGRESSION_the_deadline_is_enforced_with_the_margin_and_never_extended(self):
        deadline = ADMISSION["deadlineEpoch"]
        self.assertEqual(ins.deadline_problems(deadline, deadline - 31 * 60), [])
        self.assertEqual(codes(ins.deadline_problems(deadline, deadline - 30 * 60)), ["preflight_expired"])
        self.assertEqual(codes(ins.deadline_problems(deadline, deadline + 1)), ["preflight_expired"])
        self.assertEqual(codes(ins.deadline_problems(str(deadline), 0)), ["preflight_expired"])

    def test_REGRESSION_admission_refuses_before_opening_anything_when_the_work_directory_is_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "stale"), "w").close()
            state, problems = ins.admit("/nonexistent", ADMISSION, "/nonexistent.zip", "/nonexistent.zip", tmp, 0)
            self.assertEqual((state, codes(problems)), (None, ["workdir_not_empty"]))


class RuntimeFiles(unittest.TestCase):
    def test_CONTROL_the_runtime_files_name_configuration_and_media_and_nothing_secret(self):
        files = ins.runtime_files(profile())
        self.assertEqual(sorted(files), sorted([ins.RUNTIME_MODULE, "customer.wsgi", "admin.wsgi",
                                                "dinify_host_settings_customer.py", "dinify_host_settings_admin.py"]))
        runtime = files[ins.RUNTIME_MODULE].decode()
        self.assertIn('"/etc/dinify/dinify-customer.env"', runtime)
        self.assertIn('MEDIA_ROOT = "/srv/media/"', files["dinify_host_settings_customer.py"].decode())
        self.assertIn("from dinify_backend.settings_admin import *", files["dinify_host_settings_admin.py"].decode())
        self.assertNotIn("SECRET", runtime.replace("secrets", ""))

    def test_REGRESSION_the_release_id_moves_with_the_environment_or_the_launcher_files(self):
        a = ins.runtime_digest(ins.runtime_files(profile()))
        b = ins.runtime_digest(ins.runtime_files(profile(media__root="/srv/other-media")))
        self.assertNotEqual(a, b)
        self.assertNotEqual(ins.release_id("a" * 40, "e" * 64, a), ins.release_id("a" * 40, "f" * 64, a))
        self.assertRegex(ins.release_id("a" * 40, "e" * 64, a), ins.RELEASE_ID)

    def _release(self, tmp, receipt):
        rel = os.path.join(tmp, RID)
        os.makedirs(os.path.join(rel, "wsgi"))
        os.makedirs(os.path.join(rel, "source", "dinify_backend"))
        open(os.path.join(rel, "source", "dinify_backend", "__init__.py"), "w").close()
        with open(os.path.join(rel, "wsgi", ins.RUNTIME_MODULE), "wb") as fh:
            fh.write(ins.runtime_files(profile())[ins.RUNTIME_MODULE])
        if receipt is not None:
            with open(os.path.join(rel, ins.RECEIPT), "w") as fh:
                json.dump(receipt, fh)
        return rel

    def _identity(self, rel):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("dinify_release_runtime_test", os.path.join(rel, "wsgi", ins.RUNTIME_MODULE))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        saved = sys.modules.pop("dinify_backend", None)
        sys.path.insert(0, os.path.join(rel, "source"))
        try:
            return module.identity("customer")
        finally:
            sys.path.remove(os.path.join(rel, "source"))
            sys.modules.pop("dinify_backend", None)
            if saved is not None:
                sys.modules["dinify_backend"] = saved

    def test_REGRESSION_the_launcher_never_relabels_a_process_it_cannot_tie_to_its_own_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._identity(self._release(tmp, None))
            self.assertEqual((doc["state"], doc["reason"], doc["release"]), ("mismatch", "receipt_unreadable", None))
        with tempfile.TemporaryDirectory() as tmp:
            # valid JSON that is not an object is as unreadable as no file at all
            doc = self._identity(self._release(tmp, [RID]))
            self.assertEqual((doc["state"], doc["reason"]), ("mismatch", "receipt_unreadable"))
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._identity(self._release(tmp, {"releaseId": OLD}))
            self.assertEqual((doc["state"], doc["reason"]), ("mismatch", "receipt_names_another_release"))
        with tempfile.TemporaryDirectory() as tmp:
            # the receipt names this directory but a different commit: the id and the commit
            # it was built from must agree before the process is labelled with either
            doc = self._identity(self._release(tmp, {"releaseId": RID, "commit": "b" * 40}))
            self.assertEqual((doc["state"], doc["reason"]), ("mismatch", "receipt_names_another_release"))
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._identity(self._release(tmp, {"releaseId": RID, "commit": RID[:40]}))
            # this test's interpreter is not the release's venv
            self.assertEqual((doc["state"], doc["reason"]), ("mismatch", "interpreter_outside_release"))
            self.assertRegex(doc["process"]["instance"], r"^[0-9a-f]{32}$")


class InstalledReceipt(unittest.TestCase):
    def test_REGRESSION_a_receipt_that_no_longer_derives_its_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ins.runtime_digest(ins.runtime_files(profile()))
            rid = ins.release_id("a" * 40, "e" * 64, runtime)
            rel = os.path.join(tmp, rid)
            os.makedirs(rel)
            receipt = {"schema": ins.RECEIPT_SCHEMA, "releaseId": rid, "commit": "a" * 40, "environmentDigest": "e" * 64,
                       "runtime": {"digest": runtime}}
            for name, commit in (("CONTROL", "a" * 40), ("edited", "f" * 40)):
                with open(os.path.join(rel, ins.RECEIPT), "w") as fh:
                    json.dump(dict(receipt, commit=commit), fh)
                _, problems = ins.verify_installed(profile(), rel, tmp)
                derives = [p for p in problems if p["code"] == "receipt_invalid"]
                self.assertEqual(bool(derives), name == "edited", (name, problems))


class Include(unittest.TestCase):
    def test_CONTROL_each_plane_is_pinned_to_one_release_by_its_real_path(self):
        data = tr.render_include(profile(), "customer", RID, "op-1").decode()
        rel = "/srv/x/releases/" + RID
        self.assertTrue(data.startswith("# dinify-backend-release: %s operation: op-1\n" % RID))
        self.assertIn("python-home=%s/venv" % rel, data)
        self.assertIn("home=%s/source " % rel, data)
        self.assertIn("python-path=%s/source:%s/wsgi" % (rel, rel), data)
        self.assertIn("WSGIScriptAlias /uat %s/wsgi/customer.wsgi process-group=dinify-customer application-group=%%{GLOBAL}" % rel, data)
        self.assertIn("user=www-data group=www-data processes=2 threads=4 display-name=%{GROUP}", data)
        self.assertIn("Alias /uat/media/ /srv/media/", data)
        self.assertNotIn("/srv/media", tr.render_include(profile(), "admin", RID, "op-1").decode())
        self.assertEqual(tr.HEADER.match(data.encode()).group(1).decode(), RID)
        self.assertEqual(tr.include_group(data.encode()), "dinify-customer")

    def test_REGRESSION_a_legacy_include_names_no_release(self):
        self.assertIsNone(tr.HEADER.match(b"WSGIDaemonProcess legacy user=www-data\n"))

    def test_REGRESSION_truncated_process_titles_match_only_when_unambiguous(self):
        # measured: /usr/sbin/apache2 (17 chars) leaves "(wsgi:dinify-cust"
        self.assertTrue(tr.title_matches("(wsgi:dinify-cust", "dinify-customer", ["dinify-admin"]))
        self.assertFalse(tr.title_matches("(wsgi:dinify-admi", "dinify-customer", ["dinify-admin"]))
        self.assertFalse(tr.title_matches("(wsgi:dinify-", "dinify-customer", ["dinify-admin"]))
        self.assertFalse(tr.title_matches("(wsgi:", "dinify-customer", []))
        self.assertTrue(tr.title_matches("(wsgi:dinify-customer)", "dinify-customer", ["dinify-admin"]))


PENDING = {"id": "misc_app.0005_rehearsalnote", "sha256": "9" * 64, "operations": ["CreateModel"], "backwards": False}
DECISION = {"sha256": "9" * 64, "operations": ["CreateModel"], "class": "expand", "reviewedIn": "mugak1/Dinify-Backend#9"}


class Migrations(unittest.TestCase):
    def decide(self, pending=(), applied_unknown=(), decisions=None, conflicts=()):
        return tr.decide_migrations({"pending": list(pending), "appliedUnknown": list(applied_unknown), "conflicts": list(conflicts)},
                                    decisions or {})

    def test_CONTROL_no_pending_migration_is_the_safe_path(self):
        self.assertEqual(self.decide(), ([], []))

    def test_CONTROL_a_reviewed_expand_migration_is_applied(self):
        self.assertEqual(self.decide([PENDING], decisions={PENDING["id"]: DECISION}), ([PENDING["id"]], []))

    def test_REGRESSION_MATRIX_anything_unreviewed_stale_contracting_or_contradicted_stops_the_automated_path(self):
        cases = {
            "migration_unreviewed": ([PENDING], {}),
            "migration_decision_stale": ([PENDING], {PENDING["id"]: dict(DECISION, sha256="8" * 64)}),
            "migration_requires_maintenance": ([PENDING], {PENDING["id"]: dict(DECISION, **{"class": "contract"})}),
            "migration_evidence_contradicts": ([dict(PENDING, operations=["RemoveField"])], {PENDING["id"]: dict(DECISION, operations=["RemoveField"])}),
            "migration_backwards": ([dict(PENDING, backwards=True)], {PENDING["id"]: DECISION}),
        }
        for code, (pending, decisions) in cases.items():
            apply, problems = self.decide(pending, decisions=decisions)
            self.assertEqual((apply, codes(problems)), ([], [code]), code)
        self.assertEqual(codes(self.decide(conflicts=["misc_app: 0005_a, 0005_b"])[1]), ["migration_conflict"])

    def test_REGRESSION_an_older_release_runs_against_a_newer_schema_only_across_reviewed_expand_migrations(self):
        self.assertEqual(codes(self.decide(applied_unknown=["misc_app.0005_rehearsalnote"])[1]), ["schema_incompatible"])
        self.assertEqual(self.decide(applied_unknown=[PENDING["id"]], decisions={PENDING["id"]: DECISION})[1], [])
        self.assertEqual(codes(self.decide(applied_unknown=[PENDING["id"]], decisions={PENDING["id"]: dict(DECISION, **{"class": "data"})})[1]),
                         ["schema_incompatible"])

    def test_CONTROL_the_committed_decisions_are_well_formed_and_name_real_files(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        decisions, problems = tr.load_decisions(root)
        self.assertEqual(problems, [])
        import hashlib
        for mid, d in decisions.items():
            app, name = mid.split(".", 1)
            with open(os.path.join(root, app, "migrations", name + ".py"), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), d["sha256"], mid)

    def test_REGRESSION_identity_support_is_read_from_files_and_a_pre_b3_release_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(codes(tr.identity_support(tmp)), ["identity_unsupported"])
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for rel in ("dinify_backend/release_identity.py", "misc_app/endpoints/release_identity.py", "dinify_backend/urls.py",
                        "platform_admin_app/urls.py"):
                os.makedirs(os.path.dirname(os.path.join(tmp, "source", rel)), exist_ok=True)
                shutil.copy(os.path.join(root, rel), os.path.join(tmp, "source", rel))
            self.assertEqual(tr.identity_support(tmp), [])


class LockAndJournal(unittest.TestCase):
    def test_REGRESSION_a_held_host_lock_times_out_changing_nothing_and_names_its_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "host.lock")
            with tr.HostLock(path, "admin:promote-1"):
                fd = os.open(path, os.O_RDWR)
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # what `flock -n` (Admin's side) sees
                os.close(fd)
                with self.assertRaises(TimeoutError) as caught:
                    with tr.HostLock(path, "backend:op-2", wait=1):
                        pass
                self.assertIn("admin:promote-1", str(caught.exception))
            with tr.HostLock(path, "backend:op-2", wait=1):
                pass

    def test_REGRESSION_an_operation_left_open_blocks_the_next_one_until_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = profile(tmp)
            os.makedirs(doc["stateDir"])
            first = tr.Journal(doc, "op-1")
            self.assertEqual(first.open({}), [])
            first.record("switching", {})
            self.assertEqual(codes(tr.Journal(doc, "op-2").open({})), ["previous_operation_unresolved"])
            first.close()
            self.assertEqual(tr.Journal(doc, "op-2").open({}), [])
            self.assertEqual([e["stage"] for e in first.entries()], ["locked", "switching"])


class Switch(unittest.TestCase):
    """switch()/restore() with Apache and the verifier replaced by doubles: the ORDER of what
    is written, tested and reloaded, and what is reported."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.profile = profile(self.tmp)
        os.makedirs(self.profile["apache"]["includeDir"])
        os.makedirs(self.profile["stateDir"])
        self.old = {p: tr.render_include(self.profile, p, OLD, "op-0") for p in hp.PLANES}
        for p in hp.PLANES:
            with open(tr.include_path(self.profile, p), "wb") as fh:
                fh.write(self.old[p])
        self.calls = []

    def run_switch(self, configtest=0, reloads=(0, 0), verify=([], [])):
        reloads, verify = list(reloads), list(verify)

        def apache(profile_, action):
            self.calls.append(action)
            status = configtest if action == "configtest" else reloads.pop(0)
            return {"status": status, "stdout": "", "stderr": "Syntax error" if status else ""}

        def verify_serving(profile_, expect, budget, groups=None):
            self.calls.append(("verify", tuple(sorted(expect.items()))))
            problems = verify.pop(0)
            return {"serving": expect}, problems

        journal = tr.Journal(self.profile, "op-1")
        journal.open({})
        current = {p: tr.read_include(self.profile, p) for p in hp.PLANES}
        new = {p: tr.render_include(self.profile, p, RID, "op-1") for p in hp.PLANES}
        with mock.patch.object(tr, "apache", apache), mock.patch.object(tr, "verify_serving", verify_serving):
            outcome = tr.switch(self.profile, "op-1", journal, current, new, {p: RID for p in hp.PLANES})
        return outcome, journal

    def served(self):
        return {p: tr.read_include(self.profile, p)[1] for p in hp.PLANES}

    def test_CONTROL_a_verified_switch_leaves_both_planes_on_the_release_and_closes_the_operation(self):
        outcome, journal = self.run_switch()
        self.assertEqual(outcome["stage"], "verified")
        self.assertEqual(self.served(), {p: RID for p in hp.PLANES})
        self.assertEqual(self.calls[:2], ["configtest", "graceful"])
        self.assertFalse(os.path.exists(journal.active))
        with open(os.path.join(journal.dir, "op-1", "previous-customer.conf"), "rb") as fh:
            self.assertEqual(fh.read(), self.old["customer"])

    def test_REGRESSION_a_rejected_configuration_is_put_back_and_never_reloaded(self):
        outcome, _ = self.run_switch(configtest=1)
        self.assertEqual((outcome["stage"], codes(outcome["problems"]), outcome["restored"]), ("verification-failed", ["configtest_failed"], True))
        self.assertEqual(self.served(), {p: OLD for p in hp.PLANES})
        self.assertNotIn("graceful", self.calls)

    def test_REGRESSION_a_switch_that_does_not_verify_is_restored_and_still_reported_failed(self):
        failure = [{"code": "health_failed", "detail": "admin health answered 400"}]
        outcome, journal = self.run_switch(verify=(failure, []))
        self.assertEqual(outcome["stage"], "restored")
        self.assertEqual(codes(outcome["problems"]), ["health_failed"])
        self.assertEqual(self.served(), {p: OLD for p in hp.PLANES})
        self.assertEqual([c for c in self.calls if c != "configtest"],
                         ["graceful", ("verify", tuple(sorted({p: RID for p in hp.PLANES}.items()))), "graceful",
                          ("verify", tuple(sorted({p: OLD for p in hp.PLANES}.items())))])
        self.assertEqual([e["stage"] for e in journal.entries()], ["locked", "switching", "switched", "verification-failed", "restored"])

    def test_REGRESSION_a_restoration_that_does_not_verify_is_reported_as_that_and_the_operation_stays_open(self):
        failure = [{"code": "identity_mismatch", "detail": "x"}]
        outcome, journal = self.run_switch(verify=(failure, failure))
        self.assertEqual(outcome["stage"], "restoration-failed")
        self.assertIn("restoration_failed", codes(outcome["problems"]))
        self.assertTrue(os.path.exists(journal.active))

    def test_REGRESSION_a_restoration_whose_reload_fails_is_reported_as_that(self):
        outcome, journal = self.run_switch(reloads=(0, 1), verify=([{"code": "health_failed", "detail": "x"}],))
        self.assertEqual(outcome["stage"], "restoration-failed")
        self.assertTrue(os.path.exists(journal.active))


if __name__ == "__main__":
    unittest.main()


class TrustedTraversal(unittest.TestCase):
    """Codex P1 on #345: the unprivileged identities import the trusted verifier and run from
    inside it, so a 0700 ancestor fails every step with EACCES before anything is decided."""

    def test_REGRESSION_a_private_ancestor_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o711)
            os.makedirs(os.path.join(tmp, "base", "trusted", "op", "release"))
            os.chmod(os.path.join(tmp, "base"), 0o700)
            self.assertEqual(hp.untraversable(os.path.join(tmp, "base", "trusted", "op", "release")), [os.path.join(os.path.realpath(tmp), "base")])

    def test_CONTROL_0711_is_passable_and_needs_no_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o711)
            target = os.path.join(tmp, "base", "trusted", "op", "release")
            os.makedirs(target)
            for d in ("base", os.path.join("base", "trusted")):
                os.chmod(os.path.join(tmp, d), 0o711)
            self.assertEqual(hp.untraversable(target), [])

    def test_REGRESSION_the_deploy_refuses_it_by_name(self):
        import contextlib
        import io
        from release.__main__ import main as cli
        with tempfile.TemporaryDirectory() as tmp:   # mkdtemp is 0700: exactly the defect
            os.makedirs(os.path.join(tmp, "release"))
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                    mock.patch("release.__main__._profile", return_value=(profile(tmp), [])):
                code = cli(["host", "deploy", "--profile", "p.json",
                            "--operation", "op-trav-1", "--admission", os.path.join(tmp, "a"), "--candidate-zip", os.path.join(tmp, "c"),
                            "--preflight-zip", os.path.join(tmp, "p"), "--trusted", tmp])
        self.assertEqual(code, 1)
        self.assertIn("trusted_not_traversable", err.getvalue())


class ResumeSchema(unittest.TestCase):
    """Codex P1 on #345: an operation killed during, or failing part-way through, the migrations
    must not be closed by observing that the old release still serves."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.profile = profile(self.tmp)
        os.makedirs(self.profile["apache"]["includeDir"])
        os.makedirs(self.profile["stateDir"])
        os.makedirs(os.path.dirname(self.profile["lockPath"]))
        for p in hp.PLANES:
            with open(tr.include_path(self.profile, p), "wb") as fh:
                fh.write(tr.render_include(self.profile, p, OLD, "op-0"))
        self.verified = []

    def left_open(self, pending, migrated=False, failed=False):
        journal = tr.Journal(self.profile, "op-1")
        journal.open({})
        journal.record("gated", {"config": {}, "plan": {"pending": pending, "appliedUnknown": []}})
        if migrated:
            journal.record("migrated", {"applied": pending})
        if failed:
            journal.record("refused", {"problems": [{"code": "migrate_failed", "detail": "x"}]})
        return journal

    def resume(self, **kw):
        def verify_serving(profile_, expect, budget, groups=None):
            self.verified.append(expect)
            return {"serving": expect}, []
        with mock.patch.object(tr, "verify_serving", verify_serving):
            return tr.resume(self.profile, "op-1", **kw)

    def test_REGRESSION_an_unresolved_migration_stays_open_even_though_the_old_release_verifies(self):
        for failed in (False, True):   # killed during the migrate step / reported failure part-way
            with self.subTest(failed=failed):
                journal = self.left_open(["misc_app.0005_x"], failed=failed)
                outcome = self.resume()
                self.assertEqual((outcome["stage"], codes(outcome["problems"])), ("refused", ["schema_state_unknown"]))
                self.assertTrue(os.path.exists(journal.active))
                self.assertEqual(codes(tr.Journal(self.profile, "op-2").open({})), ["previous_operation_unresolved"])
                self.assertEqual(self.verified[-1], {p: OLD for p in hp.PLANES})
                journal.close()
                os.remove(journal.path)

    def test_REGRESSION_only_a_stated_finding_closes_it_and_it_is_recorded_verbatim(self):
        journal = self.left_open(["misc_app.0005_x"])
        self.assertEqual(codes(self.resume(schema_established="looked fine")["problems"]), ["schema_statement_invalid"])
        self.assertTrue(os.path.exists(journal.active))
        statement = "0005 absent from django_migrations; column not present; nothing applied"
        outcome = self.resume(schema_established=statement)
        self.assertEqual(outcome["stage"], "resumed")
        self.assertFalse(os.path.exists(journal.active))
        self.assertEqual(journal.entries()[-1]["detail"]["schemaEstablished"], statement)

    def test_CONTROL_no_pending_migration_or_a_completed_one_closes_on_observation(self):
        for pending, migrated in (([], False), (["misc_app.0005_x"], True)):
            with self.subTest(pending=pending, migrated=migrated):
                journal = self.left_open(pending, migrated=migrated)
                self.assertEqual(self.resume()["stage"], "resumed")
                self.assertFalse(os.path.exists(journal.active))
                os.remove(journal.path)

    def test_REGRESSION_a_statement_where_none_is_needed_is_refused_and_changes_nothing(self):
        journal = self.left_open([])
        outcome = self.resume(schema_established="0005 absent from django_migrations; nothing applied")
        self.assertEqual(codes(outcome["problems"]), ["schema_statement_unexpected"])
        self.assertTrue(os.path.exists(journal.active))

    def test_CONTRACT_the_cli_passes_the_statement_through(self):
        from release.__main__ import main as cli
        with mock.patch.object(tr, "resume", return_value={"stage": "resumed", "problems": [], "release": {p: OLD for p in hp.PLANES}}) as called, \
                mock.patch.object(hp, "load", return_value=(self.profile, None, [])), mock.patch.object(hp, "validate", return_value=[]):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                cli(["host", "resume", "--profile", "p.json", "--operation", "op-1", "--schema-established", "what was found in the db"])
        self.assertEqual(called.call_args.kwargs["schema_established"], "what was found in the db")
