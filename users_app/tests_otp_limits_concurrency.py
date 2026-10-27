"""
D11 enforcement on PostgreSQL — independent connections, real transactions.

THE CLAIMS
━━━━━━━━━━

* L1, L2 and L3 are EXACT. An issuer takes the subject (``OTPS``), destination
  (``OTPD``) and, for an unverified issuance, client (``OTPC``) advisory locks, in that
  order, after the bucket lock (``OTPI``) and before it counts. A second issuer for the
  same subject, destination or client therefore waits until the first COMMITS, and then
  counts its row.
* THE CLASSES ARE COUNTED APART, AND EACH STAYS EXACT. The subject and destination locks
  are the same for both classes, so a verified and an unverified issuer for one account
  still take turns; the second then counts only its own class, so the first's row,
  committed by then, does not spend its budget. Each class's count ends exact.
* A VERIFIED ISSUANCE TAKES NO CLIENT LOCK. L3 does not apply to it, so two logins from
  one address do not take turns, and a login does not wait behind an unverified issuer
  from the same address.
* L5 is exact the same way, under the client lock for the lookup scope.
* L4 is NOT exact, deliberately. Two unverified issuers that decide before either
  commits can both be admitted at ``cap - 1``. The overshoot is one per issuance in
  flight; the next issuer is refused.
* The locks sit inside the issuance transaction, in the stated order, and none is held
  when the code is sent.

HOW THESE DISCRIMINATE
━━━━━━━━━━━━━━━━━━━━━━

The harness is E-R3's: the first issuer (A) is parked inside its issuance transaction
straight after its challenge INSERT, holding every lock it took and with its ledger row
not yet visible to anyone. The second (B) is then started. A monitor connection must
see B WAITING ON THE NAMED LOCK, held by A; a delay is not evidence. A is released, and
B's answer is decoded. For a same-class pair: without the lock, B would count before A's
row is visible and would be admitted; with it, B is refused. For a cross-class pair, B
still waits on the shared lock and is then admitted, which on ``e50429d`` (one budget
for both classes) it was not.

WHICH TREE EACH CASE FAILS ON. A REGRESSION fails on the tree before the change that made
it pass: the #374 cases on ``02075f2`` (no limits, so B is admitted whatever the
schedule), the class cases, marked ``(e50429d)``, on #374. Each worker sends from its
own client address unless the case is about one client, so a wait can only be on the
lock the case names. Earlier traffic is produced by real requests or seeded ledger rows
of a named origin, and the lock keys are an independent statement of the reviewed
derivation, so this module loads and runs against either tree.
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
    ACKNOWLEDGEMENT, REFUSAL, UNVERIFIED, UNVERIFIED_ORIGINS, VERIFIED, VERIFIED_ORIGINS,
    client_key, enforcement, issued, lock_key, phone_key, seed, subject_of,
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

    def login(self, user, address=None):
        return self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD},
                         address=address)

    def proof_login(self, user, address=None):
        """A login that names the resend capability; returns its proof (D11 E-R4)."""
        status, body = self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD,
                                         'login_resend_protocol': 'v1'}, address=address)
        self.assertEqual(status, 200, body)
        return body['data']['login_resend_proof']

    def login_resend(self, user, proof, address=None):
        return self.post(RESEND, {'identification': 'id', 'identifier': str(user.pk),
                                  'purpose': 'login', 'login_resend_proof': proof},
                         address=address)

    def reset(self, user, address=None):
        return self.post(INITIATE, {'identifier': user.phone_number}, address=address)

    def resend_reset(self, user, address=None):
        return self.post(RESEND, {'identification': 'id', 'identifier': str(user.pk),
                                  'purpose': 'reset-password'}, address=address)

    def register_code(self, phone, address):
        return self.post(RESEND, {'identification': 'msisdn', 'identifier': phone,
                                  'purpose': 'register'}, address=address)

    def assertRefusedAnswer(self, outcome):
        self.assertEqual(outcome, ('ok', (429, REFUSAL)), outcome)

    def assertClassCounts(self, user, *, verified, unverified):
        """Each class's ledger rows for this account, exactly."""
        self.assertEqual(
            (issued(origins=VERIFIED_ORIGINS, subject=subject_of(user)),
             issued(origins=UNVERIFIED_ORIGINS, subject=subject_of(user))),
            (verified, unverified))


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — two issuers of ONE class for one account take turns on the subject lock
# ═══════════════════════════════════════════════════════════════════════════════

