"""
Real contention against ``POST admin/v1/restaurants/`` (Phase 1, Step 2D).

Creation writes six rows for one decision, and the decisions it has to get right
under concurrency are exactly the ones a double-clicking operator produces: two
identical submissions, and a world that changes between validating an owner and
writing to it.

WHAT SERIALIZES WHAT, and why there are two mechanisms rather than one:

* an EXISTING owner is locked with ``select_for_update`` — the restaurant does not
  exist yet, so the owner row is the only thing two concurrent creations share;
* a NEW owner has no row to lock, so the ``User.phone_number`` unique index is the
  serialization point, and the savepoint around the INSERT is what turns a lost race
  into a named 409 instead of an ``IntegrityError`` that would poison the outer
  transaction;
* the SAME-OWNER duplicate restaurant is backed by
  ``Restaurant.Meta.unique_together (name, location, owner)``, with the owner lock in
  front of it.

Requires a lock-capable backend (skipped on SQLite, where ``select_for_update`` is a
silent no-op and these tests would false-pass). Mirrors the harness in
``platform_admin_app/tests_admin_auth_concurrency.py``: module-level workers taking
PKs and tokens rather than model instances, a ``threading.Barrier`` for true
contention instead of sleeps, per-thread ``connection.close()``, and
``join(timeout=…)`` + ``is_alive()`` so a lock regression becomes a named failure
rather than a hung CI job.

TWO RACES THIS DOES NOT CLOSE, stated rather than papered over. Both are the same
shape — a ``SELECT`` that takes no predicate lock under READ COMMITTED, guarding a
column pair the schema does not constrain — and both are recorded in CLAUDE.md:

* two simultaneous creations naming the SAME restaurant under DIFFERENT owners. The
  same-owner case, which is the double-click an operator actually produces, IS closed
  by ``unique_together`` with the owner lock in front of it;
* two simultaneous creations with DIFFERENT phones and the SAME owner email.
  ``User.email`` has no unique constraint, so the email pre-check is best-effort —
  unlike the phone check, which the ``phone_number`` unique index makes race-free.
  This is an existing repository-wide seam rather than one this surface introduced:
  ``self_register`` and ``update_user_profile`` carry the identical non-atomic check.

Closing either needs a schema change (a partial unique index — a CONTRACT migration
that fails at deploy against a corpus already holding duplicates) rather than more
care in this module, so neither is closed here and neither is papered over with a lock
domain that would only serialise this endpoint against itself.
"""
import json
import threading
import time

from django.db import connection, transaction
from django.test import Client, TransactionTestCase, override_settings

