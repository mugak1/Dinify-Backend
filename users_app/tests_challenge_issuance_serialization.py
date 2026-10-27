"""
D11 E-R3 — two concurrent issuers of ONE replacement bucket leave ONE spendable challenge.

WHAT WAS WRONG
━━━━━━━━━━━━━━

A replacement bucket is ``(user, msisdn, purpose)``: the rows ``make_otp`` deletes before
it writes a new challenge. Issued one after another, a bucket ends with one live
challenge. Issued at the same time, it could end with two. Each issuer's DELETE ran on a
snapshot that could not see the other's uncommitted row, so both rows survived. For a
login or a password reset both codes then worked: two sessions, or two password resets,
from one bucket.

THE CONTRACT
━━━━━━━━━━━━

Every issuer of a bucket takes a transaction-scoped advisory lock on it, in PostgreSQL's
two-integer form with the first key ``0x4F545049``, AFTER the user's KEY SHARE and BEFORE
the replacement DELETE. A second issuer of the same bucket waits until the first commits.
Its DELETE then sees the first issuer's row and replaces it. The lock is released by the
COMMIT or ROLLBACK, so nothing is held when the code is sent.

That serializes the issuers that take the lock. It does not order requests by network
arrival or by SMS delivery, and it says nothing about a worker still running code that
does not take the lock.

HOW THESE TESTS DISCRIMINATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The ordered schedules park the first issuer (A) inside its issuance transaction, either
straight after its INSERT or at its last statement before COMMIT (straight after the
ledger write), and then start the second (B). A monitor connection must see B WAITING ON
THE BUCKET LOCK: an ungranted advisory lock whose keys are the namespace and this
bucket's key, held by A. A generic delay is not accepted as evidence. A is then released
and B finishes, and the oracle is ROW IDENTITY, not code strings. The row B wrote (RB)
must be the only live one, and A's row (RA) and any earlier seed row (R0) must be gone.
The real redeeming route then refuses A's code, accepts B's, and refuses A's code again
once B's is spent. The first refusal holds with or without the lock, because the consumer
compares only the newest live row, which is B's. What discriminates is row identity and
the refusal after B's code is spent. Where a route refuses everything once it has
succeeded (owner claim, registration), the replaced code is checked through the primitive
it verifies with.

Codes are distinct under ``ENV=test``, so a code identifies the row it was issued for.
Under ``ENV=dev`` every code is ``1234``. That case asserts WHICH ROW the consumer spent
and that nothing is left to spend afterwards. It never infers a row from the digits.

The controls hold with and without the lock: issuers of DIFFERENT buckets never wait on a
BUCKET lock, a rolled-back issuance leaves the bucket to the next issuer, and a redemption
overlapping two issuances completes. Nothing in a control assumes which of two unordered
issuers came first.

D11 ENFORCEMENT ADDED THREE LOCKS AFTER THE BUCKET LOCK (subject, destination, client;
``users_app.otp_limits``), so two issuers for ONE ACCOUNT now take turns on the subject
lock even when their buckets differ, and two issuers from ONE CLIENT take turns on the
client lock. The controls below say which: two accounts or two numbers are sent from two
client addresses and still do not wait, and the one-account controls assert that the wait
is on the subject lock and never on a bucket lock. Those locks are pinned in
``tests_otp_limits_concurrency``.

Delivery is replaced (SMS, e-mail, notification, and the dispatch thread runs inline), and
the code draw and ``ENV`` are fixed so each issuer's code is known in advance. Selection,
locking, accounting and the consumers are the real code, reached over HTTP.

WHAT THIS DOES NOT ESTABLISH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

It does not show the flows are "not exploitable", and it does not refute code guessing
in general: a single live challenge still accepts five wrong guesses before it locks.
It does not cover an issuer running code that takes no lock (a worker on the previous
build during a rollout), and it does not remove duplicate rows that already exist.
The lock key folds the bucket to 32 bits, so two unrelated buckets can share a key; they
then wait for each other, which can add latency and, under a lock timeout, turn a wait
into a failed issuance. The tests pin the lock's place and lifetime, not a wait deadline.
"""
import contextlib
import hashlib
import json
import os
import threading
import time
import types
import uuid
from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError, connection, transaction
from django.db.models.signals import post_save
from django.test import SimpleTestCase
from django.utils import timezone

from platform_admin_app.models import OwnerInvitation
from users_app import otp_accounting
from users_app.controllers import otp_manager
from users_app.controllers.otp_manager import OtpManager
from users_app.models import OtpIssuance, OtpVerificationFailure, User, UserOtp
from users_app.tests_challenge_replacement import (
    CLAIM_CHALLENGE, CLAIM_HEADER, CLAIM_REDEEM, CLAIM_REFUSAL, COMPLETE, DEV_OTP, INITIATE,
    INVALID_OTP, LOGIN, PASSWORD, REGISTER, RESEND, RESET_INVALID, VERIFY, WAIT, _Schedule,
    _claim, _owner, _phone,
)

C0, CA, CB = '5101', '5202', '5303'
SEND_FAILED = "We couldn't send your verification code. Please try again."

# The reviewed lock namespace and key derivation, restated here rather than imported, so
# that this module loads against a tree without them and the contract test below compares
# the production values with an independent statement of them.
BUCKET_LOCK_CLASS = 0x4F545049
BUCKET_DOMAIN = b'dinify.otp-issuance-bucket.v1\x00'


