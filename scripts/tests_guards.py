"""D08 B2.3 — the three standing source guards prove they can find what they claim to
find, an incomplete scan never reads as clean, and their real consumers cannot turn a
detector failure green.

Guards: the money-field guard, the ambient-authority gate (TENANT-AUTH-00) and the
tenant-relation ratchet (TENANT-STRUCT-00). Everything below drives the REAL CLIs as
subprocesses of this interpreter against disposable copies — a temporary tree for the
two source scanners, and real git repositories with a LOCAL bare ``origin`` for the
ratchet (no network, no GitHub). The consumer tests execute the committed ``ci.yml``
step text under the runner's own shell and ``scripts/verify.sh`` itself.

Offline and database-free: ``python -m unittest discover -t . -s scripts -p "tests_*.py"``.

Labels: CONTRACT (a committed file says what it must), CONTROL (the ordinary case
passes), REGRESSION (a reproduced false-clean now fails), NEGATIVE CONTROL (the
mechanism a CONTRACT forbids really would hide a failure).

What these guards are NOT: a proof of tenant isolation, of authorization correctness or
of financial correctness. They pin three specific, named properties of the source tree
and complement the application suites; they replace none of them.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from dependency_audit.workflow_harness import (command_of, load_workflow, run_aggregator, simulate_job,
                                               status_swallowers)
from dinify_backend.tenancy.ambient_authority import scan as ambient_scan
from scripts.check_money_fields import scan as money_scan

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
CI = load_workflow(str(ROOT / ".github" / "workflows" / "ci.yml"))
SUITE = CI["jobs"]["suite"]["steps"]
GUARDS = {
    "Money-field guard": "python scripts/check_money_fields.py",
    "Ambient-authority gate": "python scripts/check_ambient_authority.py",
    "Tenant-relation ratchet": "python scripts/check_tenant_relation_ratchet.py",
}
QUALIFICATION = 'python -m unittest discover -t . -s scripts -p "tests_*.py"'
CLEAN, VIOLATION, INCOMPLETE, SELF_TEST = 0, 1, 2, 3

AMBIENT_FILES = ("scripts/check_ambient_authority.py", "dinify_backend/__init__.py",
                 "dinify_backend/tenancy/__init__.py", "dinify_backend/tenancy/ambient_authority.py")
RATCHET_FILES = ("scripts/check_tenant_relation_ratchet.py", "dinify_backend/__init__.py",
                 "dinify_backend/tenancy/__init__.py", "dinify_backend/tenancy/ratchet.py",
                 "dinify_backend/tenancy/git_ratchet.py")
BASELINE = "dinify_backend/tenancy/baseline.txt"
LEGACY = "dinify" + "_admin"  # assembled so this module's source names no platform role
# Deterministic git: no system or user configuration, no prompt, no network remote.
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}


def copy_into(tmp, files):
    for rel in files:
        target = Path(tmp) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)


def write(tmp, rel, text):
    path = Path(tmp) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def mutate(path, find, replace):
    """Replace exactly one occurrence — a mutation that matched nothing is a vacuous test."""
    text = Path(path).read_text(encoding="utf-8")
    count = text.count(find)
    if count != 1:
        raise AssertionError(f"mutation target occurs {count} time(s) in {path}: {find!r}")
    Path(path).write_text(text.replace(find, replace), encoding="utf-8")


def run_cli(cwd, script, *args, env=None):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run([PY, script, *args], cwd=str(cwd), env={**base, **(env or {})},
                       capture_output=True, text=True, timeout=120, check=False)
    return r.returncode, r.stdout + r.stderr


def run_step_for_real(script, cwd):
    """Run a ``run:`` text the way a runner does with no ``shell:`` (``bash -e {0}``),
    with ``python`` resolving to THIS interpreter — so the status the step sees is the
    real guard's, not a number a test chose."""
    with tempfile.TemporaryDirectory(prefix="step-") as d:
        shim = Path(d) / "python"
        shim.write_text(f'#!/bin/sh\nexec "{PY}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        step = Path(d) / "step.sh"
        step.write_text(script, encoding="utf-8")
        r = subprocess.run(["bash", "-e", str(step)], cwd=str(cwd), capture_output=True, text=True,
                           env={"PATH": f"{d}:/usr/bin:/bin", "HOME": d, "PYTHONDONTWRITEBYTECODE": "1"},
                           timeout=120, check=False)
        return r.returncode, r.stdout + r.stderr


CLEAN_MODELS = """
from django.db import models

class Order(models.Model):
    total_price = models.DecimalField(max_digits=12, decimal_places=2)
    rating = models.FloatField(default=0)        # not money
    coffee_strength = models.FloatField()        # 'coffee' is not the token 'fee'
    # price = models.FloatField()  <- prose, never a declaration
"""


# =====================================================================================
class MoneyFieldGuard(unittest.TestCase):
    script = "scripts/check_money_fields.py"

    def tree(self, tmp, models=None):
        copy_into(tmp, (self.script,))
        write(tmp, "orders_app/migrations/0001_initial.py", "price = models.FloatField()\n")
        for rel, text in (models or {"orders_app/models.py": CLEAN_MODELS}).items():
            write(tmp, rel, text)

    def test_CONTROL_the_real_tree_is_analysed_completely_and_clean(self):
        code, out = run_cli(ROOT, self.script)
        self.assertEqual(code, CLEAN, out)
        self.assertIn("self-test: OK", out)
        self.assertRegex(out, r"analysed \d+ models\.py file\(s\) completely")

    def test_CONTROL_a_clean_fixture_passes_and_immutable_migrations_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp)
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, CLEAN, out)
            self.assertIn("analysed 1 models.py file(s) completely", out)

    def test_REGRESSION_every_supported_declaration_spelling_is_flagged(self):
        # The line matcher found 1 of the first 4 of these on d4aacbd.
        spellings = {
            "parenthesised": "class A(models.Model):\n    total_price = (\n        models.FloatField(default=0)\n    )\n",
            "annotated": "class A(models.Model):\n    service_fee: float = models.FloatField(default=0)\n",
            "import alias": "from django.db.models import FloatField as Number\nclass A:\n    tip_amount = Number()\n",
            "multiline constructor": "class A(models.Model):\n    unit_cost = models.FloatField(\n        default=0,\n    )\n",
            "fully dotted": "import django.db.models\nclass A:\n    refund = django.db.models.FloatField()\n",
            "module alias": "from django.db import models as dj\nclass A:\n    balance = dj.FloatField()\n",
        }
        for label, source in spellings.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                self.tree(tmp, {"app/models.py": source})
                code, out = run_cli(tmp, self.script)
                self.assertEqual(code, VIOLATION, out)
                self.assertRegex(out, r"app/models\.py:\d+: \w+ = FloatField \(monetary token")

    def test_REGRESSION_an_empty_scope_is_incomplete_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy_into(tmp, (self.script,))
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, INCOMPLETE, out)
            self.assertIn("an empty scope is not a clean one", out)
            self.assertNotIn("Money-field guard: OK", out)

    def test_REGRESSION_an_unparseable_models_py_is_incomplete_even_beside_clean_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {"orders_app/models.py": CLEAN_MODELS,
                            "menu_app/models.py": "class A(models.Model):\n    price = (models.FloatField(\n"})
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, INCOMPLETE, out)
            self.assertIn("menu_app/models.py could not be analysed", out)

    def test_REGRESSION_an_undecodable_models_py_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp)
            (Path(tmp) / "orders_app" / "models.py").write_bytes(b"price = models.FloatField()\n\xff\xfe\n")
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, INCOMPLETE, out)

    def test_REGRESSION_a_directory_that_cannot_be_listed_is_incomplete(self):
        # Simulated: permission bits mean nothing to a root process, which CI is not
        # guaranteed to avoid. The real scan() is driven with a walk that reports an error.
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp)

            def walk(top, onerror=None):
                onerror(PermissionError(13, "Permission denied", str(Path(top) / "locked_app")))
                yield from os.walk(top)
            result = money_scan(Path(tmp), walk=walk)
            self.assertTrue(any("locked_app" in line for line in result.incomplete), result)

    def test_REGRESSION_a_broken_detector_fails_the_default_invocation_on_a_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp)
            mutate(Path(tmp) / self.script, "        return func.attr == FIELD_CLASS\n", "        return False\n")
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, SELF_TEST, out)
            self.assertIn("self-test FAIL", out)
            self.assertNotIn("Money-field guard: OK", out)

    def test_NEGATIVE_CONTROL_the_same_broken_detector_without_the_self_test_misses_a_real_offender(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {"app/models.py": "class A(models.Model):\n    price = models.FloatField()\n"})
            mutate(Path(tmp) / self.script, "        return func.attr == FIELD_CLASS\n", "        return False\n")
            probe = ("import sys; sys.path.insert(0, 'scripts'); from pathlib import Path; "
                     "import check_money_fields as g; print(g.scan(Path('.')).offenders)")
            r = subprocess.run([PY, "-c", probe], cwd=tmp, capture_output=True, text=True, check=True)
            self.assertEqual(r.stdout.strip(), "[]", "this is the harm the unavoidable self-test prevents")


