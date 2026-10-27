"""
D11 enforcement on PostgreSQL — independent connections, real transactions.

THE CLAIMS
━━━━━━━━━━

* L1, L2 and L3 are EXACT. An issuer takes the subject (``OTPS``), destination
  (``OTPD``) and client (``OTPC``) advisory locks, in that order, after the bucket lock
  (``OTPI``) and before it counts. A second issuer for the same subject, destination
  or client therefore waits until the first COMMITS, and then counts its row.
* L5 is exact the same way, under the client lock for the lookup scope.
* L4 is NOT exact, deliberately. Two issuers that decide before either commits can
  both be admitted at ``cap - 1``. The overshoot is one per issuance in flight; the
  next issuer is refused.
* The locks sit inside the issuance transaction, in the stated order, and none is held
  when the code is sent.

HOW THESE DISCRIMINATE
━━━━━━━━━━━━━━━━━━━━━━

The harness is E-R3's: the first issuer (A) is parked inside its issuance transaction
straight after its challenge INSERT, holding every lock it took and with its ledger row
not yet visible to anyone. The second (B) is then started. A monitor connection must
see B WAITING ON THE NAMED LOCK, held by A; a delay is not evidence. A is released, and
B's answer is decoded. Without the lock, B would count before A's row is visible and
would be admitted; with it, B is refused. On ``02075f2`` there are no limits, so B is
admitted whatever the schedule.

Each worker sends from its own client address unless the case is about one client, so
a wait can only be on the lock the case names. Earlier traffic is produced by real
requests, and the lock keys are an independent statement of the reviewed derivation, so
this module loads and runs against a tree without the limits (``02075f2``), where every
REGRESSION fails on its assertion and every CONTROL passes.
"""
import contextlib
import datetime
import threading

from django.db import connection, transaction
from rest_framework.test import APIClient
from unittest import mock

from users_app.controllers import otp_manager
from users_app.models import OtpIssuance, User
from users_app.tests_challenge_issuance_serialization import (
    BUCKET_LOCK_CLASS, _Bucket, as_oid, spec_key,
)
from users_app.tests_challenge_replacement import (
    INITIATE, LOGIN, PASSWORD, RESEND, WAIT, _owner, _phone,
)
from users_app.tests_otp_limits import (
    REFUSAL, client_key, enforcement, lock_key, phone_key, seed, subject_of,
)

A_ADDRESS, B_ADDRESS, C_ADDRESS = '203.0.113.31', '203.0.113.32', '203.0.113.33'
LOOKUP = '/api/v1/users/msisdn-lookup/'

# The reviewed lock classes, restated rather than imported, so the contract test below
# compares the production values with an independent statement of them.
SUBJECT_CLASS, DESTINATION_CLASS, CLIENT_CLASS = 0x4F545053, 0x4F545044, 0x4F545043


def d11_lock(lock_class, *parts):
    """The ``pg_locks`` row of one D11 lock."""
    return ('advisory', lock_class, as_oid(lock_key(*parts)), 2)


def inline(run):
    run()


class _LimitSchedule(_Bucket):
    # Every issuance draws a code BEFORE its transaction, refused ones included.
    CODES = tuple(str(5400 + n) for n in range(40))

    def setUp(self):
        super().setUp()
        otp_limits = enforcement()
        if otp_limits is not None:
            spawn = mock.patch.object(otp_limits, '_spawn', inline)
            spawn.start()
            self.addCleanup(spawn.stop)
            otp_limits._ALERT_GATE.clear()
            self.addCleanup(otp_limits._ALERT_GATE.clear)

    def assertWaitedOn(self, seen, lock):
        """B waited on THIS lock, and A was the session holding it."""
        self.assertEqual(seen.get('state'), 'waiting', f'B did not wait for A: {seen}')
        self.assertEqual(seen['event'], 'advisory', seen)
        self.assertEqual(seen['waiting_for'], [lock], seen)
        self.assertIn(lock, seen['holder_holds'], seen)
        self.assertTrue(seen['blocked_by_holder'], seen)

    def login(self, user, address):
        return self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD},
                         address=address)

    def register_code(self, phone, address):
        return self.post(RESEND, {'identification': 'msisdn', 'identifier': phone,
                                  'purpose': 'register'}, address=address)

    def assertRefusedAnswer(self, outcome):
        self.assertEqual(outcome, ('ok', (429, REFUSAL)), outcome)


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — two purposes for one account take turns on the subject lock
# ═══════════════════════════════════════════════════════════════════════════════

