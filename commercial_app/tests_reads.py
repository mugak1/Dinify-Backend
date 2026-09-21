"""
The shared commercial reads: ONE selection rule, and a projection narrow enough for
the restaurant's own surfaces (D07).

Two things are pinned here and they are different in kind.

THE SELECTOR is shared by identity. ``subscription_terms._open_terms`` IS
``reads.open_terms`` — not a second implementation that agrees today — because the
writers that refuse a second open row and the readers that report the current one
must never form different opinions about which row that is.

THE PROJECTION is deliberately NARROWER than the Admin one, and the narrowing is
asserted rather than described: ``id`` (an Admin write's concurrency token),
``recorded_at`` and ``recorded_by`` (who wrote it down, and when) must not reach a
restaurant's own read. A test that only checked the fields that ARE present would
pass just as happily if the whole model were serialized.
"""
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from commercial_app import reads, subscription_terms
from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from restaurants_app.models import Restaurant

User = get_user_model()


class _ReadsBase(TestCase):
    """Fixture only — a base that carried tests would re-run them per subclass."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='cr-owner@test.com',
            phone_number='256773100201', username='256773100201',
            country='Uganda', password='password', roles=[],
        )
        self.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='cr-actor@test.com',
            phone_number='256773100202', username='256773100202',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Commercial Reads Ltd', location='loc-cr',
            status=RestaurantStatus_Live, owner=self.owner,
        )

    def _record(self, amount='150000.00', currency='UGX', unit='month', count=1,
                effective_from=None):
        return subscription_terms.record_subscription_terms(
            restaurant_id=str(self.restaurant.pk),
            recurring_amount=Decimal(amount),
            currency=currency,
            billing_interval_unit=unit,
            billing_interval_count=count,
            effective_from=effective_from or (
                timezone.now() - timedelta(days=30)
            ),
            actor=self.actor,
        ).terms


class TheSelectionRuleIsSharedTests(_ReadsBase):
    """
    One rule, one object. A copy that agrees today is what drifts tomorrow.
    """

    def test_the_writers_and_the_readers_use_the_same_selector(self):
        # IDENTITY, not equality of behaviour. Re-implementing `_open_terms` inside
        # `subscription_terms` would satisfy every behavioural assertion in this
        # file and fail exactly this one, which is the point.
        self.assertIs(subscription_terms._open_terms, reads.open_terms)

    def test_open_means_ended_at_is_null_and_nothing_else(self):
        terms = self._record()
        self.assertEqual(reads.open_terms(self.restaurant), terms)

        subscription_terms.end_subscription_terms(
            restaurant_id=str(self.restaurant.pk),
            expected_terms_id=str(terms.pk),
            # `end_subscription_terms` takes NO actor: Step 3B added no
            # `ended_by` column, and the adapter's audit row records who.
            ended_at=timezone.now(),
        )
        # CONTROL for "the latest row": the row is still the newest and the only
        # one this restaurant has ever had. It is simply no longer open.
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 1)
        self.assertIsNone(reads.open_terms(self.restaurant))

    def test_a_restaurant_with_no_terms_selects_nothing(self):
        self.assertIsNone(reads.open_terms(self.restaurant))

    def test_the_selector_accepts_an_instance_or_a_primary_key(self):
        terms = self._record()
        self.assertEqual(reads.open_terms(self.restaurant), terms)
        self.assertEqual(reads.open_terms(self.restaurant.pk), terms)

    def test_terms_are_scoped_to_their_own_restaurant(self):
        self._record()
        neighbour = Restaurant.objects.create(
            name='Neighbour Ltd', location='loc-nb',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self.assertIsNone(reads.open_terms(neighbour))


class TheProjectionTests(_ReadsBase):

    def test_it_carries_the_price_currency_recurrence_and_effect_date(self):
        terms = self._record(amount='150000.00', unit='month', count=2)
        projected = reads.project_terms(terms)
        self.assertEqual(projected['recurring_amount'], '150000.00')
        self.assertEqual(projected['currency'], 'UGX')
        self.assertEqual(projected['billing_interval'], {'unit': 'month', 'count': 2})
        self.assertEqual(
            projected['effective_from'], terms.effective_from.isoformat(),
        )

    def test_the_amount_is_an_exact_decimal_string_never_a_float(self):
        # `str()` on the stored Decimal, so the scale survives. A float here is the
        # conversion the repository's money rule forbids.
        terms = self._record(amount='150000.00')
        amount = reads.project_terms(terms)['recurring_amount']
        self.assertIsInstance(amount, str)
        self.assertEqual(amount, '150000.00')

    def test_a_zero_price_is_a_price(self):
        # A free pilot, a waived period, a rehearsing test tenant. `"0.00"` — never
        # `0`, never `0.0`, and never the word "free", which this domain does not
        # have. It is also a DIFFERENT fact from "no terms exist".
        terms = self._record(amount='0.00')
        self.assertEqual(reads.project_terms(terms)['recurring_amount'], '0.00')

    def test_it_withholds_the_admin_only_fields(self):
        # The narrowing is the contract: `id` is a concurrency token for a write
        # this caller cannot perform, and the `recorded_*` pair names a platform
        # operator and an internal bookkeeping moment.
        projected = reads.project_terms(self._record())
        self.assertEqual(
            set(projected),
            {'recurring_amount', 'currency', 'billing_interval', 'effective_from'},
        )

    def test_it_is_total_over_none(self):
        self.assertIsNone(reads.project_terms(None))

    def test_it_touches_no_database(self):
        terms = self._record()
        with self.assertNumQueries(0):
            reads.project_terms(terms)


class TheSummaryTests(_ReadsBase):

    def test_recorded_terms_are_reported_with_their_facts(self):
        self._record(amount='90000.00')
        summary = reads.subscription_terms_summary(self.restaurant)
        self.assertTrue(summary['recorded'])
        self.assertEqual(summary['current']['recurring_amount'], '90000.00')

    def test_absence_is_stated_explicitly_rather_than_omitted(self):
        # A consumer that cannot tell "no terms recorded" from "the server did not
        # say" will invent a default, and the default is what puts a price nobody
        # agreed to on a screen. Both keys are present and answer the question.
        summary = reads.subscription_terms_summary(self.restaurant)
        self.assertEqual(summary, {'recorded': False, 'current': None})

    def test_ended_terms_are_not_current(self):
        terms = self._record()
        subscription_terms.end_subscription_terms(
            restaurant_id=str(self.restaurant.pk),
            expected_terms_id=str(terms.pk),
            # `end_subscription_terms` takes NO actor: Step 3B added no
            # `ended_by` column, and the adapter's audit row records who.
            ended_at=timezone.now(),
        )
        self.assertEqual(
            reads.subscription_terms_summary(self.restaurant),
            {'recorded': False, 'current': None},
        )

    def test_it_costs_one_query(self):
        self._record()
        with self.assertNumQueries(1):
            reads.subscription_terms_summary(self.restaurant)

    def test_reading_creates_nothing(self):
        # No `get_or_create`, no default configuration row, no backfill. Reading an
        # unconfigured restaurant must leave it unconfigured.
        reads.subscription_terms_summary(self.restaurant)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 0)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 0)
