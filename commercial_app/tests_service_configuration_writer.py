"""
The payment-timing and payment-collection-mode writers (Phase 1, Step 3C).

Covers the value vocabulary, the optimistic-concurrency contract, the same-state
no-op, axis independence, targeting failures, and the four things these writers must
provably NOT do: touch the other axis, touch legacy fields, touch lifecycle, or write
an audit row.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from commercial_app import errors, service_configuration
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from restaurants_app.models import Restaurant, Table

User = get_user_model()

# `actor or self.actor` would swallow a deliberate `actor=None`, which is exactly one
# of the cases under test. A sentinel keeps "argument omitted" distinct from "argument
# is None" — the same distinction the writers themselves make about `expected_current`.
_UNSET = object()

# The legacy commercial/payment columns this domain must never write or read.
LEGACY_FIELDS = (
    'require_order_prepayments',
    'preferred_subscription_method',
    'flat_fee',
    'subscription_validity',
    'subscription_expiry_date',
)


class ServiceConfigurationWriterTests(TestCase):

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='scw-owner@test.com',
            phone_number='256773000101', username='256773000101',
            country='Uganda', password='password', roles=[],
        )
        self.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='scw-actor@test.com',
            phone_number='256773000102', username='256773000102',
            country='Uganda', password='password', roles=[],
        )
        self.other_actor = User.objects.create_user(
            first_name='Other', last_name='Staff', email='scw-actor2@test.com',
            phone_number='256773000103', username='256773000103',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Writer Ltd', location='loc-writer',
            status=RestaurantStatus_Live, owner=self.owner,
            # Legacy state set to values a careless implementation might sync with.
            require_order_prepayments=True,
            preferred_subscription_method='monthly',
            flat_fee=Decimal('50000.00'),
            subscription_validity=True,
        )

    def _legacy_snapshot(self):
        self.restaurant.refresh_from_db()
        return {name: getattr(self.restaurant, name) for name in LEGACY_FIELDS}

    def _set_timing(self, value, expected_current=None, actor=_UNSET):
        return service_configuration.set_payment_timing(
            restaurant_id=self.restaurant.id, value=value,
            actor=self.actor if actor is _UNSET else actor,
            expected_current=expected_current,
        )

    def _set_mode(self, value, expected_current=None, actor=_UNSET):
        return service_configuration.set_payment_collection_mode(
            restaurant_id=self.restaurant.id, value=value,
            actor=self.actor if actor is _UNSET else actor,
            expected_current=expected_current,
        )

    # --- creation, stamping, independence -----------------------------------

    def test_first_timing_write_creates_the_row_and_leaves_collection_null(self):
        result = self._set_timing('pay_first')

        self.assertTrue(result.changed)
        self.assertIsNone(result.previous_value)
        self.assertEqual(result.current_value, 'pay_first')

        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        self.assertEqual(config.payment_timing, 'pay_first')
        self.assertEqual(config.payment_timing_set_by_id, self.actor.id)
        self.assertIsNotNone(config.payment_timing_set_at)
        # The other axis stays unconfigured — absence is the honest state, and
        # defaulting it here would manufacture a decision nobody made.
        self.assertIsNone(config.payment_collection_mode)
        self.assertIsNone(config.payment_collection_mode_set_at)
        self.assertIsNone(config.payment_collection_mode_set_by)

    def test_first_collection_write_creates_the_row_and_leaves_timing_null(self):
        result = self._set_mode('offline')

        self.assertTrue(result.changed)
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        self.assertEqual(config.payment_collection_mode, 'offline')
        self.assertEqual(config.payment_collection_mode_set_by_id, self.actor.id)
        self.assertIsNotNone(config.payment_collection_mode_set_at)
        self.assertIsNone(config.payment_timing)
        self.assertIsNone(config.payment_timing_set_at)
        self.assertIsNone(config.payment_timing_set_by)

    def test_the_two_axes_are_configured_and_attributed_independently(self):
        self._set_timing('pay_after')
        self._set_mode('psp_online', actor=self.other_actor)

        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        self.assertEqual(config.payment_timing, 'pay_after')
        self.assertEqual(config.payment_collection_mode, 'psp_online')
        self.assertEqual(config.payment_timing_set_by_id, self.actor.id)
        self.assertEqual(config.payment_collection_mode_set_by_id, self.other_actor.id)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 1)

    def test_changing_one_axis_does_not_restamp_the_other(self):
        self._set_timing('pay_first')
        self._set_mode('offline', actor=self.other_actor)
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        timing_stamp = config.payment_timing_set_at

        self._set_mode('psp_online', expected_current='offline')

        config.refresh_from_db()
        self.assertEqual(config.payment_collection_mode, 'psp_online')
        self.assertEqual(config.payment_timing, 'pay_first')
        self.assertEqual(config.payment_timing_set_at, timing_stamp)
        self.assertEqual(config.payment_timing_set_by_id, self.actor.id)

    # --- the same-state no-op ------------------------------------------------

    def test_setting_the_same_timing_is_a_noop_that_preserves_attribution(self):
        self._set_timing('pay_first')
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        original_at, original_by = (
            config.payment_timing_set_at, config.payment_timing_set_by_id,
        )

        result = self._set_timing('pay_first', expected_current='pay_first',
                                  actor=self.other_actor)

        self.assertFalse(result.changed)
        self.assertEqual(result.current_value, 'pay_first')
        config.refresh_from_db()
        # A retry must not rewrite who decided, or when.
        self.assertEqual(config.payment_timing_set_at, original_at)
        self.assertEqual(config.payment_timing_set_by_id, original_by)

    def test_a_same_state_retry_succeeds_even_with_a_stale_expectation(self):
        """
        THE LOST-RESPONSE CASE. The first call succeeded but its response never
        arrived, so the retry still believes the value was unconfigured. Because the
        stored value already equals the request, this is a no-op — not a conflict the
        operator has to reason about.
        """
        self._set_timing('pay_after')
        result = self._set_timing('pay_after', expected_current=None)
        self.assertFalse(result.changed)
        self.assertEqual(result.current_value, 'pay_after')

    def test_same_state_collection_retry_is_a_noop(self):
        self._set_mode('offline')
        result = self._set_mode('offline', expected_current=None)
        self.assertFalse(result.changed)

    # --- optimistic concurrency ---------------------------------------------

    def test_a_stale_expectation_cannot_overwrite_a_newer_value(self):
        self._set_timing('pay_first')

        with self.assertRaises(CommercialMutationError) as ctx:
            self._set_timing('pay_after', expected_current=None)

        self.assertEqual(ctx.exception.code, errors.STALE_SERVICE_CONFIGURATION)
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        self.assertEqual(config.payment_timing, 'pay_first')

    def test_a_correct_expectation_updates_the_value(self):
        self._set_timing('pay_first')
        result = self._set_timing('pay_after', expected_current='pay_first')
        self.assertTrue(result.changed)
        self.assertEqual(result.previous_value, 'pay_first')
        self.assertEqual(result.current_value, 'pay_after')

    def test_expecting_a_value_against_an_unconfigured_axis_conflicts(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._set_timing('pay_first', expected_current='pay_after')
        self.assertEqual(ctx.exception.code, errors.STALE_SERVICE_CONFIGURATION)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_stale_collection_expectation_conflicts(self):
        self._set_mode('offline')
        with self.assertRaises(CommercialMutationError) as ctx:
            self._set_mode('psp_online', expected_current=None)
        self.assertEqual(ctx.exception.code, errors.STALE_SERVICE_CONFIGURATION)

    # --- vocabulary ----------------------------------------------------------

    def test_invalid_payment_timing_values_are_refused(self):
        for bogus in ('cash', 'momo', 'card', 'offline', 'psp_online',
                      'prepayment', 'postpayment', 'PAY_FIRST', '', None):
            with self.subTest(value=bogus):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._set_timing(bogus)
                self.assertEqual(ctx.exception.code, errors.INVALID_PAYMENT_TIMING)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_invalid_collection_mode_values_are_refused(self):
        """Tender words and provider names are refused on this axis specifically."""
        for bogus in ('cash', 'card', 'momo', 'mobile_money', 'flutterwave',
                      'pesapal', 'pay_first', 'ONLINE', '', None):
            with self.subTest(value=bogus):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._set_mode(bogus)
                self.assertEqual(
                    ctx.exception.code, errors.INVALID_PAYMENT_COLLECTION_MODE,
                )
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_offline_is_an_ordinary_configured_value(self):
        """
        `offline` is a permanent first-class commercial mode — not false, not a
        fallback, not "unconfigured". Unconfigured is NULL.
        """
        result = self._set_mode('offline')
        self.assertTrue(result.changed)
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        self.assertEqual(config.payment_collection_mode, 'offline')
        self.assertIsNotNone(config.payment_collection_mode_set_at)

    def test_psp_online_is_recordable_without_any_psp_integration(self):
        """The value is a commercial decision; merchant readiness is a later question."""
        result = self._set_mode('psp_online')
        self.assertTrue(result.changed)
        self.assertEqual(result.current_value, 'psp_online')

    def test_a_malformed_expected_current_is_refused_as_such(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._set_timing('pay_first', expected_current='nonsense')
        self.assertEqual(ctx.exception.code, errors.INVALID_PAYMENT_TIMING)

    # --- targeting and actor -------------------------------------------------

    def test_a_nonexistent_restaurant_is_refused(self):
        import uuid
        with self.assertRaises(CommercialMutationError) as ctx:
            service_configuration.set_payment_timing(
                restaurant_id=uuid.uuid4(), value='pay_first',
                actor=self.actor, expected_current=None,
            )
        self.assertEqual(ctx.exception.code, errors.RESTAURANT_NOT_FOUND)

    def test_a_malformed_restaurant_id_is_its_own_error(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            service_configuration.set_payment_timing(
                restaurant_id='Writer Ltd', value='pay_first',
                actor=self.actor, expected_current=None,
            )
        self.assertEqual(ctx.exception.code, errors.INVALID_RESTAURANT_ID)

    def test_a_soft_deleted_restaurant_is_refused_distinctly(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        with self.assertRaises(CommercialMutationError) as ctx:
            self._set_timing('pay_first')
        self.assertEqual(ctx.exception.code, errors.RESTAURANT_DELETED)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_an_unsaved_actor_is_a_domain_error_not_an_integrity_error(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._set_timing('pay_first', actor=User(email='ghost@test.com'))
        self.assertEqual(ctx.exception.code, errors.INVALID_ACTOR)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_a_non_user_actor_is_refused(self):
        for bogus in (None, 'someone', 42):
            with self.subTest(actor=bogus):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._set_timing('pay_first', actor=bogus)
                self.assertEqual(ctx.exception.code, errors.INVALID_ACTOR)

    def test_lifecycle_state_does_not_gate_commercial_configuration(self):
        """
        A restaurant may need commercial configuration or correction while
        onboarding, live or suspended. Whether an OFFBOARDED tenant may be edited
        through the Admin UI is control-plane policy, not a domain invariant, so this
        domain does not decide it.
        """
        for status in (RestaurantStatus_Onboarding, RestaurantStatus_Live,
                       RestaurantStatus_Suspended):
            with self.subTest(status=status):
                Restaurant.objects.filter(pk=self.restaurant.pk).update(status=status)
                RestaurantServiceConfiguration.objects.all().delete()
                result = self._set_timing('pay_first')
                self.assertTrue(result.changed)

    # --- the four things these writers must not do ---------------------------

    def test_a_test_tenant_has_no_special_mutation_semantics(self):
        """
        ``is_test`` is a commercial CLASSIFICATION, not a schema shortcut. A test
        tenant is meant to rehearse the same configuration path a real one walks, so
        the writers neither exempt it, nor default anything for it, nor refuse it.
        """
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)

        timing = self._set_timing('pay_first')
        mode = self._set_mode('offline')

        self.assertTrue(timing.changed)
        self.assertTrue(mode.changed)
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        self.assertEqual(config.payment_timing, 'pay_first')
        self.assertEqual(config.payment_collection_mode, 'offline')
        # And the flag itself is not something these writers touch.
        self.restaurant.refresh_from_db()
        self.assertTrue(self.restaurant.is_test)

    def test_legacy_commercial_fields_are_untouched(self):
        before = self._legacy_snapshot()
        self._set_timing('pay_first')
        self._set_mode('psp_online')
        self.assertEqual(self._legacy_snapshot(), before)

    def test_table_prepayment_flags_are_untouched(self):
        table = Table.objects.create(
            restaurant=self.restaurant, number=1, prepayment_required=True,
        )
        self._set_timing('pay_after')
        table.refresh_from_db()
        self.assertTrue(table.prepayment_required)

    def test_the_restaurant_row_itself_is_not_modified(self):
        self.restaurant.refresh_from_db()
        before_status, before_updated = (
            self.restaurant.status, self.restaurant.time_last_updated,
        )
        self._set_timing('pay_first')
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, before_status)
        self.assertEqual(self.restaurant.time_last_updated, before_updated)

    def test_no_admin_audit_row_is_written(self):
        from platform_admin_app.models import AdminAuditLog
        before = AdminAuditLog.objects.count()
        self._set_timing('pay_first')
        self._set_mode('offline')
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_no_subscription_terms_are_created_as_a_side_effect(self):
        self._set_timing('pay_first')
        self._set_mode('offline')
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    # --- atomicity -----------------------------------------------------------

    def test_a_failed_save_leaves_no_half_written_configuration(self):
        with mock.patch.object(
            RestaurantServiceConfiguration, 'save', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self._set_timing('pay_first')
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_a_failed_update_leaves_the_previous_triple_intact(self):
        self._set_timing('pay_first')
        config = RestaurantServiceConfiguration.objects.get(restaurant=self.restaurant)
        original_at, original_by = (
            config.payment_timing_set_at, config.payment_timing_set_by_id,
        )

        with mock.patch.object(
            RestaurantServiceConfiguration, 'save', side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self._set_timing('pay_after', expected_current='pay_first')

        config.refresh_from_db()
        self.assertEqual(config.payment_timing, 'pay_first')
        self.assertEqual(config.payment_timing_set_at, original_at)
        self.assertEqual(config.payment_timing_set_by_id, original_by)