def spec_key(user_pk, msisdn, purpose):
    material = json.dumps(
        [None if user_pk is None else str(user_pk), msisdn, purpose], separators=(',', ':'),
    )
    digest = hashlib.sha256(BUCKET_DOMAIN + material.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big', signed=True)


def as_oid(key):
    """How ``pg_locks`` shows a signed 32-bit advisory key: as an unsigned ``oid``."""
    return key & 0xFFFFFFFF


def bucket_lock(user_pk, msisdn, purpose):
    """The ``(locktype, classid, objid, objsubid)`` row of this bucket's lock in ``pg_locks``."""
    return ('advisory', BUCKET_LOCK_CLASS, as_oid(spec_key(user_pk, msisdn, purpose)), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# The key: one bucket, one key, and nothing that can be mistaken for another bucket
# ═══════════════════════════════════════════════════════════════════════════════

class BucketKeyContractTests(SimpleTestCase):
    """Pure: no database. The key derivation is part of the contract."""

    USER = uuid.UUID('00000000-0000-4000-8000-000000000001')

    @staticmethod
    def user(pk):
        return types.SimpleNamespace(pk=pk)

    def key(self, user_pk, msisdn, purpose):
        user = None if user_pk is None else self.user(user_pk)
        return otp_manager.issuance_bucket_key(user, msisdn, purpose)

    def test_the_namespace_and_the_derivation_are_the_reviewed_ones(self):
        self.assertEqual(otp_manager.ISSUANCE_BUCKET_LOCK_CLASS, BUCKET_LOCK_CLASS)
        for bucket in (
            (self.USER, None, 'login'),
            (self.USER, None, 'reset-password'),
            (self.USER, '256700000001', 'owner-claim'),
            (self.USER, '256700000001', None),
            (None, '256700000001', 'register'),
            (None, '256700000001', None),
        ):
            with self.subTest(bucket=bucket):
                self.assertEqual(self.key(*bucket), spec_key(*bucket))

    def test_golden_values_pin_the_encoding(self):
        """
        A change here is a rollout event, not a refactor: two workers that derive
        different keys for one bucket do not wait for each other.
        """
        self.assertEqual(self.key(self.USER, None, 'login'), 510589375)
        self.assertEqual(self.key(None, '256700000001', 'register'), -1633153524)
        self.assertEqual(self.key(None, '256700000001', None), 1742755570)

    def test_every_component_of_the_bucket_moves_the_key(self):
        other = uuid.UUID('00000000-0000-4000-8000-000000000002')
        base = (self.USER, '256700000001', 'login')
        variants = {
            'user': (other, '256700000001', 'login'),
            'no user': (None, '256700000001', 'login'),
            'msisdn': (self.USER, '256700000002', 'login'),
            'no msisdn': (self.USER, None, 'login'),
            'purpose': (self.USER, '256700000001', 'reset-password'),
            'no purpose': (self.USER, '256700000001', None),
        }
        for label, variant in variants.items():
            with self.subTest(change=label):
                self.assertNotEqual(self.key(*variant), self.key(*base))

    def test_absent_is_not_empty_and_not_the_word_null(self):
        keys = {
            self.key(None, '256700000001', None),
            self.key(None, '256700000001', ''),
            self.key(None, '256700000001', 'null'),
            self.key(None, '256700000001', 'None'),
        }
        self.assertEqual(len(keys), 4)

    def test_a_non_string_value_names_the_bucket_the_delete_names(self):
        """
        The DELETE's lookups coerce a non-string msisdn or purpose to a string
        (``CharField.get_prep_value``). The key coerces the same way, so a direct caller
        passing ``0`` or ``False`` waits with the other issuers of the bucket it deletes
        from, and nothing raises out of the key.
        """
        field = UserOtp._meta.get_field('msisdn')
        for value, as_stored in ((256700000001, '256700000001'), (0, '0'), (False, 'False')):
            with self.subTest(value=value):
                self.assertEqual(field.get_prep_value(value), as_stored)
                self.assertEqual(self.key(None, value, 'register'),
                                 self.key(None, as_stored, 'register'))
                self.assertEqual(self.key(None, '256700000001', value),
                                 self.key(None, '256700000001', as_stored))

    def test_a_separator_inside_a_value_cannot_forge_another_bucket(self):
        self.assertNotEqual(
            self.key(None, '256700000001","login', None),
            self.key(None, '256700000001', 'login'),
        )

    def test_the_key_is_deterministic_and_fits_the_int4_argument(self):
        key = self.key(self.USER, None, 'login')
        self.assertEqual(key, self.key(uuid.UUID(str(self.USER)), None, 'login'))
        self.assertTrue(-2 ** 31 <= key < 2 ** 31)
        self.assertTrue(0 < BUCKET_LOCK_CLASS < 2 ** 31)


# ═══════════════════════════════════════════════════════════════════════════════
# Harness
# ═══════════════════════════════════════════════════════════════════════════════

class _Bucket(_Schedule):
    """
    The E-R2 schedule harness plus: which thread wrote which challenge row, a seam at the
    last statement before COMMIT, what each send could see of its own connection's locks,
    and an observation of WHICH lock a waiting issuer is waiting for.
    """

    def setUp(self):
        super().setUp()
        self.created = []           # (thread name, pk) for every challenge row written

        def record(sender, instance, created, **kwargs):
            if created:
                self.created.append((threading.current_thread().name, str(instance.pk)))

        post_save.connect(record, sender=UserOtp, weak=False, dispatch_uid='er3-created')
        self.addCleanup(post_save.disconnect, sender=UserOtp, dispatch_uid='er3-created')

        self.sends = []

        def sms(message, msisdn, **kwargs):
            self.sends.append({'thread': threading.current_thread().name, 'msisdn': msisdn,
                               **self.locks_held_here()})
            return True

        patcher = mock.patch.object(otp_manager, 'send_sms', side_effect=sms)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- what this connection holds ---

    @staticmethod
    def locks_held_here():
        conn = transaction.get_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE locktype = 'advisory'), "
                "count(*) FILTER (WHERE locktype = 'advisory' AND classid::bigint = %s "
                "                 AND objsubid = 2), "
                "count(*) FILTER (WHERE locktype = 'transactionid') "
                "FROM pg_locks WHERE pid = pg_backend_pid()", [BUCKET_LOCK_CLASS])
            advisory, bucket, xid = cursor.fetchone()
        return {'in_atomic_block': conn.in_atomic_block, 'advisory': advisory,
                'bucket': bucket, 'transactionid': xid}

    def bucket_locks_anywhere(self):
        with self.monitor() as m:
            return m.execute(
                "SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid::bigint = %s AND database = "
                "(SELECT oid FROM pg_database WHERE datname = current_database())",
                [BUCKET_LOCK_CLASS]).fetchall()

    # --- seams ---

    @contextlib.contextmanager
    def park_issuer_before_commit(self, parked, resume):
        """
        Inside the issuance transaction, straight after its ledger write: the last statement
        before COMMIT.
        """
        real = otp_accounting.record_issuance

        def parked_record(**kwargs):
            result = real(**kwargs)
            if threading.current_thread().name == 'issuer' and not parked.is_set():
                parked.set()
                if not resume.wait(timeout=WAIT):
                    raise RuntimeError('the issuer seam was never released')
            return result

        with mock.patch.object(otp_accounting, 'record_issuance', parked_record):
            yield

    def park(self, seam):
        return {'after_insert': self.park_issuer_after_insert,
                'before_commit': self.park_issuer_before_commit}[seam]

    # --- observation ---

    def watch(self, name, done, holder):
        """
        ``finished`` if ``name`` completes without waiting on a lock. Otherwise what it
        waits for: the wait event, the ungranted lock rows of its backend, the advisory
        locks ``holder`` holds, and whether ``holder`` is one of the sessions blocking it.
        """
        deadline = time.monotonic() + WAIT
        with self.monitor() as m:
            while time.monotonic() < deadline:
                if done.is_set():
                    return {'state': 'finished'}
                row = m.execute(
                    'SELECT pid, wait_event_type, wait_event, query FROM pg_stat_activity '
                    'WHERE application_name = %s AND datname = current_database()',
                    [f'er2-{name}'],
                ).fetchone()
                if row and row[1] == 'Lock':
                    pid = row[0]
                    waiting_for = m.execute(
                        'SELECT locktype, classid::bigint, objid::bigint, objsubid '
                        'FROM pg_locks WHERE pid = %s AND NOT granted', [pid]).fetchall()
                    holder_pid = m.execute(
                        'SELECT pid FROM pg_stat_activity WHERE application_name = %s '
                        'AND datname = current_database()', [f'er2-{holder}']).fetchone()
                    blockers = m.execute('SELECT pg_blocking_pids(%s)', [pid]).fetchone()[0]
                    holder_holds = [] if holder_pid is None else m.execute(
                        "SELECT locktype, classid::bigint, objid::bigint, objsubid "
                        "FROM pg_locks WHERE pid = %s AND granted AND locktype = 'advisory'",
                        [holder_pid[0]]).fetchall()
                    return {'state': 'waiting', 'event': row[2], 'query': row[3],
                            'waiting_for': [tuple(r) for r in waiting_for],
                            'holder_holds': [tuple(r) for r in holder_holds],
                            'blocked_by_holder': holder_pid is not None
                            and holder_pid[0] in blockers}
                time.sleep(0.02)
        self.fail(f'{name} neither waited on a lock nor finished within {WAIT}s')

    def assertWaitedOnSubjectNotBucket(self, seen, user):
        """
        B waited on the D11 SUBJECT lock of ``user``, held by A, and on no bucket lock:
        the buckets differ, the account (one L1 subject) does not.
        """
        from users_app import otp_limits
        subject = ('advisory', 0x4F545053,
                   as_oid(otp_limits.lock_key('subject', f'u:{user.pk}')), 2)
        self.assertEqual(seen.get('state'), 'waiting', seen)
        self.assertEqual(seen['waiting_for'], [subject], seen)
        self.assertTrue(seen['blocked_by_holder'], seen)

    def assertWaitedOnBucket(self, seen, expected_lock):
        """B waited on THIS bucket's advisory lock, and A was the session holding it."""
        self.assertEqual(seen.get('state'), 'waiting',
                         f'the second issuer did not wait for the first: {seen}')
        self.assertEqual(seen['event'], 'advisory', f'it waited, but not on the bucket lock: {seen}')
        self.assertEqual(seen['waiting_for'], [expected_lock], seen)
        self.assertIn(expected_lock, seen['holder_holds'], seen)
        self.assertTrue(seen['blocked_by_holder'], seen)

    # --- schedule ---

    def race(self, first, second, seam):
        """
        ``first`` runs as ``issuer`` and is parked inside its issuance transaction at
        ``seam``. ``second`` then starts. Returns what the monitor saw of ``second`` while
        ``issuer`` was parked, and both outcomes after ``issuer`` was released.
        """
        parked, resume = threading.Event(), threading.Event()
        first_done, second_done, outcomes = threading.Event(), threading.Event(), {}
        with self.park(seam)(parked, resume):
            issuer = self.worker('issuer', first, outcomes, first_done)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            other = self.worker('second', second, outcomes, second_done)
            other.start()
            try:
                seen = self.watch('second', second_done, holder='issuer')
            finally:
                resume.set()
            self.join(issuer, other)
        return seen, outcomes

    # --- rows ---

    def created_by(self, thread):
        pks = [pk for name, pk in self.created if name == thread]
        self.assertEqual(len(pks), 1, self.created)
        return pks[0]

    @staticmethod
    def live(**bucket):
        return sorted(
            str(pk) for pk in UserOtp.objects.filter(
                consumed_at__isnull=True, expiry_time__gte=timezone.now(), **bucket,
            ).values_list('pk', flat=True)
        )

    @staticmethod
    def exists(pk):
        return UserOtp.objects.filter(pk=pk).exists()

    def assertSecondReplacedFirst(self, bucket, seed=None):
        """
        RB, the row the WAITING issuer wrote, is the bucket's only live challenge; RA and
        the earlier seed are gone. Returns ``(RA, RB)``.
        """
        ra, rb = self.created_by('issuer'), self.created_by('second')
        self.assertEqual(self.live(**bucket), [rb],
                         'the waiting issuer replaced the first issuer\'s challenge '
                         'and is the only live one')
        self.assertFalse(self.exists(ra), 'the first issuer\'s challenge survived')
        if seed is not None:
            self.assertFalse(self.exists(seed), 'the replaced seed survived')
        return ra, rb

    def assertSpent(self, pk):
        row = UserOtp.objects.get(pk=pk)
        self.assertIsNotNone(row.consumed_at, 'the consumer did not spend this challenge')
        return row

    # --- ledger and dispatch ---

    def assertLedger(self, origin, state=otp_accounting.STATE_ACCEPTED):
        """One issuance row per challenge written, each finalized; a replaced row keeps its own."""
        written = [pk for _, pk in self.created]
        self.assertEqual(OtpIssuance.objects.count(), len(written))
        for pk in written:
            row = OtpIssuance.objects.get(pk=pk)
            self.assertEqual((row.origin, row.state), (origin, state), pk)

    def assertNothingHeldWhenSent(self, threads, destination):
        """One send per issuer call, each after COMMIT with no lock of any kind left."""
        self.assertEqual(sorted(s['thread'] for s in self.sends), sorted(threads), self.sends)
        for send in self.sends:
            self.assertEqual(
                (send['in_atomic_block'], send['advisory'], send['bucket'], send['transactionid']),
                (False, 0, 0, 0), send)
            self.assertEqual(send['msisdn'], destination, send)

    def assertOk(self, outcome, status=200):
        self.assertEqual(outcome[0], 'ok', outcome)
        self.assertEqual(outcome[1][0], status, outcome)
        return outcome[1][1]

    @staticmethod
    def post(url, body, headers=None, address=None):
        cache.clear()   # every route here is throttled; throttling is not under test
        return _Schedule.request(url, body, headers, address)


