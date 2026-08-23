"""
The subscription-terms writers: record, replace, end (Phase 1, Step 3C).

Covers input normalisation, the create/close+insert/close discipline, the
optimistic-concurrency and exact-retry rules, the continuous replacement boundary,
the refusal to schedule, and the things these writers must provably NOT do — mutate
an existing row's commercial facts, touch legacy fields, create a payment record, or
write an audit row.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from commercial_app import errors, subscription_terms
from commercial_app.errors import CommercialMutationError
from commercial_app.models import (
    BILLING_INTERVAL_UNIT_VALUES,
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from finance_app.models import DinifyTransaction
from restaurants_app.models import Restaurant

User = get_user_model()

LEGACY_FIELDS = (
    'preferred_subscription_method',
    'flat_fee',
    'subscription_validity',
    'subscription_expiry_date',
    'require_order_prepayments',
)


class _TermsWriterBase(TestCase):
    """Fixture and helpers shared by the writer matrix and the regression suite.

    Carries no tests of its own: subclassing a class that HAS tests would re-run
    the whole matrix under every subclass name.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='stw-owner@test.com',
            phone_number='256773000201', username='256773000201',
            country='Uganda', password='password', roles=[],
        )
        self.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='stw-actor@test.com',
            phone_number='256773000202', username='256773000202',
            country='Uganda', password='password', roles=[],
        )
        self.other_actor = User.objects.create_user(
            first_name='Other', last_name='Staff', email='stw-actor2@test.com',
            phone_number='256773000203', username='256773000203',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Terms Writer Ltd', location='loc-tw',
            status=RestaurantStatus_Live, owner=self.owner,
            require_order_prepayments=True,
            preferred_subscription_method='monthly',
            flat_fee=Decimal('50000.00'),
            subscription_validity=True,
        )
        self.effective = timezone.now() - timedelta(days=30)

    # --- helpers -------------------------------------------------------------

    def _record(self, **overrides):
        payload = {
            'restaurant_id': self.restaurant.id,
            'recurring_amount': Decimal('250000.00'),
            'currency': 'UGX',
            'billing_interval_unit': 'month',
            'billing_interval_count': 1,
            'effective_from': self.effective,
            'actor': self.actor,
        }
        payload.update(overrides)
        return subscription_terms.record_subscription_terms(**payload)

    def _replace(self, expected_terms_id, **overrides):
        payload = {
            'restaurant_id': self.restaurant.id,
            'expected_terms_id': expected_terms_id,
            'recurring_amount': Decimal('300000.00'),
            'currency': 'UGX',
            'billing_interval_unit': 'month',
            'billing_interval_count': 1,
            'effective_from': self.effective + timedelta(days=10),
            'actor': self.actor,
        }
        payload.update(overrides)
        return subscription_terms.replace_subscription_terms(**payload)

    def _end(self, expected_terms_id, ended_at=None):
        return subscription_terms.end_subscription_terms(
            restaurant_id=self.restaurant.id,
            expected_terms_id=expected_terms_id,
            ended_at=ended_at or (self.effective + timedelta(days=20)),
        )

    def _legacy_snapshot(self):
        self.restaurant.refresh_from_db()
        return {name: getattr(self.restaurant, name) for name in LEGACY_FIELDS}

    def _open_count(self):
        return RestaurantSubscriptionTerms.objects.filter(
            restaurant=self.restaurant, ended_at__isnull=True,
        ).count()


