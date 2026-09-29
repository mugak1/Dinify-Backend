"""
D11 B2-C — OTP collection: who asked for a code, what happened to it, and which codes
were guessed wrong.

This slice COLLECTS EVIDENCE ONLY. It adds no rate limit, no shadow decision and no
refusal, and nothing here should be read as closing D11. What is pinned:

§1  THE LEDGER'S SHAPE. Two tables, closed vocabularies enforced by the database, no raw
    phone, email, code, hash, salt, purpose or message anywhere in a row, keys that are
    either a User UUID or a versioned, domain-separated HMAC of a canonical phone.
§2  ISSUANCE. Every ``make_otp`` that gets as far as writing a challenge records one
    ``pending`` row IN THE SAME TRANSACTION as the challenge, and that transaction
    COMMITS BEFORE any sender is entered. A recording failure rolls the challenge back
    and sends nothing.
§3  FINALIZATION. One conditional transition out of ``pending``, per environment and
    sender outcome, and never able to change what ``make_otp`` returns.
§4  ORIGIN. Server-owned, set by the caller that knows why it asked, and pinned through
    the REAL endpoints.
§5  WRONG CODES. Recorded only when a code was actually compared, inside a savepoint
    whose failure costs the security counters nothing, and never absorbing a lost
    connection.
§6  CONCURRENCY (PostgreSQL, real connections). The issuance takes the owner's ``users``
    row FOR KEY SHARE before it deletes the challenge it replaces, which is what keeps a
    replacement from deadlocking an owner-claim redemption in either arrival order.
§7  CLEANUP. Strictly older than seven days, any state, oldest first, bounded; the exact
    boundary is kept. The operator command, run through the real command line, prints
    only committed counts, calls a failed statement's outcome unknown, exits non-zero
    with no database text, and says "complete" only after a fresh check. The REAL
    owner-claim redemption keeps both counters and its ordinary refusal when the
    wrong-code observation fails, and cleanup overlapping finalization on separate
    connections keeps strict eligibility, finite work and no resurrection.

Every test stubs the SMS sender and the e-mail sender, and replaces the notification
thread with a synchronous stand-in wherever the environment would start one — ``ENV=dev``
does not suppress e-mail on its own.
"""
import ast
import contextlib
import datetime
import hashlib
import hmac
import io
import logging
import pathlib
import re
import threading
import time
import types
import uuid
from unittest import mock

import psycopg
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import (
    DatabaseError, IntegrityError, OperationalError, connection, connections,
    transaction,
)
from django.db.models.signals import post_save
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from users_app.controllers import otp_manager
from users_app.controllers.otp_manager import OtpManager
from users_app.models import User, UserOtp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

LOGIN_URL = '/api/v1/users/auth/login/'
RESEND_URL = '/api/v1/users/auth/resend-otp/'
VERIFY_URL = '/api/v1/users/auth/verify-otp/'
INITIATE_RESET_URL = '/api/v1/users/auth/initiate-reset-password/'
CHALLENGE_URL = '/api/v1/users/owner-claim/challenge/'
REDEEM_URL = '/api/v1/users/owner-claim/redeem/'
PASSWORD = 'Accounting-Pass-8812'
DEV_OTP = '1234'
WAIT = 30

_POSTGRES = connections['default'].vendor == 'postgresql'

# A phone range distinct from every other suite.
_PHONES = iter(f'2567013{n:05d}' for n in range(40000, 49999))

KEY_RE = re.compile(
    r'^(u:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|p1:[0-9a-f]{64})$'
)


def acct():
    """The module under test, imported lazily so the file loads on the baseline."""
    from users_app import otp_accounting
    return otp_accounting


def Issuance():
    from users_app.models import OtpIssuance
    return OtpIssuance


def Failure():
    from users_app.models import OtpVerificationFailure
    return OtpVerificationFailure


def _env(value):
    def _cfg(key, **kwargs):
        if key == 'ENV':
            return value
        return kwargs.get('default')
    return _cfg


class _SyncThread:
    """Runs the notification target on ``start()``, inside the test's patches."""
    started = []

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target
        self.daemon = daemon

    def start(self):
        _SyncThread.started.append(self.daemon)
        self._target()


class Senders:
    """
    Stub every outbound channel ``make_otp`` can reach, for the duration of a ``with``.

    ``sms`` and ``email`` are the return values (or exceptions) to produce; the mocks
    are exposed so a test can see what was attempted.
    """

    def __init__(self, env='dev', sms=True, email=True):
        self.env, self.sms_result, self.email_result = env, sms, email
        self._patches = []

    def __enter__(self):
        _SyncThread.started = []
        self.sms = mock.Mock(
            side_effect=self.sms_result if isinstance(self.sms_result, BaseException)
            else None,
            return_value=self.sms_result,
        )
        messenger = mock.Mock()
        messenger.send_email.side_effect = (
            self.email_result if isinstance(self.email_result, BaseException) else None
        )
        messenger.send_email.return_value = self.email_result
        self.email = messenger.send_email
        self._patches = [
            mock.patch.object(otp_manager, 'config', side_effect=_env(self.env)),
            mock.patch.object(otp_manager, 'send_sms', self.sms),
            mock.patch.object(otp_manager, 'Messenger', return_value=messenger),
            mock.patch.object(
                otp_manager, 'threading', types.SimpleNamespace(Thread=_SyncThread),
            ),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


def _user(phone=None, email=None, **extra):
    phone = phone or next(_PHONES)
    return User.objects.create_user(
        first_name='Acc', last_name='Ount', email=email, phone_number=phone,
        username=phone, country='UG', password=PASSWORD, roles=[], **extra,
    )


def _owner(phone=None, email=None):
    from restaurants_app.models import Restaurant, RestaurantEmployee
    user = _user(phone, email)
    restaurant = Restaurant.objects.create(
        name=f'Accounting {user.phone_number}', location='loc',
        status=RestaurantStatus_Live, owner=user,
    )
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER],
    )
    return user


def _pepper():
    return otp_manager._otp_pepper()


def _expected_phone_key(canonical):
    return 'p1:' + hmac.new(
        _pepper(), b'dinify:otp-accounting:phone:v1\x00' + canonical.encode(),
        hashlib.sha256,
    ).hexdigest()


def _row_values(row):
    return [getattr(row, f.attname) for f in row._meta.concrete_fields]


# ═══════════════════════════════════════════════════════════════════════════════
# §1 — the ledger's shape
# ═══════════════════════════════════════════════════════════════════════════════

class LedgerShapeTests(TestCase):

    def test_the_origin_vocabulary_is_exactly_six_and_server_owned(self):
        self.assertEqual(
            set(acct().ORIGINS),
            {'password_login', 'reset_initiation', 'owner_claim_challenge',
             'login_resend', 'resend_request', 'unattributed'},
        )
        self.assertEqual(
            set(acct().FAILURE_ORIGINS), set(acct().ORIGINS) | {'unrecorded'},
        )
        self.assertEqual(
            set(acct().STATES), {'pending', 'accepted', 'unknown', 'not_dispatched'},
        )

    def test_the_columns_are_exactly_the_contract(self):
        self.assertEqual(
            {f.name for f in Issuance()._meta.concrete_fields},
            {'id', 'subject_key', 'destination_key', 'origin', 'state',
             'created_at', 'finalized_at'},
        )
        self.assertEqual(
            {f.name for f in Failure()._meta.concrete_fields},
            {'id', 'failed_at', 'subject_key', 'origin', 'bound_redemption',
             'issuance_id'},
        )

    def test_nothing_is_a_foreign_key(self):
        for model in (Issuance(), Failure()):
            for field in model._meta.concrete_fields:
                self.assertFalse(field.is_relation, f'{model.__name__}.{field.name}')

    def test_there_is_no_raw_purpose_column(self):
        for model in (Issuance(), Failure()):
            names = {f.name for f in model._meta.concrete_fields}
            for forbidden in ('purpose', 'msisdn', 'phone', 'email', 'otp', 'otp_hash',
                              'salt', 'message', 'ip', 'identifier', 'user'):
                self.assertNotIn(forbidden, names, model.__name__)

    def test_the_database_refuses_an_unknown_origin_or_state(self):
        for field, value in (('origin', 'somewhere'), ('state', 'delivered')):
            with self.subTest(field), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    Issuance().objects.create(
                        id=uuid.uuid4(), subject_key=None, destination_key=None,
                        **{'origin': 'unattributed', 'state': 'pending', field: value},
                    )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Failure().objects.create(origin='somewhere')

    def test_pending_if_and_only_if_unfinalized(self):
        cases = (
            ('pending', timezone.now()),
            ('accepted', None),
            ('unknown', None),
            ('not_dispatched', None),
        )
        for state, finalized_at in cases:
            with self.subTest(state=state), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    Issuance().objects.create(
                        id=uuid.uuid4(), origin='unattributed', state=state,
                        finalized_at=finalized_at,
                    )
        # Both lawful shapes insert.
        Issuance().objects.create(id=uuid.uuid4(), origin='unattributed')
        Issuance().objects.create(
            id=uuid.uuid4(), origin='unattributed', state='accepted',
            finalized_at=timezone.now(),
        )

    def test_a_raw_phone_can_never_be_stored_as_a_key(self):
        """The database, not only the writer, refuses anything but a key."""
        for key in ('256701234567', '+256701234567', 'u:not-a-uuid',
                    'p1:' + 'A' * 64, 'p2:' + 'a' * 64, 'someone@example.test', ''):
            with self.subTest(key):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Issuance().objects.create(
                            id=uuid.uuid4(), origin='unattributed', subject_key=key,
                        )
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Issuance().objects.create(
                            id=uuid.uuid4(), origin='unattributed', destination_key=key,
                        )
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Failure().objects.create(origin='unrecorded', subject_key=key)

    def test_a_destination_is_always_a_phone_key(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Issuance().objects.create(
                    id=uuid.uuid4(), origin='unattributed',
                    destination_key='u:' + str(uuid.uuid4()),
                )

    def test_the_timestamps_are_the_databases(self):
        """Inserted WITHOUT naming the column, as a raw writer would."""
        otp_id = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO otp_issuances (id, origin, state) VALUES (%s, %s, %s)',
                [otp_id, 'unattributed', 'pending'],
            )
            cursor.execute(
                'INSERT INTO otp_verification_failures (origin, bound_redemption) '
                'VALUES (%s, %s)', ['unrecorded', False],
            )
        self.assertIsNotNone(Issuance().objects.get(pk=otp_id).created_at)
        self.assertIsNotNone(Failure().objects.get().failed_at)

    def test_both_tables_have_a_chronological_index(self):
        for model, column in ((Issuance(), 'created_at'), (Failure(), 'failed_at')):
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(
                    cursor, model._meta.db_table,
                )
            indexed = [
                c for c in constraints.values()
                if c['index'] and c['columns'] == [column]
            ]
            self.assertEqual(len(indexed), 1, model.__name__)


