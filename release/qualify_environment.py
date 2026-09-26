"""THE ENVIRONMENT: a real, fresh virtual environment built offline from nothing but the
admitted files, and every way it could differ from the lock refused by name.

These build REAL environments (``venv --without-pip``, the bundled pip wheel installing
itself, then ``--isolated --no-index --no-cache-dir --require-hashes --no-deps``) from
synthetic wheels. No network is used or reachable.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error

from release import environment as ev
from release import lockfile as lf
from release import testing as tt


def codes(problems):
    return sorted({p["code"] for p in problems})


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.wheelhouse = os.path.join(cls._tmp.name, "wheelhouse")
        os.makedirs(cls.wheelhouse)
        cls.app = tt.make_wheel(cls.wheelhouse, tt.APP, "1.0", requires=["demo-lib>=1.0", 'demo-win==1.0; sys_platform == "win32"'])
        cls.lib = tt.make_wheel(cls.wheelhouse, tt.LIB, "1.0")
        cls.boot = shutil.copy(tt.bundled_pip(), cls.wheelhouse)
        cls.extras = os.path.join(cls._tmp.name, "extras")
        os.makedirs(cls.extras)
        cls.win = tt.make_wheel(cls.extras, tt.WIN, "1.0")
        cls.requirements = b"demo-app==1.0\n"
        cls.lock = tt.make_lock(cls.requirements, cls.boot, [(cls.app, True, []), (cls.lib, False, [tt.APP])])
        cls.direct = {tt.APP: "1.0"}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def scratch(self):
        d = tempfile.mkdtemp(dir=self._tmp.name)
        return d

    def wheelhouse_copy(self, drop=(), add=()):
        d = os.path.join(self.scratch(), "wh")
        shutil.copytree(self.wheelhouse, d)
        for path in drop:
            os.remove(os.path.join(d, os.path.basename(path)))
        for path in add:
            shutil.copy(path, d)
        return d

    def build(self, lock=None, wheelhouse=None):
        lock, wheelhouse = lock or self.lock, wheelhouse or self.wheelhouse
        d = self.scratch()
        venv = os.path.join(d, "venv")
        report, problems = ev.create_environment(lock, wheelhouse, venv, os.path.join(d, "work"))
        return venv, report, problems

    def reconcile(self, venv, lock=None, wheelhouse=None):
        return ev.reconcile(lock or self.lock, wheelhouse or self.wheelhouse, venv, self.direct)


class TheCertifiedEnvironment(Fixture):
    def test_CONTROL_an_offline_install_reconciles_and_its_inventory_is_portable(self):
        first, report, problems = self.build()
        self.assertEqual(problems, [])
        self.assertEqual([s["name"] for s in report["steps"]], ["venv --without-pip", "bootstrap the pinned installer from its wheel",
                                                                "install the locked application closure"])
        inv1, p1 = self.reconcile(first)
        second, _, _ = self.build()
        inv2, p2 = self.reconcile(second)
        self.assertEqual((p1, p2), ([], []))
        self.assertNotEqual(first, second)
        self.assertEqual(inv1["digest"], inv2["digest"], "the inventory does not depend on where the environment lives")
        self.assertEqual(sorted((p["name"], p["role"]) for p in inv1["packages"]),
                         [("demo-app", "application"), ("demo-lib", "application"), ("pip", "bootstrap")])
        self.assertTrue(inv1["pipCheck"])

    def test_REGRESSION_an_unexpected_package_installed_afterwards_is_refused(self):
        venv, _, problems = self.build()
        self.assertEqual(problems, [])
        proc = subprocess.run([ev.venv_python(venv), "-I", "-m", "pip", "install", "--no-index", "--find-links", self.extras,
                               "--no-deps", "demo-win"], capture_output=True, text=True, env=ev.scrubbed_env(ev.OFFLINE), check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(codes(self.reconcile(venv)[1]), ["unexpected_package"])

    def test_REGRESSION_an_installed_file_that_is_not_the_wheels_is_refused(self):
        venv, _, _ = self.build()
        inv, _ = self.reconcile(venv)
        site = inv["facts"]["prefix"] + "/lib/python%s/site-packages" % inv["markers"]["python_version"]
        with open(os.path.join(site, "demo_lib", "__init__.py"), "a", encoding="utf-8") as fh:
            fh.write("PATCHED = True\n")
        self.assertEqual(codes(self.reconcile(venv)[1]), ["installed_file_mismatch"])

    def test_REGRESSION_an_unowned_file_in_site_packages_is_refused(self):
        venv, _, _ = self.build()
        inv, _ = self.reconcile(venv)
        site = inv["facts"]["prefix"] + "/lib/python%s/site-packages" % inv["markers"]["python_version"]
        with open(os.path.join(site, "zz-inject.pth"), "w", encoding="utf-8") as fh:
            fh.write("import os\n")
        self.assertEqual(codes(self.reconcile(venv)[1]), ["unowned_file"])


class TheLockDecidesWhatIsInstalled(Fixture):
    def test_REGRESSION_a_missing_transitive_is_refused_not_resolved(self):
        lock = tt.make_lock(self.requirements, self.boot, [(self.app, True, [])])
        venv, _, problems = self.build(lock, self.wheelhouse_copy(drop=[self.lib]))
        self.assertEqual(problems, [], "--no-deps installs exactly what is locked; nothing fetches the missing package")
        _, problems = self.reconcile(venv, lock)
        self.assertIn("missing_dependency", codes(problems))
        self.assertIn("pip_check_failed", codes(problems))

    def test_REGRESSION_a_package_only_another_platform_needs_is_refused(self):
        lock = tt.make_lock(self.requirements, self.boot, [(self.app, True, []), (self.lib, False, [tt.APP]), (self.win, False, [tt.APP])])
        wheelhouse = self.wheelhouse_copy(add=[self.win])
        venv, _, problems = self.build(lock, wheelhouse)
        self.assertEqual(problems, [])
        _, problems = self.reconcile(venv, lock, wheelhouse)
        self.assertEqual(codes(problems), ["unexpected_package"])
        self.assertIn("demo-win", problems[0]["detail"])

    def test_REGRESSION_an_incompatible_wheel_is_refused_by_the_target_interpreter(self):
        d = self.scratch()
        alien = tt.make_wheel(d, tt.LIB, "1.0", tag="py3-none-win_amd64")
        lock = tt.make_lock(self.requirements, self.boot, [(self.app, True, []), (alien, False, [tt.APP])])
        wheelhouse = self.wheelhouse_copy(drop=[self.lib], add=[alien])
        _, _, problems = self.build(lock, wheelhouse)
        self.assertEqual(codes(problems), ["install_failed"])
        self.assertFalse(lf.wheel_supported(os.path.basename(alien), lock["target"]), "and the static gate refuses it first")

    def test_REGRESSION_a_different_target_is_refused(self):
        lock = tt.make_lock(self.requirements, self.boot, [(self.app, True, []), (self.lib, False, [tt.APP])])
        venv, _, _ = self.build(lock)
        lock["target"]["markers"]["python_full_version"] = "3.12.99"
        self.assertIn("target_mismatch", codes(self.reconcile(venv, lock)[1]))
        facts = self.reconcile(venv)[0]["facts"]
        self.assertEqual(codes(ev.target_problems(dict(lock, target=dict(lock["target"], glibc="99.0")), facts)), ["target_mismatch"])


class OnlyTheAdmittedFilesAreEverUsed(Fixture):
    def test_REGRESSION_changed_wheel_bytes_are_refused_before_anything_runs(self):
        wheelhouse = self.wheelhouse_copy()
        with open(os.path.join(wheelhouse, os.path.basename(self.lib)), "ab") as fh:
            fh.write(b"\0")
        venv, report, problems = self.build(wheelhouse=wheelhouse)
        self.assertEqual(codes(problems), ["artifact_mismatch"])
        self.assertFalse(os.path.exists(venv), "nothing was created")

    def test_REGRESSION_a_changed_installer_is_refused(self):
        wheelhouse = self.wheelhouse_copy()
        shutil.copy(self.lib, os.path.join(wheelhouse, os.path.basename(self.boot)))
        self.assertEqual(codes(self.build(wheelhouse=wheelhouse)[2]), ["artifact_mismatch"])

    def test_REGRESSION_an_absent_or_extra_file_is_refused(self):
        self.assertEqual(codes(self.build(wheelhouse=self.wheelhouse_copy(drop=[self.lib]))[2]), ["missing_artifact"])
        self.assertEqual(codes(self.build(wheelhouse=self.wheelhouse_copy(add=[self.win]))[2]), ["unexpected_artifact"])

    def test_REGRESSION_a_wheel_contradicting_its_own_RECORD_is_refused_even_when_the_lock_names_its_hash(self):
        d = self.scratch()

        def tamper(files):
            files["demo_lib/__init__.py"] = "VERSION = 'tampered'\n"
            return files
        bad = tt.make_wheel(d, tt.LIB, "1.0", after_record=tamper)
        lock = tt.make_lock(self.requirements, self.boot, [(self.app, True, []), (bad, False, [tt.APP])])
        wheelhouse = self.wheelhouse_copy(drop=[self.lib], add=[bad])
        venv, _, problems = self.build(lock, wheelhouse)
        self.assertEqual(problems, [])
        self.assertIn("wheel_malformed", codes(self.reconcile(venv, lock, wheelhouse)[1]))

    def test_REGRESSION_an_absent_file_stays_absent_whatever_the_configuration_environment_or_cache_offer(self):
        """Our pre-check refuses an absent file before pip runs; this proves the install
        step ITSELF cannot be satisfied from elsewhere. The missing package is offered
        through PIP_FIND_LINKS, PIP_CONFIG_FILE, a pip.conf inside the environment and a
        cache directory. The production invocation (our flags, our scrubbed environment)
        still fails. Two NEGATIVE CONTROLS show each layer is load-bearing: pip's own
        configuration file is honoured even under --isolated unless PIP_CONFIG_FILE is
        /dev/null, and without the isolation flags the offered file is simply taken."""
        from unittest import mock
        d = self.scratch()
        venv = os.path.join(d, "venv")
        empty = os.path.join(d, "wh")
        os.makedirs(empty)
        shutil.copy(self.boot, empty)
        shutil.copy(self.app, empty)
        offer = os.path.join(d, "offer")
        os.makedirs(offer)
        shutil.copy(self.lib, offer)
        conf = os.path.join(d, "pip.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("[global]\nfind-links = %s\nno-index = false\n" % offer)
        tempting = {"PIP_FIND_LINKS": offer, "PIP_CONFIG_FILE": conf, "XDG_CACHE_HOME": os.path.join(d, "cache")}
        subprocess.run([tt.base_python(), "-m", "venv", "--without-pip", venv], check=True)
        shutil.copy(conf, os.path.join(venv, "pip.conf"))
        req = os.path.join(d, "boot.txt")
        with open(req, "w", encoding="utf-8") as fh:
            fh.write(lf.pip_requirements([tt.entry(self.boot)]))
        boot = ev.run([ev.venv_python(venv), "-I", os.path.join(empty, os.path.basename(self.boot), "pip")] + ev.pip_install_args(empty, req),
                      env=ev.scrubbed_env(ev.OFFLINE))
        self.assertEqual(boot["status"], 0, boot["stderr"])
        app_req = os.path.join(d, "app.txt")
        with open(app_req, "w", encoding="utf-8") as fh:
            fh.write(lf.pip_requirements([e for e in lf.entries(self.lock) if e["role"] == "application"]))
        strict = [ev.venv_python(venv), "-I", "-m", "pip"] + ev.pip_install_args(empty, app_req)
        with mock.patch.dict(os.environ, tempting):
            refused = ev.run(strict, env=ev.scrubbed_env(ev.OFFLINE))
        self.assertNotEqual(refused["status"], 0)
        self.assertIn("demo-lib", refused["stderr"].lower())
        # NEGATIVE CONTROL 1: the same strict flags, but pip's configuration file not disabled.
        leaky = subprocess.run(strict + ["--dry-run"], capture_output=True, text=True, check=False,
                               env=dict(ev.scrubbed_env(ev.OFFLINE), PIP_CONFIG_FILE=conf))
        self.assertEqual(leaky.returncode, 0, "the configuration file IS honoured under --isolated: %s" % leaky.stderr[-300:])
        # NEGATIVE CONTROL 2: without the isolation flags, the offered file is used.
        loose = [a for a in strict if a not in ("--isolated", "--no-cache-dir", "--no-index")]
        taken = subprocess.run(loose + ["--dry-run"], capture_output=True, text=True, check=False, env=dict(os.environ, **tempting))
        self.assertEqual(taken.returncode, 0, taken.stderr[-300:])


class Acquisition(Fixture):
    def fetcher(self, source, fail_first=0, corrupt=()):
        state = {"calls": 0}

        def fetch(url, destination, timeout):
            state["calls"] += 1
            if state["calls"] <= fail_first:
                raise urllib.error.URLError("simulated outage")
            name = url.rsplit("/", 1)[-1]
            shutil.copy(os.path.join(source, name), destination)
            if name in corrupt:
                with open(destination, "ab") as fh:
                    fh.write(b"!")
        return fetch, state

    def test_CONTROL_every_locked_file_is_fetched_and_admitted(self):
        fetch, state = self.fetcher(self.wheelhouse)
        dest = os.path.join(self.scratch(), "wh")
        listing, problems = ev.acquire(self.lock, dest, fetch=fetch)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(e["filename"] for e in listing), sorted(os.listdir(self.wheelhouse)))
        self.assertEqual(state["calls"], 3)

    def test_REGRESSION_a_substituted_file_is_refused_and_not_kept(self):
        fetch, _ = self.fetcher(self.wheelhouse, corrupt=(os.path.basename(self.lib),))
        dest = os.path.join(self.scratch(), "wh")
        _, problems = ev.acquire(self.lock, dest, fetch=fetch)
        self.assertEqual(codes(problems), ["artifact_mismatch", "missing_artifact"])
        self.assertEqual(sorted(os.listdir(dest)), sorted(n for n in os.listdir(self.wheelhouse) if n != os.path.basename(self.lib)))

    def test_REGRESSION_a_network_failure_fails_and_one_transient_failure_is_retried(self):
        fetch, _ = self.fetcher(self.wheelhouse, fail_first=99)
        _, problems = ev.acquire(self.lock, os.path.join(self.scratch(), "wh"), fetch=fetch)
        self.assertIn("acquisition_failed", codes(problems))
        fetch, _ = self.fetcher(self.wheelhouse, fail_first=1)
        self.assertEqual(ev.acquire(self.lock, os.path.join(self.scratch(), "wh"), fetch=fetch)[1], [])

    def test_REGRESSION_stale_output_is_never_extended(self):
        dest = self.wheelhouse_copy()
        fetch, state = self.fetcher(self.wheelhouse)
        self.assertEqual(codes(ev.acquire(self.lock, dest, fetch=fetch)[1]), ["wheelhouse_not_empty"])
        self.assertEqual(state["calls"], 0)


if __name__ == "__main__":
    unittest.main()