class SubscriptionTermsWriterTests(_TermsWriterBase):
    """record / replace / end, and the things none of them may do."""

    # =====================================================================
    # RECORD
    # =====================================================================

    def test_first_record_creates_one_open_row(self):
        result = self._record()
        self.assertTrue(result.changed)
        self.assertIsNone(result.previous_terms)
        self.assertEqual(self._open_count(), 1)

        terms = result.terms
        self.assertEqual(terms.recurring_amount, Decimal('250000.00'))
        self.assertEqual(terms.currency, 'UGX')
        self.assertEqual(terms.recorded_by_id, self.actor.id)
        self.assertIsNone(terms.ended_at)
        self.assertTrue(terms.is_open)

    def test_zero_price_terms_are_accepted(self):
        result = self._record(recurring_amount=Decimal('0.00'))
        self.assertTrue(result.changed)
        self.assertEqual(result.terms.recurring_amount, Decimal('0.00'))

    def test_negative_amount_is_refused(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(recurring_amount=Decimal('-1.00'))
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_a_float_amount_is_refused_rather_than_converted(self):
        """
        `Decimal(0.1)` is 0.1000000000000000055511151231257827. A price that arrives a
        fraction off what was typed is the defect nobody notices until an invoice
        disagrees with a contract.
        """
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(recurring_amount=250000.55)
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)

    def test_an_over_precise_amount_is_refused_not_rounded(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(recurring_amount=Decimal('1000.005'))
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)

    def test_string_and_int_amounts_are_accepted(self):
        result = self._record(recurring_amount='1500')
        self.assertEqual(result.terms.recurring_amount, Decimal('1500.00'))

    def test_currency_is_canonicalised_to_uppercase(self):
        result = self._record(currency='  ugx ')
        self.assertEqual(result.terms.currency, 'UGX')

    def test_malformed_currency_is_refused(self):
        for bogus in ('UG', 'UGXX', 'U1X', '', '   ', None, 123):
            with self.subTest(currency=bogus):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._record(currency=bogus)
                self.assertEqual(
                    ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS,
                )

    def test_every_interval_unit_is_accepted(self):
        for unit in BILLING_INTERVAL_UNIT_VALUES:
            with self.subTest(unit=unit):
                RestaurantSubscriptionTerms.objects.all().delete()
                result = self._record(billing_interval_unit=unit)
                self.assertEqual(result.terms.billing_interval_unit, unit)

    def test_per_order_and_other_units_are_refused(self):
        for bogus in ('per_order', 'quarter', 'MONTH', 'monthly', '', None):
            with self.subTest(unit=bogus):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._record(billing_interval_unit=bogus)
                self.assertEqual(
                    ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS,
                )

    def test_invalid_interval_counts_are_refused(self):
        for bogus in (0, -1, 1.5, '1', True, None):
            with self.subTest(count=bogus):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._record(billing_interval_count=bogus)
                self.assertEqual(
                    ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS,
                )

    def test_a_multi_unit_interval_is_representable(self):
        result = self._record(billing_interval_unit='week', billing_interval_count=2)
        self.assertEqual(
            (result.terms.billing_interval_unit, result.terms.billing_interval_count),
            ('week', 2),
        )

    def test_backdated_effective_from_is_accepted(self):
        backdated = timezone.now() - timedelta(days=365)
        result = self._record(effective_from=backdated)
        self.assertEqual(result.terms.effective_from, backdated)

    def test_future_effective_from_is_refused(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(effective_from=timezone.now() + timedelta(days=1))
        self.assertEqual(
            ctx.exception.code, errors.FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED,
        )
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_naive_effective_from_is_refused(self):
        from datetime import datetime
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(effective_from=datetime(2026, 1, 1))
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)

    def test_an_identical_retry_is_a_noop_preserving_identity_and_attribution(self):
        first = self._record()
        retry = self._record(actor=self.other_actor)

        self.assertFalse(retry.changed)
        self.assertEqual(retry.terms.pk, first.terms.pk)
        self.assertEqual(retry.terms.recorded_by_id, self.actor.id)
        self.assertEqual(retry.terms.recorded_at, first.terms.recorded_at)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)

    def test_recording_different_terms_over_open_ones_is_refused(self):
        """
        Record never supersedes. An accidental "create" must not be able to rewrite
        commercial history — replacing is a deliberate, separately named operation.
        """
        self._record()
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(recurring_amount=Decimal('999999.00'))
        self.assertEqual(ctx.exception.code, errors.SUBSCRIPTION_TERMS_ALREADY_OPEN)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)

    def test_record_after_ending_starts_a_fresh_open_row(self):
        first = self._record()
        self._end(first.terms.pk)
        second = self._record(effective_from=self.effective + timedelta(days=25))
        self.assertTrue(second.changed)
        self.assertNotEqual(second.terms.pk, first.terms.pk)
        self.assertEqual(self._open_count(), 1)

    # =====================================================================
    # REPLACE
    # =====================================================================

    def test_replacement_closes_the_old_row_and_opens_a_successor(self):
        first = self._record()
        boundary = self.effective + timedelta(days=10)

        result = self._replace(first.terms.pk, effective_from=boundary)

        self.assertTrue(result.changed)
        self.assertEqual(result.previous_terms.pk, first.terms.pk)
        self.assertNotEqual(result.terms.pk, first.terms.pk)
        self.assertEqual(self._open_count(), 1)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 2)

        old = RestaurantSubscriptionTerms.objects.get(pk=first.terms.pk)
        new = RestaurantSubscriptionTerms.objects.get(pk=result.terms.pk)
        # A CONTINUOUS boundary: no gap in which the tenant had no terms, no overlap
        # in which it had two.
        self.assertEqual(old.ended_at, boundary)
        self.assertEqual(new.effective_from, boundary)
        self.assertIsNone(new.ended_at)
        self.assertEqual(new.recorded_by_id, self.actor.id)

    def test_the_superseded_row_keeps_every_commercial_fact(self):
        first = self._record()
        snapshot = (
            first.terms.recurring_amount, first.terms.currency,
            first.terms.billing_interval_unit, first.terms.billing_interval_count,
            first.terms.effective_from, first.terms.recorded_at,
            first.terms.recorded_by_id,
        )
        self._replace(first.terms.pk)

        old = RestaurantSubscriptionTerms.objects.get(pk=first.terms.pk)
        self.assertEqual(
            (old.recurring_amount, old.currency, old.billing_interval_unit,
             old.billing_interval_count, old.effective_from, old.recorded_at,
             old.recorded_by_id),
            snapshot,
        )

    def test_replacing_with_the_same_commercial_tuple_is_a_noop(self):
        """
        No history spam. Re-dating unchanged terms is a separate correction problem;
        writing a historical row for it would fabricate a change that never happened.
        """
        first = self._record()
        result = self._replace(
            first.terms.pk,
            recurring_amount=Decimal('250000.00'),
            effective_from=self.effective + timedelta(days=5),
        )
        self.assertFalse(result.changed)
        self.assertEqual(result.terms.pk, first.terms.pk)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)
        first.terms.refresh_from_db()
        self.assertEqual(first.terms.effective_from, self.effective)

    def test_a_stale_expected_terms_id_conflicts(self):
        first = self._record()
        second = self._replace(first.terms.pk)

        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(first.terms.pk, recurring_amount=Decimal('400000.00'))

        self.assertEqual(ctx.exception.code, errors.STALE_SUBSCRIPTION_TERMS)
        self.assertEqual(self._open_count(), 1)
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.filter(
                ended_at__isnull=True).first().pk,
            second.terms.pk,
        )

    def test_an_exact_retry_of_a_completed_replacement_is_a_noop(self):
        """
        The retry names the OLD row while the open row is now the NEW one. Treated as
        a no-op only because the completed replacement is identifiable precisely: the
        named row is ended exactly at the requested instant, and the open row carries
        exactly the requested facts from exactly that instant.
        """
        first = self._record()
        boundary = self.effective + timedelta(days=10)
        done = self._replace(first.terms.pk, effective_from=boundary)

        retry = self._replace(first.terms.pk, effective_from=boundary)

        self.assertFalse(retry.changed)
        self.assertEqual(retry.terms.pk, done.terms.pk)
        self.assertEqual(retry.previous_terms.pk, first.terms.pk)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 2)

    def test_a_near_miss_retry_is_not_accepted_as_a_noop(self):
        """
        The proof must identify the exact completed replacement — not merely "some
        open row happens to have this amount". A different boundary is a different
        operation, so it conflicts.
        """
        first = self._record()
        self._replace(first.terms.pk, effective_from=self.effective + timedelta(days=10))

        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(first.terms.pk,
                          effective_from=self.effective + timedelta(days=11))
        self.assertEqual(ctx.exception.code, errors.STALE_SUBSCRIPTION_TERMS)

    def test_replacement_before_the_current_effective_from_is_refused(self):
        first = self._record()
        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(first.terms.pk,
                          effective_from=self.effective - timedelta(days=1))
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)
        first.terms.refresh_from_db()
        self.assertIsNone(first.terms.ended_at)

    def test_future_dated_replacement_is_refused(self):
        first = self._record()
        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(first.terms.pk,
                          effective_from=timezone.now() + timedelta(days=1))
        self.assertEqual(
            ctx.exception.code, errors.FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED,
        )
        first.terms.refresh_from_db()
        self.assertIsNone(first.terms.ended_at)

    def test_replacing_when_nothing_is_open_is_refused(self):
        first = self._record()
        self._end(first.terms.pk)
        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(first.terms.pk)
        self.assertEqual(ctx.exception.code, errors.NO_OPEN_SUBSCRIPTION_TERMS)

    def test_an_unknown_expected_terms_id_is_not_found(self):
        self._record()
        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(uuid.uuid4())
        self.assertEqual(ctx.exception.code, errors.SUBSCRIPTION_TERMS_NOT_FOUND)

    def test_terms_belonging_to_another_restaurant_are_not_found(self):
        other = Restaurant.objects.create(
            name='Other TW', location='loc-tw-2',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        foreign = subscription_terms.record_subscription_terms(
            restaurant_id=other.id, recurring_amount=Decimal('1.00'),
            currency='UGX', billing_interval_unit='month',
            billing_interval_count=1, effective_from=self.effective,
            actor=self.actor,
        )
        self._record()

        with self.assertRaises(CommercialMutationError) as ctx:
            self._replace(foreign.terms.pk)
        self.assertEqual(ctx.exception.code, errors.SUBSCRIPTION_TERMS_NOT_FOUND)

    def test_a_failed_replacement_insert_rolls_back_the_close(self):
        """
        ATOMICITY. If the successor cannot be created, the outgoing row must remain
        OPEN — a half-replacement would leave the tenant with no terms at all and no
        record of why.
        """
        first = self._record()
        with mock.patch.object(
            RestaurantSubscriptionTerms.objects, 'create',
            side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self._replace(first.terms.pk)

        first.terms.refresh_from_db()
        self.assertIsNone(first.terms.ended_at)
        self.assertEqual(self._open_count(), 1)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)

    # =====================================================================
    # END
    # =====================================================================

    def test_ending_closes_the_open_row_and_leaves_none_open(self):
        first = self._record()
        boundary = self.effective + timedelta(days=20)

        result = self._end(first.terms.pk, ended_at=boundary)

        self.assertTrue(result.changed)
        self.assertEqual(result.terms.pk, first.terms.pk)
        self.assertEqual(self._open_count(), 0)
        first.terms.refresh_from_db()
        self.assertEqual(first.terms.ended_at, boundary)
        self.assertFalse(first.terms.is_open)

    def test_ending_preserves_every_other_field(self):
        first = self._record()
        snapshot = (
            first.terms.recurring_amount, first.terms.currency,
            first.terms.billing_interval_unit, first.terms.billing_interval_count,
            first.terms.effective_from, first.terms.recorded_at,
            first.terms.recorded_by_id,
        )
        self._end(first.terms.pk)
        first.terms.refresh_from_db()
        self.assertEqual(
            (first.terms.recurring_amount, first.terms.currency,
             first.terms.billing_interval_unit, first.terms.billing_interval_count,
             first.terms.effective_from, first.terms.recorded_at,
             first.terms.recorded_by_id),
            snapshot,
        )

    def test_an_exact_retry_of_an_end_is_a_noop(self):
        first = self._record()
        boundary = self.effective + timedelta(days=20)
        self._end(first.terms.pk, ended_at=boundary)

        retry = self._end(first.terms.pk, ended_at=boundary)

        self.assertFalse(retry.changed)
        self.assertEqual(retry.terms.pk, first.terms.pk)
        self.assertEqual(self._open_count(), 0)

    def test_ending_with_a_different_timestamp_after_a_completed_end_conflicts(self):
        first = self._record()
        self._end(first.terms.pk, ended_at=self.effective + timedelta(days=20))
        with self.assertRaises(CommercialMutationError) as ctx:
            self._end(first.terms.pk, ended_at=self.effective + timedelta(days=21))
        self.assertEqual(ctx.exception.code, errors.NO_OPEN_SUBSCRIPTION_TERMS)

    def test_a_stale_expected_id_cannot_end_the_current_terms(self):
        first = self._record()
        second = self._replace(first.terms.pk)
        with self.assertRaises(CommercialMutationError) as ctx:
            self._end(first.terms.pk, ended_at=timezone.now())
        self.assertEqual(ctx.exception.code, errors.STALE_SUBSCRIPTION_TERMS)
        second.terms.refresh_from_db()
        self.assertIsNone(second.terms.ended_at)

    def test_ended_at_before_effective_from_is_refused(self):
        first = self._record()
        with self.assertRaises(CommercialMutationError) as ctx:
            self._end(first.terms.pk, ended_at=self.effective - timedelta(days=1))
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)
        first.terms.refresh_from_db()
        self.assertIsNone(first.terms.ended_at)

    def test_future_ended_at_is_refused(self):
        first = self._record()
        with self.assertRaises(CommercialMutationError) as ctx:
            self._end(first.terms.pk, ended_at=timezone.now() + timedelta(days=1))
        self.assertEqual(
            ctx.exception.code, errors.FUTURE_EFFECTIVE_TERMS_NOT_SUPPORTED,
        )
        first.terms.refresh_from_db()
        self.assertIsNone(first.terms.ended_at)

    def test_ending_creates_no_replacement_automatically(self):
        first = self._record()
        self._end(first.terms.pk)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)
        self.assertEqual(self._open_count(), 0)

    def test_history_is_retained_across_replacements_and_an_end(self):
        first = self._record()
        second = self._replace(first.terms.pk,
                               effective_from=self.effective + timedelta(days=10))
        self._end(second.terms.pk, ended_at=self.effective + timedelta(days=20))

        rows = RestaurantSubscriptionTerms.objects.filter(restaurant=self.restaurant)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.filter(ended_at__isnull=True).count(), 0)

    # =====================================================================
    # WHAT NONE OF THEM MAY DO
    # =====================================================================

    def test_legacy_subscription_fields_are_untouched(self):
        before = self._legacy_snapshot()
        first = self._record()
        second = self._replace(first.terms.pk)
        self._end(second.terms.pk, ended_at=timezone.now())
        self.assertEqual(self._legacy_snapshot(), before)

    def test_no_payment_or_transaction_row_is_created(self):
        before = DinifyTransaction.objects.count()
        first = self._record()
        second = self._replace(first.terms.pk)
        self._end(second.terms.pk, ended_at=timezone.now())
        self.assertEqual(DinifyTransaction.objects.count(), before)

    def test_no_admin_audit_row_is_written(self):
        from platform_admin_app.models import AdminAuditLog
        before = AdminAuditLog.objects.count()
        first = self._record()
        second = self._replace(first.terms.pk)
        self._end(second.terms.pk, ended_at=timezone.now())
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_no_service_configuration_is_created_as_a_side_effect(self):
        self._record()
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())

    def test_the_actor_is_recorded_never_described_as_agreement(self):
        """
        `recorded_by` means a platform-side operator wrote this down — never that the
        restaurant agreed, signed or accepted. The schema carries no such column and
        the writer invents none.
        """
        result = self._record()
        self.assertEqual(result.terms.recorded_by_id, self.actor.id)
        field_names = {f.name for f in RestaurantSubscriptionTerms._meta.get_fields()}
        for forbidden in ('agreed_by', 'agreed_at', 'accepted_by', 'signed_by',
                          'approved_by'):
            self.assertNotIn(forbidden, field_names)

    def test_targeting_failures_are_named_domain_errors(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record()
        self.assertEqual(ctx.exception.code, errors.RESTAURANT_DELETED)

    def test_an_unsaved_actor_is_refused_before_anything_is_written(self):
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(actor=User(email='ghost@test.com'))
        self.assertEqual(ctx.exception.code, errors.INVALID_ACTOR)
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())


