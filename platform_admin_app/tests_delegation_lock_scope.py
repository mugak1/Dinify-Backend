"""
Delegation redemption's lock BREADTH — the deadlock proof (PostgreSQL only).

``exchange_code`` locks the grant row so a code cannot be redeemed twice. It used to
lock two more rows by accident: ``select_for_update()`` was chained with
``select_related('administrator', 'restaurant')`` and no ``of=``, and PostgreSQL
applies ``FOR UPDATE`` with no ``OF`` clause to every table in the join. Redemption
therefore held an EXCLUSIVE lock on a ``users`` row and a ``restaurants`` row that it
reads five fields from and writes nothing to.

THE CYCLE that opened, against the lifecycle transition service:

    redemption:  holds users (FOR UPDATE)       -> waits for restaurants
    transition:  holds restaurants (FOR UPDATE) -> waits for users (FOR KEY SHARE,
                                                   taken by the AdminAuditLog insert
                                                   whose actor FK points at that row)

Same administrator, same restaurant, concurrent. PostgreSQL detects it and aborts one
side, so the symptom is a 500 rather than corruption — an availability failure, and
one that only gets more likely. Delegated drill-in is a Phase 1 feature: once an
operator can click into a restaurant from the admin portal, redemption stops being a
rare one-time exchange, and the lifecycle controls sit on the same screen.

The lock ORDER was never wrong — redemption takes all three rows in a single
statement, so it cannot self-deadlock or interleave with itself. Only the BREADTH was.

TWO PROOFS, deliberately. ``LockBreadthTests`` is structural: it pauses redemption
mid-transaction and asks, from a second connection, whether the two rows are locked.
That answer does not depend on any scheduling detail. ``RedemptionTransitionRaceTests``
is the race itself, which is what the operator would actually hit — but whether it
deadlocks depends on which row of the join PostgreSQL marks first inside redemption's
single statement, a planner detail no test should quietly rely on. The structural test
is the one that cannot go silently green for the wrong reason; the race is the one that
reproduces the reported failure. Both fail before the ``of=('self',)`` fix.

Requires a lock-capable backend; skipped on SQLite, where ``select_for_update`` is a
no-op and the whole suite would pass by not running.
"""
import threading
import time
from unittest import mock

from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, tag

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RestaurantStatus_Live,
    RestaurantStatus_Suspended,
)
from platform_admin_app import delegated_sessions, delegation
from platform_admin_app.models import SCOPE_SUPPORT, SCOPE_VIEW, DelegatedSession
from restaurants_app.controllers import lifecycle
from restaurants_app.models import Restaurant
from users_app.models import User