class SubjectLimitIsExactTests(_LimitSchedule):

    def test_REGRESSION_two_unverified_issuers_for_one_account_cannot_both_take_the_last_code(self):
        """
        CHANGED BY THE CLASS CORRECTION. #374 raced a reset against a LOGIN here; those
        are now different classes (see the next class of tests). Two unverified issuers,
        a reset and a reset resend, are in different buckets and one class.
        """
        user = _owner('d11-s1')
        seed(2, origin=UNVERIFIED, subject=subject_of(user))
        seen, outcomes = self.race(
            lambda: self.reset(user, A_ADDRESS),
            lambda: self.resend_reset(user, B_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(SUBJECT_CLASS, 'subject', subject_of(user)))
        self.assertEqual(self.assertOk(outcomes['issuer']), ACKNOWLEDGEMENT)  # the third
        self.assertRefusedAnswer(outcomes['second'])                          # the fourth
        self.assertClassCounts(user, verified=0, unverified=3)
        self.assertNothingHeldWhenSent(['issuer'], user.phone_number)

    def test_REGRESSION_two_verified_issuers_for_one_account_cannot_both_take_the_last_code(self):
        """A password login and a login resend: different buckets, one verified budget."""
        user = _owner('d11-s1v')
        seed(1, origin=VERIFIED, subject=subject_of(user))
        proof = self.proof_login(user, C_ADDRESS)                             # the second
        seen, outcomes = self.race(
            lambda: self.login(user, A_ADDRESS),
            lambda: self.login_resend(user, proof, B_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(SUBJECT_CLASS, 'subject', subject_of(user)))
        self.assertOk(outcomes['issuer'])                                     # the third
        self.assertRefusedAnswer(outcomes['second'])                          # the fourth
        self.assertClassCounts(user, verified=3, unverified=0)
        # (e50429d) The account's anonymous budget is whole.
        self.assertEqual(self.resend_reset(user)[0], 200)


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — a verified and an unverified issuer for one account: each class stays exact
# ═══════════════════════════════════════════════════════════════════════════════

class ClassesAreCountedApartTests(_LimitSchedule):

    def test_REGRESSION_a_login_is_not_counted_against_a_reset_in_flight(self):
        """
        (e50429d) A takes the account's last UNVERIFIED code and is parked holding the
        subject lock. B, a password login, waits on that lock and is then issued: A's row
        is not in its class. Each class then refuses exactly its own fourth code.
        """
        user = _owner('d11-x1')
        seed(2, origin=UNVERIFIED, subject=subject_of(user))
        seen, outcomes = self.race(
            lambda: self.reset(user, A_ADDRESS),
            lambda: self.login(user, B_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(SUBJECT_CLASS, 'subject', subject_of(user)))
        self.assertEqual(self.assertOk(outcomes['issuer']), ACKNOWLEDGEMENT)
        self.assertOk(outcomes['second'])
        self.assertClassCounts(user, verified=1, unverified=3)
        self.assertEqual(self.login(user)[0], 200)
        self.assertEqual(self.login(user)[0], 200)
        self.assertEqual(self.login(user), (429, REFUSAL))
        self.assertEqual(self.resend_reset(user), (429, REFUSAL))
        self.assertClassCounts(user, verified=3, unverified=3)

    def test_REGRESSION_a_reset_is_not_counted_against_a_login_in_flight(self):
        """(e50429d) The reverse: A takes the last VERIFIED code; B, a reset resend, is issued."""
        user = _owner('d11-x2')
        seed(2, origin=VERIFIED, subject=subject_of(user))
        seen, outcomes = self.race(
            lambda: self.login(user, A_ADDRESS),
            lambda: self.resend_reset(user, B_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(SUBJECT_CLASS, 'subject', subject_of(user)))
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        self.assertClassCounts(user, verified=3, unverified=1)
        self.assertEqual(self.reset(user), (200, ACKNOWLEDGEMENT))
        self.assertEqual(self.resend_reset(user)[0], 200)
        self.assertEqual(self.resend_reset(user), (429, REFUSAL))
        self.assertEqual(self.login(user), (429, REFUSAL))
        self.assertClassCounts(user, verified=3, unverified=3)


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — two subjects, one phone, take turns on the destination lock
# ═══════════════════════════════════════════════════════════════════════════════

class DestinationLimitIsExactTests(_LimitSchedule):

    def test_REGRESSION_a_sign_up_code_and_a_reset_code_to_one_phone_take_turns(self):
        """
        CHANGED BY THE CLASS CORRECTION. #374's B was the account's LOGIN, which is now
        verified and has its own destination budget; B is the account's reset resend.
        """
        phone = _phone()
        seed(2, origin=UNVERIFIED, destination=phone_key(phone))
        owner = _owner('d11-s2')
        parked, resume = threading.Event(), threading.Event()
        first_done, second_done, outcomes = threading.Event(), threading.Event(), {}
        with self.park('after_insert')(parked, resume):
            # A: a sign-up code to a phone with no account (its subject is the phone).
            issuer = self.worker('issuer', lambda: self.register_code(phone, A_ADDRESS),
                                 outcomes, first_done)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            # The account takes the phone while A is parked. B is its owner asking for a
            # reset code: a different subject, the same destination, the same class.
            User.objects.filter(pk=owner.pk).update(phone_number=phone, username=phone)
            owner.refresh_from_db()
            second = self.worker('second', lambda: self.resend_reset(owner, B_ADDRESS),
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
        # (e50429d) The owner's login is verified: its destination budget is whole.
        self.assertEqual(self.login(owner, C_ADDRESS)[0], 200)


# ═══════════════════════════════════════════════════════════════════════════════
# L3 — unverified issuers from one client take turns on the client lock; logins do not
# ═══════════════════════════════════════════════════════════════════════════════

class ClientLimitIsExactTests(_LimitSchedule):

    def test_REGRESSION_two_unverified_issuers_from_one_client_cannot_both_take_the_last_code(self):
        """
        CHANGED BY THE CLASS CORRECTION. #374 raced two LOGINS here; logins are verified
        and take no client lock, so the pair is two sign-up codes.
        """
        for _ in range(19):                 # nineteen earlier codes from this client
            self.assertEqual(self.register_code(_phone(), A_ADDRESS)[0], 200)
        seen, outcomes = self.race(
            lambda: self.register_code(_phone(), A_ADDRESS),
            lambda: self.register_code(_phone(), A_ADDRESS),
            'after_insert',
        )
        self.assertWaitedOn(seen, d11_lock(CLIENT_CLASS, 'otp_issue', client_key(A_ADDRESS)))
        self.assertOk(outcomes['issuer'])
        self.assertRefusedAnswer(outcomes['second'])
        self.assertEqual(OtpIssuance.objects.count(), 20)

    def test_REGRESSION_two_logins_from_one_client_do_not_take_turns(self):
        """(e50429d) A verified issuance takes no client lock."""
        one, two = _owner('d11-s3a'), _owner('d11-s3b')
        seen, outcomes = self.race(
            lambda: self.login(one, A_ADDRESS),
            lambda: self.login(two, A_ADDRESS),
            'after_insert',
        )
        self.assertEqual(seen, {'state': 'finished'}, seen)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])

    def test_REGRESSION_a_login_is_neither_held_nor_refused_by_a_spent_client(self):
        """
        (e50429d) Regression (c) under concurrency. A takes this client's last code and
        is parked holding the client lock. B, a login from the same address, neither
        waits for A nor is refused once A commits and the client's hour is spent.
        """
        for _ in range(19):
            self.assertEqual(self.register_code(_phone(), A_ADDRESS)[0], 200)
        owner = _owner('d11-s3c')
        seen, outcomes = self.race(
            lambda: self.register_code(_phone(), A_ADDRESS),
            lambda: self.login(owner, A_ADDRESS),
            'after_insert',
        )
        self.assertEqual(seen, {'state': 'finished'}, seen)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        self.assertEqual(self.register_code(_phone(), A_ADDRESS), (429, REFUSAL))
        self.assertEqual(self.login(_owner('d11-s3d'), A_ADDRESS)[0], 200)

    def test_CONTROL_two_accounts_from_two_clients_do_not_wait(self):
        one, two = _owner('d11-s3e'), _owner('d11-s3f')
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
        seed(299, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
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
    KEY SHARE, bucket lock, subject lock, destination lock, client lock (unverified
    only), the decision, DELETE, challenge INSERT, ledger INSERT, the client's hit
    (unverified only), COMMIT, then the send.
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

    def assertInOrder(self, statements, needles, atomic):
        positions = [next(i for i, (sql, _, _) in enumerate(statements) if n in sql)
                     for n in needles]
        self.assertEqual(positions, sorted(positions), dict(zip(needles, positions)))
        self.assertEqual([statements[i][2] for i in positions], atomic,
                         'everything but the send is in the transaction')

    def test_REGRESSION_an_unverified_user_backed_issuance(self):
        """
        CHANGED BY THE CLASS CORRECTION. #374 pinned this order on a LOGIN, which no
        longer takes the client lock; the full order is an unverified issuance's.
        """
        user = _owner('d11-order')
        with self.recorded() as statements:
            self.assertEqual(self.reset(user, A_ADDRESS), (200, ACKNOWLEDGEMENT))
        advisory = [(list(p), atomic) for sql, p, atomic in statements if 'pg_advisory' in sql]
        self.assertEqual(advisory, [
            ([BUCKET_LOCK_CLASS, spec_key(user.pk, None, 'reset-password')], True),
            ([SUBJECT_CLASS, lock_key('subject', subject_of(user))], True),
            ([DESTINATION_CLASS, lock_key('destination', phone_key(user.phone_number))], True),
            ([CLIENT_CLASS, lock_key('otp_issue', client_key(A_ADDRESS))], True),
        ])
        self.assertInOrder(statements, [
            'FOR KEY SHARE', 'pg_advisory_xact_lock(', 'FROM "otp_issuances"',
            'FROM "account_abuse_counters"', 'DELETE FROM "user_otps"',
            'INSERT INTO "user_otps"', 'INSERT INTO "otp_issuances"',
            'INTO "account_abuse_counters"', '<send>',
        ], [True] * 8 + [False])
        self.assertNothingHeldWhenSent(['MainThread'], user.phone_number)

    def test_REGRESSION_a_verified_issuance_takes_no_client_lock(self):
        """
        (e50429d) A password login: KEY SHARE, bucket, subject and destination locks, the
        ledger statement, and nothing that reads or writes a client's counter.
        """
        user = _owner('d11-order-v')
        with self.recorded() as statements:
            self.assertEqual(self.login(user, A_ADDRESS)[0], 200)
        advisory = [(list(p), atomic) for sql, p, atomic in statements if 'pg_advisory' in sql]
        self.assertEqual(advisory, [
            ([BUCKET_LOCK_CLASS, spec_key(user.pk, None, 'login')], True),
            ([SUBJECT_CLASS, lock_key('subject', subject_of(user))], True),
            ([DESTINATION_CLASS, lock_key('destination', phone_key(user.phone_number))], True),
        ])
        self.assertInOrder(statements, [
            'FOR KEY SHARE', 'pg_advisory_xact_lock(', 'FROM "otp_issuances"',
            'DELETE FROM "user_otps"', 'INSERT INTO "user_otps"',
            'INSERT INTO "otp_issuances"', '<send>',
        ], [True] * 6 + [False])
        send = next(i for i, (sql, _, _) in enumerate(statements) if sql == '<send>')
        # The housekeeping DELETE of expired counter rows runs after the send, for every
        # admitted issuance; it reads no client's hits.
        self.assertEqual(
            [sql for sql, _, _ in statements[:send] if 'account_abuse_counters' in sql], [])
        self.assertEqual(
            [sql for sql, _, _ in statements[send:] if 'account_abuse_counters' in sql
             and not sql.startswith('DELETE FROM "account_abuse_counters"')], [])
        self.assertNothingHeldWhenSent(['MainThread'], user.phone_number)