# =====================================================================================
class AmbientAuthorityGate(unittest.TestCase):
    script = "scripts/check_ambient_authority.py"

    def tree(self, tmp, modules):
        copy_into(tmp, AMBIENT_FILES)
        for rel, text in modules.items():
            write(tmp, rel, text)

    def test_CONTROL_the_real_customer_plane_is_analysed_completely_and_clean(self):
        code, out = run_cli(ROOT, self.script)
        self.assertEqual(code, CLEAN, out)
        self.assertRegex(out, r"analysed \d+ customer-plane module\(s\) completely")

    def test_REGRESSION_a_production_entry_point_that_reintroduces_the_predicate_and_fails_to_parse_is_not_clean(self):
        # Reproduced on d4aacbd with the REAL wsgi_admin.py: this gate said OK and so did
        # `django check`, which never imports wsgi_admin.py.
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {
                "dinify_backend/wsgi_admin.py": f"def is_dinify_admin(user):\n    return {LEGACY!r} in user.roles\nbroken = (\n",
                "orders_app/views.py": "def ok():\n    return 1\n",
            })
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, INCOMPLETE, out)
            self.assertIn("dinify_backend/wsgi_admin.py could not be analysed", out)
            self.assertNotIn("Ambient-authority gate: OK", out)

    def test_REGRESSION_a_reintroduced_predicate_fails_and_the_exclusions_still_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {
                "orders_app/views.py": "from users_app.controllers.permissions_check import is_dinify_admin as ok\n",
                "platform_admin_app/services.py": f"PLATFORM_ONLY_ROLES = [{LEGACY!r}]\n",
                "users_app/migrations/0011_flip.py": f"ROLE = {LEGACY!r}\n",
                "users_app/tests_roles.py": f"ROLE = {LEGACY!r}\n",
            })
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, VIOLATION, out)
            self.assertIn("orders_app/views.py:1: is_dinify_admin", out)
            for excluded in ("platform_admin_app", "migrations", "tests_roles"):
                self.assertNotIn(excluded, out)

    def test_REGRESSION_an_empty_scope_is_incomplete_not_clean(self):
        # Only the CLI and the detector, both self-exempt (namespace packages, so no
        # __init__ is needed to import): nothing in scope remains to analyse.
        with tempfile.TemporaryDirectory() as tmp:
            copy_into(tmp, (self.script, "dinify_backend/tenancy/ambient_authority.py"))
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, INCOMPLETE, out)
            self.assertIn("an empty scope is not a clean one", out)
            self.assertNotIn("Ambient-authority gate: OK", out)

    def test_REGRESSION_an_undecodable_module_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {"orders_app/views.py": "x = 1\n"})
            (Path(tmp) / "orders_app" / "views.py").write_bytes(b"x = '\xff'\n")
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, INCOMPLETE, out)

    def test_REGRESSION_a_directory_that_cannot_be_listed_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {"orders_app/views.py": "x = 1\n"})

            def walk(top, onerror=None):
                onerror(PermissionError(13, "Permission denied", str(Path(top) / "locked_app")))
                yield from os.walk(top)
            self.assertTrue(any("locked_app" in line for line in ambient_scan(Path(tmp), walk=walk).incomplete))

    def test_REGRESSION_a_broken_detector_fails_the_default_invocation_on_a_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(tmp, {"orders_app/views.py": "x = 1\n"})
            mutate(Path(tmp) / "dinify_backend/tenancy/ambient_authority.py",
                   "                if alias.name in RETIRED_NAMES:\n", "                if False:\n")
            code, out = run_cli(tmp, self.script)
            self.assertEqual(code, SELF_TEST, out)
            self.assertNotIn("Ambient-authority gate: OK", out)