# ═══════════════════════════════════════════════════════════════════════════════
# REGRESSIONS — one bucket, two concurrent issuers, the second replaces the first
# ═══════════════════════════════════════════════════════════════════════════════

class _LoginBucket(_Bucket):
    """``(user, NULL, 'login')`` — the password login's challenge."""

    def setUp(self):
        super().setUp()
        self.user = _owner('er3-login')
        self.lock = bucket_lock(self.user.pk, None, 'login')

    def login(self):
        return self.post(LOGIN, {'username': self.user.phone_number, 'password': PASSWORD})

    def proof_login(self):
        """A login that names the resend capability; returns the proof it was given (D11 E-R4)."""
        status, body = self.post(LOGIN, {'username': self.user.phone_number, 'password': PASSWORD,
                                         'login_resend_protocol': 'v1'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['data']['require_otp'], body)
        return body['data']['login_resend_proof']

    def login_resend(self, proof):
        return self.post(RESEND, {'identification': 'id', 'identifier': str(self.user.pk),
                                  'purpose': 'login', 'login_resend_proof': proof})

    def verify(self, code):
        return self.post(VERIFY, {'user': str(self.user.pk), 'otp': code})

    def assertOnlyTheSecondLoginCodeWorks(self, seen, outcomes, seed=None):
        self.assertWaitedOnBucket(seen, self.lock)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        ra, rb = self.assertSecondReplacedFirst(
            {'user': self.user, 'msisdn': None, 'purpose': 'login'}, seed)

        self.assertEqual(self.verify(CA), (200, INVALID_OTP),
                         'the replaced code was accepted before the survivor was spent')
        status, body = self.verify(CB)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.user)
        self.assertSpent(rb)
        self.assertEqual(self.verify(CA), (200, INVALID_OTP),
                         'the replaced code minted a second session')
        self.assertEqual(self.live(user=self.user, purpose='login'), [])

        # The one wrong guess before RB was spent was charged to RB, the row compared.
        self.assertEqual(OtpVerificationFailure.objects.filter(issuance_id=rb).count(), 1)
        self.assertEqual(OtpVerificationFailure.objects.filter(issuance_id=ra).count(), 0)
        self.assertLedger(otp_accounting.ORIGIN_PASSWORD_LOGIN)
        threads = (['MainThread'] if seed else []) + ['issuer', 'second']
        self.assertNothingHeldWhenSent(threads, self.user.phone_number)