# Distinct phone range: tests.py …01…, tests_transport.py …02…, tests_audit.py …03…,
# tests_auth.py …04…, tests_delegation.py …05…, tests_delegated_session.py …06…,
# this module …07….
_PHONE = iter(f'2567070000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'
GRANT_REASON = 'Diagnosing a reported sold-out item on the live menu.'
TRANSITION_REASON = 'Suspending for the lock-breadth concurrency proof.'


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


def _sync(barrier, times=1):
    """Pass ``times`` barrier phases, tolerating a barrier already broken by a peer.

    A worker that raised before reaching its own phase must not leave the others
    blocked; ``_run``'s join timeout would catch the hang, but the failure it
    reported would be the hang rather than the original error.
    """
    if barrier is None:
        return
    for _ in range(times):
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:       # pragma: no cover - defensive
            return


def _wait_until_blocked(timeout=10):
    """Spin until another backend on this database is WAITING on a lock.

    A barrier only synchronises ARRIVAL. The cycle under test needs the peer to be not
    merely running but genuinely blocked on the row this transaction holds, before this
    transaction reaches for the peer's row — otherwise the two lock requests interleave
    freely, one side simply waits for the other, and the race becomes a coin toss.

    This is the one place in the repo that reads ``pg_stat_activity``; there is no
    other way to answer "is the other backend waiting yet?" from inside the test. It is
    a positioning device only — nothing is asserted about what it returns.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' "
                "AND datname = current_database() "
                "AND pid <> pg_backend_pid()"
            )
            if cursor.fetchone()[0]:
                return True
        time.sleep(0.02)
    return False                                   # pragma: no cover - defensive


# --- fixtures ---------------------------------------------------------------------

def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email, phone_number=phone_number,
        username=username or (phone_number or email), country='Uganda',
        password=PASSWORD, roles=[], account_type=account_type,
    )


def _make_admin(email='lock-scope-admin@t.com', username='lock-scope-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name='Lock Scope Grill', owner=None):
    owner = owner or _make_user(f'owner-{name}@t.com'.replace(' ', '-'))
    return Restaurant.objects.create(
        name=name, location=f'{name} loc', status=RestaurantStatus_Live, owner=owner,
    )


def _mint(admin, restaurant, scope=SCOPE_VIEW):
    """Mint a grant through the real PR-4a service; returns ``(raw_code, grant)``."""
    return delegation.mint_grant(
        administrator=admin, admin_session=None, restaurant=restaurant,
        scope=scope, reason=GRANT_REASON,
    )


# --- workers ----------------------------------------------------------------------
# Each lets the production code own its transaction and closes its connection, per the
# house concurrency-test convention (orders_app/tests_order_admission_concurrency.py).

def _redeem(ids, results, key='redeem', phases=1, barrier=None):
    """The REAL redemption path, raced as-is."""
    try:
        _sync(barrier, phases)
        _token, context = delegated_sessions.exchange_code(ids['code'])
        results[key] = 'ok'
        results[key + '_scope'] = context.scope
        results[key + '_restaurant'] = str(context.restaurant_id)
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        _sync(barrier, phases)
    finally:
        connection.close()


def _redeem_paused(ids, results, key='redeem', barrier=None):
    """Redemption held open at the point where it holds every lock it will ever hold.

    ``delegated_sessions._audit`` is the seam: the grant row is written, the session
    row is inserted, and the AdminAuditLog insert has not happened yet. Whatever the
    locking statement took, it is still held here.
    """
    real_audit = delegated_sessions._audit
    reached = threading.Event()

    def gated_audit(*args, **kwargs):
        reached.set()
        # Phase 1 releases the prober; phase 2 waits until it has released its own
        # locks, so this transaction's audit INSERT is never the thing that blocks.
        _sync(barrier, 2)
        return real_audit(*args, **kwargs)

    try:
        with mock.patch.object(delegated_sessions, '_audit', gated_audit):
            _token, context = delegated_sessions.exchange_code(ids['code'])
        results[key] = 'ok'
        results[key + '_scope'] = context.scope
        results[key + '_restaurant'] = str(context.restaurant_id)
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        if not reached.is_set():
            _sync(barrier, 2)
    finally:
        connection.close()


def _probe_rows(ids, results, key='probe', barrier=None):
    """Ask, from a second connection, whether redemption is holding rows it must not.

    ``NOWAIT`` turns "is this row locked?" into an immediate answer instead of a wait,
    and each probe commits straight away so it cannot become the blocker itself.
    """
    locked = []
    try:
        _sync(barrier)                             # redemption is now paused, holding
        for label, model, pk in (
            ('users', User, ids['admin']),
            ('restaurants', Restaurant, ids['restaurant']),
        ):
            try:
                with transaction.atomic():
                    model.objects.select_for_update(nowait=True).get(pk=pk)
            except DatabaseError:
                locked.append(label)
        results[key] = 'ok' if not locked else 'locked:' + ','.join(locked)
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
    finally:
        _sync(barrier)                             # release redemption
        connection.close()


def _transition(ids, to_state, results, key='transition', barrier=None):
    """The REAL lifecycle writer, paused between its two locks.

    ``lifecycle._audit`` is the seam: by the time it is called the transition holds the
    advisory lock and the ``restaurants`` row and has written the status, but has not
    yet inserted the AdminAuditLog row whose ``actor`` FK reaches for ``users``. That
    is exactly the moment the peer must be blocked — releasing it there, and then
    waiting until it genuinely IS blocked, is what makes the cycle deterministic
    rather than a timing accident.
    """
    real_audit = lifecycle._audit
    reached = threading.Event()

    def gated_audit(*args, **kwargs):
        reached.set()
        _sync(barrier)                             # redemption goes for `users`...
        _wait_until_blocked()                      # ...and blocks on `restaurants`
        return real_audit(*args, **kwargs)

    try:
        with mock.patch.object(lifecycle, '_audit', gated_audit):
            lifecycle.transition_restaurant(
                restaurant=Restaurant.objects.get(pk=ids['restaurant']),
                to_state=to_state,
                reason=TRANSITION_REASON,
                actor=User.objects.get(pk=ids['admin']),
            )
        results[key] = 'ok'
    except Exception as exc:                       # pragma: no cover - defensive
        results[key] = f'error:{exc!r}'
        if not reached.is_set():
            _sync(barrier)
    finally:
        connection.close()


class _Fixture(TransactionTestCase):
    """One platform administrator, one live restaurant, one unredeemed grant."""

    reset_sequences = False

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking requires PostgreSQL')
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()
        self.ids = {
            'admin': self.admin.pk,
            'restaurant': self.restaurant.pk,
            'code': self._fresh_code(),
        }

    def _fresh_code(self, scope=SCOPE_VIEW):
        raw_code, _grant = _mint(self.admin, self.restaurant, scope=scope)
        return raw_code


# --- the structural proof ---------------------------------------------------------

@tag('concurrency')
class LockBreadthTests(_Fixture):
    """Redemption must lock the grant row and nothing else."""

    def test_redemption_does_not_lock_the_administrator_or_restaurant_rows(self):
        """
        The load-bearing assertion of this PR, and the one with no planner dependency.

        With redemption paused mid-transaction, a second connection must be able to
        take ``FOR UPDATE`` on both joined rows. Before ``of=('self',)`` it could take
        neither: ``select_related`` had silently widened the lock to the whole join.
        """
        results = {}
        _run([
            (_redeem_paused, (self.ids, results)),
            (_probe_rows, (self.ids, results)),
        ])

        self.assertEqual(results.get('redeem'), 'ok', results)
        self.assertEqual(
            results.get('probe'), 'ok',
            'redemption is locking rows it only reads: '
            f'{results.get("probe")} (full results: {results})',
        )

    def test_redemption_still_locks_the_grant_row(self):
        """The lock that was always intended is still there — scoped, not removed."""
        results = {}

        def probe_grant(ids, res, key='probe', barrier=None):
            try:
                _sync(barrier)
                from platform_admin_app.models import DelegationGrant
                try:
                    with transaction.atomic():
                        (DelegationGrant.objects
                         .select_for_update(nowait=True)
                         .get(pk=ids['grant']))
                    res[key] = 'unlocked'
                except DatabaseError:
                    res[key] = 'locked'
            except Exception as exc:               # pragma: no cover - defensive
                res[key] = f'error:{exc!r}'
            finally:
                _sync(barrier)
                connection.close()

        raw_code, grant = _mint(self.admin, self.restaurant)
        ids = dict(self.ids, code=raw_code, grant=grant.pk)
        _run([
            (_redeem_paused, (ids, results)),
            (probe_grant, (ids, results)),
        ])

        self.assertEqual(results.get('redeem'), 'ok', results)
        self.assertEqual(results.get('probe'), 'locked', results)


# --- the race --------------------------------------------------------------------

@tag('concurrency')
class RedemptionTransitionRaceTests(_Fixture):
    """The ABBA cycle itself: redemption and a transition, same admin, same restaurant."""

    def test_redemption_and_transition_do_not_deadlock(self):
        """
        Neither side may abort. Both operations are legitimate and both must complete.

        Positioned so each holds its first lock before requesting its second: the
        transition holds ``restaurants`` and pauses at its audit insert, releases the
        redeemer, and waits until the redeemer is genuinely blocked before reaching for
        ``users``. Before ``of=('self',)`` PostgreSQL resolved that by killing one side
        with ``deadlock detected``; the aborted worker's error text lands in ``results``
        and this assertion prints it.
        """
        results = {}
        _run([
            (_transition, (self.ids, RestaurantStatus_Suspended, results)),
            (_redeem, (self.ids, results)),
        ])

        self.assertEqual(results.get('transition'), 'ok', results)
        self.assertEqual(results.get('redeem'), 'ok', results)

        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Suspended)
        self.assertEqual(DelegatedSession.objects.count(), 1)

    def test_the_redeemed_session_is_intact_after_the_race(self):
        """A session that survived the race is still bound to the right tenant and scope."""
        results = {}
        ids = dict(self.ids, code=self._fresh_code(scope=SCOPE_SUPPORT))
        _run([
            (_transition, (ids, RestaurantStatus_Suspended, results)),
            (_redeem, (ids, results)),
        ])

        self.assertEqual(results.get('redeem'), 'ok', results)
        self.assertEqual(results.get('redeem_scope'), SCOPE_SUPPORT, results)
        self.assertEqual(
            results.get('redeem_restaurant'), str(self.restaurant.pk), results,
        )


# --- sequential companions --------------------------------------------------------
# The races prove the locking. These prove the operations themselves, without threads,
# so a failure here is unambiguous about which half broke.

@tag('concurrency')
class SequentialCompanionTests(_Fixture):
    def test_redemption_alone(self):
        _token, context = delegated_sessions.exchange_code(self.ids['code'])

        self.assertEqual(context.scope, SCOPE_VIEW)
        self.assertEqual(context.restaurant_id, str(self.restaurant.pk))
        self.assertEqual(DelegatedSession.objects.count(), 1)

    def test_transition_alone(self):
        lifecycle.transition_restaurant(
            restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
            reason=TRANSITION_REASON, actor=self.admin,
        )

        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Suspended)

    def test_redemption_then_transition(self):
        _token, context = delegated_sessions.exchange_code(self.ids['code'])
        lifecycle.transition_restaurant(
            restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
            reason=TRANSITION_REASON, actor=self.admin,
        )

        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Suspended)
        self.assertEqual(context.restaurant_id, str(self.restaurant.pk))

    def test_transition_then_redemption(self):
        lifecycle.transition_restaurant(
            restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
            reason=TRANSITION_REASON, actor=self.admin,
        )
        _token, context = delegated_sessions.exchange_code(self.ids['code'])

        # Suspension does not invalidate a grant — only deletion does. The scope
        # ceiling for a suspended restaurant is applied per request, not at exchange.
        self.assertEqual(context.restaurant_id, str(self.restaurant.pk))
        self.assertEqual(DelegatedSession.objects.count(), 1)