class SubjectLimitIsExactTests(_LimitSchedule):

    def test_REGRESSION_a_reset_and_a_login_for_one_account_cannot_both_take_the_last_code(self):
        user = _owner('d11-s1')
        seed(2, subject=subject_of(user))
        seen, outcomes = self.race(
            lambda: self.post(INITIATE, {'identifier': user.phone_number}, address=A_ADDRESS),
            lambda: self.login(user, B_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(SUBJECT_CLASS, 'subject', subject_of(user)))
        self.assertOk(outcomes['issuer'])                # the third code: issued
        self.assertRefusedAnswer(outcomes['second'])     # the fourth: refused
        self.assertEqual(OtpIssuance.objects.filter(subject_key=subject_of(user)).count(), 3)
        self.assertNothingHeldWhenSent(['issuer'], user.phone_number)


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — two subjects, one phone, take turns on the destination lock
# ═══════════════════════════════════════════════════════════════════════════════

class DestinationLimitIsExactTests(_LimitSchedule):

    def test_REGRESSION_a_sign_up_code_and_a_login_code_to_one_phone_take_turns(self):
        phone = _phone()
        seed(2, destination=phone_key(phone))
        owner = _owner('d11-s2')
        parked, resume = threading.Event(), threading.Event()
        first_done, second_done, outcomes = threading.Event(), threading.Event(), {}
        with self.park('after_insert')(parked, resume):
            # A: a sign-up code to a phone with no account (its subject is the phone).
            issuer = self.worker('issuer', lambda: self.register_code(phone, A_ADDRESS),
                                 outcomes, first_done)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            # The account takes the phone while A is parked. B is its owner signing in:
            # a different subject, the same destination.
            User.objects.filter(pk=owner.pk).update(phone_number=phone, username=phone)
            owner.refresh_from_db()
            second = self.worker('second', lambda: self.login(owner, B_ADDRESS),
                                 outcomes, second_done)
            second.start()
            try:
                seen = self.watch('second', second_done, holder='issuer')
            finally:
                resume.set()
            self.join(issuer, second)
        self.assertWaitedOn(seen, d11_lock(DESTINATION_CLASS, 'destination', phone_key(phone)))
        self.assertOk(outcomes['issuer'])
        self.assertRefusedAnswer(outcomes['second'])
        self.assertEqual(OtpIssuance.objects.filter(destination_key=phone_key(phone)).count(), 3)


# ═══════════════════════════════════════════════════════════════════════════════
# L3 — two accounts, one client, take turns on the client lock
# ═══════════════════════════════════════════════════════════════════════════════

class ClientLimitIsExactTests(_LimitSchedule):

    def test_REGRESSION_two_accounts_from_one_client_cannot_both_take_the_last_code(self):
        one, two = _owner('d11-s3a'), _owner('d11-s3b')
        for _ in range(19):                 # nineteen earlier codes from this client
            self.assertEqual(self.register_code(_phone(), A_ADDRESS)[0], 200)
        seen, outcomes = self.race(
            lambda: self.login(one, A_ADDRESS),
            lambda: self.login(two, A_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(CLIENT_CLASS, 'otp_issue', client_key(A_ADDRESS)))
        self.assertOk(outcomes['issuer'])
        self.assertRefusedAnswer(outcomes['second'])
        self.assertEqual(OtpIssuance.objects.count(), 20)

    def test_CONTROL_two_accounts_from_two_clients_do_not_wait(self):
        one, two = _owner('d11-s3c'), _owner('d11-s3d')
        seen, outcomes = self.race(
            lambda: self.login(one, A_ADDRESS),
            lambda: self.login(two, B_ADDRESS),
            'after_insert',
        )
        self.assertEqual(seen, {'state': 'finished'}, seen)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])


# ═══════════════════════════════════════════════════════════════════════════════
# L4 — the bounded overshoot, pinned
# ═══════════════════════════════════════════════════════════════════════════════

class GlobalLimitOvershootTests(_LimitSchedule):

    def test_REGRESSION_two_issuers_deciding_at_cap_minus_one_are_both_admitted_and_the_next_is_not(self):
        seed(299, age=datetime.timedelta(hours=1))
        seen, outcomes = self.race(
            lambda: self.register_code(_phone(), A_ADDRESS),
            lambda: self.register_code(_phone(), B_ADDRESS),
            'after_insert',
        )
        # Nothing makes B wait: L4 takes no lock. Both decide at 299, both are admitted.
        self.assertEqual(seen, {'state': 'finished'}, seen)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        self.assertEqual(OtpIssuance.objects.count(), 301)
        self.assertEqual(self.register_code(_phone(), C_ADDRESS), (429, REFUSAL))
        self.assertEqual(OtpIssuance.objects.count(), 301)


# ═══════════════════════════════════════════════════════════════════════════════
# L5 — two lookups from one client take turns
# ═══════════════════════════════════════════════════════════════════════════════