class PasswordLoginAfterEarlierLoginTests(_LoginBucket):
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        status, body = self.login()
        self.assertTrue(body['data']['require_otp'], body)
        self.seed = str(UserOtp.objects.get(user=self.user, purpose='login').pk)

    def test_parked_after_insert_the_second_login_waits_and_replaces_it(self):
        seen, outcomes = self.race(self.login, self.login, 'after_insert')
        self.assertOnlyTheSecondLoginCodeWorks(seen, outcomes, self.seed)

    def test_parked_before_commit_the_second_login_waits_and_replaces_it(self):
        seen, outcomes = self.race(self.login, self.login, 'before_commit')
        self.assertOnlyTheSecondLoginCodeWorks(seen, outcomes, self.seed)


class FirstPasswordLoginTests(_LoginBucket):
    """No earlier challenge: there is no row for the second issuer's DELETE to wait on."""
    CODES = (CA, CB)

    def test_parked_after_insert(self):
        seen, outcomes = self.race(self.login, self.login, 'after_insert')
        self.assertOnlyTheSecondLoginCodeWorks(seen, outcomes)

    def test_parked_before_commit(self):
        seen, outcomes = self.race(self.login, self.login, 'before_commit')
        self.assertOnlyTheSecondLoginCodeWorks(seen, outcomes)


class _ResetBucket(_Bucket):
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        self.user = _owner('er3-reset')

    def complete(self, code):
        return self.post(COMPLETE, {'identifier': self.user.phone_number, 'otp': code})

    def assertOnlyTheSecondResetCodeWorks(self, seen, outcomes, bucket, seed, origin):
        self.assertWaitedOnBucket(
            seen, bucket_lock(self.user.pk, bucket['msisdn'], 'reset-password'))
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        ra, rb = self.assertSecondReplacedFirst(
            {'user': self.user, 'purpose': 'reset-password', **bucket}, seed)

        self.assertEqual(self.complete(CA), (400, RESET_INVALID))
        before = User.objects.get(pk=self.user.pk).password
        status, body = self.complete(CB)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.user)
        self.assertSpent(rb)
        changed = User.objects.get(pk=self.user.pk).password
        self.assertNotEqual(changed, before)
        self.assertEqual(self.complete(CA), (400, RESET_INVALID),
                         'the replaced code reset the password a second time')
        self.assertEqual(User.objects.get(pk=self.user.pk).password, changed)
        self.assertLedger(origin)
        self.assertNothingHeldWhenSent(['MainThread', 'issuer', 'second'],
                                       self.user.phone_number)


class ResetInitiationBucketTests(_ResetBucket):
    """``(user, NULL, 'reset-password')`` — two reset initiations at once."""

    def setUp(self):
        super().setUp()
        status, body = self.initiate()
        self.assertEqual(status, 200, body)
        self.seed = str(UserOtp.objects.get(user=self.user, purpose='reset-password').pk)

    def initiate(self):
        return self.post(INITIATE, {'identifier': self.user.phone_number})

    def test_parked_after_insert(self):
        seen, outcomes = self.race(self.initiate, self.initiate, 'after_insert')
        self.assertOnlyTheSecondResetCodeWorks(seen, outcomes, {'msisdn': None}, self.seed,
                                               otp_accounting.ORIGIN_RESET_INITIATION)

    def test_parked_before_commit(self):
        seen, outcomes = self.race(self.initiate, self.initiate, 'before_commit')
        self.assertOnlyTheSecondResetCodeWorks(seen, outcomes, {'msisdn': None}, self.seed,
                                               otp_accounting.ORIGIN_RESET_INITIATION)


class ResetResendBucketTests(_ResetBucket):
    """``(user, phone, 'reset-password')`` — two account-resolving resends at once."""

    def setUp(self):
        super().setUp()
        status, body = self.resend()
        self.assertEqual(status, 200, body)
        self.seed = str(UserOtp.objects.get(user=self.user, purpose='reset-password').pk)

    def resend(self):
        return self.post(RESEND, {'identification': 'phone', 'identifier': self.user.phone_number,
                                  'purpose': 'reset-password'})

    def test_parked_after_insert(self):
        seen, outcomes = self.race(self.resend, self.resend, 'after_insert')
        self.assertOnlyTheSecondResetCodeWorks(
            seen, outcomes, {'msisdn': self.user.phone_number}, self.seed,
            otp_accounting.ORIGIN_RESEND_REQUEST)


