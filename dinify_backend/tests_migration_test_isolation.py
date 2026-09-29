"""
A test that rewinds migrations must leave the WHOLE schema migrated when it finishes.

Rewinding one app also unapplies every migration in OTHER apps that depends on the
part rewound, and a ``TransactionTestCase``'s DDL is not rolled back afterwards. So a
tearDown that restores only the app it rewound leaves the dependants unapplied for
every test that runs after it. Measured on ``0513adb``:
``restaurants_app.tests_migration_0055_sanitize`` left ``commercial_app.0001`` and
``platform_admin_app.0005``-``0010`` unapplied, so any later ``TransactionTestCase``
touching ``restaurant_onboarding`` failed with "relation does not exist". It stayed
hidden only because no such test happened to be ordered after it.

Two checks, because they fail in different ways:

* STRUCTURAL: every test module that drives ``MigrationExecutor`` is listed here, and
  each listed class's ``tearDown`` restores the project graph's leaves
  (``leaf_nodes()`` with no app), never one app's.
* BEHAVIOURAL: each listed class is run in-process, and afterwards nothing in the
  project graph may be left unapplied.
"""
import ast
import importlib
import inspect
import pathlib
import textwrap
import unittest

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# module -> the TransactionTestCase in it that rewinds migrations.
REWINDING_CLASSES = {
    'restaurants_app.tests_migration_0055_sanitize': 'Migration0055ExecutorTests',
    'platform_admin_app.tests_migration_flip': 'FlipAccountTypeMigrationTests',
    'platform_admin_app.tests_onboarding_migration': 'OnboardingMigrationApplicationTests',
}


def _unapplied():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    return [f'{m.app_label}.{m.name}' for m, _ in executor.migration_plan(leaves)]


def _restore_everything():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _test_modules():
    """Every test module of this repository's own apps and project package."""
    roots = {REPO_ROOT / 'dinify_backend'} | {
        pathlib.Path(config.path) for config in apps.get_app_configs()
        if pathlib.Path(config.path).is_relative_to(REPO_ROOT)
    }
    for root in sorted(roots):
        for path in sorted(root.rglob('tests*.py')):
            yield path


class RewindingTestsAreListedTests(SimpleTestCase):

    def test_every_module_that_drives_the_migration_executor_is_listed(self):
        found = set()
        for path in _test_modules():
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            if 'MigrationExecutor' in path.read_text():
                found.add(
                    path.relative_to(REPO_ROOT).with_suffix('').as_posix().replace('/', '.')
                )
        self.assertEqual(found, set(REWINDING_CLASSES))

    def test_each_teardown_restores_the_whole_project_graph(self):
        for module_name, class_name in REWINDING_CLASSES.items():
            with self.subTest(module_name):
                cls = getattr(importlib.import_module(module_name), class_name)
                self.assertTrue(issubclass(cls, TransactionTestCase))
                source = textwrap.dedent(inspect.getsource(cls.tearDown))
                calls = [
                    node for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'leaf_nodes'
                ]
                self.assertTrue(calls, 'tearDown does not restore the graph leaves')
                for call in calls:
                    self.assertEqual(
                        len(call.args) + len(call.keywords), 0,
                        f'{class_name}.tearDown restores one app\'s leaves '
                        '(leaf_nodes(<app>)), not the project graph\'s (leaf_nodes())',
                    )


class RewindingTestsLeaveNothingUnappliedTests(TransactionTestCase):
    """Runs each rewinding class in-process and inspects the schema it leaves."""

    def tearDown(self):
        # Never let a failure here cascade into the tests that follow.
        _restore_everything()
        super().tearDown()

    def test_each_rewinding_class_leaves_the_whole_graph_applied(self):
        self.assertEqual(_unapplied(), [], 'the schema was not fully migrated to begin with')
        for module_name, class_name in REWINDING_CLASSES.items():
            with self.subTest(module_name):
                cls = getattr(importlib.import_module(module_name), class_name)
                result = unittest.TestResult()
                unittest.defaultTestLoader.loadTestsFromTestCase(cls).run(result)
                self.assertTrue(
                    result.wasSuccessful(),
                    f'{class_name} itself failed: {result.failures + result.errors}',
                )
                left = _unapplied()
                _restore_everything()
                self.assertEqual(left, [], f'{class_name} left migrations unapplied')