class LookupLimitIsExactTests(_LimitSchedule):

    @staticmethod
    def lookup(address):
        response = APIClient(raise_request_exception=False).get(
            f'{LOOKUP}?msisdn={_phone()}', REMOTE_ADDR=address)
        return response.status_code, response.json()

    @contextlib.contextmanager
    def park_after_count(self, parked, resume):
        """
        Inside the lookup, after its hit is written and before it commits; where there
        is no limit (``02075f2``), at the lookup's own query instead.
        """
        def park():
            if threading.current_thread().name == 'issuer' and not parked.is_set():
                parked.set()
                if not resume.wait(timeout=WAIT):
                    raise RuntimeError('the lookup seam was never released')

        otp_limits = enforcement()
        if otp_limits is None:
            real_filter = User.objects.filter

            def parked_filter(*args, **kwargs):
                park()
                return real_filter(*args, **kwargs)

            with mock.patch.object(User.objects, 'filter', parked_filter):
                yield
            return
        real = otp_limits._count_hit

        def parked_count(*args):
            result = real(*args)
            park()
            return result

        with mock.patch.object(otp_limits, '_count_hit', parked_count):
            yield

    def test_REGRESSION_two_lookups_cannot_both_take_the_last_one(self):
        for _ in range(29):                 # twenty-nine earlier lookups from this client
            self.assertEqual(self.lookup(A_ADDRESS)[0], 404)
        parked, resume = threading.Event(), threading.Event()
        first_done, second_done, outcomes = threading.Event(), threading.Event(), {}
        with self.park_after_count(parked, resume):
            issuer = self.worker('issuer', lambda: self.lookup(A_ADDRESS), outcomes, first_done)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the lookup never reached its seam')
            second = self.worker('second', lambda: self.lookup(A_ADDRESS), outcomes,
                                 second_done)
            second.start()
            try:
                seen = self.watch('second', second_done, holder='issuer')
            finally:
                resume.set()
            self.join(issuer, second)
        self.assertWaitedOn(seen, d11_lock(CLIENT_CLASS, 'msisdn_lookup', client_key(A_ADDRESS)))
        self.assertEqual(outcomes['issuer'][1][0], 404, outcomes)
        self.assertEqual(outcomes['second'], ('ok', (429, REFUSAL)), outcomes)


# ═══════════════════════════════════════════════════════════════════════════════
# The lock order, statement by statement
# ═══════════════════════════════════════════════════════════════════════════════

class LockOrderTests(_LimitSchedule):
    """
    KEY SHARE, bucket lock, subject lock, destination lock, client lock, the decision,
    DELETE, challenge INSERT, ledger INSERT, the client's hit, COMMIT, then the send.
    """

    def test_REGRESSION_the_reviewed_lock_classes_and_derivations(self):
        otp_limits = enforcement()
        self.assertIsNotNone(otp_limits, 'there are no D11 limits on this tree')
        self.assertEqual(
            (otp_limits.SUBJECT_LOCK_CLASS, otp_limits.DESTINATION_LOCK_CLASS,
             otp_limits.CLIENT_LOCK_CLASS),
            (SUBJECT_CLASS, DESTINATION_CLASS, CLIENT_CLASS))
        self.assertEqual(len({SUBJECT_CLASS, DESTINATION_CLASS, CLIENT_CLASS,
                              BUCKET_LOCK_CLASS}), 4)
        for parts in (('subject', 'u:00000000-0000-0000-0000-000000000001'),
                      ('destination', 'p1:' + '0' * 64), ('otp_issue', 'ip1:' + 'a' * 64)):
            self.assertEqual(otp_limits.lock_key(*parts), lock_key(*parts))
        self.assertEqual(
            otp_limits.client_key(otp_limits.Client(A_ADDRESS), otp_manager._otp_pepper()),
            client_key(A_ADDRESS))

    @contextlib.contextmanager
    def recorded(self):
        statements = []
        real_send = otp_manager.send_sms

        def record(execute, sql, params, many, context):
            statements.append((sql, params, connection.in_atomic_block))
            return execute(sql, params, many, context)

        def send(*args, **kwargs):
            statements.append(('<send>', None, transaction.get_connection().in_atomic_block))
            return real_send(*args, **kwargs)

        with mock.patch.object(otp_manager, 'send_sms', send), connection.execute_wrapper(record):
            yield statements

    def test_REGRESSION_a_user_backed_issuance(self):
        user = _owner('d11-order')
        with self.recorded() as statements:
            self.assertEqual(self.login(user, A_ADDRESS)[0], 200)
        advisory = [(list(p), atomic) for sql, p, atomic in statements if 'pg_advisory' in sql]
        self.assertEqual(advisory, [
            ([BUCKET_LOCK_CLASS, spec_key(user.pk, None, 'login')], True),
            ([SUBJECT_CLASS, lock_key('subject', subject_of(user))], True),
            ([DESTINATION_CLASS, lock_key('destination', phone_key(user.phone_number))], True),
            ([CLIENT_CLASS, lock_key('otp_issue', client_key(A_ADDRESS))], True),
        ])
        needles = ['FOR KEY SHARE', 'pg_advisory_xact_lock(', 'FROM "otp_issuances"',
                   'FROM "account_abuse_counters"', 'DELETE FROM "user_otps"',
                   'INSERT INTO "user_otps"', 'INSERT INTO "otp_issuances"',
                   'INTO "account_abuse_counters"', '<send>']
        positions = [next(i for i, (sql, _, _) in enumerate(statements) if n in sql)
                     for n in needles]
        self.assertEqual(positions, sorted(positions), dict(zip(needles, positions)))
        self.assertEqual([statements[i][2] for i in positions],
                         [True] * 8 + [False], 'everything but the send is in the transaction')
        self.assertNothingHeldWhenSent(['MainThread'], user.phone_number)
