"""
PR-C concurrency suite — real contention against the admin authentication row locks.

Three races were open before PR-C, all because nothing was locked:

* ``resolve_challenge`` took no lock and ``consume`` wrote ``consumed_at``
  unconditionally, so two requests could resolve the SAME challenge, verify the same
  TOTP counter, both consume, and both mint a session;
* the same race spent one recovery code twice;
* ``lockout.register_failure`` incremented in memory and saved, so parallel failures
  overwrote one another and the durable counter — the only guarantee that survives a
  worker restart — could be outrun by attacking in parallel.

Requires a lock-capable backend (skipped on SQLite, where ``select_for_update`` is a
silent no-op and these tests would false-pass). Mirrors the harness in
``restaurants_app/tests_menu_relationships_concurrency.py``: module-level workers that
take PKs and tokens rather than model instances, a ``threading.Barrier`` for true
contention instead of sleeps, per-thread ``connection.close()``, and
``join(timeout=…)`` + ``is_alive()`` so a lock regression becomes a named failure
rather than a hung CI job.
"""
import threading

from django.core.cache import cache
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings, tag
from django.utils import timezone

from platform_admin_app import challenges, lockout, recovery
from platform_admin_app.cookies import challenge_cookie_name
from platform_admin_app.models import (
    AdminLoginChallenge, AdminSession, PlatformStaffAuth,
)
from platform_admin_app.second_factor import METHOD_RECOVERY, METHOD_TOTP
from platform_admin_app.tests_auth import PASSWORD, _ADMIN_OVERRIDES, _code, _make_admin

VERIFY_URL = '/admin/v1/auth/verify/'
LOGIN_URL = '/admin/v1/auth/login/'


def _verify_worker(raw_challenge, method, code, results, key, barrier=None):
    """
    POST one verification with ``raw_challenge`` in the cookie jar.

    Each thread builds its OWN Client so the two requests are genuinely independent,
    and both carry the same challenge token — the collision under test. The barrier is
    awaited immediately before the POST so both land inside the view together; the row
    lock supplies the real blocking.
    """
    try:
        client = Client()
        client.cookies[challenge_cookie_name()] = raw_challenge
        if barrier is not None:
            barrier.wait(timeout=10)
        response = client.post(
            VERIFY_URL,
            data={'method': method, 'code': code},
            content_type='application/json',
        )
        results[key] = 'ok' if response.status_code == 200 else str(response.status_code)
    except Exception as exc:  # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
    finally:
        connection.close()


def _failed_login_worker(username, results, key, barrier=None):
    """One wrong-password login, to race the durable failure counter."""
    try:
        client = Client()
        if barrier is not None:
            barrier.wait(timeout=10)
        response = client.post(
            LOGIN_URL,
            data={'username': username, 'password': 'definitely-wrong'},
            content_type='application/json',
        )
        results[key] = str(response.status_code)
    except Exception as exc:  # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
    finally:
        connection.close()


def _run(workers):
    """Start every worker at a shared barrier, join, and fail loudly on a hang."""
    barrier = threading.Barrier(len(workers))
    threads = [
        threading.Thread(target=fn, args=args, kwargs={'barrier': barrier})
        for fn, args in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    for index, thread in enumerate(threads):
        assert not thread.is_alive(), f'worker {index} hung (lock regressed)'


@tag('concurrency')
@override_settings(**_ADMIN_OVERRIDES)
class AdminVerifyConcurrencyTests(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking requires PostgreSQL')
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='race-admin',
        )

    def _fresh_challenge(self):
        """Log in on the main thread and hand the raw challenge token to the threads."""
        client = Client()
        response = client.post(
            LOGIN_URL,
            data={'username': self.user.username, 'password': PASSWORD},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return response.cookies[challenge_cookie_name()].value

    def _reset(self):
        """
        Ready the account for another round. Returns the current session count.

        Sessions are counted rather than deleted: ``AdminAuditLog.session`` is a
        PROTECT FK, so the audit rows from earlier rounds pin their sessions in place —
        the append-only log doing its job. Each round asserts a delta of exactly one.

        Also clears the DRF throttle counters: every round fires several requests from
        one apparent client inside the same minute, so the per-IP throttle would answer
        429 long before the row lock — the actual property under test — mattered. The
        throttles are defence in depth; the lock and the durable counter are the
        guarantees.
        """
        cache.clear()
        PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
            failed_attempts=0, locked_until=None, last_totp_counter=None,
        )
        return AdminSession.objects.count()

    def test_same_challenge_and_totp_code_mints_exactly_one_session(self):
        for _ in range(6):
            before = self._reset()
            raw = self._fresh_challenge()
            code = _code(self.secret)
            results = {}
            _run([
                (_verify_worker, (raw, METHOD_TOTP, code, results, 'a')),
                (_verify_worker, (raw, METHOD_TOTP, code, results, 'b')),
            ])

            self.assertNotIn(
                'error', str(results.get('a')) + str(results.get('b')), results,
            )
            wins = [k for k, v in results.items() if v == 'ok']
            self.assertEqual(len(wins), 1, f'expected exactly one winner: {results}')
            minted = AdminSession.objects.count() - before
            self.assertEqual(
                minted, 1, f'concurrent verify minted {minted} sessions: {results}',
            )
            # The loser is a generic denial, never a 500.
            loser = [v for k, v in results.items() if v != 'ok']
            self.assertEqual(loser, ['401'], results)

    def test_same_recovery_code_is_consumed_exactly_once(self):
        for _ in range(6):
            before = self._reset()
            # A fresh set each round, keeping the plaintext so the round has a
            # known-good code to race.
            codes, hashes = recovery.generate_codes()
            PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
                recovery_code_hashes=hashes,
            )
            raw = self._fresh_challenge()
            results = {}
            _run([
                (_verify_worker, (raw, METHOD_RECOVERY, codes[0], results, 'a')),
                (_verify_worker, (raw, METHOD_RECOVERY, codes[0], results, 'b')),
            ])

            wins = [k for k, v in results.items() if v == 'ok']
            self.assertEqual(len(wins), 1, f'expected exactly one winner: {results}')
            self.assertEqual(AdminSession.objects.count() - before, 1, results)

            self.auth.refresh_from_db()
            self.assertEqual(
                recovery.remaining(self.auth), 9,
                'the recovery code was spent more than once',
            )

    def test_parallel_failures_do_not_lose_increments(self):
        """
        N concurrent wrong passwords must produce a count of N.

        This is the durable guarantee: the DRF throttles are per-process
        ``LocMemCache`` and reset on restart, so if this counter can be outrun by
        parallelism there is no effective lockout at all.
        """
        self._reset()
        attempts = 8
        results = {}
        _run([
            (_failed_login_worker, (self.user.username, results, str(i)))
            for i in range(attempts)
        ])

        self.assertFalse(
            [v for v in results.values() if str(v).startswith('error')], results,
        )
        self.auth.refresh_from_db()
        self.assertEqual(
            self.auth.failed_attempts, attempts,
            f'lost increments under concurrency: {self.auth.failed_attempts} '
            f'of {attempts} recorded',
        )
        # Below the threshold, so still unlocked — the count is the assertion.
        self.assertLess(attempts, lockout.threshold())
        self.assertFalse(lockout.is_locked(self.auth))


