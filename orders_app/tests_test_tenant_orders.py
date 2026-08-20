"""
Test TENANTS and test ORDERS — the expanded ``Order.is_test`` invariant.

``Order.is_test`` used to mean one thing: a pre-go-live rehearsal order. It now means
either of two independent things, and the second is new:

    TENANT    — the restaurant itself exists for testing (``Restaurant.is_test``),
                so nothing it produces is commerce, whatever its lifecycle state;
    LIFECYCLE — the order predates go-live, so it is a rehearsal.

The governing rule is unchanged and still asserted by ``tests_launch_boundary``: a
test order is operationally real and commercially invisible. What this suite adds is
that a test restaurant which has gone ``live`` still writes test orders — the case
the old rule got wrong, because it asked only about lifecycle state.

THE CONCURRENCY POINT, which is why this is not simply ``restaurant.is_test``: the
tenant flag is read in the SAME locked query as the lifecycle status, inside
``order_admission.admit``. Reading it off the ``Restaurant`` instance the caller
loaded before the transaction would reproduce, on a different field, exactly the
stale-read bug the advisory lock was introduced to close.
"""
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
)
from orders_app.controllers.services import order_admission
from orders_app.controllers.services.order_admission import (
    STAGE_CREATE,
    AdmissionVerdict,
    admit,
    evaluate,
)
from orders_app.models import Order
from orders_app.tests_launch_boundary import LaunchBoundaryFixture
from restaurants_app.models import Restaurant


class TestTenantClassificationTests(LaunchBoundaryFixture):
    """The four-cell truth table: {normal, test} tenant x {onboarding, live}."""

    def _mark_test_tenant(self, value=True):
        """Flip the flag in the DATABASE, leaving any in-memory instance stale."""
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=value)

    # --- normal tenant: unchanged behaviour ---------------------------------

    def test_normal_tenant_onboarding_rehearsal_is_test(self):
        self._at(RestaurantStatus_Onboarding)
        response = self._order(created_by=self.owner)
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertTrue(order.is_test)

    def test_normal_tenant_live_real_order_is_not_test(self):
        self._at(RestaurantStatus_Live)
        response = self._order()
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertFalse(order.is_test)

    # --- test tenant: the new rule ------------------------------------------

    def test_test_tenant_onboarding_rehearsal_is_test(self):
        self._mark_test_tenant()
        self._at(RestaurantStatus_Onboarding)
        response = self._order(created_by=self.owner)
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertTrue(order.is_test)

    def test_test_tenant_live_order_is_STILL_test(self):
        """
        The case the lifecycle-only rule got wrong.

        A test restaurant that has gone live would otherwise start writing orders
        that count as revenue — the exact outcome `Restaurant.is_test` exists to
        prevent, and the one that would be discovered from a wrong figure rather
        than from an error.
        """
        self._mark_test_tenant()
        self._at(RestaurantStatus_Live)
        response = self._order()
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertTrue(order.is_test)

    def test_a_test_tenant_still_admits_orders_normally(self):
        """
        The tenant flag classifies; it does not gate.

        A test restaurant admits exactly what a real one would — otherwise it could
        not be used to rehearse the thing it exists to rehearse.
        """
        self._mark_test_tenant()
        self._at(RestaurantStatus_Live)
        self.assertEqual(self._order().get('status'), 200)
        self._free_table()
        self._at(RestaurantStatus_Onboarding)
        # Still refuses the anonymous public while onboarding, exactly as before.
        self.assertEqual(self._order().get('status'), 400)


