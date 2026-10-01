"""D15 R3 — the restore-usability check (``misc_app.restore_usability``).

Four groups, each answering a different question:

* CONTRACT — the pure rules: what a verdict may say, what the inputs and manifest must
  hold, and which comparisons can never PASS by default. Several tests here are the
  REGRESSIONS for false passes in the rehearsal prototype: a menu check that never
  matched the real lifecycle constants, supplied evidence that compared nothing
  (``{existingOrderId: {}}``, an unknown state, an empty golden set), and a sticker
  check that took HTTP 200 as identity. Each sits beside the positive control that
  must keep passing.
* STARTUP ORDER — real subprocesses with SENTINEL ``django`` and settings modules on
  the path: the supported bootstrap refuses an unsafe environment WITHOUT importing
  either, and a direct ``manage.py`` invocation refuses outright.
* BOUNDED EXECUTION — real subprocesses timed out, cancelled and crashed: no partial
  run is ever reported as PASS, and what the run opened is closed.
* APPLICATION READS and the POSTGRESQL ROLE — the checks against real reads of a real
  fixture, and the read-only role enforced by the server itself, ending in a full
  bootstrap-to-adapter hand-off that passes.

The isolated profile itself (no network interface, a Unix-socket database, a read-only
media mount) cannot be built inside CI; the one test that needs it is skipped unless
``RESTORE_USABILITY_PROFILE_INPUTS`` names a prepared inputs document, and says so.
"""
import contextlib
import copy
import importlib.util
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jwt
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live, RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding, RestaurantStatus_Suspended,
)
from misc_app import restore_usability as ru

ROOT = Path(__file__).resolve().parent.parent
REV = 'a' * 40
NONCE = '0123456789abcdef'
U1, U2, U3 = (str(uuid.UUID(int=n)) for n in (1, 2, 3))


def _inputs(**over):
    doc = {
        'schema': ru.INPUTS_SCHEMA,
        'release': {'source_revision': REV},
        'target': {'settings_module': 'dinify_backend.settings',
                   'database': {'name': 'restored', 'user': 'reader', 'host': '/run/restore', 'port': '5432'},
                   'media_root': '/srv/restore/media'},
        'limits': {'wall_clock_seconds': 600},
        'attestations': {'no_live_routing': True, 'no_background_work': True,
                         'configuration_custody': True, 'attested_by': 'owner'},
    }
    doc.update(over)
    return doc


def _manifest(**over):
    doc = {
        'schema': ru.MANIFEST_SCHEMA, 'source_revision': REV,
        'declared_transformations': copy.deepcopy(ru.DECLARED_TRANSFORMATIONS),
        'sample': {'restaurant_ids': [U1], 'order_ids': [U2]},
        'tables': {'restaurants': {'rows': 1, 'sha256': '0' * 64}},
        'media_listing': {},
        'golden_reads': {f'order.details.{U2}': {'status': 200, 'body': {'data': {}}}},
    }
    doc.update(over)
    return doc


def _problems(doc):
    inputs, problems = ru.validate_inputs(doc)
    return inputs, problems


# ============================================================================ CONTRACT

class VerdictContractTests(SimpleTestCase):

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            ru.check('OK')

    def test_REGRESSION_no_intrinsic_check_is_never_a_usable_pass(self):
        v = ru.verdicts({'P.inputs': ru.check('PASS')})
        self.assertEqual(v, {'usable': 'INCOMPLETE', 'independent': 'UNAVAILABLE', 'identity': 'UNAVAILABLE'})

    def test_CONTROL_every_intrinsic_check_passing_is_usable(self):
        v = ru.verdicts({'P.inputs': ru.check('PASS'), 'I.a': ru.check('PASS'), 'I.b': ru.check('PASS')})
        self.assertEqual(v['usable'], 'PASS')

    def test_a_failure_dominates_and_unavailable_folds_to_incomplete(self):
        self.assertEqual(ru.verdicts({'I.a': ru.check('PASS'), 'I.b': ru.check('FAIL'),
                                      'I.c': ru.check('UNAVAILABLE')})['usable'], 'FAIL')
        self.assertEqual(ru.verdicts({'I.a': ru.check('PASS'), 'I.b': ru.check('UNAVAILABLE')})['usable'],
                         'INCOMPLETE')

    def test_supplied_but_partial_evidence_is_incomplete_not_unavailable(self):
        v = ru.verdicts({'I.a': ru.check('PASS'), 'H.a': ru.check('PASS'), 'H.b': ru.check('UNAVAILABLE'),
                         'R.a': ru.check('UNAVAILABLE')})
        self.assertEqual((v['independent'], v['identity']), ('INCOMPLETE', 'UNAVAILABLE'))

    def test_observations_never_decide_a_verdict(self):
        v = ru.verdicts({'I.a': ru.check('PASS'), 'O.x': ru.check('OBSERVED')})
        self.assertEqual(v['usable'], 'PASS')

    def test_only_the_attestation_precondition_may_be_attested(self):
        self.assertTrue(ru.preconditions_satisfied({'P.attestations': ru.check('ATTESTED')}))
        self.assertFalse(ru.preconditions_satisfied({'P.db_readonly': ru.check('ATTESTED')}))
        self.assertFalse(ru.preconditions_satisfied({'P.runtime': ru.check('INCOMPLETE')}))

    def test_an_unsatisfied_precondition_runs_no_verdict(self):
        v = ru.verdicts({'P.runtime': ru.check('FAIL'), 'I.a': ru.check('PASS')})
        self.assertEqual(set(v.values()), {'NOT_RUN'})

    def test_a_run_that_did_not_complete_reports_nothing_as_pass(self):
        checks = {'I.a': ru.check('PASS'), 'H.a': ru.check('PASS'), 'R.a': ru.check('PASS')}
        for status in ('TIMED_OUT', 'CANCELLED', 'CRASHED'):
            with self.subTest(status):
                self.assertEqual(set(ru.verdicts(checks, status).values()), {'INTERRUPTED'})
        self.assertEqual(set(ru.verdicts(checks, 'REFUSED').values()), {'NOT_RUN'})

    def test_exit_codes(self):
        ok = {'usable': 'PASS', 'independent': 'UNAVAILABLE', 'identity': 'UNAVAILABLE'}
        self.assertEqual(ru.exit_code('COMPLETED', ok, ['usable']), 0)
        self.assertEqual(ru.exit_code('COMPLETED', ok, ['usable', 'identity']), 1)
        self.assertEqual(ru.exit_code('COMPLETED', ok, []), 1)
        self.assertEqual(ru.exit_code('REFUSED', ok, ['usable']), 3)
        self.assertEqual(ru.exit_code('TIMED_OUT', ok, ['usable']), 4)
        self.assertEqual(ru.exit_code('CANCELLED', ok, ['usable']), 4)
        self.assertEqual(ru.exit_code('CRASHED', ok, ['usable']), 70)


class GoldenComparisonTests(SimpleTestCase):
    KEY = f'order.details.{U2}'

    def test_REGRESSION_an_empty_comparison_is_never_a_pass(self):
        self.assertEqual(ru.compare_golden({}, {})['status'], 'INCOMPLETE')
        self.assertEqual(ru.compare_golden({}, {self.KEY: {'status': 200, 'body': {}}})['status'], 'INCOMPLETE')

    def test_a_golden_read_that_was_not_executed_is_incomplete(self):
        gold = {self.KEY: {'status': 200, 'body': {}}, f'kitchen.state.{U3}': {'status': 200, 'body': {}}}
        res = ru.compare_golden(gold, {self.KEY: {'status': 200, 'body': {}}})
        self.assertEqual((res['status'], res['golden_not_executed']), ('INCOMPLETE', [f'kitchen.state.{U3}']))

    def test_an_executed_identity_read_with_no_golden_record_is_incomplete(self):
        res = ru.compare_golden({self.KEY: {'status': 200, 'body': {}}},
                                {self.KEY: {'status': 200, 'body': {}}, f'qr.scan.{U1}': {'status': 200, 'body': {}}})
        self.assertEqual(res['status'], 'INCOMPLETE')

    def test_a_difference_fails(self):
        res = ru.compare_golden({self.KEY: {'status': 200, 'body': {'a': 1}}},
                                {self.KEY: {'status': 200, 'body': {'a': 2}}})
        self.assertEqual((res['status'], res['differing']), ('FAIL', [self.KEY]))

    def test_CONTROL_a_complete_equal_comparison_passes(self):
        gold = {self.KEY: {'status': 200, 'body': {'a': 1}}}
        self.assertEqual(ru.compare_golden(gold, copy.deepcopy(gold))['status'], 'PASS')

    def test_only_the_declared_paths_are_removed_and_nothing_else(self):
        body = {'data': {'session_token': 't', 'quote_policy': {'status': 'live', 'expires_at': 'E'},
                         'issued_at': 'T', 'id': U1}}
        applied = set()
        out = ru.apply_transformations(f'qr.scan.{U1}', body, applied)
        self.assertEqual(out, {'data': {'quote_policy': {'expires_at': 'E'}, 'issued_at': 'T', 'id': U1}})
        self.assertEqual(applied, {f'qr.scan.{U1}:data.session_token', f'qr.scan.{U1}:data.quote_policy.status'})
        self.assertIn('session_token', body['data'], 'the input must not be mutated')

    def test_the_session_token_is_removed_from_scans_only(self):
        applied = set()
        out = ru.apply_transformations(self.KEY, {'data': {'session_token': 't'}}, applied)
        self.assertEqual((out, applied), ({'data': {'session_token': 't'}}, set()))

    def test_identity_read_keys(self):
        self.assertTrue(ru.identity_read_key(self.KEY))
        self.assertTrue(ru.identity_read_key(ru.BEARER_READ_KEY))
        for key in (f'menu.public.{U1}', f'kitchen.active.{U1}', 'order.details.not-a-uuid', 'readiness'):
            with self.subTest(key):
                self.assertFalse(ru.identity_read_key(key))