# =====================================================================================
class TenantRelationRatchet(unittest.TestCase):
    """Real git, a LOCAL bare origin, the real CLI. CI is simulated by GITHUB_ACTIONS."""

    script = "scripts/check_tenant_relation_ratchet.py"

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                              env={**GIT_ENV, "HOME": str(cwd)}).stdout.strip()

    def repo(self, tmp, entries=("A::x", "A::y"), with_ratchet=True):
        origin, work = Path(tmp) / "origin.git", Path(tmp) / "work"
        self.git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(tmp, "init", "-q", "-b", "main", str(work))
        if with_ratchet:
            copy_into(work, RATCHET_FILES)
            self.write_baseline(work, entries)
        else:
            write(work, "README", "before the ratchet\n")
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "base")
        self.git(work, "remote", "add", "origin", str(origin))
        self.git(work, "push", "-q", "origin", "main")
        return work

    @staticmethod
    def write_baseline(work, entries):
        write(work, BASELINE, "\n".join(["# baseline"] + list(entries)) + "\n")

    def commit(self, work, message="change"):
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", message)

    def ci(self, work, **env):
        return run_cli(work, self.script, env={"GITHUB_ACTIONS": "true", **env})

    def pr(self, work):
        return self.ci(work, GITHUB_EVENT_NAME="pull_request", GITHUB_BASE_REF="main")

    def test_CONTROL_an_unchanged_or_shrinking_baseline_passes_and_says_it_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            self.git(work, "checkout", "-q", "-b", "feature")
            write(work, "orders_app/views.py", "x = 1\n")
            self.commit(work)
            code, out = self.pr(work)
            self.assertEqual(code, CLEAN, out)
            self.assertIn("COMPARED against base 'main'", out)
            self.write_baseline(work, ["A::x"])
            self.commit(work, "shrink")
            code, out = self.pr(work)
            self.assertEqual(code, CLEAN, out)
            self.assertIn("1 removed since base", out)

    def test_REGRESSION_a_new_relation_cannot_be_hidden_by_adding_it_to_the_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            self.git(work, "checkout", "-q", "-b", "feature")
            self.write_baseline(work, ["A::x", "A::y", "A::z"])
            self.commit(work, "baseline a new relation")
            code, out = self.pr(work)
            self.assertEqual(code, VIOLATION, out)
            self.assertIn("+ A::z", out)

    def test_REGRESSION_a_push_is_compared_with_the_commit_before_it_never_with_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            before = self.git(work, "rev-parse", "HEAD")
            self.write_baseline(work, ["A::x", "A::y", "A::z"])
            self.commit(work, "addition pushed straight to main")
            self.git(work, "push", "-q", "origin", "main")
            code, out = self.ci(work, GITHUB_EVENT_NAME="push", GITHUB_EVENT_BEFORE=before)
            self.assertEqual(code, VIOLATION, out)
            # Reproduced on d4aacbd: with `before` not plumbed the base fell back to
            # `main` — the pushed commit — and the addition passed as "OK".
            for env in ({"GITHUB_EVENT_NAME": "push"},
                        {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_BEFORE": "0" * 40},
                        {}):
                with self.subTest(env=env):
                    code, out = self.ci(work, **env)
                    self.assertEqual(code, INCOMPLETE, out)
                    self.assertIn("NO COMPARISON PERFORMED", out)
                    self.assertNotIn("OK", out.split("self-test: OK", 1)[-1])

    def test_REGRESSION_an_unreadable_or_missing_base_fails_closed_in_CI(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            code, out = self.ci(work, GITHUB_EVENT_NAME="pull_request", GITHUB_BASE_REF="no-such-branch")
            self.assertEqual(code, INCOMPLETE, out)
            code, out = self.ci(work, GITHUB_EVENT_NAME="pull_request")
            self.assertEqual(code, INCOMPLETE, out)

    def test_REGRESSION_a_moved_baseline_is_not_a_bootstrap(self):
        # Reproduced on d4aacbd: moving the file (and BASELINE_PATH) while adding an
        # entry read as "baseline is new at base (bootstrap)" and passed in CI.
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            self.git(work, "checkout", "-q", "-b", "feature")
            (work / "dinify_backend/tenancy/moved").mkdir()
            self.git(work, "mv", BASELINE, "dinify_backend/tenancy/moved/baseline.txt")
            mutate(work / "dinify_backend/tenancy/ratchet.py", '/ "baseline.txt"', '/ "moved" / "baseline.txt"')
            write(work, "dinify_backend/tenancy/moved/baseline.txt", "# baseline\nA::x\nA::y\nA::z\n")
            self.commit(work, "move and add")
            code, out = self.pr(work)
            self.assertEqual(code, INCOMPLETE, out)
            self.assertIn("moved or removed", out)

    def test_CONTROL_a_genuine_bootstrap_passes_but_says_nothing_was_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp, with_ratchet=False)
            self.git(work, "checkout", "-q", "-b", "introduce-ratchet")
            copy_into(work, RATCHET_FILES)
            self.write_baseline(work, ["A::x"])
            self.commit(work, "introduce the ratchet")
            code, out = self.pr(work)
            self.assertEqual(code, CLEAN, out)
            self.assertIn("BOOTSTRAP — NO COMPARISON PERFORMED", out)
            self.assertNotIn("ratchet: OK", out)

    def test_CONTROL_a_local_run_that_cannot_compare_is_a_labelled_skip_never_an_OK(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            code, out = run_cli(work, self.script)  # local, HEAD == main
            self.assertEqual(code, CLEAN, out)
            self.assertIn("NO COMPARISON PERFORMED (local run)", out)
            self.assertNotIn("ratchet: OK", out)

    def test_REGRESSION_zero_debt_and_missing_evidence_are_different_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            self.git(work, "checkout", "-q", "-b", "feature")
            self.write_baseline(work, [])
            self.commit(work, "debt paid")
            code, out = self.pr(work)
            self.assertEqual(code, CLEAN, out)
            self.assertIn("The debt is ZERO", out)
            (work / BASELINE).unlink()
            for runner in (self.pr, lambda w: run_cli(w, self.script)):
                code, out = runner(work)
                self.assertEqual(code, INCOMPLETE, out)
                self.assertIn("Missing evidence is not zero debt", out)

    def test_REGRESSION_a_comparator_forced_to_report_no_additions_fails_the_self_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self.repo(tmp)
            self.git(work, "checkout", "-q", "-b", "feature")
            self.write_baseline(work, ["A::x", "A::y", "A::z"])
            self.commit(work, "add")
            mutate(work / "dinify_backend/tenancy/ratchet.py",
                   "    return set(current_keys) - set(base_keys)\n", "    return set()\n")
            code, out = self.pr(work)
            self.assertEqual(code, SELF_TEST, out)
            self.assertNotIn("+ A::z", out)
            # NEGATIVE CONTROL: the same comparator, with the self-test bypassed, would
            # report the addition as compared-and-clean.
            probe = ("import sys; sys.path.insert(0, '.'); "
                     "from dinify_backend.tenancy.git_ratchet import check_ratchet; "
                     f"print(check_ratchet('.', {BASELINE!r}, 'main', False))")
            r = subprocess.run([PY, "-c", probe], cwd=work, capture_output=True, text=True, check=True)
            self.assertIn("COMPARED", r.stdout)


# =====================================================================================
class TheGuardsAreConsumedByTheRequiredCheck(unittest.TestCase):
    def step(self, name):
        return next(s for s in SUITE if s.get("name") == name)

    def test_CONTRACT_each_guard_keeps_its_step_and_command_and_nothing_softens_it(self):
        run_tests = next(i for i, s in enumerate(SUITE) if s.get("name") == "Run tests")
        for name, command in GUARDS.items():
            step = self.step(name)
            self.assertEqual(command_of(step), command, name)
            for key in ("if", "continue-on-error", "shell"):
                self.assertNotIn(key, step, name)
            self.assertEqual(status_swallowers(command), [], name)
            self.assertLess(SUITE.index(step), run_tests, name)
        self.assertEqual(self.step("Tenant-relation ratchet")["env"]["GITHUB_EVENT_BEFORE"], "${{ github.event.before }}")

    def test_CONTRACT_the_qualification_suite_is_its_own_step_before_the_long_suite(self):
        step = self.step("Guard qualification tests")
        self.assertEqual(command_of(step), QUALIFICATION)
        for key in ("if", "continue-on-error", "shell"):
            self.assertNotIn(key, step)
        self.assertLess(SUITE.index(step), next(i for i, s in enumerate(SUITE) if s.get("name") == "Run tests"))

    def test_REGRESSION_the_committed_step_text_carries_each_guards_real_status(self):
        # The step text from ci.yml, the runner's shell, the real guard, a real tree.
        with tempfile.TemporaryDirectory() as tmp:
            copy_into(tmp, ("scripts/check_money_fields.py",))
            write(tmp, "orders_app/models.py", CLEAN_MODELS)
            step = self.step("Money-field guard")["run"]
            self.assertEqual(run_step_for_real(step, tmp)[0], CLEAN)
            write(tmp, "menu_app/models.py", "class A(models.Model):\n    price = models.FloatField()\n")
            self.assertEqual(run_step_for_real(step, tmp)[0], VIOLATION)
            write(tmp, "menu_app/models.py", "class A(\n")
            self.assertEqual(run_step_for_real(step, tmp)[0], INCOMPLETE)
            (Path(tmp) / "menu_app/models.py").unlink()
            mutate(Path(tmp) / "scripts/check_money_fields.py", "        return func.attr == FIELD_CLASS\n", "        return False\n")
            self.assertEqual(run_step_for_real(step, tmp)[0], SELF_TEST)

    def test_REGRESSION_MATRIX_the_leg_and_the_required_aggregator_stay_red_when_a_guard_fails(self):
        aggregate = CI["jobs"]["test"]["steps"][0]["run"]
        for name, command in GUARDS.items():
            status, ran = simulate_job(SUITE, lambda step, i: "failure" if command_of(step) == command else "success")
            self.assertEqual(status, "failure", name)
            self.assertIn(("Retain the dependency-audit evidence", "success"), ran, "evidence is retained")
            self.assertIn(("Run tests", "skipped"), ran, "and it does not rescue the leg")
            self.assertNotEqual(run_aggregator(aggregate, status), 0, name)
        status, _ = simulate_job(SUITE, lambda step, i: "failure" if command_of(step) == QUALIFICATION else "success")
        self.assertEqual(status, "failure")
        self.assertEqual(simulate_job(SUITE, lambda step, i: "success")[0], "success", "CONTROL")

    def test_REGRESSION_verify_sh_fails_when_only_a_guard_fails_and_passes_when_it_does_not(self):
        # verify.sh itself, with every non-guard step stubbed green: the guard's REAL
        # status must still decide the run.
        with tempfile.TemporaryDirectory() as tmp:
            copy_into(tmp, ("scripts/verify.sh", "scripts/check_money_fields.py", *AMBIENT_FILES, *RATCHET_FILES))
            write(tmp, "orders_app/models.py", CLEAN_MODELS)
            write(tmp, BASELINE, "# baseline\nA::x\n")
            stub = Path(tmp) / "python-stub"
            stub.write_text(f'#!/bin/sh\ncase "$1" in scripts/check_*) exec "{PY}" "$@";; esac\nexit 0\n', encoding="utf-8")
            stub.chmod(0o755)

            def verify():
                r = subprocess.run(["bash", "scripts/verify.sh"], cwd=tmp, capture_output=True, text=True, timeout=300,
                                   env={"PATH": "/usr/bin:/bin", "HOME": tmp, "PYTHON": str(stub),
                                        "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
                return r.returncode, r.stdout + r.stderr

            code, out = verify()
            self.assertEqual(code, 0, out)
            self.assertIn("All checks passed", out)
            write(tmp, "menu_app/models.py", "class A(models.Model):\n    price = models.FloatField()\n")
            code, out = verify()
            self.assertEqual(code, 1, out)
            self.assertIn("FAILED: money-field guard", out)


if __name__ == "__main__":
    unittest.main()