class OwnerClaimChallengeBucketTests(_Bucket):
    """``(owner, phone, 'owner-claim')`` — two claim challenges at once."""
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        self.owner, self.token, self.invitation = _claim(pending=False)
        status, body = self.challenge()
        self.assertEqual(status, 200, body)
        self.seed = str(UserOtp.objects.get(user=self.owner, purpose='owner-claim').pk)

    def challenge(self):
        return self.post(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: self.token})

    def redeem(self, code):
        return self.post(CLAIM_REDEEM, {'otp': code}, headers={CLAIM_HEADER: self.token})

    def test_parked_after_insert(self):
        seen, outcomes = self.race(self.challenge, self.challenge, 'after_insert')
        self.assertWaitedOnBucket(
            seen, bucket_lock(self.owner.pk, self.owner.phone_number, 'owner-claim'))
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        ra, rb = self.assertSecondReplacedFirst(
            {'user': self.owner, 'msisdn': self.owner.phone_number, 'purpose': 'owner-claim'},
            self.seed)

        self.assertEqual(self.redeem(CA), (400, CLAIM_REFUSAL))
        status, body = self.redeem(CB)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.owner)
        self.assertSpent(rb)
        self.assertEqual(self.redeem(CA), (400, CLAIM_REFUSAL))
        invitation = OwnerInvitation.objects.get(pk=self.invitation)
        self.assertEqual((invitation.claim_failed_attempts, invitation.consumed_at is not None),
                         (1, True))
        # The consumed invitation refuses any code, so the route cannot tell whether A's
        # challenge survived. Ask the primitive redemption verifies with.
        self.assertFalse(self.exists(ra))
        self.assertEqual(
            OtpManager().verify_otp(otp=CA, user_id=self.owner.pk, expected_purpose='owner-claim',
                                    expected_msisdn=self.owner.phone_number),
            INVALID_OTP)
        self.assertLedger(otp_accounting.ORIGIN_OWNER_CLAIM_CHALLENGE)
        self.assertNothingHeldWhenSent(['MainThread', 'issuer', 'second'],
                                       self.owner.phone_number)


class _MsisdnOnlyBucket(_Bucket):
    """
    ``(NULL, msisdn, purpose)`` — a resend for a number no account holds. There is no
    ``users`` row to hold, so the bucket lock is the only thing the two issuers share.
    The consumer is registration, which verifies by msisdn and binds no purpose.
    """

    def setUp(self):
        super().setUp()
        self.phone = _phone()

    def register(self, code):
        return self.post(REGISTER, {'first_name': 'Er', 'last_name': 'Registrant',
                                    'phone_number': self.phone, 'country': 'UG',
                                    'password': PASSWORD, 'otp': code})

    def assertOnlyTheSecondRegistrationCodeWorks(self, seen, outcomes, purpose, seed=None):
        self.assertWaitedOnBucket(seen, bucket_lock(None, self.phone, purpose))
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        ra, rb = self.assertSecondReplacedFirst(
            {'user': None, 'msisdn': self.phone, 'purpose': purpose}, seed)

        status, body = self.register(CA)
        self.assertEqual((status, body.get('message')), (400, 'Invalid OTP.'), body)
        self.assertFalse(User.objects.filter(phone_number=self.phone).exists())
        status, body = self.register(CB)
        self.assertEqual(status, 200, body)
        self.assertTrue(User.objects.filter(phone_number=self.phone).exists())
        self.assertSpent(rb)
        # Once the number has an account the route refuses before it verifies, so the
        # replaced code is checked through the primitive registration verifies with. The
        # route's answer for an existing number is not pinned here.
        self.assertFalse(self.exists(ra))
        self.assertEqual(OtpManager().verify_otp(msisdn=self.phone, otp=CA), INVALID_OTP)
        self.assertLedger(otp_accounting.ORIGIN_RESEND_REQUEST)
        threads = (['MainThread'] if seed else []) + ['issuer', 'second']
        self.assertNothingHeldWhenSent(threads, self.phone)


class RegistrationResendBucketTests(_MsisdnOnlyBucket):
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        status, body = self.resend()
        self.assertEqual(status, 200, body)
        self.seed = str(UserOtp.objects.get(msisdn=self.phone, purpose='register').pk)

    def resend(self):
        return self.post(RESEND, {'identification': 'msisdn', 'identifier': self.phone,
                                  'purpose': 'register'})

    def test_parked_after_insert(self):
        seen, outcomes = self.race(self.resend, self.resend, 'after_insert')
        self.assertOnlyTheSecondRegistrationCodeWorks(seen, outcomes, 'register', self.seed)

    def test_parked_before_commit(self):
        seen, outcomes = self.race(self.resend, self.resend, 'before_commit')
        self.assertOnlyTheSecondRegistrationCodeWorks(seen, outcomes, 'register', self.seed)


class FirstRegistrationResendTests(_MsisdnOnlyBucket):
    """No earlier challenge and no account: before the lock, the two issuers shared nothing."""
    CODES = (CA, CB)

    def resend(self):
        return self.post(RESEND, {'identification': 'msisdn', 'identifier': self.phone,
                                  'purpose': 'register'})

    def test_parked_before_commit(self):
        seen, outcomes = self.race(self.resend, self.resend, 'before_commit')
        self.assertOnlyTheSecondRegistrationCodeWorks(seen, outcomes, 'register')


class NullPurposeResendBucketTests(_MsisdnOnlyBucket):
    """
    ``(NULL, msisdn, NULL)`` — the shape the registration screen's Resend link sends
    (``purpose`` null). An omitted purpose and an explicit null are ONE bucket, so they
    serialize on one lock. The public request shape and answer are unchanged.
    """
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        status, body = self.resend_omitted()
        self.assertEqual((status, body.get('message')), (200, 'OTP sent successfully'), body)
        self.seed = str(UserOtp.objects.get(msisdn=self.phone, purpose__isnull=True).pk)

    def resend_omitted(self):
        return self.post(RESEND, {'identification': 'msisdn', 'identifier': self.phone})

    def resend_null(self):
        return self.post(RESEND, {'identification': 'msisdn', 'identifier': self.phone,
                                  'purpose': None})

    def test_omitted_then_explicit_null_parked_after_insert(self):
        seen, outcomes = self.race(self.resend_omitted, self.resend_null, 'after_insert')
        for name in ('issuer', 'second'):
            self.assertEqual(self.assertOk(outcomes[name]).get('message'), 'OTP sent successfully')
        self.assertOnlyTheSecondRegistrationCodeWorks(seen, outcomes, None, self.seed)

    def test_explicit_null_then_omitted_parked_before_commit(self):
        seen, outcomes = self.race(self.resend_null, self.resend_omitted, 'before_commit')
        self.assertOnlyTheSecondRegistrationCodeWorks(seen, outcomes, None, self.seed)