class InputsContractTests(SimpleTestCase):

    def test_CONTROL_a_minimal_document_is_accepted_with_the_default_limits(self):
        inputs, problems = _problems(_inputs())
        self.assertEqual(problems, [])
        self.assertEqual(inputs['require'], ['usable'])
        self.assertEqual(inputs['limits']['max_restaurants'], 10)

    def test_unknown_keys_are_refused_at_every_level(self):
        for doc in (_inputs(extra=1),
                    _inputs(release={'source_revision': REV, 'branch': 'main'}),
                    _inputs(limits={'wall_clock_seconds': 5, 'rto_seconds': 9}),
                    _inputs(originals={'printed_qr': [{'table_id': U1, 'restaurant_id': U2, 'credential': 'c',
                                                       'generation': 1}]})):
            with self.subTest(doc=doc):
                _, problems = _problems(doc)
                self.assertTrue(any('unknown key' in p for p in problems), problems)

    def test_required_sections_and_shapes(self):
        base = _inputs()
        for key in ('release', 'target', 'limits', 'attestations'):
            doc = copy.deepcopy(base)
            del doc[key]
            with self.subTest(key):
                self.assertIn(f'inputs.{key}: required', _problems(doc)[1])
        self.assertTrue(_problems(_inputs(release={'source_revision': 'abc123'}))[1])
        self.assertTrue(_problems(_inputs(limits={'wall_clock_seconds': True}))[1], 'a bool is not an integer')
        self.assertTrue(_problems(_inputs(limits={'wall_clock_seconds': 0}))[1])
        bad_db = copy.deepcopy(base['target'])
        bad_db['database']['host'] = '127.0.0.1'
        self.assertTrue(_problems(_inputs(target=bad_db))[1], 'a TCP host is not a socket directory')
        self.assertTrue(_problems(_inputs(require=['usable', 'global']))[1])
        for require in ([['usable']], [{'v': 1}], 'usable', []):
            with self.subTest(require=require):            # refused, never a crash on an unhashable value
                self.assertTrue(_problems(_inputs(require=require))[1])

    def test_a_trailing_newline_never_satisfies_a_format(self):
        # '$' matches before a final newline; every format here is anchored with \Z instead.
        self.assertTrue(_problems(_inputs(release={'source_revision': REV + '\n'}))[1])
        tgt = copy.deepcopy(_inputs()['target'])
        tgt['database']['port'] = '5432\n'
        self.assertTrue(_problems(_inputs(target=tgt))[1])
        self.assertTrue(_problems(_inputs(manifest={'path': '/m.json', 'sha256': '0' * 64 + '\n'}))[1])
        self.assertIsNone(ru.NONCE.match(NONCE + '\n'))

    def test_every_attestation_must_be_exactly_true(self):
        for value in (False, 'true', 1, None):
            att = dict(_inputs()['attestations'], no_background_work=value)
            with self.subTest(value=value):
                self.assertTrue(_problems(_inputs(attestations=att))[1])

    def test_REGRESSION_an_order_record_keyed_by_id_with_no_facts_is_refused(self):
        # The rehearsal prototype accepted {existingOrderId: {}} and compared nothing.
        _, problems = _problems(_inputs(originals={'orders': {U1: {}}}))
        self.assertIn('inputs.originals.orders: must be a non-empty list '
                      '(omit the key when there is nothing to supply)', problems)
        _, problems = _problems(_inputs(originals={'orders': [{'order_id': U1}]}))
        self.assertEqual(sorted(p.split(':')[0] for p in problems),
                         ['inputs.originals.orders[0].restaurant_id', 'inputs.originals.orders[0].state',
                          'inputs.originals.orders[0].table_id'])

    def test_REGRESSION_an_unknown_expected_state_is_refused(self):
        rec = {'order_id': U1, 'restaurant_id': U2, 'table_id': U3, 'state': 'not_accepted'}
        self.assertIn("inputs.originals.orders[0].state: must be 'accepted' or 'exists'",
                      _problems(_inputs(originals={'orders': [rec]}))[1])

    def test_accepted_needs_a_reference_and_exists_must_not_carry_one(self):
        rec = {'order_id': U1, 'restaurant_id': U2, 'table_id': U3, 'state': 'accepted'}
        self.assertTrue(_problems(_inputs(originals={'orders': [rec]}))[1])
        self.assertTrue(_problems(_inputs(originals={'orders': [dict(rec, state='exists', quote_ref='q')]}))[1])

    def test_CONTROL_complete_original_records_are_accepted(self):
        orig = {'orders': [{'order_id': U1, 'restaurant_id': U2, 'table_id': U3, 'state': 'accepted',
                            'quote_ref': 'q1'},
                           {'order_id': U2, 'restaurant_id': U2, 'table_id': U3, 'state': 'exists'}],
                'printed_qr': [{'table_id': U3, 'restaurant_id': U2, 'credential': 'cred'}],
                'staff_bearer_token': 'tok'}
        self.assertEqual(_problems(_inputs(originals=orig))[1], [])

    def test_empty_or_duplicated_originals_are_refused(self):
        self.assertIn('inputs.originals: is empty (omit it when no originals are supplied)',
                      _problems(_inputs(originals={}))[1])
        self.assertTrue(_problems(_inputs(originals={'printed_qr': []}))[1])
        self.assertTrue(_problems(_inputs(originals={'staff_bearer_token': '  '}))[1])
        dup = [{'table_id': U1, 'restaurant_id': U2, 'credential': 'a'},
               {'table_id': U1, 'restaurant_id': U2, 'credential': 'b'}]
        self.assertTrue(_problems(_inputs(originals={'printed_qr': dup}))[1])

    def test_a_sample_beside_a_manifest_is_refused(self):
        doc = _inputs(sample={'restaurant_ids': [U1]}, manifest={'path': '/m.json', 'sha256': '0' * 64})
        self.assertTrue(any(p.startswith('inputs.sample: must be omitted') for p in _problems(doc)[1]))

    def test_redaction_keeps_no_credential(self):
        orig = {'printed_qr': [{'table_id': U3, 'restaurant_id': U2, 'credential': 'SECRET-CREDENTIAL'}],
                'staff_bearer_token': 'SECRET-TOKEN'}
        inputs, _ = _problems(_inputs(originals=orig))
        text = json.dumps(ru.redacted_inputs(inputs))
        self.assertNotIn('SECRET-CREDENTIAL', text)
        self.assertNotIn('SECRET-TOKEN', text)
        self.assertIn(U3, text)
        self.assertRegex(ru.redacted_digest(inputs), r'^[0-9a-f]{64}$')


class ManifestContractTests(SimpleTestCase):
    LIMITS = dict(ru.DEFAULT_LIMITS, wall_clock_seconds=60)

    def test_CONTROL_a_complete_manifest_is_accepted(self):
        self.assertEqual(ru.validate_manifest(_manifest(), self.LIMITS), [])

    def test_REGRESSION_an_empty_or_missing_golden_set_is_refused(self):
        self.assertIn('manifest.golden_reads: must be a non-empty object (an empty set compares nothing)',
                      ru.validate_manifest(_manifest(golden_reads={}), self.LIMITS))
        doc = _manifest()
        del doc['golden_reads']
        self.assertIn('manifest.golden_reads: required', ru.validate_manifest(doc, self.LIMITS))

    def test_a_null_or_short_source_revision_is_refused(self):
        for value in (None, '', 'a' * 39, 'A' * 40):
            with self.subTest(value=value):
                self.assertTrue(ru.validate_manifest(_manifest(source_revision=value), self.LIMITS))

    def test_a_golden_read_that_is_not_an_identity_read_is_refused(self):
        gold = {f'menu.public.{U1}': {'status': 200, 'body': {}}}
        self.assertTrue(any('is not an identity read' in p
                            for p in ru.validate_manifest(_manifest(golden_reads=gold), self.LIMITS)))

    def test_other_transformations_and_empty_tables_are_refused(self):
        other = copy.deepcopy(ru.DECLARED_TRANSFORMATIONS) + [{'reads': '', 'path': ['data', 'id'], 'why': 'x'}]
        self.assertTrue(ru.validate_manifest(_manifest(declared_transformations=other), self.LIMITS))
        self.assertTrue(ru.validate_manifest(_manifest(tables={}), self.LIMITS))
        self.assertTrue(ru.validate_manifest(_manifest(sample={'restaurant_ids': [], 'order_ids': []}), self.LIMITS))


# ============================================================================ PRE-START OBSERVERS

class EntryObserverTests(SimpleTestCase):
    MAIN = SimpleNamespace(__spec__=SimpleNamespace(name=ru.BOOTSTRAP_MODULE))
    CLEAN = {'sys': 1, 'json': 1, 'misc_app': 1, ru.BOOTSTRAP_MODULE: 1}

    def observe(self, modules=None, main=None, environ=None, settings_module='dinify_backend.settings'):
        return ru.observe_entry(settings_module, modules=modules or self.CLEAN,
                                main_module=main or self.MAIN, environ=environ or {})

    def test_CONTROL_a_clean_supported_entry_passes(self):
        self.assertEqual(self.observe()['status'], 'PASS')

    def test_anything_from_the_framework_or_the_application_already_imported_fails(self):
        for early in ('django', 'django.conf', 'rest_framework', 'psycopg', 'pymongo', 'orders_app.models',
                      'dinify_backend.settings', 'misc_app.controllers'):
            with self.subTest(early):
                res = self.observe(modules=dict(self.CLEAN, **{early: 1}))
                self.assertEqual((res['status'], res['imported_too_early']), ('FAIL', [early]))

    def test_the_named_settings_module_already_imported_fails(self):
        res = self.observe(modules=dict(self.CLEAN, sentinel_settings=1), settings_module='sentinel_settings')
        self.assertEqual(res['status'], 'FAIL')

    def test_any_other_entry_fails(self):
        other = SimpleNamespace(__spec__=None)
        self.assertEqual(self.observe(main=other)['status'], 'FAIL')

    def test_a_conflicting_settings_module_in_the_environment_fails(self):
        res = self.observe(environ={'DJANGO_SETTINGS_MODULE': 'dinify_backend.test_settings'})
        self.assertEqual(res['status'], 'FAIL')