class SubscriptionTermsReviewRegressionTests(_TermsWriterBase):
    """
    Four defects found by review on PR #298, each pinned so it cannot return.

    All four share a shape worth naming: input that is *syntactically* plausible
    escaping the domain-error contract — either as a raw Python/database exception
    (a 500 through a future adapter) or as a success whose stated postcondition is
    no longer true.
    """

    # --- an oversized amount must not escape as decimal.InvalidOperation ------

    def test_an_oversized_amount_is_a_domain_error_not_a_decimal_exception(self):
        """
        `Decimal('1e100').quantize(Decimal('0.01'))` raises `InvalidOperation` — the
        result exceeds the context precision. Testing the SCALE before the MAGNITUDE
        let that escape uncaught; the bound now runs first.
        """
        for oversized in ('1e100', '1E30', Decimal('1e100'), '99999999999999999999'):
            with self.subTest(amount=oversized):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._record(recurring_amount=oversized)
                self.assertEqual(
                    ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS,
                )
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_the_largest_storable_amount_is_still_accepted(self):
        """The bound must refuse only what the column cannot hold."""
        result = self._record(recurring_amount=Decimal('9999999999.99'))
        self.assertEqual(result.terms.recurring_amount, Decimal('9999999999.99'))

    # --- an out-of-range interval count must not escape as DataError ---------

    def test_an_interval_count_beyond_the_column_range_is_a_domain_error(self):
        """
        `PositiveIntegerField` is a 32-bit `integer` on PostgreSQL, so a larger count
        reached the INSERT and raised `DataError: integer out of range`.
        """
        for oversized in (2 ** 31, 2 ** 40):
            with self.subTest(count=oversized):
                with self.assertRaises(CommercialMutationError) as ctx:
                    self._record(billing_interval_count=oversized)
                self.assertEqual(
                    ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS,
                )
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_the_largest_storable_interval_count_is_still_accepted(self):
        result = self._record(billing_interval_count=2 ** 31 - 1)
        self.assertEqual(result.terms.billing_interval_count, 2 ** 31 - 1)

    # --- recording after an end must not overlap the closed window -----------

    def test_new_terms_may_not_begin_before_the_previous_terms_ended(self):
        """
        Terms effective 1 July, ended 1 August, then new terms effective 15 July
        would leave two sets in force from 15 July to 1 August — "which terms applied
        on 20 July?" would have two answers.
        """
        first = self._record()
        end_at = self.effective + timedelta(days=20)
        self._end(first.terms.pk, ended_at=end_at)

        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(effective_from=self.effective + timedelta(days=10))

        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)

    def test_new_terms_may_begin_exactly_when_the_previous_terms_ended(self):
        """The boundary is continuous, not exclusive: back-to-back is legitimate."""
        first = self._record()
        end_at = self.effective + timedelta(days=20)
        self._end(first.terms.pk, ended_at=end_at)

        second = self._record(effective_from=end_at)

        self.assertTrue(second.changed)
        self.assertEqual(second.terms.effective_from, end_at)
        self.assertEqual(self._open_count(), 1)

    def test_the_overlap_rule_uses_the_latest_end_across_all_history(self):
        first = self._record()
        second = self._replace(first.terms.pk,
                               effective_from=self.effective + timedelta(days=5))
        last_end = self.effective + timedelta(days=20)
        self._end(second.terms.pk, ended_at=last_end)

        # Between the first end (day 5) and the last (day 20) is still an overlap.
        with self.assertRaises(CommercialMutationError) as ctx:
            self._record(effective_from=self.effective + timedelta(days=12))
        self.assertEqual(ctx.exception.code, errors.INVALID_SUBSCRIPTION_TERMS)

        fresh = self._record(effective_from=last_end)
        self.assertTrue(fresh.changed)

    # --- an end retry must not mask newly-opened terms -----------------------

    def test_an_end_retry_is_refused_once_new_terms_have_opened(self):
        """
        Ending is documented to leave the restaurant with NO open terms. Once another
        operator has recorded fresh terms, replying "already done" would report
        success for a postcondition that no longer holds — and would slip past the
        `expected_terms_id` guard entirely.
        """
        first = self._record()
        end_at = self.effective + timedelta(days=20)
        self._end(first.terms.pk, ended_at=end_at)
        reopened = self._record(effective_from=end_at)

        with self.assertRaises(CommercialMutationError) as ctx:
            self._end(first.terms.pk, ended_at=end_at)

        self.assertEqual(ctx.exception.code, errors.STALE_SUBSCRIPTION_TERMS)
        self.assertEqual(ctx.exception.details['open_terms_id'],
                         str(reopened.terms.pk))
        # The newly-opened terms are untouched by the refused retry.
        reopened.terms.refresh_from_db()
        self.assertIsNone(reopened.terms.ended_at)
        self.assertEqual(self._open_count(), 1)

    def test_an_end_retry_still_no_ops_while_nothing_is_open(self):
        """The legitimate retry — the case the conditional must not break."""
        first = self._record()
        end_at = self.effective + timedelta(days=20)
        self._end(first.terms.pk, ended_at=end_at)

        retry = self._end(first.terms.pk, ended_at=end_at)

        self.assertFalse(retry.changed)
        self.assertEqual(retry.terms.pk, first.terms.pk)
        self.assertEqual(self._open_count(), 0)