class LoginResendUnderAnchorTests(_LoginBucket):
    """
    A login resend writes ``(user, phone, 'login')``. The password login's own challenge,
    ``(user, NULL, 'login')``, is the five-minute anchor that admits the resend, and it is
    a DIFFERENT bucket: it survives, as E-R2 decided. The competing resends reduce to the
    second one.
    """
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        self.proof = self.proof_login()
        self.anchor = str(UserOtp.objects.get(user=self.user, purpose='login', msisdn=None).pk)

    def resend(self):
        # Both resends carry the proof this login returned (D11 E-R4). Two sends of one
        # proof are two issuances into one bucket, which is what E-R3 serializes.
        return self.login_resend(self.proof)

    def test_two_resends_under_one_anchor(self):
        seen, outcomes = self.race(self.resend, self.resend, 'after_insert')
        self.assertWaitedOnBucket(seen, bucket_lock(self.user.pk, self.user.phone_number, 'login'))
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        ra, rb = self.assertSecondReplacedFirst(
            {'user': self.user, 'msisdn': self.user.phone_number, 'purpose': 'login'})

        anchor = UserOtp.objects.get(pk=self.anchor)
        self.assertEqual((anchor.consumed_at, anchor.attempts), (None, 0),
                         'the race touched the anchor, which is another bucket')
        self.assertEqual(self.live(user=self.user, purpose='login'), sorted([self.anchor, rb]))

        self.assertEqual(self.verify(CA), (200, INVALID_OTP))
        status, body = self.verify(CB)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.user)
        self.assertSpent(rb)
        self.assertEqual(self.verify(CA), (200, INVALID_OTP))
        self.assertTrue(self.exists(self.anchor))
        self.assertEqual(OtpIssuance.objects.get(pk=ra).origin, otp_accounting.ORIGIN_LOGIN_RESEND)
        self.assertEqual(OtpIssuance.objects.get(pk=rb).origin, otp_accounting.ORIGIN_LOGIN_RESEND)
        self.assertNothingHeldWhenSent(['MainThread', 'issuer', 'second'], self.user.phone_number)


class DevIdenticalCodesTests(_LoginBucket):
    """
    ``ENV=dev``: every code is ``1234``, so a code cannot say which row it was issued for.
    Replacement is established by ROW IDENTITY: which row survived, and which row the
    consumer spent.
    """
    CODES = ('9101', '9102', '9103')   # drawn, then replaced by 1234 in dev

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {'ENV': 'dev'})
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.login()
        self.assertTrue(body['data']['require_otp'], body)
        self.seed = str(UserOtp.objects.get(user=self.user, purpose='login').pk)

    def test_the_survivor_is_the_second_row_and_the_only_one_spendable(self):
        seen, outcomes = self.race(self.login, self.login, 'after_insert')
        self.assertWaitedOnBucket(seen, self.lock)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        ra, rb = self.assertSecondReplacedFirst(
            {'user': self.user, 'msisdn': None, 'purpose': 'login'}, self.seed)

        status, body = self.verify(DEV_OTP)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.user)
        self.assertSpent(rb)
        self.assertEqual(self.verify(DEV_OTP), (200, INVALID_OTP),
                         'a second row accepted the same 1234')
        self.assertEqual(self.live(user=self.user, purpose='login'), [])
        self.assertNothingHeldWhenSent(['MainThread', 'issuer', 'second'], self.user.phone_number)


# ═══════════════════════════════════════════════════════════════════════════════
# A failed lock wait is an ordinary database failure
# ═══════════════════════════════════════════════════════════════════════════════

class FailedLockWaitTests(_LoginBucket):
    """
    The second issuer waits on the bucket lock under a ``lock_timeout``, and the timeout
    fires while the first issuer still holds it. That is a ``DatabaseError`` inside the
    issuance transaction: it rolls back, nothing is written or sent, and the route gives
    its existing delivery-failure answer. The first issuer's challenge is unaffected.
    """
    CODES = (CA, CB)

    def test_a_timed_out_wait_rolls_back_and_sends_nothing(self):
        def second():
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            return self.login()

        parked, resume = threading.Event(), threading.Event()
        first_done, second_done, outcomes = threading.Event(), threading.Event(), {}
        with self.park_issuer_before_commit(parked, resume):
            issuer = self.worker('issuer', self.login, outcomes, first_done)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            other = self.worker('second', second, outcomes, second_done)
            other.start()
            try:
                seen = self.watch('second', second_done, holder='issuer')
                self.assertTrue(second_done.wait(WAIT), 'the lock timeout never fired')
            finally:
                resume.set()
            self.join(issuer, other)

        self.assertWaitedOnBucket(seen, self.lock)
        self.assertEqual(self.assertOk(outcomes['second'], status=500).get('message'), SEND_FAILED)
        self.assertOk(outcomes['issuer'])
        self.assertEqual([name for name, _ in self.created], ['issuer'],
                         'the failed issuance wrote a challenge')
        ra = self.created_by('issuer')
        self.assertEqual(self.live(user=self.user, purpose='login'), [ra])
        self.assertEqual(list(OtpIssuance.objects.values_list('pk', flat=True)), [uuid.UUID(ra)])
        self.assertNothingHeldWhenSent(['issuer'], self.user.phone_number)
        status, body = self.verify(CA)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.user)


# ═══════════════════════════════════════════════════════════════════════════════
# CONTROLS — must hold with and without the lock
# ═══════════════════════════════════════════════════════════════════════════════