class SourceIdentityObserverTests(SimpleTestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.dir)])
        git = ['git', '-C', str(self.dir), '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(['git', 'init', '-q', str(self.dir)], check=True)
        (self.dir / 'a.txt').write_text('a')
        subprocess.run(git + ['add', 'a.txt'], check=True)
        subprocess.run(git + ['commit', '-q', '-m', 'x'], check=True)
        self.rev = subprocess.run(['git', '-C', str(self.dir), 'rev-parse', 'HEAD'], check=True,
                                  capture_output=True, text=True).stdout.strip()

    def test_CONTROL_a_clean_checkout_at_the_revision_passes(self):
        res = ru.observe_source_identity(self.rev, root=self.dir)
        self.assertEqual((res['status'], res['revision']), ('PASS', self.rev))

    def test_another_revision_fails(self):
        self.assertEqual(ru.observe_source_identity('b' * 40, root=self.dir)['status'], 'FAIL')

    def test_a_modified_or_untracked_file_fails(self):
        (self.dir / 'a.txt').write_text('b')
        self.assertEqual(ru.observe_source_identity(self.rev, root=self.dir)['status'], 'FAIL')
        subprocess.run(['git', '-C', str(self.dir), 'checkout', '-q', '--', 'a.txt'], check=True)
        (self.dir / 'new.txt').write_text('n')
        self.assertEqual(ru.observe_source_identity(self.rev, root=self.dir)['status'], 'FAIL')

    def test_a_directory_that_is_not_the_checkout_root_fails(self):
        sub = self.dir / 'sub'
        sub.mkdir()
        self.assertEqual(ru.observe_source_identity(self.rev, root=sub)['status'], 'FAIL')


class RuntimeObserverTests(SimpleTestCase):
    """A lock describing THIS interpreter passes; any difference fails."""

    def write_lock(self, mutate=None):
        import importlib.metadata
        root = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(root)])
        libc = platform.libc_ver()
        packages = [{'name': d.metadata['Name'], 'version': d.version} for d in importlib.metadata.distributions()]
        lock = {'target': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                           'platform': sys.platform, 'machine': platform.machine(),
                           'glibc': libc[1] if libc[0] == 'glibc' else None},
                'packages': packages, 'bootstrap': []}
        if mutate:
            mutate(lock)
        (root / 'release').mkdir()
        (root / 'release' / 'python-lock.json').write_text(json.dumps(lock))
        return root

    def test_CONTROL_a_lock_equal_to_this_environment_passes(self):
        res = ru.observe_runtime(self.write_lock())
        self.assertEqual(res['status'], 'PASS', res)

    def test_a_different_interpreter_fails(self):
        res = ru.observe_runtime(self.write_lock(lambda lk: lk['target'].update(python='3.99.0')))
        self.assertEqual(res['status'], 'FAIL')

    def test_a_different_installed_set_fails(self):
        def bump(lk):
            lk['packages'][0]['version'] += '.post999'
            lk['packages'].append({'name': 'not-installed-anywhere', 'version': '1'})
        res = ru.observe_runtime(self.write_lock(bump))
        self.assertEqual(res['status'], 'FAIL')
        self.assertEqual(res['missing'], ['not-installed-anywhere'])
        self.assertEqual(len(res['wrong_version']), 1)

    def test_an_unreadable_lock_fails(self):
        self.assertEqual(ru.observe_runtime(Path(tempfile.gettempdir()) / 'no-such-root')['status'], 'FAIL')


class IsolationObserverTests(SimpleTestCase):
    IFACES = [(1, 'lo'), (2, 'eth0')]

    def test_CONTROL_no_interface_up_and_no_route_passes(self):
        res = ru.observe_outbound_denied(self.IFACES, lambda n: 0, [], [])
        self.assertEqual(res['status'], 'PASS')

    def test_any_interface_up_fails_loopback_included(self):
        for up in ('lo', 'eth0'):
            with self.subTest(up):
                res = ru.observe_outbound_denied(self.IFACES, lambda n, u=up: ru.IFF_UP if n == u else 0, [], [])
                self.assertEqual((res['status'], res['interfaces_up']), ('FAIL', [up]))

    def test_a_route_or_an_unreadable_interface_fails(self):
        self.assertEqual(ru.observe_outbound_denied(self.IFACES, lambda n: 0, ['eth0 00000000'], [])['status'],
                         'FAIL')
        self.assertEqual(ru.observe_outbound_denied(self.IFACES, lambda n: 0, [], ['::/0 ...'])['status'], 'FAIL')

        def unreadable(_n):
            raise OSError('no')
        self.assertEqual(ru.observe_outbound_denied(self.IFACES, unreadable, [], [])['status'], 'FAIL')

    def test_a_writable_or_missing_media_root_fails(self):
        with tempfile.TemporaryDirectory() as d:
            res = ru.observe_media_readonly(d)
            self.assertEqual((res['status'], res['read_only_mount']), ('FAIL', False))
        self.assertEqual(ru.observe_media_readonly('/no/such/media')['status'], 'FAIL')

    def test_a_read_only_mount_that_is_still_writable_fails_and_a_true_one_passes(self):
        ro = SimpleNamespace(f_flag=os.ST_RDONLY)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ru.os, 'statvfs', return_value=ro):
            with mock.patch.object(ru.os, 'access', return_value=True):
                self.assertEqual(ru.observe_media_readonly(d)['status'], 'FAIL')
            with mock.patch.object(ru.os, 'access', return_value=False):
                self.assertEqual(ru.observe_media_readonly(d)['status'], 'PASS')

    def test_an_unwritable_directory_that_is_not_a_read_only_mount_fails(self):
        # Permissions are not a mount: anyone who can change them can write again, and the
        # restored bytes are then evidence nobody can vouch for.
        rw = SimpleNamespace(f_flag=0)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ru.os, 'statvfs', return_value=rw), \
                mock.patch.object(ru.os, 'access', return_value=False):
            res = ru.observe_media_readonly(d)
        self.assertEqual((res['status'], res['read_only_mount'], res['writable']), ('FAIL', False, False))

    def test_attestations_are_attested_and_never_pass(self):
        res = ru.observe_attestations(_inputs()['attestations'])
        self.assertEqual(res['status'], 'ATTESTED')
        self.assertEqual(ru.observe_attestations({'no_live_routing': True})['status'], 'FAIL')