def _successful_login_worker(username, results, key, barrier=None):
    """One CORRECT-password login, to race challenge minting."""
    try:
        client = Client()
        if barrier is not None:
            barrier.wait(timeout=10)
        response = client.post(
            LOGIN_URL,
            data={'username': username, 'password': PASSWORD},
            content_type='application/json',
        )
        results[key] = str(response.status_code)
    except Exception as exc:  # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
    finally:
        connection.close()


@tag('concurrency')
@override_settings(**_ADMIN_OVERRIDES)
class AdminLoginChallengeConcurrencyTests(TransactionTestCase):
    """
    A fourth race, closed after PR-C: ``create_challenge`` left two live challenges.

    ``challenges.create_challenge`` promised that a fresh password submission
    invalidates the previous half-finished attempt, but it consumed and inserted in
    two separate autocommitted statements with no lock. Two simultaneous
    correct-password logins could interleave ``UPDATE → UPDATE → INSERT → INSERT``
    and leave the account holding TWO spendable challenges.

    It could never reproduce PR-C's double-session bug — verification serialises on
    ``PlatformStaffAuth`` and a consumed factor cannot be respent — so this is about
    the promise being true rather than nearly true. It is now held twice over: a
    ``User`` row lock serialises the minting, and the partial unique index
    ``one_live_admin_challenge_per_user`` enforces it in the database.
    """

    reset_sequences = False

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking requires PostgreSQL')
        super().setUp()
        self.user, self.auth, self.secret, self.codes = _make_admin(
            username='challenge-race-admin',
        )

    def _reset(self):
        cache.clear()
        PlatformStaffAuth.objects.filter(pk=self.auth.pk).update(
            failed_attempts=0, locked_until=None, last_totp_counter=None,
        )

    def _live_challenges(self):
        return AdminLoginChallenge.objects.filter(
            user=self.user, consumed_at__isnull=True,
        ).count()

    def test_concurrent_logins_leave_exactly_one_live_challenge(self):
        for _ in range(6):
            self._reset()
            AdminLoginChallenge.objects.filter(user=self.user).delete()

            results = {}
            _run([
                (_successful_login_worker, (self.user.username, results, str(i)))
                for i in range(4)
            ])

            # Every login must SUCCEED — the point is that they serialise, not that
            # the loser is turned away. A 500 here would mean the unique index fired
            # as an IntegrityError instead of the lock doing its job.
            self.assertEqual(
                sorted(results.values()), ['200'] * 4,
                f'a concurrent login did not return 200: {results}',
            )
            self.assertEqual(
                self._live_challenges(), 1,
                'concurrent logins left more than one spendable challenge',
            )

    def test_the_surviving_challenge_is_the_one_handed_to_its_caller(self):
        """
        Serialising must not orphan the winner: the live row is a real, usable one.

        A lock that produced exactly one row by discarding the token its own caller
        was handed would satisfy the count above and still be broken — the browser
        would hold a challenge the server had already consumed.
        """
        self._reset()
        AdminLoginChallenge.objects.filter(user=self.user).delete()

        results = {}
        _run([
            (_successful_login_worker, (self.user.username, results, str(i)))
            for i in range(3)
        ])

        live = AdminLoginChallenge.objects.filter(
            user=self.user, consumed_at__isnull=True,
        )
        self.assertEqual(live.count(), 1)

        # Every condition `resolve_challenge` checks, so the survivor is spendable
        # rather than a husk that would fail at verify/.
        challenge = live.first()
        self.assertIsNone(challenge.consumed_at)
        self.assertEqual(challenge.attempts, 0)
        self.assertLess(challenge.attempts, challenges.max_attempts())
        self.assertGreater(challenge.expires_at, timezone.now())
        self.assertFalse(challenge.recovery_only)