class DifferentBucketControls(_Bucket):
    CODES = (CA, CB)

    def test_a_reset_initiation_and_a_login_for_one_account_wait_on_no_bucket_lock(self):
        # CHANGED BY D11 ENFORCEMENT, deliberately. This used to assert no wait at all.
        # The buckets still differ, so neither waits on a BUCKET lock; but one account is
        # one L1 subject, and its issuers now take turns on the subject lock so the
        # account's budget is counted exactly.
        user = _owner('er3-two-purposes')
        seen, outcomes = self.race(
            lambda: self.post(INITIATE, {'identifier': user.phone_number}, address='203.0.113.1'),
            lambda: self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD},
                              address='203.0.113.2'),
            'after_insert',
        )
        self.assertWaitedOnSubjectNotBucket(seen, user)
        self.assertEqual(self.live(user=user, purpose='reset-password'),
                         [self.created_by('issuer')])
        self.assertEqual(self.live(user=user, purpose='login'), [self.created_by('second')])
        status, body = self.post(VERIFY, {'user': str(user.pk), 'otp': CB})
        self.assertTokensFor(body['data'], user)
        status, body = self.post(COMPLETE, {'identifier': user.phone_number, 'otp': CA})
        self.assertEqual(status, 200, body)
        self.assertNothingHeldWhenSent(['issuer', 'second'], user.phone_number)

    def test_two_accounts_logging_in_at_once_do_not_wait(self):
        # Two accounts from two clients: since D11, two issuers from ONE client take turns
        # on that client's lock, which is not what this control is about.
        first, other = _owner('er3-one'), _owner('er3-two')
        seen, outcomes = self.race(
            lambda: self.post(LOGIN, {'username': first.phone_number, 'password': PASSWORD},
                              address='203.0.113.1'),
            lambda: self.post(LOGIN, {'username': other.phone_number, 'password': PASSWORD},
                              address='203.0.113.2'),
            'after_insert',
        )
        self.assertEqual(seen, {'state': 'finished'}, 'issuers for two users serialized')
        self.assertEqual(self.live(user=first, purpose='login'), [self.created_by('issuer')])
        self.assertEqual(self.live(user=other, purpose='login'), [self.created_by('second')])
        for user, code in ((first, CA), (other, CB)):
            status, body = self.post(VERIFY, {'user': str(user.pk), 'otp': code})
            self.assertTokensFor(body['data'], user)

    def test_registration_resends_for_two_numbers_do_not_wait(self):
        one, two = _phone(), _phone()
        seen, outcomes = self.race(
            lambda: self.post(RESEND, {'identification': 'msisdn', 'identifier': one,
                                       'purpose': 'register'}, address='203.0.113.1'),
            lambda: self.post(RESEND, {'identification': 'msisdn', 'identifier': two,
                                       'purpose': 'register'}, address='203.0.113.2'),
            'after_insert',
        )
        self.assertEqual(seen, {'state': 'finished'}, 'issuers for two numbers serialized')
        self.assertEqual(self.live(user=None, msisdn=one, purpose='register'),
                         [self.created_by('issuer')])
        self.assertEqual(self.live(user=None, msisdn=two, purpose='register'),
                         [self.created_by('second')])


class AnchorAndResendControl(_LoginBucket):
    """
    One user, one purpose, two destinations: the password login's anchor
    ``(user, NULL, 'login')`` and a login resend ``(user, phone, 'login')`` are different
    buckets, so a new password login and a resend do not wait on a BUCKET lock. Since D11
    they are one L1 subject, and take turns on the subject lock instead.
    """
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        self.proof = self.proof_login()

    def test_a_password_login_and_a_login_resend_wait_on_no_bucket_lock(self):
        # The resend carries the first login's proof. The second login's replacement of
        # that anchor is not committed while it is parked, so the proof still matches.
        # CHANGED BY D11 ENFORCEMENT: this used to assert no wait at all; see the class.
        seen, outcomes = self.race(
            self.login,
            lambda: self.login_resend(self.proof),
            'after_insert',
        )
        self.assertWaitedOnSubjectNotBucket(seen, self.user)
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        self.assertEqual(self.live(user=self.user, msisdn=None, purpose='login'),
                         [self.created_by('issuer')])
        self.assertEqual(self.live(user=self.user, msisdn=self.user.phone_number,
                                   purpose='login'), [self.created_by('second')])


class RolledBackIssuanceControl(_Bucket):
    """An issuance that fails after its INSERT rolls back and leaves the bucket to the next."""
    CODES = (C0, CA, CB)

    def test_a_failed_issuance_leaves_the_bucket_to_the_next_issuer(self):
        user = _owner('er3-rollback')
        self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD})
        seed = str(UserOtp.objects.get(user=user, purpose='login').pk)
        real = otp_accounting.record_issuance

        def fail_for_issuer(**kwargs):
            if threading.current_thread().name == 'issuer':
                raise DatabaseError('synthetic ledger failure')
            return real(**kwargs)

        login = lambda: self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD})  # noqa: E731
        with mock.patch.object(otp_accounting, 'record_issuance', fail_for_issuer):
            seen, outcomes = self.race(login, login, 'after_insert')

        self.assertEqual(seen['state'], 'waiting', seen)
        self.assertEqual(self.assertOk(outcomes['issuer'], status=500).get('message'), SEND_FAILED)
        self.assertOk(outcomes['second'])
        self.assertFalse(self.exists(self.created_by('issuer')))
        self.assertFalse(self.exists(seed))
        self.assertEqual(self.live(user=user, purpose='login'), [self.created_by('second')])
        self.assertNothingHeldWhenSent(['MainThread', 'second'], user.phone_number)
        self.assertEqual(self.bucket_locks_anywhere(), [])


class RedemptionOverlapControl(_Bucket):
    """
    A claim challenge parked inside its transaction, a second challenge for the same
    bucket, and a redemption with the code the second challenge issues. All three
    complete: the redemption waits for the owner's row, then succeeds.
    """
    CODES = (C0, CA, CB)

    def test_two_challenges_and_a_redemption_all_complete(self):
        owner, token, invitation = _claim(pending=False)
        challenge = lambda: self.post(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})  # noqa: E731
        status, body = challenge()
        self.assertEqual(status, 200, body)

        parked, resume = threading.Event(), threading.Event()
        done = {name: threading.Event() for name in ('issuer', 'second', 'redeemer')}
        outcomes = {}
        with self.park_issuer_after_insert(parked, resume):
            issuer = self.worker('issuer', challenge, outcomes, done['issuer'])
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            second = self.worker('second', challenge, outcomes, done['second'])
            second.start()
            try:
                seen_second = self.watch('second', done['second'], holder='issuer')
                redeemer = self.worker(
                    'redeemer',
                    lambda: self.post(CLAIM_REDEEM, {'otp': CB}, headers={CLAIM_HEADER: token}),
                    outcomes, done['redeemer'])
                redeemer.start()
                seen_redeemer = self.watch('redeemer', done['redeemer'], holder='issuer')
            finally:
                resume.set()
            self.join(issuer, second, redeemer)

        self.assertEqual(seen_second['state'], 'waiting', seen_second)
        self.assertEqual(seen_redeemer['state'], 'waiting', seen_redeemer)
        self.assertIn('FROM "users"', seen_redeemer['query'])
        self.assertOk(outcomes['issuer'])
        self.assertOk(outcomes['second'])
        status, body = outcomes['redeemer'][1]
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], owner)
        row = OwnerInvitation.objects.get(pk=invitation)
        self.assertEqual((row.claim_failed_attempts, row.consumed_at is not None), (0, True))
        self.assertNothingHeldWhenSent(['MainThread', 'issuer', 'second'], owner.phone_number)