class SettingsPhaseTests(SimpleTestCase):

    def settings_like(self, sock, media, **over):
        values = {'DATABASES': {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'restored',
                                            'USER': 'reader', 'HOST': sock, 'PORT': '5432'}},
                  'MEDIA_ROOT': media, 'ALLOWED_HOSTS': ['*'],
                  'SECRET_KEY': 'S' * 50, 'DINER_CAP_KEY': 'D' * 50}
        values.update(over)
        return SimpleNamespace(**values)

    def run_check(self, **over):
        with tempfile.TemporaryDirectory() as sock, tempfile.TemporaryDirectory() as media:
            tgt = dict(_inputs()['target'], media_root=media)
            tgt['database'] = dict(tgt['database'], host=sock)
            res = ru.check_settings({'target': tgt}, self.settings_like(sock, media, **over))
        return res

    def test_CONTROL_a_matching_target_passes_and_no_key_value_is_emitted(self):
        res = self.run_check()
        self.assertEqual({k: v['status'] for k, v in res.items()},
                         {'P.target_identity': 'PASS', 'P.signing_keys_present': 'PASS'})
        self.assertEqual(res['P.signing_keys_present']['keys_checked'], ['SECRET_KEY', 'DINER_CAP_KEY'])
        text = json.dumps(res)
        self.assertNotIn('S' * 50, text)
        self.assertNotIn('D' * 50, text)

    def test_a_tcp_host_another_engine_or_another_media_root_fails(self):
        self.assertEqual(self.run_check(MEDIA_ROOT='/elsewhere')['P.target_identity']['status'], 'FAIL')
        self.assertEqual(self.run_check(DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3'}})
                         ['P.target_identity']['status'], 'FAIL')
        self.assertEqual(self.run_check(ALLOWED_HOSTS=[])['P.target_identity']['status'], 'FAIL')

    def test_an_absent_equal_or_derived_signing_key_fails(self):
        self.assertEqual(self.run_check(DINER_CAP_KEY=None)['P.signing_keys_present']['status'], 'FAIL')
        self.assertEqual(self.run_check(DINER_CAP_KEY='S' * 50)['P.signing_keys_present']['status'], 'FAIL')
        with mock.patch('decouple.config', return_value=None):
            self.assertEqual(self.run_check()['P.signing_keys_present']['status'], 'FAIL')

    def test_the_in_process_host(self):
        self.assertEqual(ru.http_host(['*']), 'restore-check.invalid')
        self.assertEqual(ru.http_host(['.dinifyapp.com']), 'restore-check.dinifyapp.com')
        self.assertEqual(ru.http_host(['api.example.com']), 'api.example.com')
        self.assertIsNone(ru.http_host([]))


# ============================================================================ HAND-OVER

class HandoverTests(SimpleTestCase):

    def test_the_adapter_refuses_without_the_bootstrap_handover(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(ru.COMMAND)
        self.assertEqual(ctx.exception.returncode, ru.EXIT_REFUSED)
        self.assertIn('cannot provide pre-start protection', str(ctx.exception))

    def test_a_handover_this_bootstrap_did_not_issue_is_refused(self):
        forged = ru.BootstrapProof('f' * 32, ru.new_result(NONCE), {}, None, time.monotonic() + 60)
        with self.assertRaises(CommandError) as ctx:
            call_command(ru.COMMAND, bootstrap=forged)
        self.assertEqual(ctx.exception.returncode, ru.EXIT_REFUSED)

    def test_a_handover_is_accepted_once(self):
        proof = ru.issue_proof(ru.new_result(NONCE), {}, None, time.monotonic() + 60)
        self.assertIs(ru.accept_proof(proof), proof)
        with self.assertRaises(ru.BootstrapRefused):
            ru.accept_proof(proof)

    def test_an_environment_variable_is_not_a_handover(self):
        with mock.patch.dict(os.environ, {'RESTORE_USABILITY_BOOTSTRAPPED': '1'}):
            with self.assertRaises(ru.BootstrapRefused):
                ru.accept_proof('1')

    def test_the_bootstrap_entry_point_rejects_a_bad_command_line_before_any_check(self):
        with tempfile.TemporaryDirectory() as d:
            existing = Path(d) / 'r.json'
            existing.write_text('{}')
            with mock.patch.object(ru, 'supervise') as supervise:
                self.assertEqual(ru.main(['--inputs', 'x', '--result', str(Path(d) / 'n.json'), '--nonce', 'XYZ']),
                                 ru.EXIT_USAGE)
                self.assertEqual(ru.main(['--inputs', 'x', '--result', str(existing), '--nonce', NONCE]),
                                 ru.EXIT_USAGE)
            supervise.assert_not_called()
            self.assertEqual(existing.read_text(), '{}')

    def test_publish_never_overwrites_and_leaves_nothing_partial(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'result.json'
            ru.publish(path, {'nonce': NONCE, 'v': 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                ru.publish(path, {'nonce': NONCE, 'v': 2})
            self.assertEqual(json.loads(path.read_text())['v'], 1)
            self.assertEqual(os.listdir(d), ['result.json'])


# ============================================================================ STARTUP ORDER (subprocess)

def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != 'DJANGO_SETTINGS_MODULE'}
    env.update(extra)
    return env


class StartupOrderTests(SimpleTestCase):
    """The supported bootstrap, run for real, with sentinel ``django`` and settings modules.

    The sentinels record that they were imported and then stop the process. Every
    refusal here happens in the pre-start phase, so neither may ever be imported; the
    CONTROL proves they fire when something does import them, so their absence means
    something.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.dir)])
        self.sentinels = self.dir / 'sentinels'
        (self.sentinels / 'django').mkdir(parents=True)
        mark = "import os, pathlib; pathlib.Path(os.environ['SENTINEL_MARKS'], '{name}').touch(); raise SystemExit(97)\n"
        (self.sentinels / 'django' / '__init__.py').write_text(mark.format(name='django'))
        (self.sentinels / 'sentinel_settings.py').write_text(mark.format(name='settings'))
        self.marks = self.dir / 'marks'
        self.marks.mkdir()
        self.media = self.dir / 'media'
        self.media.mkdir()
        self.env = _clean_env(PYTHONPATH=os.pathsep.join([str(self.sentinels), str(ROOT)]),
                              SENTINEL_MARKS=str(self.marks))

    def write_inputs(self, **over):
        tgt = dict(_inputs()['target'], settings_module='sentinel_settings', media_root=str(self.media))
        orig = {'printed_qr': [{'table_id': U1, 'restaurant_id': U2, 'credential': 'SENTINEL-CREDENTIAL'}],
                'staff_bearer_token': 'SENTINEL-BEARER-TOKEN'}
        doc = _inputs(target=tgt, originals=orig, **over)
        path = self.dir / 'inputs.json'
        path.write_text(json.dumps(doc))
        return path

    def run_bootstrap(self, argv_prefix, inputs, env=None):
        result = self.dir / 'result.json'
        proc = subprocess.run(argv_prefix + ['--inputs', str(inputs), '--result', str(result), '--nonce', NONCE],
                              cwd=ROOT, env=env or self.env, capture_output=True, text=True, timeout=120)
        doc = json.loads(result.read_text()) if result.exists() else None
        return proc, doc

    def assert_no_sentinel(self, proc, doc):
        self.assertEqual(sorted(os.listdir(self.marks)), [], proc.stderr)
        for text in (proc.stdout, proc.stderr, json.dumps(doc)):
            self.assertNotIn('SENTINEL-CREDENTIAL', text)
            self.assertNotIn('SENTINEL-BEARER-TOKEN', text)

    def test_CONTROL_the_sentinels_fire_when_imported(self):
        for module, mark in (('django', 'django'), ('sentinel_settings', 'settings')):
            with self.subTest(module):
                proc = subprocess.run([sys.executable, '-c', f'import {module}'], cwd=ROOT, env=self.env,
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 97)
                self.assertIn(mark, os.listdir(self.marks))

    def test_an_unsafe_supported_bootstrap_refuses_before_the_settings_or_django(self):
        # A revision nobody has: P.source_identity cannot pass, whatever else this host is.
        proc, doc = self.run_bootstrap([sys.executable, '-m', ru.BOOTSTRAP_MODULE],
                                       self.write_inputs(release={'source_revision': 'f' * 40}))
        self.assertEqual(proc.returncode, ru.EXIT_REFUSED, proc.stderr)
        self.assert_no_sentinel(proc, doc)
        self.assertEqual(doc['run']['status'], 'REFUSED')
        self.assertEqual(doc['run']['refused_before'], 'importing the settings or any application code')
        self.assertEqual(doc['nonce'], NONCE)
        self.assertEqual(doc['checks']['P.entrypoint']['status'], 'PASS', doc['checks']['P.entrypoint'])
        self.assertEqual(doc['checks']['P.source_identity']['status'], 'FAIL')
        self.assertNotIn('P.target_identity', doc['checks'])
        self.assertEqual(set(doc['verdicts'].values()), {'NOT_RUN'})
        self.assertNotIn('controls', doc, 'no configured control is applied after a refusal')

    def test_any_other_entry_is_refused_and_imports_nothing(self):
        script = (f'import sys; from misc_app import restore_usability as m; '
                  f'sys.exit(m.main(sys.argv[1:]))')
        proc, doc = self.run_bootstrap([sys.executable, '-c', script], self.write_inputs())
        self.assertEqual(proc.returncode, ru.EXIT_REFUSED, proc.stderr)
        self.assert_no_sentinel(proc, doc)
        self.assertEqual(doc['checks']['P.entrypoint']['status'], 'FAIL')

    def test_a_conflicting_settings_module_in_the_environment_is_refused(self):
        env = dict(self.env, DJANGO_SETTINGS_MODULE='dinify_backend.settings')
        proc, doc = self.run_bootstrap([sys.executable, '-m', ru.BOOTSTRAP_MODULE], self.write_inputs(), env=env)
        self.assertEqual(proc.returncode, ru.EXIT_REFUSED, proc.stderr)
        self.assert_no_sentinel(proc, doc)
        self.assertEqual(doc['checks']['P.entrypoint']['status'], 'FAIL')

    def test_django_imported_before_the_bootstrap_is_detected(self):
        # The real Django this time (no sentinels on the path), imported first.
        script = ('import sys, runpy, django; sys.argv = ["x"] + sys.argv[1:]; '
                  f'runpy.run_module({ru.BOOTSTRAP_MODULE!r}, run_name="__main__", alter_sys=True)')
        env = _clean_env(PYTHONPATH=str(ROOT))
        proc, doc = self.run_bootstrap([sys.executable, '-c', script], self.write_inputs(), env=env)
        self.assertEqual(proc.returncode, ru.EXIT_REFUSED, proc.stderr)
        self.assertEqual(doc['checks']['P.entrypoint']['status'], 'FAIL')
        self.assertIn('django', doc['checks']['P.entrypoint']['imported_too_early'])

    def test_unacceptable_inputs_are_refused_and_nothing_else_runs(self):
        bad = self.dir / 'bad.json'
        bad.write_text(json.dumps(_inputs(originals={'orders': {U1: {}}})))
        proc, doc = self.run_bootstrap([sys.executable, '-m', ru.BOOTSTRAP_MODULE], bad)
        self.assertEqual(proc.returncode, ru.EXIT_REFUSED)
        self.assertEqual(sorted(doc['checks']), ['P.inputs'])
        self.assertEqual(doc['run']['refused_before'], 'any other check (the inputs are not acceptable)')

    def test_manage_py_refuses_and_is_not_presented_as_pre_start_protection(self):
        env = dict(os.environ, DJANGO_SETTINGS_MODULE='dinify_backend.test_settings')
        proc = subprocess.run([sys.executable, 'manage.py', ru.COMMAND], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, ru.EXIT_REFUSED, proc.stderr)
        self.assertIn('cannot provide pre-start protection', proc.stderr)

    def test_the_help_documents_the_invocation(self):
        proc = subprocess.run([sys.executable, '-m', ru.BOOTSTRAP_MODULE, '--help'], cwd=ROOT,
                              env=_clean_env(PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        for text in ('--inputs', '--result', '--nonce', ru.INPUTS_SCHEMA, 'There is no global PASS'):
            self.assertIn(text, proc.stdout)


class IsolatedProfileTests(SimpleTestCase):
    """The successful bootstrap-to-adapter path in a REAL isolated profile.

    Needs what CI cannot provide: a network namespace with no interface up, a restored
    database behind a Unix socket with the read-only role, a read-only media mount and
    a clean checkout at the pinned revision. It runs only when
    ``RESTORE_USABILITY_PROFILE_INPUTS`` names a prepared inputs document. The in-process
    hand-off (``ReaderRoleTests``) covers the adapter in CI.
    """

    def test_the_supported_bootstrap_reaches_the_adapter_in_an_isolated_profile(self):
        inputs = os.environ.get('RESTORE_USABILITY_PROFILE_INPUTS')
        if not inputs:
            self.skipTest('needs a prepared isolated profile (RESTORE_USABILITY_PROFILE_INPUTS)')
        with tempfile.TemporaryDirectory() as d:
            result = Path(d) / 'result.json'
            proc = subprocess.run([sys.executable, '-m', ru.BOOTSTRAP_MODULE, '--inputs', inputs,
                                   '--result', str(result), '--nonce', NONCE], cwd=ROOT,
                                  env=_clean_env(), capture_output=True, text=True, timeout=900)
            doc = json.loads(result.read_text())
        self.assertEqual(doc['run']['status'], 'COMPLETED', proc.stdout + proc.stderr)
        self.assertEqual(doc['run']['phase'], 'done')
        self.assertTrue(ru.preconditions_satisfied(doc['checks']))
        self.assertEqual(proc.returncode, doc['exit'])


# ============================================================================ BOUNDED EXECUTION (subprocess)

SUPERVISE_SCRIPT = r'''
import json, os, signal, subprocess, sys, time
from misc_app import restore_usability as ru
assert not [m for m in sys.modules if m.split('.')[0] == 'django'], 'the module imported Django'
result = ru.new_result('0123456789abcdef')
result['checks']['I.reached'] = ru.check('PASS')
mode = sys.argv[1]
child = []
def body():
    if mode == 'refuse':
        result['run']['refused_before'] = 'a test refusal'
    elif mode == 'crash':
        raise ValueError('SECRET-CRASH-DETAIL')
    elif mode == 'child':
        child.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']).pid)
        time.sleep(60)
    elif mode in ('timeout', 'cancel'):
        print('READY', flush=True)
        time.sleep(60)
if mode == 'late':
    real_close = ru.close_resources
    def close_with_a_signal():
        os.kill(os.getpid(), signal.SIGTERM)          # a cancellation landing mid-cleanup
        return real_close()
    ru.close_resources = close_with_a_signal
started = time.monotonic()
ru.supervise(result, 0.5 if mode in ('timeout', 'child') else None, body)
out = {'run': result['run'], 'verdicts': result['verdicts'], 'exit': result['exit'],
       'partial': result.get('partial'), 'resources': result['resources'],
       'elapsed': time.monotonic() - started,
       'itimer': signal.getitimer(signal.ITIMER_REAL)[0],
       'handlers_restored': (signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
                             and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
                             and signal.getsignal(signal.SIGINT) is signal.default_int_handler)}
if child:
    try:
        os.kill(child[0], 0)
        out['child_alive'] = True
    except ProcessLookupError:
        out['child_alive'] = False
print('RESULT ' + json.dumps(out), flush=True)
'''


class BoundedExecutionTests(SimpleTestCase):
    """``supervise`` run for real in a fresh process: the deadline, a cancellation
    signal from outside, a crash and a child process."""

    def run_mode(self, mode, cancel=False):
        proc = subprocess.Popen([sys.executable, '-c', SUPERVISE_SCRIPT, mode], cwd=ROOT,
                                env=_clean_env(PYTHONPATH=str(ROOT)), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if cancel:
            self.assertEqual(proc.stdout.readline().strip(), 'READY')
            proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        line = [ln for ln in out.splitlines() if ln.startswith('RESULT ')][-1]
        return json.loads(line[len('RESULT '):]), err

    def assert_interrupted(self, res, status):
        self.assertEqual(res['run']['status'], status)
        self.assertEqual(set(res['verdicts'].values()), {'INTERRUPTED'},
                         'a PASS reached before the interruption must not become a verdict')
        self.assertEqual(res['exit'], ru.EXIT_INTERRUPTED)
        self.assertIs(res['partial'], True)
        self.assertEqual(res['itimer'], 0.0)
        self.assertTrue(res['handlers_restored'])

    def test_CONTROL_a_completed_run_computes_its_verdicts(self):
        res, _ = self.run_mode('complete')
        self.assertEqual((res['run']['status'], res['verdicts']['usable'], res['exit']), ('COMPLETED', 'PASS', 0))
        self.assertTrue(res['handlers_restored'])

    def test_the_deadline_interrupts_and_nothing_partial_is_a_pass(self):
        res, _ = self.run_mode('timeout')
        self.assert_interrupted(res, 'TIMED_OUT')
        self.assertLess(res['elapsed'], 10)

    def test_a_cancellation_signal_interrupts(self):
        res, _ = self.run_mode('cancel', cancel=True)
        self.assert_interrupted(res, 'CANCELLED')

    def test_a_crash_is_reported_without_its_message(self):
        res, err = self.run_mode('crash')
        self.assertEqual((res['run']['status'], res['exit']), ('CRASHED', ru.EXIT_CRASHED))
        self.assertEqual(res['run']['crash']['type'], 'builtins.ValueError')
        self.assertEqual(set(res['verdicts'].values()), {'INTERRUPTED'})
        self.assertNotIn('SECRET-CRASH-DETAIL', json.dumps(res) + err)

    def test_a_signal_during_cleanup_is_recorded_and_never_escapes(self):
        out, _ = self.run_mode('late')
        self.assertEqual(out['run']['status'], 'COMPLETED')
        self.assertEqual(out['run']['late_signals'], ['SIGTERM'])
        self.assertEqual((out['verdicts']['usable'], out['exit']), ('PASS', ru.EXIT_OK))
        self.assertTrue(out['handlers_restored'])

    def test_a_refusal_runs_no_verdict(self):
        res, _ = self.run_mode('refuse')
        self.assertEqual((res['run']['status'], res['exit'], res['partial']), ('REFUSED', ru.EXIT_REFUSED, False))
        self.assertEqual(set(res['verdicts'].values()), {'NOT_RUN'})

    def test_a_child_process_is_reaped_when_the_run_ends(self):
        res, _ = self.run_mode('child')
        self.assert_interrupted(res, 'TIMED_OUT')
        self.assertGreaterEqual(res['resources']['child_processes_reaped'], 1)
        self.assertIs(res['child_alive'], False)


class StatementCancellationTests(TransactionTestCase):
    """The deadline interrupts a query IN THE SERVER, not merely in the client."""

    def test_an_interrupted_query_is_cancelled_on_the_server(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL only')
        result = ru.new_result(NONCE)

        def body():
            with connection.cursor() as cur:
                cur.execute("select pg_sleep(30), 'restore-usability-cancel-probe'")

        started = time.monotonic()
        with mock.patch.object(ru, '_children', return_value=[]):
            ru.supervise(result, 0.5, body)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((result['run']['status'], result['exit']), ('TIMED_OUT', ru.EXIT_INTERRUPTED))
        self.assertEqual(result['resources']['database_connections'], 'closed')
        with connection.cursor() as cur:
            cur.execute("select count(*) from pg_stat_activity where state = 'active' "
                        "and query like '%%restore-usability-cancel-probe%%' and pid <> pg_backend_pid()")
            self.assertEqual(cur.fetchone()[0], 0, 'the interrupted query is still running on the server')


# ============================================================================ APPLICATION READS

def _user(phone):
    from users_app.models import User
    return User.objects.create_user(first_name='A', last_name='B', email=f'{phone}@example.com',
                                    phone_number=phone, username=phone, country='Uganda', password='password',
                                    roles=[])


def _venue(status=RestaurantStatus_Live, phone='256700000971', name='R'):
    from restaurants_app.models import DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table
    owner = _user(phone)
    r = Restaurant.objects.create(name=name, location='l', owner=owner, status=status)
    RestaurantEmployee.objects.create(user=owner, restaurant=r, roles=[RESTAURANT_OWNER], active=True)
    area = DiningArea.objects.create(name='Main', restaurant=r)
    tables = [Table.objects.create(number=n, restaurant=r, dining_area=area, enabled=True, is_active=True,
                                   qr_mode='order_only', has_qr=True) for n in (1, 2)]
    section = MenuSection.objects.create(name='Mains', restaurant=r, approved=True, enabled=True, available=True)
    item = MenuItem.objects.create(name='Rolex', section=section, primary_price=Decimal('10000'), approved=True,
                                   enabled=True, available=True, in_stock=True)
    return SimpleNamespace(owner=owner, restaurant=r, tables=tables, section=section, item=item,
                           rid=str(r.pk))


def _accepted_order(v, table):
    from orders_app.controllers.manage_order import update_order_status
    from orders_app.controllers.services.create_order import _create_order
    from orders_app.controllers.services.order_quote import quote_ref
    from orders_app.models import OrderAcceptance
    res = _create_order(restaurant=v.restaurant, table=table, items=[{'item': str(v.item.pk), 'quantity': 1}],
                        client_order_id=str(uuid.uuid4()))
    order = res['order']
    update_order_status(order, OrderStatus_Pending, None, quote_ref=quote_ref(order))
    order.refresh_from_db()
    return order, OrderAcceptance.objects.get(order=order).quote_ref


def _credential(table):
    from restaurants_app.controllers.diner_capability import issue_qr_credential
    table.refresh_from_db()
    return issue_qr_credential(table.restaurant_id, table.pk, table.qr_version)


def _png(path):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (4, 4), (200, 30, 30)).save(path, 'PNG')


class PublicMenuContractTests(TestCase):

    def read(self, v):
        reads = ru.Reads(None)
        reads.get(f'menu.public.{v.rid}', reads.public, '/api/v1/orders/journey/show-menu/', {'restaurant': v.rid})
        return reads.done

    def test_the_contract_comes_from_the_named_lifecycle_constants(self):
        from restaurants_app.controllers.lifecycle_policy import DINER_MENU_ALLOWED, diner_menu_visibility
        self.assertEqual(diner_menu_visibility(RestaurantStatus_Live), DINER_MENU_ALLOWED)
        for status, http in ((RestaurantStatus_Live, 200), (RestaurantStatus_Onboarding, 200),
                             (RestaurantStatus_Suspended, 503), (RestaurantStatus_Offboarded, 404),
                             ('paused', None)):
            with self.subTest(status):
                self.assertEqual(ru.expected_menu_answer(SimpleNamespace(status=status, deleted=False))[0], http)
        self.assertEqual(ru.expected_menu_answer(SimpleNamespace(status=RestaurantStatus_Live, deleted=True))[0],
                         404)

    def test_CONTROL_live_and_onboarding_menus_are_served_and_meaningful(self):
        for n, status in enumerate((RestaurantStatus_Live, RestaurantStatus_Onboarding)):
            v = _venue(status, phone=f'25670000097{n}')
            with self.subTest(status):
                res = ru.check_public_menu(self.read(v), [v.rid])
                self.assertEqual(res['status'], 'PASS', res)

    def test_CONTROL_suspended_and_offboarded_menus_are_the_exact_refusals(self):
        for n, status in enumerate((RestaurantStatus_Suspended, RestaurantStatus_Offboarded)):
            v = _venue(status, phone=f'25670000098{n}')
            with self.subTest(status):
                res = ru.check_public_menu(self.read(v), [v.rid])
                self.assertEqual(res['status'], 'PASS', res)

    def test_REGRESSION_a_failing_live_menu_fails(self):
        # The rehearsal prototype compared the visibility against literals the policy never
        # returns, so every restaurant read as "not served" and a broken menu passed.
        v = _venue()
        for read in ({'status': 500, 'body': {'_not_json': True}},
                     {'status': 503, 'body': {'status': 503, 'message': 'x'}},
                     {'status': 200, 'body': {'status': 200, 'data': 'not a list'}}):
            with self.subTest(read=read):
                res = ru.check_public_menu({f'menu.public.{v.rid}': read}, [v.rid])
                self.assertEqual(res['status'], 'FAIL')

    def test_a_menu_that_is_not_this_restaurants_published_menu_fails(self):
        v = _venue()
        done = self.read(v)
        body = copy.deepcopy(done[f'menu.public.{v.rid}']['body'])
        body['data'][0]['id'] = U1
        self.assertEqual(ru.check_public_menu({f'menu.public.{v.rid}': {'status': 200, 'body': body}},
                                              [v.rid])['status'], 'FAIL')
        body = copy.deepcopy(done[f'menu.public.{v.rid}']['body'])
        body['data'][0]['item_count'] += 1
        self.assertEqual(ru.check_public_menu({f'menu.public.{v.rid}': {'status': 200, 'body': body}},
                                              [v.rid])['status'], 'FAIL')

    def test_a_suspended_menu_answering_200_fails(self):
        v = _venue(RestaurantStatus_Suspended)
        live = _venue(phone='256700000990')
        res = ru.check_public_menu({f'menu.public.{v.rid}': self.read(live)[f'menu.public.{live.rid}']}, [v.rid])
        self.assertEqual(res['status'], 'FAIL')

    def test_an_unknown_lifecycle_state_fails(self):
        v = _venue()
        type(v.restaurant).objects.filter(pk=v.restaurant.pk).update(status='paused')
        self.assertEqual(ru.check_public_menu(self.read(v), [v.rid])['status'], 'FAIL')

    def test_no_restaurant_is_incomplete(self):
        self.assertEqual(ru.check_public_menu({}, [])['status'], 'INCOMPLETE')

    def test_a_read_whose_view_raises_is_a_failed_read_not_a_crash(self):
        # A read that tried to write under the read-only role raises in the view. The run
        # records the application's own answer and that read's check fails; nothing escapes.
        v = _venue()
        with mock.patch('restaurants_app.endpoints.order_journey.handle_show_menu',
                        side_effect=RuntimeError('boom')), self.assertLogs('django.request', 'ERROR'):
            done = self.read(v)
        read = done[f'menu.public.{v.rid}']
        self.assertEqual((read['status'], read['exception']), (500, 'builtins.RuntimeError'))
        self.assertEqual(ru.check_public_menu(done, [v.rid])['status'], 'FAIL')

    def test_a_discovered_sample_says_how_much_it_left_out(self):
        first, second = _venue(), _venue(phone='256700000981', name='S')
        _accepted_order(first, first.tables[0])
        inputs, _ = ru.validate_inputs(_inputs(limits={'wall_clock_seconds': 60, 'max_restaurants': 1,
                                                       'max_orders_per_restaurant': 1}))
        sample, source = ru.choose_sample(inputs, None)
        self.assertEqual(source, 'discovered')
        self.assertEqual(len(sample['restaurant_ids']), 1)
        self.assertEqual(sample['not_sampled']['restaurants'], 1)
        self.assertIn(sample['restaurant_ids'][0], {first.rid, second.rid})


class IndependentEvidenceTests(TestCase):

    def setUp(self):
        self.v = _venue()
        self.t1, self.t2 = self.v.tables
        self.c1, self.c2 = _credential(self.t1), _credential(self.t2)

    def stickers(self, records):
        inputs, problems = ru.validate_inputs(_inputs(originals={'printed_qr': records}))
        self.assertEqual(problems, [])
        reads = ru.Reads(None)
        plan = ru.read_plan(reads, inputs, {'restaurant_ids': [], 'order_ids': []}, None)
        return ru.check_stickers(records, reads.done, plan['stickers']), reads

    def record(self, table, credential):
        return {'table_id': str(table.pk), 'restaurant_id': self.v.rid, 'credential': credential}

    def test_CONTROL_original_credentials_resolve_to_their_own_tables(self):
        res, _ = self.stickers([self.record(self.t1, self.c1), self.record(self.t2, self.c2)])
        self.assertEqual(res['status'], 'PASS', res)

    def test_REGRESSION_two_valid_stickers_swapped_between_tables_fail(self):
        # Both scans answer 200; the prototype took that as identity.
        res, reads = self.stickers([self.record(self.t1, self.c2), self.record(self.t2, self.c1)])
        self.assertEqual({r['status'] for r in reads.done.values()}, {200})
        self.assertEqual(res['status'], 'FAIL')
        self.assertTrue(all('different table' in v for v in res['tables'].values()), res)

    def test_a_credential_under_another_key_is_refused(self):
        from restaurants_app.controllers.diner_capability import issue_qr_credential
        with override_settings(DINER_CAP_KEY='another-key-0123456789abcdef0123456789abcdef'):
            foreign = issue_qr_credential(self.t1.restaurant_id, self.t1.pk, self.t1.qr_version)
        res, _ = self.stickers([self.record(self.t1, foreign)])
        self.assertEqual(res['status'], 'FAIL')

    def test_an_original_revoked_by_rotation_fails_and_is_never_replaced_by_a_fresh_one(self):
        type(self.t1).objects.filter(pk=self.t1.pk).update(qr_version=self.t1.qr_version + 1)
        res, _ = self.stickers([self.record(self.t1, self.c1)])
        self.assertEqual(res['status'], 'FAIL')

    def test_a_missing_table_fails_and_an_unscannable_one_is_incomplete(self):
        record = {'table_id': U1, 'restaurant_id': self.v.rid, 'credential': self.c1}
        self.assertEqual(self.stickers([record])[0]['status'], 'FAIL')
        type(self.t1).objects.filter(pk=self.t1.pk).update(enabled=False)
        self.assertEqual(self.stickers([self.record(self.t1, self.c1)])[0]['status'], 'INCOMPLETE')

    def test_original_orders(self):
        order, ref = _accepted_order(self.v, self.t1)
        rec = {'order_id': str(order.pk), 'restaurant_id': self.v.rid, 'table_id': str(self.t1.pk),
               'state': 'accepted', 'quote_ref': ref}
        self.assertEqual(ru.check_original_orders([rec])['status'], 'PASS')
        self.assertEqual(ru.check_original_orders([dict(rec, state='exists', quote_ref=None)])['status'], 'PASS')
        for bad in (dict(rec, quote_ref='another'), dict(rec, table_id=str(self.t2.pk)), dict(rec, order_id=U1)):
            with self.subTest(bad=bad):
                self.assertEqual(ru.check_original_orders([bad])['status'], 'FAIL')

    def test_bearer_tokens_are_evidence_about_the_key(self):
        from users_app.customer_access import issue_customer_tokens
        key = settings.SIMPLE_JWT['SIGNING_KEY']
        refresh = issue_customer_tokens(self.v.owner)
        access = str(refresh.access_token)
        claims = jwt.decode(access, key, algorithms=['HS256'])
        expired = jwt.encode(dict(claims, exp=int(time.time()) - 60), key, algorithm='HS256')
        forged = jwt.encode(claims, 'not-the-signing-key-0123456789abcdef', algorithm='HS256')
        self.assertEqual(ru.token_status(access)['usable'], True)
        self.assertEqual({k: ru.token_status(expired)[k] for k in ('key', 'usable')}, {'key': 'PASS', 'usable': False})
        self.assertEqual(ru.token_status(forged)['key'], 'FAIL')
        self.assertTrue(ru.token_status(str(refresh)).get('incomplete'))
        self.assertEqual(ru.token_status('not-a-token')['key'], 'FAIL')

        for token, key_status, bearer in ((access, 'PASS', 'PASS'), (expired, 'PASS', 'UNAVAILABLE'),
                                          (forged, 'FAIL', 'UNAVAILABLE')):
            with self.subTest(bearer=bearer, key=key_status):
                inputs, _ = ru.validate_inputs(_inputs(originals={'staff_bearer_token': token}))
                reads = ru.Reads(None)
                plan = ru.read_plan(reads, inputs, {'restaurant_ids': [], 'order_ids': []}, None)
                res = ru.independent_checks(inputs, reads, plan)
                self.assertEqual((res['H.key_continuity']['status'], res['H.bearer_read']['status']),
                                 (key_status, bearer))
                self.assertEqual(ru.BEARER_READ_KEY in reads.done, bearer == 'PASS',
                                 'only an unexpired original authorises a read')
                self.assertNotIn(token, json.dumps(res))

    def test_nothing_supplied_is_unavailable(self):
        inputs, _ = ru.validate_inputs(_inputs())
        reads = ru.Reads(None)
        plan = ru.read_plan(reads, inputs, {'restaurant_ids': [], 'order_ids': []}, None)
        res = ru.independent_checks(inputs, reads, plan)
        self.assertEqual({c['status'] for c in res.values()}, {'UNAVAILABLE'})


class IntrinsicReadTests(TestCase):

    def setUp(self):
        self.v = _venue()
        self.order, self.ref = _accepted_order(self.v, self.v.tables[0])
        self.sample = {'restaurant_ids': [self.v.rid], 'order_ids': [str(self.order.pk)]}

    def plan(self, principal=True):
        inputs, _ = ru.validate_inputs(_inputs())
        reads = ru.Reads(self.v.owner if principal else None)
        plan = ru.read_plan(reads, inputs, self.sample, self.v.owner if principal else None)
        return reads, plan

    def test_CONTROL_the_restored_reads_agree_with_the_stored_data(self):
        reads, plan = self.plan()
        note = 'in-process principal (JWT verification NOT exercised)'
        self.assertEqual(ru.check_order_reads(reads.done, plan['orders'])['status'], 'PASS')
        self.assertEqual(ru.check_staff_reads(reads.done, plan, note)['status'], 'PASS',
                         ru.check_staff_reads(reads.done, plan, note))
        self.assertEqual(ru.check_kitchen(reads.done, plan, note)['status'], 'PASS',
                         ru.check_kitchen(reads.done, plan, note))
        self.assertEqual(ru.check_readiness(reads.done['readiness'])['status'], 'PASS')

    def test_an_order_read_that_disagrees_with_the_stored_order_fails(self):
        reads, plan = self.plan()
        key = f'order.details.{self.order.pk}'
        for path, value in ((('checkout', 'scope', 'table'), str(self.v.tables[1].pk)),
                            (('checkout', 'acceptance', 'state'), 'not_accepted'),
                            (('checkout', 'acceptance', 'quote_ref'), 'another'),
                            (('id',), U1)):
            with self.subTest(path=path):
                done = copy.deepcopy(reads.done)
                node = done[key]['body']['data']
                for part in path[:-1]:
                    node = node[part]
                node[path[-1]] = value
                self.assertEqual(ru.check_order_reads(done, plan['orders'])['status'], 'FAIL')

    def test_the_intent_selector_must_agree(self):
        reads, plan = self.plan()
        done = copy.deepcopy(reads.done)
        done[f'order.details_by_intent.{self.order.pk}']['status'] = 404
        self.assertEqual(ru.check_order_reads(done, plan['orders'])['status'], 'FAIL')

    def test_an_order_at_an_unscannable_table_is_not_covered_and_none_covered_is_incomplete(self):
        type(self.v.tables[0]).objects.filter(pk=self.v.tables[0].pk).update(enabled=False)
        reads, plan = self.plan()
        res = ru.check_order_reads(reads.done, plan['orders'])
        self.assertEqual(res['status'], 'INCOMPLETE')
        self.assertIn('NOT_COVERED', res['orders'][str(self.order.pk)])

    def test_a_staff_or_kitchen_read_that_disagrees_fails(self):
        reads, plan = self.plan()
        note = 'in-process principal (JWT verification NOT exercised)'
        done = copy.deepcopy(reads.done)
        done[f'staff.sections.{self.v.rid}']['body']['data']['records'][0]['id'] = U1
        self.assertEqual(ru.check_staff_reads(done, plan, note)['status'], 'FAIL')
        done = copy.deepcopy(reads.done)
        done[f'kitchen.state.{self.order.pk}']['body']['data']['fulfilment_revision'] += 1
        self.assertEqual(ru.check_kitchen(done, plan, note)['status'], 'FAIL')

    def test_no_principal_is_unavailable_and_labelled(self):
        reads, plan = self.plan(principal=False)
        self.assertEqual(ru.check_staff_reads(reads.done, plan, 'no staff principal supplied')['status'],
                         'UNAVAILABLE')
        self.assertFalse(any(k.startswith(('staff.', 'kitchen.')) for k in reads.done))

    def test_a_principal_is_held_to_the_customer_refusals(self):
        user_id = str(self.v.owner.pk)
        self.assertEqual(ru.resolve_principal({'staff_principal': {'user_id': user_id}})[0], self.v.owner)
        type(self.v.owner).objects.filter(pk=user_id).update(is_active=False)
        self.assertIsNone(ru.resolve_principal({'staff_principal': {'user_id': user_id}})[0])
        self.assertIsNone(ru.resolve_principal({'staff_principal': {'user_id': U1}})[0])

    def test_the_declared_transformations_remove_only_the_scan_token(self):
        inputs, _ = ru.validate_inputs(_inputs(originals={'printed_qr': [
            {'table_id': str(self.v.tables[0].pk), 'restaurant_id': self.v.rid,
             'credential': _credential(self.v.tables[0])}]}))
        reads = ru.Reads(None)
        ru.read_plan(reads, inputs, self.sample, None)
        executed, applied = ru.identity_reads(reads)
        scan = f'qr.scan.{self.v.tables[0].pk}'
        self.assertIn('session_token', reads.done[scan]['body']['data'])
        self.assertNotIn('session_token', executed[scan]['body']['data'])
        self.assertIn(f'{scan}:data.session_token', applied)
        self.assertEqual(sorted(k.split('.')[0] for k in executed), ['order', 'order', 'qr'])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='restore-usability-media-'))
class MediaReferenceTests(TestCase):

    def setUp(self):
        self.v = _venue()
        _png(Path(settings.MEDIA_ROOT) / 'menu_items' / 'dish.png')
        type(self.v.item).objects.filter(pk=self.v.item.pk).update(image='menu_items/dish.png')

    def media(self):
        reads = ru.Reads(None)
        reads.get(f'menu.public.{self.v.rid}', reads.public, '/api/v1/orders/journey/show-menu/',
                  {'restaurant': self.v.rid})
        return ru.check_media(ru.media_references([self.v.rid], 500), reads.done)

    def test_CONTROL_a_referenced_image_reads_decodes_and_serves(self):
        res = self.media()
        self.assertEqual(res['I.media_references']['status'], 'PASS', res)
        self.assertEqual(res['I.app_media_paths_are_db_references']['status'], 'PASS', res)

    def test_a_missing_or_corrupt_object_fails(self):
        path = Path(settings.MEDIA_ROOT) / 'menu_items' / 'dish.png'
        path.write_bytes(b'not an image')
        self.assertEqual(self.media()['I.media_references']['status'], 'FAIL')
        path.unlink()
        self.assertEqual(self.media()['I.media_references']['status'], 'FAIL')

    def test_a_sample_with_no_media_is_incomplete_and_a_truncated_one_never_passes(self):
        self.assertEqual(ru.check_media({'refs': [], 'truncated': False}, {})['I.media_references']['status'],
                         'INCOMPLETE')
        refs = ru.media_references([self.v.rid], 500)
        refs['truncated'] = True
        self.assertEqual(ru.check_media(refs, {})['I.media_references']['status'], 'INCOMPLETE')


# ============================================================================ POSTGRESQL ROLE + HAND-OFF


class _UnixSocketForwarder:
    """A Unix-socket listener in front of the test cluster's TCP port.

    The supported profile reaches the restored database through a Unix socket, and
    ``P.target_identity`` checks exactly that. CI's PostgreSQL is a TCP service, so the
    fresh-process test puts this forwarder in front of it: libpq connects to a real
    ``.s.PGSQL.<port>`` socket and the check runs unaltered. It carries bytes and nothing
    else, and opens no database session of its own.
    """

    def __init__(self, tcp_host, tcp_port):
        base = '/tmp' if os.path.isdir('/tmp') else None      # socket paths are length-limited
        self.directory = tempfile.mkdtemp(prefix='ru-sock-', dir=base)
        self.port = str(tcp_port)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(os.path.join(self.directory, f'.s.PGSQL.{self.port}'))
        self.server.listen(8)
        self.upstream = (tcp_host or 'localhost', int(tcp_port))
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.upstream, timeout=10)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self):
        self.server.close()
        subprocess.run(['rm', '-rf', self.directory])


# The pre-start observations CI's runner cannot satisfy: a clean checkout at a pinned
# revision, the certified interpreter, a network namespace with no interface up and a
# read-only media mount. Each has its own observer tests above and is observed for real in
# the isolated-profile run. Here the REAL observer still runs (before Django, asserted),
# and only its verdict is substituted and labelled in the result.
FRESH_PROCESS_DRIVER = """
import sys
from types import SimpleNamespace
import misc_app.restore_usability as ru

LABEL = ('SUBSTITUTED by the CI driver: this runner cannot provide the condition; the real '
         'observer ran before Django and its verdict was replaced')


def stand_in(name):
    real = getattr(ru, name)

    def observe(*args, **kwargs):
        assert 'django' not in sys.modules, name + ' ran after Django was imported'
        observed = real(*args, **kwargs)
        kept = {k: observed[k] for k in ('revision',) if k in observed}   # the checkout's real HEAD
        return ru.check('PASS', substituted=LABEL, observed_status=observed['status'], **kept)
    return observe


for name in ('observe_source_identity', 'observe_runtime', 'observe_outbound_denied', 'observe_media_readonly'):
    setattr(ru, name, stand_in(name))

# The ENTRY observation stays real except for the one fact a driver changes: __main__ is
# this script rather than the module. Its import check sees exactly what was imported.
real_entry = ru.observe_entry
main = SimpleNamespace(__spec__=SimpleNamespace(name=ru.BOOTSTRAP_MODULE))
ru.observe_entry = lambda settings_module, modules=None: real_entry(settings_module, modules=modules,
                                                                     main_module=main)
sys.exit(ru.main(sys.argv[1:]))
"""

class _Roles:
    """Disposable login roles on the test cluster, dropped on cleanup."""

    def __init__(self, case):
        self.case, self.created = case, []
        self.db = connection.settings_dict['NAME']

    def make(self, *, stats=True, read_only=True, grant_insert_on=None):
        name = f'restore_ru_{uuid.uuid4().hex[:10]}'
        password = uuid.uuid4().hex
        with connection.cursor() as c:
            c.execute(f'create role {name} login password %s', [password])
            c.execute(f'grant pg_read_all_data to {name}')
            if stats:
                c.execute(f'grant pg_read_all_stats to {name}')
            if read_only:
                c.execute(f'alter role {name} set default_transaction_read_only = on')
            if grant_insert_on:
                c.execute(f'grant insert on {grant_insert_on} to {name}')
        self.created.append(name)
        return name, password

    def drop(self):
        with connection.cursor() as c:
            for name in self.created:
                c.execute(f"select pg_terminate_backend(pid) from pg_stat_activity where usename = '{name}'")
                c.execute(f'drop owned by {name}')
                c.execute(f'drop role if exists {name}')


class ReaderRoleTests(TransactionTestCase):
    """``P.db_readonly`` and ``P.db_exclusive`` against roles the SERVER enforces, then the
    full bootstrap-to-adapter hand-off, in process, with the read-only role."""

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL only')
        self.roles = _Roles(self)
        self.original = dict(connection.settings_dict)
        self.addCleanup(self.roles.drop)
        self.addCleanup(self.restore_superuser)

    def restore_superuser(self):
        connection.close()
        connection.settings_dict.update(USER=self.original['USER'], PASSWORD=self.original['PASSWORD'])

    def become(self, name, password):
        connection.close()
        connection.settings_dict.update(USER=name, PASSWORD=password)

    def test_CONTROL_the_reader_role_is_prevention_in_two_layers(self):
        name, password = self.roles.make()
        self.become(name, password)
        res = ru.check_db_readonly(connection)
        self.assertEqual(res['status'], 'PASS', res)
        from django.db import DatabaseError
        for sql in ("update restaurants set name = name",
                    "set default_transaction_read_only = off; update restaurants set name = name",
                    "select nextval(pg_get_serial_sequence('django_migrations', 'id'))"):
            with self.subTest(sql), self.assertRaises(DatabaseError) as ctx:
                with connection.cursor() as c:
                    for stmt in sql.split('; '):
                        c.execute(stmt)
            self.assertIn(getattr(ctx.exception.__cause__, 'sqlstate', None), ('25006', '42501'))
            connection.close()

    def test_a_superuser_or_a_role_that_can_write_is_refused(self):
        self.assertEqual(ru.check_db_readonly(connection)['status'], 'FAIL')
        for kwargs in ({'read_only': False}, {'grant_insert_on': 'restaurants'}):
            with self.subTest(kwargs):
                name, password = self.roles.make(**kwargs)
                self.become(name, password)
                try:
                    self.assertEqual(ru.check_db_readonly(connection)['status'], 'FAIL')
                finally:
                    self.restore_superuser()

    def test_exclusivity_requires_seeing_every_session(self):
        import psycopg
        other = psycopg.connect(dbname=self.original['NAME'], user=self.original['USER'],
                                password=self.original['PASSWORD'], host=self.original['HOST'],
                                port=self.original['PORT'])
        self.addCleanup(other.close)
        blind, blind_pw = self.roles.make(stats=False)
        seeing, seeing_pw = self.roles.make()
        self.become(blind, blind_pw)
        res = ru.check_db_exclusive(connection)
        self.assertEqual(res['status'], 'FAIL')
        self.assertIn('not visible', res['reason'])
        self.become(seeing, seeing_pw)
        self.assertEqual(ru.check_db_exclusive(connection)['other_client_sessions'], 1)
        other.close()
        deadline = time.monotonic() + 10           # the server reaps the closed backend shortly
        while ru.check_db_exclusive(connection)['status'] != 'PASS' and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(ru.check_db_exclusive(connection)['status'], 'PASS')

    # ------------------------------------------------------------------ the hand-off

    def seeded_result(self):
        """Pre-start results are SEEDED here: each has its own tests above, and CI cannot
        build the isolated profile they observe. Everything after the hand-off is real."""
        result = ru.new_result(NONCE)
        for name in ('P.inputs', 'P.entrypoint', 'P.runtime', 'P.outbound_denied', 'P.media_readonly',
                     'P.target_identity', 'P.signing_keys_present'):
            result['checks'][name] = ru.check('PASS', seeded_for_the_in_process_test=True)
        result['checks']['P.source_identity'] = ru.check('PASS', revision=REV, seeded_for_the_in_process_test=True)
        result['checks']['P.attestations'] = ru.observe_attestations(_inputs()['attestations'])
        return result

    @contextlib.contextmanager
    def inert_mongo(self):
        """The REAL Mongo module under the bootstrap's control, in place of the test mock.

        The connection target is read when the client is first used, not at import, so the
        environment stays set for the whole block — as the bootstrap leaves it set for the
        whole run.
        """
        path = ROOT / 'dinify_backend' / 'mongo_db.py'
        spec = importlib.util.spec_from_file_location('dinify_backend.mongo_db', path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {'MONGO_HOST': ru.INERT_MONGO_URI}):
            spec.loader.exec_module(module)
            with mock.patch.dict(sys.modules, {'dinify_backend.mongo_db': module}):
                yield module

    def fixture(self, media_root):
        from users_app.customer_access import issue_customer_tokens
        v = _venue()
        order, ref = _accepted_order(v, v.tables[0])
        _png(Path(media_root) / 'menu_items' / 'dish.png')
        type(v.item).objects.filter(pk=v.item.pk).update(image='menu_items/dish.png')
        originals = {
            'printed_qr': [{'table_id': str(t.pk), 'restaurant_id': v.rid, 'credential': _credential(t)}
                           for t in v.tables],
            'orders': [{'order_id': str(order.pk), 'restaurant_id': v.rid, 'table_id': str(v.tables[0].pk),
                        'state': 'accepted', 'quote_ref': ref}],
            'staff_bearer_token': str(issue_customer_tokens(v.owner).access_token)}
        return v, order, originals

    def backup_time_manifest(self, inputs, v, order, media_root):
        """What a backup-time capture records, made by the same reads as superuser."""
        sample = {'restaurant_ids': [v.rid], 'order_ids': [str(order.pk)]}
        reads = ru.Reads(v.owner)
        ru.read_plan(reads, inputs, sample, v.owner)
        golden, _ = ru.identity_reads(reads)
        return {'schema': ru.MANIFEST_SCHEMA, 'source_revision': REV,
                'declared_transformations': copy.deepcopy(ru.DECLARED_TRANSFORMATIONS), 'sample': sample,
                'tables': ru.table_digests(connection), 'media_listing': ru.media_listing(media_root),
                'golden_reads': golden}

    def hand_off(self, inputs, manifest):
        result = self.seeded_result()
        proof = ru.issue_proof(result, inputs, manifest, time.monotonic() + 300)
        call_command(ru.COMMAND, bootstrap=proof)
        # The status supervise() would record for a body that returned normally.
        result['run']['status'] = 'REFUSED' if result['run'].get('refused_before') else 'COMPLETED'
        ru.finalise(result)
        return result, proof

    def test_the_full_hand_off_passes_with_the_reader_role(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root), \
                self.inert_mongo():
            v, order, originals = self.fixture(media_root)
            inputs, problems = ru.validate_inputs(_inputs(originals=originals,
                                                          staff_principal={'user_id': str(v.owner.pk)},
                                                          require=['usable', 'independent', 'identity']))
            self.assertEqual(problems, [])
            inputs['manifest'] = {'path': '/manifest.json', 'sha256': '0' * 64}
            manifest = self.backup_time_manifest(inputs, v, order, media_root)
            self.assertEqual(ru.validate_manifest(manifest, inputs['limits']), [])

            name, password = self.roles.make()
            self.become(name, password)
            result, proof = self.hand_off(inputs, manifest)

        checks = result['checks']
        not_pass = {k: c for k, c in checks.items() if c['status'] not in ('PASS', 'ATTESTED', 'OBSERVED')}
        self.assertEqual(not_pass, {}, json.dumps(not_pass, indent=1, default=str))
        self.assertEqual(result['run']['phase'], 'done')
        self.assertEqual(result['verdicts'], {'usable': 'PASS', 'independent': 'PASS', 'identity': 'PASS'})
        self.assertEqual(result['exit'], ru.EXIT_OK)
        self.assertEqual(result['sample']['source'], 'manifest')
        self.assertEqual(checks['P.db_readonly']['role'], name)
        self.assertTrue(result['observations']['O.database_unchanged']['sequences_equal'])
        self.assertEqual(result['observations']['O.mail_captured']['messages'], 0)
        self.assertIn('JWT verification NOT exercised', result['authorisation']['staff_reads'])
        text = json.dumps(result, default=str)
        for secret in [r['credential'] for r in originals['printed_qr']] + [originals['staff_bearer_token']]:
            self.assertNotIn(secret, text)
        with self.assertRaises(CommandError):
            call_command(ru.COMMAND, bootstrap=proof)

    def test_the_supported_bootstrap_hands_over_and_completes_in_a_fresh_process(self):
        """The REAL bootstrap -> settings -> django.setup() -> handover -> adapter, in a new
        interpreter, against the read-only role over a Unix socket. Four environment
        observations are substituted and labelled (see FRESH_PROCESS_DRIVER); every other
        precondition — the entry's import check, the inputs, the target identity, the
        signing keys, both inert controls, the role and exclusivity — runs for real."""
        work = Path(tempfile.mkdtemp(prefix='ru-fresh-'))
        self.addCleanup(subprocess.run, ['rm', '-rf', str(work)])
        media_root = work / 'media'
        media_root.mkdir()
        with override_settings(MEDIA_ROOT=str(media_root)), self.inert_mongo():
            v, order, originals = self.fixture(str(media_root))
            draft, problems = ru.validate_inputs(_inputs(originals=originals,
                                                         staff_principal={'user_id': str(v.owner.pk)},
                                                         require=['usable', 'independent', 'identity']))
            self.assertEqual(problems, [])
            draft['manifest'] = {'path': '/manifest.json', 'sha256': '0' * 64}
            manifest = self.backup_time_manifest(draft, v, order, str(media_root))
        # R.source compares the manifest's revision with the checkout's REAL HEAD, which the
        # substituted P.source_identity still reports (only its cleanliness verdict is replaced).
        head = ru._run_git(ROOT, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.strip()
        manifest['source_revision'] = head
        manifest_bytes = json.dumps(manifest, default=str).encode()
        (work / 'manifest.json').write_bytes(manifest_bytes)

        name, password = self.roles.make()
        forwarder = _UnixSocketForwarder(self.original['HOST'], self.original['PORT'] or 5432)
        self.addCleanup(forwarder.close)
        (work / 'ru_fresh_settings.py').write_text(
            'from dinify_backend.test_settings import *  # noqa: F401,F403\n'
            'import sys as _sys\n'
            "_sys.modules.pop('dinify_backend.mongo_db', None)   # the real module, not the test mock\n"
            f"DATABASES = {{'default': dict(DATABASES['default'], NAME={self.original['NAME']!r}, "
            f"USER={name!r}, PASSWORD={password!r}, HOST={forwarder.directory!r}, PORT={forwarder.port!r})}}\n"
            f'MEDIA_ROOT = {str(media_root)!r}\n')
        (work / 'driver.py').write_text(FRESH_PROCESS_DRIVER)
        doc = _inputs(target={'settings_module': 'ru_fresh_settings',
                              'database': {'name': self.original['NAME'], 'user': name,
                                           'host': forwarder.directory, 'port': forwarder.port},
                              'media_root': str(media_root)},
                      release={'source_revision': head},
                      limits={'wall_clock_seconds': 300}, originals=originals,
                      staff_principal={'user_id': str(v.owner.pk)},
                      manifest={'path': str(work / 'manifest.json'), 'sha256': ru.sha256(manifest_bytes)},
                      require=['usable', 'independent', 'identity'])
        (work / 'inputs.json').write_text(json.dumps(doc))

        connections.close_all()          # the only other session on the target is this runner's
        result_path = work / 'result.json'
        proc = subprocess.run([sys.executable, str(work / 'driver.py'), '--inputs', str(work / 'inputs.json'),
                               '--result', str(result_path), '--nonce', NONCE], cwd=ROOT,
                              env=_clean_env(PYTHONPATH=os.pathsep.join([str(work), str(ROOT)])),
                              capture_output=True, text=True, timeout=600)
        result = json.loads(result_path.read_text())

        checks = result['checks']
        self.assertEqual(result['run']['status'], 'COMPLETED', proc.stdout + proc.stderr)
        self.assertEqual(result['run']['phase'], 'done')
        substituted = sorted(k for k, c in checks.items() if 'substituted' in c)
        self.assertEqual(substituted, ['P.media_readonly', 'P.outbound_denied', 'P.runtime', 'P.source_identity'])
        for real in ('P.inputs', 'P.entrypoint', 'P.target_identity', 'P.signing_keys_present',
                     'P.providers_inert', 'P.db_readonly', 'P.db_exclusive'):
            with self.subTest(real):
                self.assertEqual(checks[real]['status'], 'PASS', checks[real])
                self.assertNotIn('substituted', checks[real])
        self.assertEqual(checks['P.db_readonly']['role'], name)
        self.assertEqual(checks['P.attestations']['status'], 'ATTESTED')
        not_pass = {k: c for k, c in checks.items() if c['status'] not in ('PASS', 'ATTESTED', 'OBSERVED')}
        self.assertEqual(not_pass, {}, json.dumps(not_pass, indent=1, default=str))
        self.assertEqual(set(result['controls']), {'mongo', 'settings_module', 'email'})
        self.assertEqual(checks['R.source']['running'], head)
        self.assertEqual(result['verdicts'], {'usable': 'PASS', 'independent': 'PASS', 'identity': 'PASS'})
        self.assertEqual(result['exit'], ru.EXIT_OK)
        self.assertEqual(proc.returncode, result['exit'])
        self.assertEqual(json.loads(proc.stdout)['exit'], ru.EXIT_OK)
        for text in (proc.stdout, proc.stderr, result_path.read_text()):
            for secret in [r['credential'] for r in originals['printed_qr']] + [originals['staff_bearer_token'],
                                                                                 password]:
                self.assertNotIn(secret, text)

    def test_a_restore_that_differs_from_its_manifest_fails_identity(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root), \
                self.inert_mongo():
            v, order, originals = self.fixture(media_root)
            inputs, _ = ru.validate_inputs(_inputs(staff_principal={'user_id': str(v.owner.pk)}))
            inputs['manifest'] = {'path': '/manifest.json', 'sha256': '0' * 64}
            manifest = self.backup_time_manifest(inputs, v, order, media_root)
            # The restore lost a dish name and a media object after the backup was recorded.
            type(v.item).objects.filter(pk=v.item.pk).update(name='Renamed')
            (Path(media_root) / 'menu_items' / 'dish.png').unlink()
            name, password = self.roles.make()
            self.become(name, password)
            result, _ = self.hand_off(inputs, manifest)
        self.assertEqual(result['checks']['R.tables']['status'], 'FAIL')
        self.assertIn('menu_items', result['checks']['R.tables']['differing'])
        self.assertEqual(result['checks']['R.media_set']['status'], 'FAIL')
        self.assertEqual(result['checks']['I.media_references']['status'], 'FAIL')
        self.assertEqual(result['verdicts']['identity'], 'FAIL')

    def test_the_hand_off_refuses_before_any_read_when_a_post_setup_control_fails(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            v, _order, _ = self.fixture(media_root)
            inputs, _ = ru.validate_inputs(_inputs(staff_principal={'user_id': str(v.owner.pk)}))
            name, password = self.roles.make()
            self.become(name, password)
            # The test settings replace the Mongo module with a mock that answers: not inert.
            result, _ = self.hand_off(inputs, None)
        self.assertEqual(result['checks']['P.providers_inert']['status'], 'FAIL')
        self.assertEqual(result['checks']['P.db_readonly']['status'], 'PASS')
        self.assertEqual(result['run']['refused_before'], 'any application read')
        self.assertNotIn('sample', result)
        self.assertFalse(any(k.startswith(('I.', 'H.', 'R.')) for k in result['checks']))