class TestTenantAuthoritativeReadTests(LaunchBoundaryFixture):
    """The flag is read under the admission lock, not from a caller's instance."""

    def test_admit_reads_the_tenant_flag_from_the_database(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)
        # `self.restaurant` is deliberately NOT refreshed: it still says False.
        self.assertFalse(self.restaurant.is_test)

        with transaction.atomic():
            verdict = admit(
                restaurant_id=self.restaurant.id,
                created_by=self.owner,
                stage=STAGE_CREATE,
            )
        self.assertTrue(verdict.restaurant_is_test)

    def test_admit_reads_status_and_flag_from_the_same_locked_moment(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            is_test=True, status=RestaurantStatus_Live,
        )
        with transaction.atomic():
            verdict = admit(
                restaurant_id=self.restaurant.id,
                created_by=None,
                stage=STAGE_CREATE,
            )
        self.assertEqual(verdict.status, RestaurantStatus_Live)
        self.assertTrue(verdict.restaurant_is_test)

    def test_the_pure_preflight_never_asserts_a_tenant_flag(self):
        """
        `evaluate` has no database and therefore no authority over the tenant flag.

        It must default to False rather than guess, so a preflight verdict can never
        be mistaken for a statement about the tenant.
        """
        verdict = evaluate(RestaurantStatus_Live, None, STAGE_CREATE)
        self.assertFalse(verdict.restaurant_is_test)

    def test_classification_follows_the_verdict_not_the_stale_instance(self):
        """
        THE REGRESSION. Order creation must classify from the admission verdict.

        The restaurant row says `is_test=False` and is `live`, so anything reading
        the row — or an instance loaded from it — would classify this order as
        commerce. Only a code path that takes the value from the verdict returned by
        `admit` (the read taken under the advisory lock) writes `is_test=True` here.

        This is the deterministic stand-in for the race: an administrator flipping
        the flag while a request waits on the table lock produces exactly this
        divergence between the row the caller loaded and the row the lock protects.
        """
        self._at(RestaurantStatus_Live)
        self.assertFalse(Restaurant.objects.get(pk=self.restaurant.pk).is_test)

        real_admit = order_admission.admit

        def _admit_reporting_test_tenant(**kwargs):
            verdict = real_admit(**kwargs)
            # What `admit` would have returned had the flag been set under the lock.
            return AdmissionVerdict(
                allowed=verdict.allowed,
                status=verdict.status,
                message=verdict.message,
                code=verdict.code,
                restaurant_is_test=True,
            )

        with patch(
            'orders_app.controllers.services.create_order.admit',
            side_effect=_admit_reporting_test_tenant,
        ):
            response = self._order()

        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        self.assertTrue(
            order.is_test,
            'Order.is_test must be derived from the admission verdict, not from a '
            'Restaurant instance loaded before the transaction.',
        )

    def test_classification_still_honours_lifecycle_when_the_tenant_is_normal(self):
        """The two conditions are independent — neither masks the other."""
        self._at(RestaurantStatus_Onboarding)
        with transaction.atomic():
            verdict = admit(
                restaurant_id=self.restaurant.id,
                created_by=self.owner,
                stage=STAGE_CREATE,
            )
        self.assertFalse(verdict.restaurant_is_test)
        self.assertEqual(verdict.status, RestaurantStatus_Onboarding)


class TestTenantReplayAndInputTests(LaunchBoundaryFixture):
    """Replay keeps its original classification; the client can never set it."""

    def test_idempotent_replay_returns_the_existing_order_unreclassified(self):
        self._at(RestaurantStatus_Live)
        client_order_id = '11111111-1111-1111-1111-111111111111'

        first = self._order_with_client_id(client_order_id)
        self.assertEqual(first.get('status'), 200, first)
        order_id = first['data']['order_details']['id']
        order = Order.objects.get(id=order_id)
        self.assertFalse(order.is_test)

        # The tenant becomes a test tenant AFTER the order exists.
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)

        replay = self._order_with_client_id(client_order_id)
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertEqual(replay['data']['order_details']['id'], order_id)

        order.refresh_from_db()
        self.assertFalse(
            order.is_test,
            'A replay returns the original order and must not reclassify it.',
        )
        self.assertEqual(Order.objects.count(), 1)

    def test_order_creation_accepts_no_is_test_input(self):
        """
        Server-derived, never client-supplied — asserted at the signature.

        There is no request field for it and there must never be one; a caller that
        tries to pass it gets a TypeError rather than a silently ignored key.
        """
        from orders_app.controllers.con_orders import ConOrder

        with self.assertRaises(TypeError):
            ConOrder.initiate_order(
                restaurant_id=str(self.restaurant.id),
                table_id=str(self.table.id),
                items=[{'item': str(self.item.id), 'quantity': 1}],
                created_by=None,
                is_test=False,
            )

    def test_an_is_test_key_in_the_item_payload_is_inert(self):
        """A stray key on a line item cannot reach the order's classification."""
        from orders_app.controllers.con_orders import ConOrder

        self._at(RestaurantStatus_Onboarding)
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.id),
            table_id=str(self.table.id),
            items=[{'item': str(self.item.id), 'quantity': 1, 'is_test': False}],
            created_by=self.owner,
        )
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(id=response['data']['order_details']['id'])
        # Still classified from lifecycle state alone.
        self.assertTrue(order.is_test)

    # --- helpers ------------------------------------------------------------

    def _order_with_client_id(self, client_order_id):
        from orders_app.controllers.con_orders import ConOrder

        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.id),
            table_id=str(self.table.id),
            items=[{'item': str(self.item.id), 'quantity': 1}],
            created_by=None,
            client_order_id=client_order_id,
        )