# ═══════════════════════════════════════════════════════════════════════════════
# Where the lock sits, which bucket it names, and when it is released
# ═══════════════════════════════════════════════════════════════════════════════

class IssuanceTransactionOrderTests(_Bucket):
    """
    One issuance, statement by statement, on the connection the transaction is open on:
    KEY SHARE (user-backed only), bucket lock, replacement DELETE, challenge INSERT,
    ledger INSERT, COMMIT, then the send and the ledger's finalization.
    """
    CODES = (CA, CB)

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

    @staticmethod
    def first(statements, needle):
        hits = [i for i, (sql, _, _) in enumerate(statements) if needle in sql]
        return hits[0] if hits else None

    def locks(self, statements):
        # The BUCKET locks. Since D11 enforcement the subject, destination and client
        # locks follow this one in the same transaction (pinned in
        # tests_otp_limits_concurrency.LockOrderTests); they are not bucket locks.
        return [(params, atomic) for sql, params, atomic in statements
                if 'pg_advisory' in sql and list(params)[0] == BUCKET_LOCK_CLASS]

    def assertOneBucketLock(self, statements, user_pk, msisdn, purpose):
        locks = self.locks(statements)
        self.assertEqual(len(locks), 1, f'expected exactly one bucket lock: {locks}')
        params, atomic = locks[0]
        self.assertEqual(list(params), [BUCKET_LOCK_CLASS, spec_key(user_pk, msisdn, purpose)])
        self.assertTrue(atomic, 'the bucket lock must sit inside the issuance transaction')
        sql = statements[self.first(statements, 'pg_advisory')][0]
        self.assertIn('pg_advisory_xact_lock(', sql, 'a session-level lock would outlive the commit')

    def assertOrder(self, statements, needles):
        positions = [self.first(statements, n) for n in needles]
        self.assertNotIn(None, positions, dict(zip(needles, positions)))
        self.assertEqual(positions, sorted(positions), dict(zip(needles, positions)))
        return positions

    def test_a_user_backed_issuance(self):
        user = _owner('er3-order')
        with self.recorded() as statements:
            self.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD})
        self.assertOneBucketLock(statements, user.pk, None, 'login')
        order = self.assertOrder(statements, [
            'FOR KEY SHARE', 'pg_advisory_xact_lock(', 'DELETE FROM "user_otps"',
            'INSERT INTO "user_otps"', 'INSERT INTO "otp_issuances"', '<send>',
            'UPDATE "otp_issuances"',
        ])
        self.assertEqual([statements[i][2] for i in order],
                         [True, True, True, True, True, False, False])

    def test_an_msisdn_only_issuance_takes_the_bucket_lock_and_no_user_lock(self):
        phone = _phone()
        with self.recorded() as statements:
            self.post(RESEND, {'identification': 'msisdn', 'identifier': phone,
                               'purpose': 'register'})
        self.assertOneBucketLock(statements, None, phone, 'register')
        self.assertIsNone(self.first(statements, 'FOR KEY SHARE'))
        self.assertOrder(statements, ['pg_advisory_xact_lock(', 'DELETE FROM "user_otps"',
                                      'INSERT INTO "user_otps"', '<send>'])

    def test_an_omitted_and_an_explicit_null_purpose_name_one_bucket(self):
        phone = _phone()
        for body in ({'identification': 'msisdn', 'identifier': phone},
                     {'identification': 'msisdn', 'identifier': phone, 'purpose': None}):
            with self.subTest(body=body), self.recorded() as statements:
                self.post(RESEND, body)
                self.assertOneBucketLock(statements, None, phone, None)

    def test_the_owner_claim_challenge_names_its_destination_bucket(self):
        owner, token, _ = _claim(pending=False)
        with self.recorded() as statements:
            status, body = self.post(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertEqual(status, 200, body)
        self.assertOneBucketLock(statements, owner.pk, owner.phone_number, 'owner-claim')


class BucketLockLifetimeTests(_Bucket):
    """
    Held inside the issuance transaction, gone at COMMIT: not at the sender, not after the
    request. A session-level lock passes the first check and fails the other two here,
    by assertion rather than by a timeout somewhere else.
    """
    CODES = (CA, CB)

    def test_held_inside_the_transaction_and_released_by_its_commit(self):
        user = _owner('er3-lifetime')
        inside = []
        real = otp_accounting.record_issuance

        def observe(**kwargs):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT locktype, classid::bigint, objid::bigint, objsubid FROM pg_locks "
                    "WHERE pid = pg_backend_pid() AND locktype = 'advisory' AND granted")
                inside.extend(tuple(r) for r in cursor.fetchall())
            return real(**kwargs)

        with mock.patch.object(otp_accounting, 'record_issuance', observe):
            status, body = self.post(LOGIN, {'username': user.phone_number,
                                             'password': PASSWORD})
        self.assertEqual(status, 200, body)
        self.assertEqual([lock for lock in inside if lock[1] == BUCKET_LOCK_CLASS],
                         [bucket_lock(user.pk, None, 'login')],
                         'the issuance did not hold exactly its own bucket lock')
        # Beside it, only the D11 subject and destination locks (one each). CHANGED BY THE
        # D11 CLASS CORRECTION: a password login is a verified issuance, which takes no
        # client lock (``OTPC``, 0x4F545043); #374 expected that lock here too.
        self.assertEqual(sorted(lock[1] for lock in inside if lock[1] != BUCKET_LOCK_CLASS),
                         sorted([0x4F545053, 0x4F545044]), inside)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.sends[0]['bucket'], 0,
                         'the bucket lock was still held when the code was sent')
        self.assertEqual(self.sends[0]['advisory'], 0)
        self.assertEqual(self.bucket_locks_anywhere(), [],
                         'the bucket lock outlived the request that took it')

    def test_the_lock_is_refused_outside_a_transaction(self):
        user = _owner('er3-guard')
        with self.assertRaises(RuntimeError):
            otp_manager._hold_bucket(user, None, 'login')
        self.assertEqual(self.bucket_locks_anywhere(), [])