class PhoneKeyTests(TestCase):

    def test_it_is_a_versioned_domain_separated_hmac_of_the_canonical_phone(self):
        key = acct().phone_key('256701234567', _pepper())
        self.assertEqual(key, _expected_phone_key('256701234567'))
        self.assertTrue(KEY_RE.match(key))

    def test_it_is_not_the_bare_hmac_or_the_otp_hash_construction(self):
        bare = hmac.new(_pepper(), b'256701234567', hashlib.sha256).hexdigest()
        self.assertNotIn(bare, acct().phone_key('256701234567', _pepper()))

    def test_a_different_pepper_is_a_different_key(self):
        self.assertNotEqual(
            acct().phone_key('256701234567', b'one'),
            acct().phone_key('256701234567', b'two'),
        )

    def test_it_refuses_a_non_canonical_value_rather_than_keying_it(self):
        for raw in ('+256701234567', '0701234567', '256 701 234 567', 'nonsense', ''):
            with self.subTest(raw), self.assertRaises(ValueError):
                acct().phone_key(raw, _pepper())

    def test_the_otp_pepper_derivation_is_unchanged(self):
        expected = hmac.new(
            settings.SECRET_KEY.encode(), b'otp-pepper', hashlib.sha256,
        ).hexdigest().encode()
        with mock.patch.object(otp_manager, 'config', side_effect=_env('dev')):
            self.assertEqual(otp_manager._otp_pepper(), expected)

    def test_the_accounting_module_imports_neither_the_otp_manager_nor_the_admin_plane(self):
        tree = ast.parse((REPO_ROOT / 'users_app' / 'otp_accounting.py').read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertFalse(
            [m for m in imported
             if m.startswith('platform_admin_app') or m.endswith('otp_manager')],
            imported,
        )

    def test_the_bound_redemption_purpose_is_the_redemption_purpose(self):
        from platform_admin_app.owner_claim_redemption import OWNER_CLAIM_OTP_PURPOSE
        self.assertEqual(acct().BOUND_REDEMPTION_PURPOSE, OWNER_CLAIM_OTP_PURPOSE)


# ═══════════════════════════════════════════════════════════════════════════════
# §2 — issuance
# ═══════════════════════════════════════════════════════════════════════════════

class IssuanceRecordingTests(TestCase):

    def test_a_user_challenge_records_one_pending_then_final_row_keyed_to_it(self):
        user = _user()
        with Senders():
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        challenge = UserOtp.objects.get(user=user)
        issuance = Issuance().objects.get()
        self.assertEqual(issuance.pk, challenge.pk)
        self.assertEqual(issuance.subject_key, f'u:{user.pk}')
        self.assertEqual(issuance.destination_key, _expected_phone_key(user.phone_number))
        self.assertEqual(issuance.origin, 'unattributed')
        self.assertIsNotNone(issuance.created_at)

    def test_an_msisdn_only_challenge_is_keyed_by_the_phone(self):
        phone = next(_PHONES)
        with Senders():
            self.assertTrue(OtpManager().make_otp(msisdn=phone, purpose='register'))
        issuance = Issuance().objects.get()
        self.assertEqual(issuance.subject_key, _expected_phone_key(phone))
        self.assertEqual(issuance.destination_key, issuance.subject_key)

    def test_the_destination_is_where_the_code_went_not_the_account_phone(self):
        user = _user()
        other = next(_PHONES)
        with Senders() as senders:
            OtpManager().make_otp(user=user, msisdn=other, purpose='owner-claim')
        self.assertEqual(
            Issuance().objects.get().destination_key, _expected_phone_key(other),
        )
        self.assertEqual(senders.sms.call_args.kwargs['msisdn'], other)

    def test_an_unusable_legacy_phone_is_unavailable_never_raw_and_never_a_refusal(self):
        user = _user('12345')
        with Senders() as senders, self.assertLogs('users_app', level='INFO') as logs:
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        issuance = Issuance().objects.get()
        self.assertIsNone(issuance.destination_key)
        self.assertEqual(issuance.subject_key, f'u:{user.pk}')
        self.assertTrue(
            any('destination_unavailable' in line for line in logs.output), logs.output,
        )
        self.assertFalse(any('12345' in line for line in logs.output))
        # The send still happened exactly as before.
        self.assertEqual(senders.sms.call_args.kwargs['msisdn'], '12345')

    def test_an_unknown_origin_is_recorded_as_unattributed(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login', origin='made_up')
        self.assertEqual(Issuance().objects.get().origin, 'unattributed')

    def test_a_replacement_keeps_the_earlier_issuance(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
            OtpManager().make_otp(user=user, purpose='login')
        self.assertEqual(UserOtp.objects.filter(user=user).count(), 1)
        self.assertEqual(Issuance().objects.count(), 2)

    def test_a_recording_failure_rolls_the_challenge_back_and_sends_nothing(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        previous = UserOtp.objects.get(user=user)
        with Senders() as senders, mock.patch.object(
            acct(), 'record_issuance', side_effect=DatabaseError('boom'),
        ), self.assertLogs('users_app', level='ERROR') as logs:
            self.assertFalse(OtpManager().make_otp(user=user, purpose='login'))
        senders.sms.assert_not_called()
        senders.email.assert_not_called()
        self.assertEqual(_SyncThread.started, [])
        # The replacement's delete rolled back with it: the earlier challenge survives.
        self.assertEqual(list(UserOtp.objects.filter(user=user)), [previous])
        self.assertEqual(Issuance().objects.count(), 1)
        self.assertFalse(any('boom' in line for line in logs.output), logs.output)

    def test_the_access_gate_still_records_nothing(self):
        from dinify_backend.configss.string_definitions import (
            CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        user = _user(customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        with Senders() as senders:
            self.assertFalse(OtpManager().make_otp(user=user, purpose='login'))
        self.assertEqual(Issuance().objects.count(), 0)
        senders.sms.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# §3 — finalization, every environment and sender branch
# ═══════════════════════════════════════════════════════════════════════════════

class FinalizationTests(TestCase):

    def issue(self, env, sms=True, email=True, has_email=False):
        user = _user(email=f'acc{next(_PHONES)}@example.test' if has_email else None)
        with Senders(env=env, sms=sms, email=email) as senders:
            result = OtpManager().make_otp(user=user, purpose='login')
        return result, Issuance().objects.get(), senders

    def assertFinal(self, issuance, state):
        self.assertEqual(issuance.state, state)
        self.assertIsNotNone(issuance.finalized_at)

    def test_dev_with_no_email_recipient_is_not_dispatched(self):
        result, issuance, senders = self.issue('dev')
        self.assertTrue(result)
        self.assertFinal(issuance, 'not_dispatched')
        self.assertEqual(_SyncThread.started, [True], 'dev dispatch is still a daemon thread')

    def test_dev_with_an_email_recipient_is_unknown(self):
        result, issuance, senders = self.issue('dev', has_email=True)
        self.assertTrue(result)
        self.assertFinal(issuance, 'unknown')
        self.assertEqual(_SyncThread.started, [True])

    def test_dev_still_issues_1234(self):
        user = _user()
        with Senders('dev'):
            OtpManager().make_otp(user=user, purpose='owner-claim')
        self.assertTrue(
            OtpManager().verify_otp(otp=DEV_OTP, user_id=str(user.pk))['data']['valid'],
        )

    def test_test_env_sms_accepted(self):
        result, issuance, senders = self.issue('test', sms=True)
        self.assertTrue(result)
        self.assertFinal(issuance, 'accepted')

    def test_test_env_sms_refused_email_accepted(self):
        result, issuance, senders = self.issue('test', sms=False, email=True, has_email=True)
        self.assertTrue(result)
        self.assertFinal(issuance, 'accepted')

    def test_test_env_sms_refused_no_email(self):
        result, issuance, senders = self.issue('test', sms=False)
        self.assertFalse(result)
        self.assertFinal(issuance, 'unknown')

    def test_test_env_sms_refused_email_refused(self):
        result, issuance, senders = self.issue('test', sms=False, email=False, has_email=True)
        self.assertFalse(result)
        self.assertFinal(issuance, 'unknown')

    def test_test_env_sms_refused_email_raises(self):
        result, issuance, senders = self.issue(
            'test', sms=False, email=OSError('smtp down'), has_email=True,
        )
        self.assertFalse(result)
        self.assertFinal(issuance, 'unknown')

    def test_prod_sms_accepted(self):
        result, issuance, senders = self.issue('prod', sms=True, has_email=True)
        self.assertTrue(result)
        self.assertFinal(issuance, 'accepted')
        senders.email.assert_not_called()

    def test_prod_sms_refused(self):
        result, issuance, senders = self.issue('prod', sms=False, has_email=True)
        self.assertFalse(result)
        self.assertFinal(issuance, 'unknown')
        senders.email.assert_not_called()

    def test_a_sender_that_raises_still_finalizes_unknown_and_still_raises(self):
        user = _user()
        with Senders('prod', sms=RuntimeError('gateway exploded')):
            with self.assertRaises(RuntimeError):
                OtpManager().make_otp(user=user, purpose='login')
        self.assertFinal(Issuance().objects.get(), 'unknown')

    def test_finalization_happens_once(self):
        result, issuance, _ = self.issue('prod', sms=True)
        acct().finalize_issuance(issuance.pk, 'unknown')
        again = Issuance().objects.get(pk=issuance.pk)
        self.assertEqual(again.state, 'accepted')
        self.assertEqual(again.finalized_at, issuance.finalized_at)

    def test_finalize_refuses_a_non_terminal_state(self):
        result, issuance, _ = self.issue('prod', sms=True)
        with self.assertRaises(ValueError):
            acct().finalize_issuance(issuance.pk, 'pending')

    def test_a_finalize_failure_changes_nothing_the_caller_sees(self):
        user = _user()
        for env, sms, expected in (('prod', True, True), ('prod', False, False),
                                   ('dev', True, True)):
            with self.subTest(env=env, sms=sms):
                with Senders(env, sms=sms) as senders, mock.patch.object(
                    Issuance().objects, 'filter', side_effect=DatabaseError('secret-x'),
                ), self.assertLogs('users_app', level='WARNING') as logs:
                    self.assertIs(
                        OtpManager().make_otp(user=user, purpose='login'), expected,
                    )
                self.assertLessEqual(senders.sms.call_count, 1, 'no resend')
                self.assertFalse(any('secret-x' in line for line in logs.output))

    def test_a_prune_failure_changes_nothing_the_caller_sees(self):
        user = _user()
        with Senders('prod', sms=True), mock.patch.object(
            acct(), 'prune', side_effect=DatabaseError('secret-y'),
        ), self.assertLogs('users_app', level='WARNING') as logs:
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        self.assertEqual(Issuance().objects.get().state, 'accepted')
        self.assertFalse(any('secret-y' in line for line in logs.output))

    def test_a_cleanup_statement_failure_is_logged_by_category_only(self):
        user = _user()
        with Senders('prod', sms=True), mock.patch.object(
            acct(), '_prune_table', side_effect=OperationalError(CANARY),
        ), self.assertLogs('users_app', level='WARNING') as logs:
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        self.assertEqual(Issuance().objects.get().state, 'accepted')
        self.assertEqual(logs.output, [
            'WARNING:users_app.otp_accounting:otp_accounting: prune_failed '
            '(category=operational)',
        ])

    def test_a_finalize_failure_does_not_prevent_the_cleanup_attempt(self):
        user = _user()
        pruned = []
        with Senders('prod', sms=True) as senders, mock.patch.object(
            acct(), 'finalize_issuance', side_effect=OperationalError(CANARY),
        ), mock.patch.object(
            acct(), 'prune', side_effect=lambda **kwargs: pruned.append(kwargs) or {},
        ), self.assertLogs('users_app', level='WARNING'):
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        self.assertEqual(pruned, [{'batch_size': acct().OPPORTUNISTIC_PRUNE_BATCH}])
        self.assertEqual(senders.sms.call_count, 1, 'no resend')


# ═══════════════════════════════════════════════════════════════════════════════
# §4 — origin, through the real endpoints
# ═══════════════════════════════════════════════════════════════════════════════

class OriginThroughEndpointsTests(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def post(self, url, body, **kwargs):
        cache.clear()
        return self.client.post(url, body, format='json', **kwargs)

    def origin_of_latest(self):
        return Issuance().objects.order_by('-created_at').first().origin

    def test_password_login(self):
        owner = _owner()
        with Senders():
            body = self.post(
                LOGIN_URL, {'username': owner.phone_number, 'password': PASSWORD},
            ).json()
        self.assertTrue(body['data']['require_otp'])
        self.assertEqual(self.origin_of_latest(), 'password_login')

    def test_reset_initiation(self):
        user = _user()
        with Senders():
            response = self.post(
                INITIATE_RESET_URL,
                {'identifier': user.phone_number, 'identification': 'phone'},
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.origin_of_latest(), 'reset_initiation')

    def test_login_resend(self):
        owner = _owner()
        with Senders():
            self.post(LOGIN_URL, {'username': owner.phone_number, 'password': PASSWORD})
            response = self.post(RESEND_URL, {
                'identification': 'id', 'identifier': str(owner.pk), 'purpose': 'login',
            })
        self.assertEqual(response.json()['status'], 200, response.content)
        self.assertEqual(
            list(Issuance().objects.order_by('created_at').values_list('origin', flat=True)),
            ['password_login', 'login_resend'],
        )

    def test_resend_request_for_another_purpose(self):
        user = _user()
        with Senders():
            response = self.post(RESEND_URL, {
                'identification': 'phone', 'identifier': user.phone_number,
                'purpose': 'reset-password',
            })
        self.assertEqual(response.json()['status'], 200, response.content)
        self.assertEqual(self.origin_of_latest(), 'resend_request')

    def test_resend_request_by_msisdn(self):
        with Senders():
            response = self.post(RESEND_URL, {
                'identification': 'msisdn', 'identifier': next(_PHONES),
                'purpose': 'register',
            })
        self.assertEqual(response.json()['status'], 200, response.content)
        self.assertEqual(self.origin_of_latest(), 'resend_request')

    def test_a_refused_login_resend_records_nothing(self):
        owner = _owner()
        with Senders():
            response = self.post(RESEND_URL, {
                'identification': 'id', 'identifier': str(owner.pk), 'purpose': 'login',
            })
        self.assertEqual(response.json()['status'], 400)
        self.assertEqual(Issuance().objects.count(), 0)

    def test_owner_claim_challenge(self):
        from platform_admin_app import onboarding_creation
        from platform_admin_app.endpoints.owner_claim import CLAIM_TOKEN_HEADER
        from platform_admin_app.onboarding_creation import NewOwner
        staff = User.objects.create_user(
            first_name='Ada', last_name='Min', email='acc-admin@example.test',
            username='acc-admin', country='UG', password='x', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        creation = onboarding_creation.create_admin_restaurant(
            name='Accounting Cafe', location='Muyenga', is_test=False,
            owner=NewOwner('Acc', 'Owner', next(_PHONES), None),
            actor=staff, reason='Creating the accounting fixture.',
        )
        with Senders():
            response = self.post(
                CHALLENGE_URL, {}, headers={CLAIM_TOKEN_HEADER: creation.claim_token},
            )
        self.assertEqual(response.status_code, 200, response.content)
        issuance = Issuance().objects.get()
        self.assertEqual(issuance.origin, 'owner_claim_challenge')
        self.assertEqual(issuance.subject_key, f'u:{creation.owner.pk}')
        # The claim token is nowhere in the row.
        self.assertNotIn(creation.claim_token, repr(_row_values(issuance)))

    def test_a_direct_call_is_unattributed(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        self.assertEqual(self.origin_of_latest(), 'unattributed')


class CallSiteStructureTests(TestCase):
    """Structural: each production issuer names its origin, and only its own."""

    EXPECTED = {
        'users_app/controllers/login.py': {'password_login'},
        'users_app/controllers/reset_password.py': {'reset_initiation'},
        'platform_admin_app/endpoints/owner_claim.py': {'owner_claim_challenge'},
        'users_app/controllers/otp_manager.py': {'login_resend', 'resend_request'},
    }

    def calls(self, path, name):
        tree = ast.parse((REPO_ROOT / path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                if called == name:
                    yield node

    def test_every_production_make_otp_call_passes_its_origin(self):
        for path, expected in self.EXPECTED.items():
            with self.subTest(path):
                origins = set()
                calls = list(self.calls(path, 'make_otp'))
                self.assertTrue(calls, path)
                for call in calls:
                    keywords = {k.arg: k.value for k in call.keywords}
                    self.assertIn('origin', keywords, f'{path}:{call.lineno}')
                    value = keywords['origin']
                    if isinstance(value, ast.IfExp):
                        values = [value.body, value.orelse]
                    else:
                        values = [value]
                    for v in values:
                        # A literal from the vocabulary, or the named constant for one.
                        if isinstance(v, ast.Constant):
                            origins.add(v.value)
                        else:
                            self.assertIsInstance(v, ast.Attribute, f'{path}:{call.lineno}')
                            origins.add(getattr(acct(), v.attr))
                self.assertEqual(origins, expected)
                self.assertLessEqual(origins, set(acct().ORIGINS))

    def test_no_other_production_module_issues_an_otp(self):
        """Every module of this repository's own Django apps, tests and migrations aside."""
        from django.apps import apps
        roots = {REPO_ROOT / 'dinify_backend'} | {
            pathlib.Path(config.path) for config in apps.get_app_configs()
            if pathlib.Path(config.path).is_relative_to(REPO_ROOT)
        }
        self.assertIn(REPO_ROOT / 'users_app', roots)
        offenders = []
        for root in roots:
            for path in root.rglob('*.py'):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if (path.name.startswith('tests') or '/migrations/' in rel
                        or rel in self.EXPECTED):
                    continue
                if list(self.calls(rel, 'make_otp')):
                    offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_every_production_create_employee_call_skips_the_otp_literally(self):
        path = 'restaurants_app/endpoints/restaurant_setup.py'
        calls = list(self.calls(path, 'create_employee'))
        self.assertTrue(calls)
        for call in calls:
            keywords = {k.arg: k.value for k in call.keywords}
            self.assertIn('skip_otp', keywords)
            self.assertIsInstance(keywords['skip_otp'], ast.Constant)
            self.assertIs(keywords['skip_otp'].value, True)


# ═══════════════════════════════════════════════════════════════════════════════
# §5 — wrong codes
# ═══════════════════════════════════════════════════════════════════════════════

class VerificationFailureTests(TestCase):

    def issue(self, user, purpose='login', **kwargs):
        with Senders():
            OtpManager().make_otp(user=user, purpose=purpose, **kwargs)
        return UserOtp.objects.filter(user=user).latest('time_created')

    def test_a_wrong_code_is_recorded_against_the_issuance(self):
        user = _user()
        challenge = self.issue(user, origin=acct().ORIGIN_PASSWORD_LOGIN)
        result = OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        self.assertFalse(result['data']['valid'])
        failure = Failure().objects.get()
        self.assertEqual(failure.subject_key, f'u:{user.pk}')
        self.assertEqual(failure.origin, 'password_login')
        self.assertEqual(failure.issuance_id, challenge.pk)
        self.assertFalse(failure.bound_redemption)
        self.assertIsNotNone(failure.failed_at)
        challenge.refresh_from_db()
        self.assertEqual(challenge.attempts, 1)

    def test_a_correct_code_records_no_failure(self):
        user = _user()
        self.issue(user)
        self.assertTrue(
            OtpManager().verify_otp(otp=DEV_OTP, user_id=str(user.pk))['data']['valid'],
        )
        self.assertEqual(Failure().objects.count(), 0)

    def test_no_challenge_records_nothing(self):
        user = _user()
        OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        self.assertEqual(Failure().objects.count(), 0)

    def test_a_locked_challenge_is_not_compared_and_not_recorded(self):
        user = _user()
        challenge = self.issue(user)
        UserOtp.objects.filter(pk=challenge.pk).update(attempts=otp_manager.OTP_MAX_ATTEMPTS)
        OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        self.assertEqual(Failure().objects.count(), 0)

    def test_every_counted_guess_is_one_row(self):
        user = _user()
        self.issue(user)
        for _ in range(otp_manager.OTP_MAX_ATTEMPTS + 2):
            OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        self.assertEqual(Failure().objects.count(), otp_manager.OTP_MAX_ATTEMPTS)

    def test_a_pre_ledger_challenge_is_unrecorded_and_keyed_by_user(self):
        user = _user()
        challenge = UserOtp.objects.create(
            user=user, msisdn=None, purpose='login', otp_hash='x', salt='s',
            identifier=f'user:{user.pk}',
        )
        OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        failure = Failure().objects.get()
        self.assertEqual(failure.origin, 'unrecorded')
        self.assertIsNone(failure.issuance_id)
        self.assertEqual(failure.subject_key, f'u:{user.pk}')
        self.assertNotEqual(failure.issuance_id, challenge.pk)

    def test_a_pre_ledger_phone_challenge_is_keyed_by_the_canonical_phone(self):
        phone = next(_PHONES)
        UserOtp.objects.create(
            user=None, msisdn=phone, purpose='register', otp_hash='x', salt='s',
            identifier=f'msisdn:{phone}',
        )
        OtpManager().verify_otp(otp='9999', msisdn=phone)
        self.assertEqual(Failure().objects.get().subject_key, _expected_phone_key(phone))

    def test_a_non_canonical_stored_phone_is_unavailable_not_raw(self):
        UserOtp.objects.create(
            user=None, msisdn='12345', purpose='register', otp_hash='x', salt='s',
            identifier='msisdn:12345',
        )
        OtpManager().verify_otp(otp='9999', msisdn='12345')
        failure = Failure().objects.get()
        self.assertIsNone(failure.subject_key)
        self.assertNotIn('12345', repr(_row_values(failure)))

    def test_the_issuance_subject_is_copied_even_when_it_differs(self):
        """A phone-keyed issuance for a user-less challenge stays phone-keyed."""
        phone = next(_PHONES)
        with Senders():
            OtpManager().make_otp(msisdn=phone, purpose='register')
        OtpManager().verify_otp(otp='9999', msisdn=phone)
        self.assertEqual(Failure().objects.get().subject_key, _expected_phone_key(phone))

    def test_bound_redemption_comes_only_from_the_bound_purpose(self):
        user = _user()
        self.issue(user, purpose='owner-claim', msisdn=user.phone_number)
        # Unbound: the verify-otp endpoint path, even for an owner-claim row.
        OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        # Bound: exactly what redemption passes.
        OtpManager().verify_otp(
            otp='9999', user_id=str(user.pk), expected_purpose='owner-claim',
            expected_msisdn=user.phone_number,
        )
        self.assertEqual(
            list(Failure().objects.order_by('failed_at', 'id')
                 .values_list('bound_redemption', flat=True)),
            [False, True],
        )

    def test_a_failure_observation_error_keeps_the_invalid_answer_and_the_counter(self):
        user = _user()
        challenge = self.issue(user)
        with mock.patch.object(acct(), '_failure_origin', return_value='not-a-real-origin'), \
                self.assertLogs('users_app', level='WARNING') as logs:
            result = OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
            # The surrounding transaction is still usable: a later statement runs.
            self.assertTrue(UserOtp.objects.filter(pk=challenge.pk).exists())
        self.assertEqual(result['message'], 'Invalid OTP')
        self.assertFalse(result['data']['valid'])
        challenge.refresh_from_db()
        self.assertEqual(challenge.attempts, 1)
        self.assertEqual(Failure().objects.count(), 0)
        self.assertTrue(any('verification_failure_unrecorded' in line
                            for line in logs.output), logs.output)


# ═══════════════════════════════════════════════════════════════════════════════
# §5b — the same, with real transactions
# ═══════════════════════════════════════════════════════════════════════════════

class _RealTransactions(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        super().setUp()
        if not _POSTGRES:
            self.skipTest('Real-transaction semantics are asserted on PostgreSQL.')
        cache.clear()
        self.addCleanup(cache.clear)

    def tearDown(self):
        connections.close_all()
        super().tearDown()

    def raw(self):
        """A second, independent connection to the same test database."""
        d = connection.settings_dict
        other = psycopg.connect(
            dbname=d['NAME'], user=d['USER'], password=d['PASSWORD'] or None,
            host=d['HOST'], port=d['PORT'], autocommit=True,
        )
        # Text comes back as ``str`` whatever the server encoding is.
        other.execute("SET client_encoding TO 'UTF8'")
        return other


class FailureObservationIsolationTests(_RealTransactions):

    def test_top_level_the_counter_is_durable_when_the_observation_fails(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        with mock.patch.object(acct(), '_failure_origin', return_value='bad'):
            result = OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        self.assertFalse(result['data']['valid'])
        with self.raw() as other:
            attempts = other.execute(
                'SELECT attempts FROM user_otps WHERE user_id = %s', [user.pk],
            ).fetchone()[0]
            failures = other.execute(
                'SELECT count(*) FROM otp_verification_failures',
            ).fetchone()[0]
        self.assertEqual((attempts, failures), (1, 0))

    def test_top_level_a_recorded_failure_commits_with_the_counter(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        with self.raw() as other:
            failures = other.execute(
                'SELECT count(*) FROM otp_verification_failures',
            ).fetchone()[0]
        self.assertEqual(failures, 1)

    def test_nested_the_counter_and_outer_writes_survive_the_observation_failing(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        with transaction.atomic():
            with mock.patch.object(acct(), '_failure_origin', return_value='bad'):
                OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
            # The outer transaction keeps working after the isolated failure.
            User.objects.filter(pk=user.pk).update(first_name='Still')
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Still')
        self.assertEqual(UserOtp.objects.get(user=user).attempts, 1)
        self.assertEqual(Failure().objects.count(), 0)

    def test_a_lost_connection_is_not_absorbed(self):
        """
        The observation's own statement loses its connection. The savepoint cannot be
        rolled back, so the verify must RAISE — returning ``invalid`` would claim a
        counter increment that can no longer commit.
        """
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        real = acct()._failure_origin
        test = self

        def kill_then_answer(*args, **kwargs):
            pid = connection.cursor().connection.info.backend_pid
            with test.raw() as other:
                other.execute('SELECT pg_terminate_backend(%s)', [pid])
            return real(*args, **kwargs)

        with mock.patch.object(acct(), '_failure_origin', side_effect=kill_then_answer):
            with self.assertRaises(DatabaseError):
                OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
        connections.close_all()
        with self.raw() as other:
            attempts = other.execute(
                'SELECT attempts FROM user_otps WHERE user_id = %s', [user.pk],
            ).fetchone()[0]
        self.assertEqual(attempts, 0, 'nothing was committed, and nothing claimed it was')


# ═══════════════════════════════════════════════════════════════════════════════
# §6 — concurrency
# ═══════════════════════════════════════════════════════════════════════════════

class IssuanceTransactionTests(_RealTransactions):

    def test_the_challenge_and_its_issuance_are_committed_before_the_sender_runs(self):
        user = _user()
        seen = {}
        test = self

        def sms_spy(message, msisdn, **kwargs):
            seen['atomic'] = transaction.get_connection().in_atomic_block
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() "
                    "AND locktype = 'transactionid'",
                )
                seen['xid_locks'] = cursor.fetchone()[0]
            with test.raw() as other:
                row = other.execute(
                    'SELECT id FROM user_otps WHERE user_id = %s', [user.pk],
                ).fetchone()
                seen['challenge_visible'] = row is not None
                seen['issuance_state'] = other.execute(
                    'SELECT state FROM otp_issuances WHERE id = %s', [row[0]],
                ).fetchone()[0]
                # Nothing this issuance touched is still locked by it.
                other.execute('SELECT 1 FROM users WHERE id = %s FOR UPDATE NOWAIT', [user.pk])
                other.execute('SELECT 1 FROM user_otps WHERE id = %s FOR UPDATE NOWAIT', [row[0]])
                seen['nowait_ok'] = True
            return True

        with Senders('prod') as senders:
            senders.sms.side_effect = sms_spy
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        self.assertEqual(seen, {
            'atomic': False, 'xid_locks': 0, 'challenge_visible': True,
            'issuance_state': 'pending', 'nowait_ok': True,
        })
        self.assertEqual(Issuance().objects.get().state, 'accepted')

    def test_the_email_sender_too_runs_with_nothing_held(self):
        user = _user(email='held@example.test')
        seen = []

        def email_spy(**kwargs):
            seen.append(transaction.get_connection().in_atomic_block)
            return True

        with Senders('test', sms=False) as senders:
            senders.email.side_effect = email_spy
            self.assertTrue(OtpManager().make_otp(user=user, purpose='login'))
        self.assertEqual(seen, [False])

    def test_the_issuance_takes_the_user_key_share_first(self):
        user = _user()
        statements = []

        def record(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)

        with Senders(), connection.execute_wrapper(record):
            OtpManager().make_otp(user=user, purpose='login')
        key_share = [i for i, s in enumerate(statements) if 'FOR KEY SHARE' in s]
        delete = [i for i, s in enumerate(statements)
                  if s.lstrip().upper().startswith('DELETE') and 'user_otps' in s]
        self.assertEqual(len(key_share), 1, statements)
        self.assertIn('"users"', statements[key_share[0]])
        self.assertTrue(delete)
        self.assertLess(key_share[0], delete[0])

    def test_an_msisdn_only_issuance_locks_no_user(self):
        statements = []

        def record(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)

        with Senders(), connection.execute_wrapper(record):
            OtpManager().make_otp(msisdn=next(_PHONES), purpose='register')
        self.assertFalse([s for s in statements if 'FOR ' in s and 'SHARE' in s])

    def test_an_enclosing_transaction_is_refused_before_any_write_or_send(self):
        user = _user()
        with Senders():
            OtpManager().make_otp(user=user, purpose='login')
        before = list(UserOtp.objects.filter(user=user).values_list('pk', flat=True))
        with Senders() as senders:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    OtpManager().make_otp(user=user, purpose='login')
        senders.sms.assert_not_called()
        senders.email.assert_not_called()
        self.assertEqual(
            list(UserOtp.objects.filter(user=user).values_list('pk', flat=True)), before,
        )
        self.assertEqual(Issuance().objects.count(), 1)

    def test_the_opportunistic_prune_runs_after_finalization_outside_any_transaction(self):
        user = _user()
        order = []
        real_finalize, real_prune = acct().finalize_issuance, acct().prune

        def finalize(*args, **kwargs):
            order.append(('finalize', transaction.get_connection().in_atomic_block))
            return real_finalize(*args, **kwargs)

        def prune(*args, **kwargs):
            order.append(('prune', transaction.get_connection().in_atomic_block,
                          kwargs.get('batch_size')))
            return real_prune(*args, **kwargs)

        with Senders('prod'), mock.patch.object(acct(), 'finalize_issuance', finalize), \
                mock.patch.object(acct(), 'prune', prune):
            OtpManager().make_otp(user=user, purpose='login')
        self.assertEqual(order, [
            ('finalize', False),
            ('prune', False, acct().OPPORTUNISTIC_PRUNE_BATCH),
        ])
        self.assertLessEqual(acct().OPPORTUNISTIC_PRUNE_BATCH, 1000)


class ReplacementVersusRedemptionTests(_RealTransactions):
    """
    An owner-claim challenge being REPLACED while the same owner's redemption is in
    flight, in both arrival orders.

    Redemption takes ``Restaurant -> ... -> User FOR UPDATE -> UserOtp FOR UPDATE``.
    Without the early KEY SHARE, the issuance takes ``UserOtp`` (its delete) and only
    reaches ``User`` at COMMIT, through the deferred foreign key — the reverse order, and
    PostgreSQL answers one side with a deadlock. With it, the issuance waits for the
    ``users`` row holding nothing, and both complete.

    Arrival is controlled by NAMED seams and events, never by sleeping; a lock wait is
    confirmed from ``pg_stat_activity`` under a deadline.
    """

    def setUp(self):
        super().setUp()
        from platform_admin_app import onboarding_creation
        from platform_admin_app.onboarding_creation import NewOwner
        staff = User.objects.create_user(
            first_name='Ada', last_name='Min', email='race-admin@example.test',
            username='race-admin', country='UG', password='x', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        creation = onboarding_creation.create_admin_restaurant(
            name='Race Accounting Cafe', location='Muyenga', is_test=False,
            owner=NewOwner('Race', 'Owner', next(_PHONES), None),
            actor=staff, reason='Creating the replacement race fixture.',
        )
        self.owner = creation.owner
        self.token = creation.claim_token
        self.senders = Senders()
        self.senders.__enter__()
        self.addCleanup(self.senders.__exit__, None, None, None)
        self.issue()

    def issue(self):
        return OtpManager().make_otp(
            user=self.owner, msisdn=self.owner.phone_number, purpose='owner-claim',
        )

    def redeem(self):
        from platform_admin_app import owner_claim_redemption
        return owner_claim_redemption.redeem_owner_claim(
            raw_token=self.token, otp=DEV_OTP,
            encoded_password=make_password('Race-Accounting-Pass-1'),
        )

    @staticmethod
    def name_connection(name):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('application_name', %s, false)", [name])

    def wait_until_lock_waiting(self, name, done):
        """
        Return once ``name`` is waiting on a lock, or has already finished without
        needing to (``done``). Polling is observation under a deadline, not the
        synchronisation: the order of arrival is fixed by the seams above.
        """
        deadline = time.monotonic() + WAIT
        with self.raw() as monitor:
            while time.monotonic() < deadline:
                if done.is_set():
                    return 'finished'
                row = monitor.execute(
                    'SELECT wait_event_type FROM pg_stat_activity WHERE application_name = %s',
                    [name],
                ).fetchone()
                if row and row[0] == 'Lock':
                    return 'waiting'
                time.sleep(0.02)
        self.fail(f'{name} neither waited on a lock nor finished')

    def run_threads(self, *targets):
        threads = [threading.Thread(target=t, name=n) for n, t in targets]
        for thread in threads:
            thread.start()
        return threads

    def join(self, threads):
        for thread in threads:
            thread.join(timeout=WAIT)
            self.assertFalse(thread.is_alive(), f'{thread.name} did not finish')

    def assertNoDeadlock(self, outcomes):
        for name, (kind, value) in outcomes.items():
            self.assertEqual(kind, 'ok', f'{name}: {value!r}')

    def test_redemption_arrives_first(self):
        from platform_admin_app import owner_claim_redemption
        inside, resume = threading.Event(), threading.Event()
        outcomes = {}
        real = owner_claim_redemption.assert_owner_consistency

        def parked(restaurant):
            result = real(restaurant)
            inside.set()
            assert resume.wait(timeout=WAIT)
            return result

        def redeemer():
            try:
                self.name_connection('d11-redeem')
                outcomes['redeem'] = ('ok', self.redeem())
            except Exception as exc:  # noqa: BLE001 - reported
                outcomes['redeem'] = ('error', exc)
            finally:
                inside.set()
                connections.close_all()

        issued = threading.Event()

        def issuer():
            try:
                assert inside.wait(timeout=WAIT)
                self.name_connection('d11-issue')
                outcomes['issue'] = ('ok', self.issue())
            except Exception as exc:  # noqa: BLE001 - reported
                outcomes['issue'] = ('error', exc)
            finally:
                issued.set()
                connections.close_all()

        with mock.patch.object(owner_claim_redemption, 'assert_owner_consistency', parked):
            threads = self.run_threads(('redeemer', redeemer), ('issuer', issuer))
            try:
                seen = self.wait_until_lock_waiting('d11-issue', issued)
            finally:
                resume.set()
            self.join(threads)
        self.assertNoDeadlock(outcomes)
        self.assertEqual(seen, 'waiting', 'the replacement must wait for the redemption')
        self.assertTrue(outcomes['issue'][1])

    def test_replacement_arrives_first(self):
        parked_inside, resume = threading.Event(), threading.Event()
        outcomes = {}

        def seam(sender, instance, created, **kwargs):
            if threading.current_thread().name == 'issuer' and created:
                parked_inside.set()
                assert resume.wait(timeout=WAIT)

        post_save.connect(seam, sender=UserOtp, weak=False, dispatch_uid='d11-b2c-seam')
        self.addCleanup(post_save.disconnect, sender=UserOtp, dispatch_uid='d11-b2c-seam')

        def issuer():
            try:
                self.name_connection('d11-issue')
                outcomes['issue'] = ('ok', self.issue())
            except Exception as exc:  # noqa: BLE001 - reported
                outcomes['issue'] = ('error', exc)
            finally:
                parked_inside.set()
                connections.close_all()

        redeemed = threading.Event()

        def redeemer():
            try:
                assert parked_inside.wait(timeout=WAIT)
                self.name_connection('d11-redeem')
                outcomes['redeem'] = ('ok', self.redeem())
            except Exception as exc:  # noqa: BLE001 - reported
                outcomes['redeem'] = ('error', exc)
            finally:
                redeemed.set()
                connections.close_all()

        threads = self.run_threads(('issuer', issuer), ('redeemer', redeemer))
        try:
            seen = self.wait_until_lock_waiting('d11-redeem', redeemed)
        finally:
            resume.set()
        self.join(threads)
        self.assertNoDeadlock(outcomes)
        self.assertEqual(seen, 'waiting', 'the redemption must wait for the replacement')
        self.assertTrue(outcomes['issue'][1])


# ═══════════════════════════════════════════════════════════════════════════════
# §7 — cleanup
# ═══════════════════════════════════════════════════════════════════════════════

class CleanupTests(TestCase):

    def seed(self, now):
        seven = datetime.timedelta(days=7)
        tick = datetime.timedelta(microseconds=1)
        rows = {
            'oldest_pending': now - seven - datetime.timedelta(days=3),
            'old_accepted': now - seven - datetime.timedelta(days=1),
            'just_older': now - seven - tick,
            'boundary': now - seven,
            'young': now - datetime.timedelta(days=1),
        }
        ids = {}
        for label, created in rows.items():
            state = 'pending' if 'pending' in label else 'accepted'
            issuance = Issuance().objects.create(
                id=uuid.uuid4(), origin='unattributed', state=state,
                finalized_at=None if state == 'pending' else created,
                created_at=created,
            )
            failure = Failure().objects.create(origin='unrecorded', failed_at=created)
            ids[label] = (issuance.pk, failure.pk)
        return ids

    def test_strictly_older_than_seven_days_in_any_state_is_eligible(self):
        now = timezone.now()
        ids = self.seed(now)
        counts = acct().prune(batch_size=100, now=now)
        self.assertEqual(counts, {'otp_issuances': 3, 'otp_verification_failures': 3})
        remaining = set(Issuance().objects.values_list('pk', flat=True))
        self.assertEqual(remaining, {ids['boundary'][0], ids['young'][0]})
        remaining = set(Failure().objects.values_list('pk', flat=True))
        self.assertEqual(remaining, {ids['boundary'][1], ids['young'][1]})

    def test_the_batch_is_bounded_and_oldest_first(self):
        now = timezone.now()
        ids = self.seed(now)
        counts = acct().prune(batch_size=2, now=now)
        self.assertEqual(counts, {'otp_issuances': 2, 'otp_verification_failures': 2})
        self.assertTrue(Issuance().objects.filter(pk=ids['just_older'][0]).exists())
        self.assertFalse(Issuance().objects.filter(pk=ids['oldest_pending'][0]).exists())
        self.assertFalse(Issuance().objects.filter(pk=ids['old_accepted'][0]).exists())

    def test_the_batch_size_is_bounded(self):
        for bad in (0, -1, acct().MAX_PRUNE_BATCH + 1):
            with self.subTest(bad), self.assertRaises(ValueError):
                acct().prune(batch_size=bad)

    def test_the_default_clock_is_the_databases_and_keeps_recent_rows(self):
        Issuance().objects.create(id=uuid.uuid4(), origin='unattributed')
        Failure().objects.create(origin='unrecorded')
        self.assertEqual(
            acct().prune(batch_size=10),
            {'otp_issuances': 0, 'otp_verification_failures': 0},
        )

    def test_the_retention_is_seven_days(self):
        self.assertEqual(acct().RETENTION, datetime.timedelta(days=7))


# A database error whose text looks like everything that must never be printed: a key,
# an id, a timestamp, SQL and a parameter.
CANARY = (
    'p1:' + 'ab' * 32 + ' 1f0e3c9a-0000-4000-8000-00000000c0de '
    '2026-09-21 12:00:00+00 DELETE FROM "otp_issuances" WHERE id = %s password=hunter2'
)


def _run_cli(*args):
    """
    The operator's path: ``manage.py prune_otp_accounting ...``, discovered and run by
    Django's own command-line utility, with its real stdout, stderr and exit status.
    """
    from django.core.management import execute_from_command_line
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            execute_from_command_line(['manage.py', 'prune_otp_accounting', *args])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class PruneCommandTests(_RealTransactions):
    """
    The operator command, run through the real command-line path against real
    PostgreSQL, with its durable effect read back from a SEPARATE connection.

    What an operator is told must be true: every count printed is a deletion that has
    COMMITTED, a failure exits non-zero with a fixed category and no database text, a
    failed statement's outcome is called unknown rather than guessed, and "complete" is
    only said after a fresh check found nothing eligible.
    """

    def seed(self, n, *, failures=None):
        """``n`` eligible rows in each table (``failures`` overrides the second)."""
        created = timezone.now() - datetime.timedelta(days=9)
        for _ in range(n):
            Issuance().objects.create(
                id=uuid.uuid4(), origin='unattributed', created_at=created,
            )
        for _ in range(n if failures is None else failures):
            Failure().objects.create(origin='unrecorded', failed_at=created)

    def seed_young(self):
        """Rows the command must never touch: a day old, and an hour inside retention."""
        for age in (datetime.timedelta(days=1),
                    datetime.timedelta(days=7) - datetime.timedelta(hours=1)):
            created = timezone.now() - age
            Issuance().objects.create(
                id=uuid.uuid4(), origin='unattributed', created_at=created,
            )
            Failure().objects.create(origin='unrecorded', failed_at=created)

    def durable(self):
        """Row counts as another connection sees them: only what has committed."""
        with self.raw() as other:
            return (
                other.execute('SELECT count(*) FROM otp_issuances').fetchone()[0],
                other.execute('SELECT count(*) FROM otp_verification_failures').fetchone()[0],
            )

    def assertSanitized(self, *texts):
        for text in texts:
            for fragment in ('p1:', 'c0de', '2026-09-21', 'DELETE', 'hunter2',
                             'Traceback', 'lock timeout', 'otp_issuances"'):
                self.assertNotIn(fragment, text)
            self.assertIsNone(
                re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', text), text,
            )

    def failing_after(self, calls, exc):
        """``_prune_table`` that performs ``calls`` real deletes, then raises ``exc``."""
        real = acct()._prune_table
        seen = []

        def prune_table(*args, **kwargs):
            seen.append(args[0])
            if len(seen) > calls:
                raise exc
            return real(*args, **kwargs)
        return mock.patch.object(acct(), '_prune_table', side_effect=prune_table)

    # --- success ---

    def test_it_drains_in_bounded_batches_and_reports_each_one(self):
        self.seed(5)
        self.seed_young()
        code, out, err = _run_cli('--batch-size', '2', '--max-batches', '10')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.durable(), (2, 2), 'only the young rows survive')
        self.assertIn('batch 1: otp_issuances deleted 2, otp_verification_failures deleted 2', out)
        self.assertIn('batch 3: otp_issuances deleted 1, otp_verification_failures deleted 1', out)
        self.assertIn('otp_issuances: deleted 5', out)
        self.assertIn('otp_verification_failures: deleted 5', out)
        self.assertIn('complete: no eligible rows found when last checked', out)
        self.assertEqual(err, '')
        self.assertSanitized(out)

    def test_it_stops_at_the_batch_limit_and_says_so(self):
        self.seed(5)
        code, out, err = _run_cli('--batch-size', '2', '--max-batches', '1')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.durable(), (3, 3))
        self.assertIn('otp_issuances: deleted 2', out)
        self.assertIn('batch limit reached: eligible rows may remain', out)
        self.assertNotIn('complete:', out)

    def test_nothing_eligible_is_complete_after_one_checked_batch(self):
        self.seed_young()
        code, out, err = _run_cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.durable(), (2, 2))
        self.assertIn('otp_issuances: deleted 0', out)
        self.assertIn('complete: no eligible rows found when last checked', out)

    def test_the_defaults_and_limits_are_finite(self):
        from users_app.management.commands import prune_otp_accounting as command
        self.assertEqual((command.DEFAULT_BATCH_SIZE, command.DEFAULT_MAX_BATCHES,
                          command.MAX_BATCHES), (500, 100, 10000))
        code, _, err = _run_cli('--batch-size', '1000', '--max-batches', '1')
        self.assertEqual(code, 0, err)
        for args in (('--batch-size', '0'), ('--batch-size', '1001'),
                     ('--max-batches', '0'), ('--max-batches', '10001')):
            with self.subTest(args):
                code, out, err = _run_cli(*args)
                self.assertNotEqual(code, 0)
                self.assertIn('CommandError', err)

    def test_there_is_no_retention_override(self):
        from users_app.management.commands.prune_otp_accounting import Command
        parser = Command().create_parser('manage.py', 'prune_otp_accounting')
        options = {a.dest for a in parser._actions}
        self.assertFalse({o for o in options if 'retention' in o or 'days' in o or 'older' in o})

    def test_it_refuses_to_run_inside_a_callers_transaction(self):
        """Counts printed inside someone else's transaction would not be committed."""
        self.seed(3)
        with self.assertRaises(CommandError):
            with transaction.atomic():
                call_command('prune_otp_accounting', stdout=io.StringIO())
        self.assertEqual(self.durable(), (3, 3))

    def test_it_refuses_when_the_caller_has_turned_autocommit_off(self):
        """
        No atomic block, but no autocommit either: every delete would join the caller's
        open transaction, which the caller can still roll back.
        """
        self.seed(3)
        transaction.set_autocommit(False)
        try:
            with self.assertRaises(CommandError):
                call_command('prune_otp_accounting', stdout=io.StringIO())
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
        self.assertEqual(self.durable(), (3, 3))

    # --- failure: exit status, sanitised text, confirmed counts only ---

    def test_a_failure_before_any_work_exits_nonzero_and_prints_no_database_text(self):
        self.seed(3)
        with self.failing_after(0, OperationalError(CANARY)):
            code, out, err = _run_cli('--batch-size', '2')
        self.assertEqual(code, 1)
        self.assertEqual(self.durable(), (3, 3))
        self.assertIn('otp_issuances: deleted 0', out)
        self.assertIn('category=operational', err)
        self.assertIn('outcome is unknown', err)
        self.assertIn('eligible rows may remain', err)
        self.assertNotIn('complete:', out + err)
        self.assertSanitized(out, err)

    def test_a_failure_after_a_completed_batch_reports_that_batch(self):
        self.seed(5)
        with self.failing_after(2, OperationalError(CANARY)):
            code, out, err = _run_cli('--batch-size', '2', '--max-batches', '10')
        self.assertEqual(code, 1)
        self.assertEqual(self.durable(), (3, 3), 'batch 1 committed; nothing after it')
        self.assertIn('batch 1: otp_issuances deleted 2, otp_verification_failures deleted 2', out)
        self.assertIn('otp_issuances: deleted 2', out)
        self.assertIn('otp_verification_failures: deleted 2', out)
        self.assertIn('otp_issuances delete in batch 2', err)
        self.assertNotIn('complete:', out + err)
        self.assertSanitized(out, err)

    def test_a_failure_between_the_tables_keeps_the_first_tables_committed_count(self):
        self.seed(3)
        with self.failing_after(1, DatabaseError(CANARY)):
            code, out, err = _run_cli('--batch-size', '5')
        self.assertEqual(code, 1)
        self.assertEqual(self.durable(), (0, 3))
        self.assertIn(
            'batch 1 (incomplete): otp_issuances deleted 3, '
            'otp_verification_failures outcome unknown', out,
        )
        self.assertIn('otp_issuances: deleted 3', out)
        self.assertIn('otp_verification_failures: deleted 0', out)
        self.assertIn('category=database', err)
        self.assertIn('otp_verification_failures delete in batch 1', err)
        self.assertSanitized(out, err)

    def test_a_real_lock_timeout_between_the_tables_is_a_real_partial_commit(self):
        """
        No stub: the second table is locked by another connection and the command's
        session gives up waiting. PostgreSQL has already committed the first table's
        delete, and the command says exactly that.
        """
        self.seed(3)
        with self.raw() as holder:
            holder.autocommit = False
            holder.execute('LOCK TABLE otp_verification_failures IN ACCESS EXCLUSIVE MODE')
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '300ms'")
            code, out, err = _run_cli('--batch-size', '5')
            holder.rollback()
        self.assertEqual(code, 1)
        self.assertEqual(self.durable(), (0, 3))
        self.assertIn('otp_issuances: deleted 3', out)
        self.assertIn('category=operational', err)
        self.assertIn('outcome is unknown', err)
        self.assertSanitized(out, err)

    def test_the_command_line_error_carries_no_chained_exception(self):
        self.seed(1)
        with self.failing_after(0, OperationalError(CANARY)):
            with self.assertRaises(CommandError) as caught:
                call_command('prune_otp_accounting', stdout=io.StringIO())
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertSanitized(str(caught.exception))

    def test_a_failed_freshness_check_is_not_reported_as_complete(self):
        self.seed(1)
        with mock.patch.object(
            acct(), 'eligible_rows_remain', side_effect=OperationalError(CANARY),
        ):
            code, out, err = _run_cli('--batch-size', '5')
        self.assertEqual(code, 1)
        self.assertEqual(self.durable(), (0, 0))
        self.assertIn('otp_issuances: deleted 1', out)
        self.assertIn('could not check', err)
        self.assertNotIn('complete:', out + err)
        self.assertSanitized(out, err)

    # --- a short batch is not proof that nothing is eligible ---

    def test_a_short_batch_caused_by_concurrent_cleanup_is_not_called_complete(self):
        """
        Another cleanup deletes the two oldest rows but has not committed. This
        command's first batch selects those same two rows, waits for their locks, and
        finds them gone — a SHORT batch of zero while three eligible rows remain. It
        must look again rather than say nothing was left.
        """
        self.seed(5, failures=0)
        oldest = list(
            Issuance().objects.order_by('created_at', 'pk').values_list('pk', flat=True)[:2]
        )
        outcome, done = {}, threading.Event()

        def command():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('application_name', 'd11-prune', false)")
                outcome['result'] = _run_cli('--batch-size', '2', '--max-batches', '5')
            finally:
                done.set()
                connections.close_all()

        with self.raw() as other:
            other.autocommit = False
            other.execute(
                'DELETE FROM otp_issuances WHERE id = ANY(%s)', [[str(p) for p in oldest]],
            )
            thread = threading.Thread(target=command, name='prune-command')
            thread.start()
            deadline = time.monotonic() + WAIT
            with self.raw() as monitor:
                while time.monotonic() < deadline and not done.is_set():
                    row = monitor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE application_name = 'd11-prune'",
                    ).fetchone()
                    if row and row[0] == 'Lock':
                        break
                    time.sleep(0.02)
                else:
                    other.rollback()
                    thread.join(timeout=WAIT)
                    self.fail('the command never waited on the concurrent cleanup')
            other.commit()
        thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive())
        code, out, err = outcome['result']
        self.assertEqual(code, 0, err)
        self.assertIn('batch 1: otp_issuances deleted 0', out)
        self.assertEqual(self.durable(), (0, 0), 'the remaining three were still found')
        self.assertIn('otp_issuances: deleted 3', out)
        self.assertIn('complete: no eligible rows found when last checked', out)


class RedemptionTelemetryFailureTests(_RealTransactions):
    """
    The REAL owner-claim redemption, through its endpoint, with a wrong code — and the
    observation of that wrong code failing inside redemption's own transaction.

    Redemption is the consumer the savepoint exists for: its wrong-code branch COMMITS
    two counters (the challenge's ``attempts`` and the invitation's
    ``claim_failed_attempts``) so that guessing cannot be erased by an error. A telemetry
    failure inside it must cost neither counter, must not change the refusal, and must
    not let the claim through. Everything is read back from a SEPARATE connection, so
    only what committed counts.
    """

    CLAIM_PASSWORD = 'Accounting-Claim-Pass-7'

    def setUp(self):
        super().setUp()
        from platform_admin_app import onboarding_creation
        from platform_admin_app.onboarding_creation import NewOwner
        staff = User.objects.create_user(
            first_name='Ada', last_name='Min', email='telemetry-admin@example.test',
            username='telemetry-admin', country='UG', password='x', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        creation = onboarding_creation.create_admin_restaurant(
            name='Telemetry Cafe', location='Kansanga', is_test=False,
            owner=NewOwner('Tele', 'Metry', next(_PHONES), None),
            actor=staff, reason='Creating the telemetry-failure fixture.',
        )
        self.owner, self.token = creation.owner, creation.claim_token
        self.invitation_id = creation.invitation.pk
        self.senders = Senders()
        self.senders.__enter__()
        self.addCleanup(self.senders.__exit__, None, None, None)
        self.client = APIClient()
        response = self.client.post(
            CHALLENGE_URL, {}, format='json', headers={'X-Owner-Claim-Token': self.token},
        )
        self.assertEqual(response.status_code, 200, response.data)

    def redeem(self, otp):
        return self.client.post(
            REDEEM_URL, {'otp': otp, 'new_password': self.CLAIM_PASSWORD}, format='json',
            headers={'X-Owner-Claim-Token': self.token},
        )

    def committed(self):
        with self.raw() as other:
            attempts = other.execute(
                "SELECT attempts FROM user_otps WHERE user_id = %s AND purpose = 'owner-claim'",
                [self.owner.pk],
            ).fetchone()[0]
            claim_failed, consumed = other.execute(
                'SELECT claim_failed_attempts, consumed_at FROM owner_invitation WHERE id = %s',
                [self.invitation_id],
            ).fetchone()
            access, password = other.execute(
                'SELECT customer_access_state, password FROM users WHERE id = %s',
                [self.owner.pk],
            ).fetchone()
            tokens = other.execute(
                'SELECT count(*) FROM token_blacklist_outstandingtoken WHERE user_id = %s',
                [self.owner.pk],
            ).fetchone()[0]
            failures = other.execute(
                'SELECT origin, bound_redemption FROM otp_verification_failures',
            ).fetchall()
        return {
            'attempts': attempts, 'claim_failed_attempts': claim_failed,
            'consumed': consumed is not None, 'access': access,
            'usable_password': not password.startswith('!'), 'tokens': tokens,
            'failures': failures,
        }

    def assertOrdinaryRefusal(self, response):
        from platform_admin_app.endpoints.owner_claim import REDEEM_REFUSAL_MESSAGE
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            dict(response.data), {'status': 400, 'message': REDEEM_REFUSAL_MESSAGE},
        )

    def assertCountedButNotClaimed(self, state, failures):
        self.assertEqual(state['attempts'], 1)
        self.assertEqual(state['claim_failed_attempts'], 1)
        self.assertFalse(state['consumed'])
        self.assertEqual(state['access'], 'pending_initial_claim')
        self.assertFalse(state['usable_password'])
        self.assertEqual(state['tokens'], 0)
        self.assertEqual(state['failures'], failures)

    def test_control_the_observation_commits_with_both_counters(self):
        response = self.redeem('9999')
        self.assertOrdinaryRefusal(response)
        self.assertCountedButNotClaimed(
            self.committed(), [('owner_claim_challenge', True)],
        )

    def test_a_failed_origin_read_costs_neither_counter_nor_the_refusal(self):
        """
        The origin read raises a REAL database error (division by zero in the same
        transaction), which puts PostgreSQL's transaction into the aborted state — the
        case only a savepoint rollback recovers from.
        """
        def failing_read(challenge, pepper):
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 / 0')
        with mock.patch.object(acct(), '_failure_identity', side_effect=failing_read), \
                self.assertLogs('users_app.otp_accounting', level='WARNING') as logs:
            response = self.redeem('9999')
        self.assertOrdinaryRefusal(response)
        self.assertCountedButNotClaimed(self.committed(), [])
        self.assertEqual(logs.output, [
            'WARNING:users_app.otp_accounting:otp_accounting: '
            'verification_failure_unrecorded (category=database)',
        ])

    def test_a_failed_observation_write_costs_neither_counter_nor_the_refusal(self):
        """The row itself is refused by the database's origin CHECK constraint."""
        with mock.patch.object(acct(), '_failure_origin', return_value='not-an-origin'), \
                self.assertLogs('users_app.otp_accounting', level='WARNING') as logs:
            response = self.redeem('9999')
        self.assertOrdinaryRefusal(response)
        self.assertCountedButNotClaimed(self.committed(), [])
        self.assertIn('category=integrity', logs.output[0])

    def test_the_right_code_still_claims_after_a_contained_failure(self):
        """The contained failure left the challenge usable: the owner can still claim."""
        with mock.patch.object(acct(), '_failure_origin', return_value='not-an-origin'):
            self.assertOrdinaryRefusal(self.redeem('9999'))
        response = self.redeem(DEV_OTP)
        self.assertEqual(response.status_code, 200, response.data)
        state = self.committed()
        self.assertTrue(state['consumed'])
        self.assertEqual(state['access'], 'established')
        self.assertEqual(state['tokens'], 1)


class CleanupOverlapTests(_RealTransactions):
    """
    Cleanup and finalization on SEPARATE connections, interleaved deterministically: one
    side holds a row lock in an open transaction, the other is started and observed
    waiting on it in ``pg_stat_activity`` under a deadline, then the holder commits.

    What must survive the overlap: strict eligibility (the exact-boundary and young rows
    are never touched), finite work (at most one batch per table), and no resurrection
    (a finalization that arrives after the delete changes nothing and inserts nothing).
    """

    def seed(self, now):
        seven = datetime.timedelta(days=7)
        ids = {}
        for label, created in (
            ('old_a', now - seven - datetime.timedelta(days=3)),
            ('old_b', now - seven - datetime.timedelta(days=2)),
            ('old_c', now - seven - datetime.timedelta(days=1)),
            ('boundary', now - seven),
            ('young', now - datetime.timedelta(days=1)),
        ):
            ids[label] = Issuance().objects.create(
                id=uuid.uuid4(), origin='unattributed', created_at=created,
            ).pk
        return ids

    def in_thread(self, name, target):
        outcome, done = {}, threading.Event()

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('application_name', %s, false)", [name])
                outcome['value'] = target()
            except Exception as exc:  # noqa: BLE001 - reported to the test
                outcome['error'] = exc
            finally:
                done.set()
                connections.close_all()

        thread = threading.Thread(target=run, name=name)
        thread.start()
        return thread, done, outcome

    def await_lock_wait(self, name, done):
        deadline = time.monotonic() + WAIT
        with self.raw() as monitor:
            while time.monotonic() < deadline:
                if done.is_set():
                    self.fail(f'{name} finished without waiting on the held row')
                row = monitor.execute(
                    'SELECT wait_event_type FROM pg_stat_activity WHERE application_name = %s',
                    [name],
                ).fetchone()
                if row and row[0] == 'Lock':
                    return
                time.sleep(0.02)
        self.fail(f'{name} never waited on the held row')

    def finish(self, thread, outcome):
        thread.join(timeout=WAIT)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', outcome, outcome.get('error'))
        return outcome['value']

    def remaining(self):
        with self.raw() as other:
            return dict(other.execute('SELECT id, state FROM otp_issuances').fetchall())

    def test_cleanup_waits_for_an_inflight_finalization_and_a_late_one_resurrects_nothing(self):
        now = timezone.now()
        ids = self.seed(now)
        with self.raw() as holder:
            holder.autocommit = False
            holder.execute(
                "UPDATE otp_issuances SET state = 'accepted', finalized_at = now() "
                "WHERE id = %s AND state = 'pending'", [ids['old_a']],
            )
            thread, done, outcome = self.in_thread(
                'd11-overlap-prune', lambda: acct().prune(batch_size=2, now=now),
            )
            self.await_lock_wait('d11-overlap-prune', done)
            holder.commit()
        counts = self.finish(thread, outcome)
        self.assertEqual(counts['otp_issuances'], 2, 'one bounded batch, no more')
        left = self.remaining()
        self.assertNotIn(ids['old_a'], left)
        self.assertEqual(
            set(left), {ids['old_c'], ids['boundary'], ids['young']},
            'oldest-first, and the boundary and young rows are never eligible',
        )
        # A finalization that arrives after the delete is a no-op, not an insert.
        self.assertEqual(acct().finalize_issuance(ids['old_a'], 'accepted'), 0)
        self.assertNotIn(ids['old_a'], self.remaining())

    def test_a_finalization_waiting_on_an_inflight_delete_changes_nothing(self):
        now = timezone.now()
        ids = self.seed(now)
        with self.raw() as holder:
            holder.autocommit = False
            holder.execute('DELETE FROM otp_issuances WHERE id = %s', [ids['old_b']])
            thread, done, outcome = self.in_thread(
                'd11-overlap-finalize',
                lambda: acct().finalize_issuance(ids['old_b'], 'accepted'),
            )
            self.await_lock_wait('d11-overlap-finalize', done)
            holder.commit()
        self.assertEqual(self.finish(thread, outcome), 0)
        left = self.remaining()
        self.assertNotIn(ids['old_b'], left)
        self.assertEqual(left[ids['boundary']], 'pending')
        self.assertEqual(left[ids['young']], 'pending')

    def test_concurrent_cleanup_makes_a_short_batch_while_eligible_rows_remain(self):
        """
        The control for the command's wording: a batch can come back short because
        another cleanup took the rows it selected, while other eligible rows are still
        there. Only a fresh check can tell.
        """
        now = timezone.now()
        ids = self.seed(now)
        with self.raw() as holder:
            holder.autocommit = False
            holder.execute(
                'DELETE FROM otp_issuances WHERE id = ANY(%s)',
                [[str(ids['old_a']), str(ids['old_b'])]],
            )
            thread, done, outcome = self.in_thread(
                'd11-overlap-short', lambda: acct().prune(batch_size=2, now=now),
            )
            self.await_lock_wait('d11-overlap-short', done)
            holder.commit()
        counts = self.finish(thread, outcome)
        self.assertEqual(counts['otp_issuances'], 0, 'short: its selected rows were taken')
        self.assertEqual(
            acct().eligible_rows_remain(now=now),
            {'otp_issuances': True, 'otp_verification_failures': False},
        )
        self.assertEqual(set(self.remaining()), {ids['old_c'], ids['boundary'], ids['young']})


# ═══════════════════════════════════════════════════════════════════════════════
# §8 — nothing secret reaches a row or a log
# ═══════════════════════════════════════════════════════════════════════════════

class NoSecretsTests(TestCase):

    def test_a_full_issue_guess_and_verify_cycle_leaks_nothing(self):
        user = _user(email='secret.owner@example.test')
        with Senders('test', sms=False, email=True), \
                self.assertLogs('users_app', level='DEBUG') as logs:
            logging.getLogger('users_app').debug('capture-start')
            OtpManager().make_otp(user=user, purpose='owner-claim', msisdn=user.phone_number)
            challenge = UserOtp.objects.get(user=user)
            OtpManager().verify_otp(otp='9999', user_id=str(user.pk))
            OtpManager().verify_otp(otp=DEV_OTP, user_id=str(user.pk))
        secrets = [user.phone_number, user.email, challenge.salt, challenge.otp_hash,
                   'owner-claim', str(_pepper())]
        values = repr([_row_values(r) for r in Issuance().objects.all()]
                      + [_row_values(r) for r in Failure().objects.all()])
        for secret in secrets:
            self.assertNotIn(secret, values)
            for line in logs.output:
                self.assertNotIn(secret, line)


# ═══════════════════════════════════════════════════════════════════════════════
# §9 — the migration is expand-only
# ═══════════════════════════════════════════════════════════════════════════════

class MigrationShapeTests(TestCase):

    def migration(self):
        from django.db.migrations.loader import MigrationLoader
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return loader, loader.get_migration('users_app', '0015_otp_accounting')

    def test_it_follows_the_customer_access_migration_and_is_the_leaf(self):
        loader, migration = self.migration()
        self.assertEqual(
            migration.dependencies, [('users_app', '0014_customer_access_state')],
        )
        self.assertEqual(
            loader.graph.leaf_nodes('users_app'), [('users_app', '0015_otp_accounting')],
        )

    def test_it_only_creates_the_two_ledger_tables(self):
        from django.db.migrations.operations import CreateModel
        _, migration = self.migration()
        self.assertEqual(
            [(type(op), op.name) for op in migration.operations],
            [(CreateModel, 'OtpIssuance'), (CreateModel, 'OtpVerificationFailure')],
        )
        for op in migration.operations:
            for _, field in op.fields:
                self.assertFalse(field.is_relation, op.name)

    def test_old_code_on_the_new_schema_writes_a_challenge_without_the_ledger(self):
        """
        What a rolled-back build does: a challenge written the pre-B2-C way, with no
        issuance row, is verified normally and any wrong guess is recorded `unrecorded`.
        """
        user = _user()
        salt = 'rollback-salt'
        UserOtp.objects.create(
            user=user, msisdn=None, purpose='login', salt=salt,
            identifier=f'user:{user.pk}',
            otp_hash=hmac.new(_pepper(), (salt + '4321').encode(), hashlib.sha256).hexdigest(),
        )
        self.assertFalse(
            OtpManager().verify_otp(otp='0000', user_id=str(user.pk))['data']['valid'],
        )
        self.assertEqual(Failure().objects.get().origin, 'unrecorded')
        self.assertTrue(
            OtpManager().verify_otp(otp='4321', user_id=str(user.pk))['data']['valid'],
        )
