"""
Schema tests for ``RestaurantServiceConfiguration`` (Phase 1, Step 3B).

These assert DATABASE behaviour, not model-validation behaviour. Every negative
case writes through ``objects.create`` / ``.save()``, which never calls
``full_clean()`` — so a case that passes here is one a direct ORM write, a shell
session or a future service that forgets to validate cannot produce either. That
is the whole reason the vocabularies and the attribution triples are
``CheckConstraint``s rather than only ``choices=``.

The last test in each group pins an ARCHITECTURAL boundary rather than a behaviour:
tender belongs to a transaction, never to a restaurant.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from commercial_app.models import (
    PAYMENT_COLLECTION_MODE_OFFLINE,
    PAYMENT_COLLECTION_MODE_PSP_ONLINE,
    PAYMENT_TIMING_PAY_AFTER,
    PAYMENT_TIMING_PAY_FIRST,
    RestaurantServiceConfiguration,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from restaurants_app.models import Restaurant

User = get_user_model()


class ServiceConfigurationSchemaTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='svc-owner@test.com',
            phone_number='256772000101', username='256772000101',
            country='Uganda', password='password', roles=[],
        )
        cls.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='svc-actor@test.com',
            phone_number='256772000102', username='256772000102',
            country='Uganda', password='password', roles=[],
        )
        cls.restaurant = Restaurant.objects.create(
            name='Service Config Ltd', location='loc-svc',
            status=RestaurantStatus_Live, owner=cls.owner,
        )

    def _config(self, **fields):
        return RestaurantServiceConfiguration.objects.create(
            restaurant=self.restaurant, **fields
        )

    def _timing(self, value):
        return {
            'payment_timing': value,
            'payment_timing_set_at': timezone.now(),
            'payment_timing_set_by': self.actor,
        }

    def _collection(self, value):
        return {
            'payment_collection_mode': value,
            'payment_collection_mode_set_at': timezone.now(),
            'payment_collection_mode_set_by': self.actor,
        }

    # --- A: absence is the normal state -------------------------------------

    def test_a_restaurant_can_exist_with_no_configuration_row(self):
        """
        The default state of every restaurant, today and after a future creation
        flow. Nothing creates this row implicitly.
        """
        self.assertFalse(
            RestaurantServiceConfiguration.objects
            .filter(restaurant=self.restaurant).exists()
        )

    # --- B: a row may exist with nothing decided ----------------------------

    def test_b_configuration_row_may_have_both_axes_null(self):
        """
        A row can exist before either decision is made. "Not configured" is a
        persisted state, not an error — which is why neither axis has a default.
        """
        config = self._config()
        config.refresh_from_db()
        self.assertIsNone(config.payment_timing)
        self.assertIsNone(config.payment_collection_mode)
        self.assertIsNone(config.payment_timing_set_at)
        self.assertIsNone(config.payment_collection_mode_set_by)

    # --- C, D, E: the payment-timing vocabulary -----------------------------

    def test_c_pay_first_is_valid(self):
        config = self._config(**self._timing(PAYMENT_TIMING_PAY_FIRST))
        config.refresh_from_db()
        self.assertEqual(config.payment_timing, 'pay_first')

    def test_d_pay_after_is_valid(self):
        config = self._config(**self._timing(PAYMENT_TIMING_PAY_AFTER))
        config.refresh_from_db()
        self.assertEqual(config.payment_timing, 'pay_after')

    def test_e_invalid_payment_timing_is_rejected_by_the_database(self):
        """
        `choices=` alone would not catch this: `objects.create` does not validate.
        Includes an empty string, which is what a caller who "cleared" the field by
        assigning '' rather than None would write.
        """
        for bogus in ('prepaid', 'PAY_FIRST', 'pay_later', ''):
            with self.subTest(payment_timing=bogus):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._config(
                        payment_timing=bogus,
                        payment_timing_set_at=timezone.now(),
                        payment_timing_set_by=self.actor,
                    )

    # --- F, G, H: the collection-mode vocabulary ----------------------------

    def test_f_offline_is_valid(self):
        """
        `offline` is a PERMANENT first-class commercial mode — not degraded, not a
        fallback, not pre-launch-only. The first commercial restaurant must be able
        to launch in it.
        """
        config = self._config(**self._collection(PAYMENT_COLLECTION_MODE_OFFLINE))
        config.refresh_from_db()
        self.assertEqual(config.payment_collection_mode, 'offline')

    def test_g_psp_online_is_valid(self):
        config = self._config(**self._collection(PAYMENT_COLLECTION_MODE_PSP_ONLINE))
        config.refresh_from_db()
        self.assertEqual(config.payment_collection_mode, 'psp_online')

    def test_h_invalid_collection_mode_is_rejected_by_the_database(self):
        """
        Note what is refused here alongside the obvious typos: `cash`, `momo` and
        `card` are TENDER, and a provider name is not a mode at all. Neither belongs
        on this axis, and the database says so.
        """
        for bogus in ('cash', 'momo', 'card', 'flutterwave', 'ONLINE', ''):
            with self.subTest(payment_collection_mode=bogus):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._config(
                        payment_collection_mode=bogus,
                        payment_collection_mode_set_at=timezone.now(),
                        payment_collection_mode_set_by=self.actor,
                    )

    # --- I: the axes are independent ----------------------------------------

    def test_i_all_four_timing_and_collection_combinations_are_valid(self):
        """
        THE INDEPENDENCE INVARIANT. Timing is a service-model fact; collection mode
        is a custody fact. A counter cafe taking cash is pay_first+offline; a
        full-service restaurant on Dinify-initiated mobile money is
        pay_after+psp_online; the other two are equally real businesses.

        A cross-constraint coupling them would encode a product opinion the domain
        does not hold, so this test exists to fail if one is ever added.
        """
        combinations = [
            (PAYMENT_TIMING_PAY_FIRST, PAYMENT_COLLECTION_MODE_OFFLINE),
            (PAYMENT_TIMING_PAY_FIRST, PAYMENT_COLLECTION_MODE_PSP_ONLINE),
            (PAYMENT_TIMING_PAY_AFTER, PAYMENT_COLLECTION_MODE_OFFLINE),
            (PAYMENT_TIMING_PAY_AFTER, PAYMENT_COLLECTION_MODE_PSP_ONLINE),
        ]
        for index, (timing, collection) in enumerate(combinations):
            with self.subTest(payment_timing=timing, payment_collection_mode=collection):
                # A distinct restaurant per combination: the OneToOne allows only
                # one configuration row per restaurant (test N).
                restaurant = Restaurant.objects.create(
                    name=f'Combo {index}', location=f'loc-combo-{index}',
                    status=RestaurantStatus_Live, owner=self.owner,
                )
                config = RestaurantServiceConfiguration.objects.create(
                    restaurant=restaurant,
                    **self._timing(timing),
                    **self._collection(collection),
                )
                config.refresh_from_db()
                self.assertEqual(config.payment_timing, timing)
                self.assertEqual(config.payment_collection_mode, collection)

    # --- J, K, L, M: the attribution triples --------------------------------

    def test_j_payment_timing_without_both_stamps_is_rejected(self):
        """A decision nobody is attached to is an unattributable assertion."""
        for missing in ('set_at', 'set_by'):
            with self.subTest(missing=missing):
                fields = self._timing(PAYMENT_TIMING_PAY_FIRST)
                fields[f'payment_timing_{missing}'] = None
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._config(**fields)

    def test_k_payment_timing_stamps_without_a_value_are_rejected(self):
        """The mirror: a stamp attributing a decision that was never made."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._config(
                payment_timing_set_at=timezone.now(),
                payment_timing_set_by=self.actor,
            )

    def test_l_collection_mode_without_both_stamps_is_rejected(self):
        for missing in ('set_at', 'set_by'):
            with self.subTest(missing=missing):
                fields = self._collection(PAYMENT_COLLECTION_MODE_OFFLINE)
                fields[f'payment_collection_mode_{missing}'] = None
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._config(**fields)

    def test_m_collection_mode_stamps_without_a_value_are_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._config(
                payment_collection_mode_set_at=timezone.now(),
                payment_collection_mode_set_by=self.actor,
            )

    def test_the_two_axes_are_attributed_separately(self):
        """
        Each axis carries its OWN stamp pair. One generic `updated_by` would make
        it impossible to say who chose the service model versus who chose the
        custody arrangement — and those may end up with different write authority.
        """
        other_actor = User.objects.create_user(
            first_name='Second', last_name='Actor', email='svc-actor2@test.com',
            phone_number='256772000103', username='256772000103',
            country='Uganda', password='password', roles=[],
        )
        config = self._config(
            **self._timing(PAYMENT_TIMING_PAY_FIRST),
            payment_collection_mode=PAYMENT_COLLECTION_MODE_OFFLINE,
            payment_collection_mode_set_at=timezone.now(),
            payment_collection_mode_set_by=other_actor,
        )
        config.refresh_from_db()
        self.assertEqual(config.payment_timing_set_by_id, self.actor.id)
        self.assertEqual(config.payment_collection_mode_set_by_id, other_actor.id)

    # --- N: one row per restaurant ------------------------------------------

    def test_n_a_restaurant_cannot_have_two_configuration_rows(self):
        """Enforced by the OneToOne's unique index, at the database."""
        self._config()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._config()

    # --- O: the architectural boundary --------------------------------------

    def test_o_no_tender_or_provider_field_exists_on_this_model(self):
        """
        TENDER IS A TRANSACTION FACT, NOT A RESTAURANT FACT. `cash` / `momo` /
        `card` live on ``DinifyTransaction.payment_mode``; a restaurant on `offline`
        may take cash from one diner and mobile money from the next.

        Provider identity is likewise absent: the restaurant-level domain is
        PSP-agnostic, and merchant state arrives with the first real integration.

        This test exists so that adding any of these to this model is a deliberate,
        visible act rather than a plausible-looking convenience.
        """
        field_names = {f.name for f in RestaurantServiceConfiguration._meta.get_fields()}
        for forbidden in (
            'payment_mode', 'payment_method', 'tender', 'cash', 'card',
            'mobile_money', 'momo',
            'provider', 'psp', 'merchant_id', 'merchant_status',
            'psp_merchant_state', 'webhook_state',
        ):
            self.assertNotIn(forbidden, field_names)

    def test_the_model_does_not_carry_soft_delete_or_archival_state(self):
        """
        Not ``users_app.BaseModel``: ``deleted`` / ``archived`` / ``vacuumed`` all
        assert that rows get hidden or reaped, which is wrong for commercial
        configuration. It is superseded by being rewritten, with the history in
        AdminAuditLog.
        """
        field_names = {f.name for f in RestaurantServiceConfiguration._meta.get_fields()}
        for forbidden in ('deleted', 'archived', 'vacuumed', 'deletion_reason',
                          'deleted_by', 'time_deleted'):
            self.assertNotIn(forbidden, field_names)