from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from platform_admin_app import sessions
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import (
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.tests_restaurant_creation_endpoint import (
    COLLECTION_URL,
    _ADMIN_OVERRIDES,
    _make_admin,
    _make_user,
    creation_body,
    new_owner_body,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

# How long a worker may take before we call it hung rather than slow.
JOIN_TIMEOUT = 20


def _create_worker(raw_session, body, results, key, barrier=None):
    """
    POST one creation with ``raw_session`` in the cookie jar.

    Each thread builds its OWN client so the requests are genuinely independent. The
    barrier is awaited immediately before the POST so both land in the view together;
    the row lock and the unique index supply the real blocking.
    """
    try:
        client = Client()
        client.cookies[cookie_name()] = raw_session
        if barrier is not None:
            barrier.wait(timeout=10)
        response = client.post(
            COLLECTION_URL, data=body, content_type='application/json',
        )
        payload = {}
        try:
            payload = json.loads(response.content or b'{}')
        except ValueError:  # pragma: no cover - defensive
            pass
        results[key] = (response.status_code, payload.get('code'))
    except Exception as exc:  # pragma: no cover - defensive
        results[key] = ('error', repr(exc))
    finally:
        connection.close()


def _hold_and_mutate_worker(user_pk, updates, barrier, results):
    """
    Lock one ``User`` row, then change it and commit — the world moving mid-request.

    The lock is taken BEFORE the barrier releases, so the request thread meets it
    already held. The short sleep afterwards only biases towards the interesting
    interleaving (the request genuinely BLOCKING on the lock); the assertion holds
    either way, because a request that arrives after the commit simply reads the
    changed row.
    """
    try:
        with transaction.atomic():
            User.objects.select_for_update().filter(pk=user_pk).first()
            barrier.wait(timeout=10)
            time.sleep(0.3)
            User.objects.filter(pk=user_pk).update(**updates)
        results['holder'] = 'committed'
    except Exception as exc:  # pragma: no cover - defensive
        results['holder'] = f'error:{exc!r}'
    finally:
        connection.close()


@override_settings(**_ADMIN_OVERRIDES)
class _CreationConcurrencyTestCase(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('row-level locking requires PostgreSQL')
        self.admin = _make_admin(
            email='rcc-admin@t.com', username='rcc-admin',
        )
        self.raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)

    def run_both(self, first_body, second_body):
        """Fire two creations simultaneously and return their ``(status, code)``."""
        results = {}
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(
                target=_create_worker,
                args=(self.raw, body, results, key, barrier),
            )
            for key, body in (('a', first_body), ('b', second_body))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
            self.assertFalse(
                thread.is_alive(), 'a creation worker hung — lock regression?',
            )
        return results['a'], results['b']

    def assertOneCreatedOneRefused(self, first, second, expected_code):
        statuses = sorted([first[0], second[0]])
        self.assertEqual(
            statuses, [201, 409], f'expected one create and one conflict: {first} {second}',
        )
        refused = first if first[0] == 409 else second
        self.assertEqual(refused[1], expected_code)

    def assertOneWholeTenant(self):
        """Exactly one complete tenant, and no half of a second one."""
        self.assertEqual(Restaurant.objects.count(), 1)
        self.assertEqual(RestaurantEmployee.objects.count(), 1)
        self.assertEqual(RestaurantOnboarding.objects.count(), 1)
        self.assertEqual(OwnerInvitation.objects.count(), 1)


# --- §29 A: double click, new owner ------------------------------------------

class ConcurrentNewOwnerTests(_CreationConcurrencyTestCase):
    """
    Two simultaneous ``mode: "new"`` requests for the same phone.

    The restaurant names differ, so the ONLY thing that can collide is the identity —
    which is the point: a phone number must produce exactly one Dinify account, and
    the loser must be told so rather than handed a 500.
    """

    def bodies(self):
        first = creation_body(owner=new_owner_body(email=None))
        second = creation_body(owner=new_owner_body(email=None))
        second['restaurant']['name'] = 'Second Bistro'
        second['restaurant']['location'] = 'Ntinda, Kampala'
        return first, second

    def test_at_most_one_account_exists_for_the_phone(self):
        self.run_both(*self.bodies())
        self.assertEqual(
            User.objects.filter(phone_number='256772123456').count(), 1,
        )

    def test_one_creates_and_one_gets_a_controlled_conflict(self):
        first, second = self.run_both(*self.bodies())
        self.assertOneCreatedOneRefused(
            first, second, 'owner_account_already_exists',
        )

    def test_the_loser_leaves_no_partial_tenant(self):
        self.run_both(*self.bodies())
        self.assertOneWholeTenant()

    def test_the_loser_never_surfaces_an_integrity_error(self):
        first, second = self.run_both(*self.bodies())
        for outcome in (first, second):
            self.assertNotEqual(outcome[0], 500, outcome)
            self.assertNotEqual(outcome[0], 'error', outcome)

    def test_each_request_still_writes_exactly_one_audit_row(self):
        self.run_both(*self.bodies())
        self.assertEqual(AdminAuditLog.objects.count(), 2)
        self.assertEqual(
            AdminAuditLog.objects.filter(result='success').count(), 1,
        )
        self.assertEqual(
            AdminAuditLog.objects.filter(result='failure').count(), 1,
        )


# --- §29 B: one owner, two legitimate restaurants ----------------------------

class ConcurrentDistinctRestaurantsTests(_CreationConcurrencyTestCase):
    """
    A ``User`` may own more than one restaurant, so two DIFFERENT tenants for the
    same existing owner must both succeed. The owner lock serialises them; it must
    not refuse them.
    """

    def setUp(self):
        super().setUp()
        self.owner = _make_user('multi-owner@t.com')

    def bodies(self):
        def body(name, location):
            payload = creation_body(owner={
                'mode': 'existing', 'user_id': str(self.owner.pk),
            })
            payload['restaurant']['name'] = name
            payload['restaurant']['location'] = location
            return payload

        return body('First Bistro', 'Kololo'), body('Second Bistro', 'Ntinda')

    def test_both_creations_succeed(self):
        first, second = self.run_both(*self.bodies())
        self.assertEqual((first[0], second[0]), (201, 201), f'{first} {second}')

    def test_each_restaurant_gets_its_own_authority_and_credential(self):
        self.run_both(*self.bodies())
        self.assertEqual(Restaurant.objects.count(), 2)
        self.assertEqual(
            RestaurantEmployee.objects.filter(
                user=self.owner, active=True, deleted=False,
            ).count(),
            2,
        )
        self.assertEqual(RestaurantOnboarding.objects.count(), 2)
        self.assertEqual(OwnerInvitation.objects.count(), 2)
        self.assertEqual(
            OwnerInvitation.objects.values('token_hash').distinct().count(), 2,
        )

    def test_no_new_account_is_created(self):
        self.run_both(*self.bodies())
        self.assertEqual(User.objects.filter(pk=self.owner.pk).count(), 1)
        # The actor and the one owner.
        self.assertEqual(User.objects.count(), 2)


# --- §29 C: same owner, same restaurant, double submit -----------------------

class ConcurrentDuplicateRestaurantTests(_CreationConcurrencyTestCase):
    """
    The double-click an operator actually produces: the same request twice.

    ``unique_together (name, location, owner)`` is the database fact behind the
    refusal, and the owner row lock in front of it is what makes the two requests
    queue rather than race into it.
    """

    def setUp(self):
        super().setUp()
        self.owner = _make_user('double-click@t.com')

    def body(self):
        return creation_body(owner={
            'mode': 'existing', 'user_id': str(self.owner.pk),
        })

    def test_only_one_tenant_is_created(self):
        first, second = self.run_both(self.body(), self.body())
        self.assertOneCreatedOneRefused(first, second, 'restaurant_already_exists')
        self.assertOneWholeTenant()

    def test_only_one_invitation_is_minted(self):
        self.run_both(self.body(), self.body())
        self.assertEqual(OwnerInvitation.objects.count(), 1)

    def test_creation_is_never_answered_with_the_other_requests_restaurant(self):
        """Creation is not adoption: the loser is refused, never handed the winner's."""
        first, second = self.run_both(self.body(), self.body())
        refused = first if first[0] == 409 else second
        self.assertEqual(refused[1], 'restaurant_already_exists')


# --- §29 D/E: the world moves between validation and write -------------------

class OwnerChangedMidRequestTests(_CreationConcurrencyTestCase):
    """
    The owner's eligibility is re-read UNDER THE LOCK, so a change that commits
    while the request is in flight is seen — and refused.

    These are the two facts that must never be read from a stale instance: an
    account deactivated moments ago must not become a restaurant owner, and an
    account promoted to platform staff must never gain a restaurant membership.
    """

    def setUp(self):
        super().setUp()
        self.owner = _make_user('mid-flight@t.com')

    def race(self, updates):
        results = {}
        barrier = threading.Barrier(2)
        body = creation_body(owner={
            'mode': 'existing', 'user_id': str(self.owner.pk),
        })
        threads = [
            threading.Thread(
                target=_hold_and_mutate_worker,
                args=(self.owner.pk, updates, barrier, results),
            ),
            threading.Thread(
                target=_create_worker,
                args=(self.raw, body, results, 'request', barrier),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
            self.assertFalse(thread.is_alive(), 'a worker hung — lock regression?')
        self.assertEqual(results.get('holder'), 'committed')
        return results['request']

    def test_a_deactivation_committing_mid_request_refuses_the_creation(self):
        status, code = self.race({'is_active': False})
        self.assertEqual((status, code), (409, 'owner_account_inactive'))

    def test_the_deactivated_account_is_never_reactivated(self):
        self.race({'is_active': False})
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)

    def test_a_promotion_to_platform_staff_refuses_the_creation(self):
        status, code = self.race(
            {'account_type': ACCOUNT_TYPE_PLATFORM_STAFF},
        )
        self.assertEqual(
            (status, code), (409, 'owner_account_not_restaurant_user'),
        )

    def test_platform_staff_never_gain_a_restaurant_membership(self):
        self.race({'account_type': ACCOUNT_TYPE_PLATFORM_STAFF})
        self.assertFalse(
            RestaurantEmployee.objects.filter(user=self.owner).exists(),
        )

    def test_neither_refusal_leaves_a_tenant_behind(self):
        for updates in ({'is_active': False},
                        {'account_type': ACCOUNT_TYPE_PLATFORM_STAFF}):
            with self.subTest(updates=updates):
                User.objects.filter(pk=self.owner.pk).update(
                    is_active=True, account_type='restaurant_user',
                )
                self.race(updates)
                self.assertFalse(Restaurant.objects.exists())
                self.assertFalse(RestaurantOnboarding.objects.exists())
                self.assertFalse(OwnerInvitation.objects.exists())
