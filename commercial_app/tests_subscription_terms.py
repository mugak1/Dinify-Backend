"""
Schema tests for ``RestaurantSubscriptionTerms`` (Phase 1, Step 3B).

As with the service-configuration suite, every negative case writes directly
through the ORM so what is proved is a DATABASE guarantee, not model validation.

The last three tests pin ARCHITECTURAL boundaries rather than behaviours: terms are
commercial intent, and the ways they could quietly become a payment-state model or
a commission model are named explicitly so that adding one has to be deliberate.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from commercial_app.models import (
    BILLING_INTERVAL_UNIT_VALUES,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from restaurants_app.models import Restaurant

User = get_user_model()


class SubscriptionTermsSchemaTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='terms-owner@test.com',
            phone_number='256772000201', username='256772000201',
            country='Uganda', password='password', roles=[],
        )
        cls.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='terms-actor@test.com',
            phone_number='256772000202', username='256772000202',
            country='Uganda', password='password', roles=[],
        )
        cls.restaurant = Restaurant.objects.create(
            name='Terms Ltd', location='loc-terms',
            status=RestaurantStatus_Live, owner=cls.owner,
        )
        cls.effective = timezone.now()

    def _terms(self, **overrides):
        fields = {
            'restaurant': self.restaurant,
            'recurring_amount': Decimal('250000.00'),
            'currency': 'UGX',
            'billing_interval_unit': 'month',
            'billing_interval_count': 1,
            'effective_from': self.effective,
            'recorded_by': self.actor,
        }
        fields.update(overrides)
        return RestaurantSubscriptionTerms.objects.create(**fields)

    # --- A, B, C, D: history and the open-row invariant ---------------------

    def test_a_restaurant_can_exist_with_zero_terms_rows(self):
        """Absence means "no terms recorded", which is every restaurant today."""
        self.assertFalse(
            RestaurantSubscriptionTerms.objects
            .filter(restaurant=self.restaurant).exists()
        )

    def test_b_one_open_terms_row_is_valid(self):
        terms = self._terms()
        terms.refresh_from_db()
        self.assertIsNone(terms.ended_at)
        self.assertTrue(terms.is_open)

    def test_c_multiple_historical_ended_rows_are_valid(self):
        """
        HISTORY IS THE POINT. Terms change by closing the open row and inserting a
        replacement, never by editing an amount in place — a future invoice or
        owner approval references the exact row it was raised or given under, and
        neither is expressible if the numbers move underneath it.
        """
        for index in range(3):
            start = self.effective - timedelta(days=90 * (index + 1))
            self._terms(
                recurring_amount=Decimal('100000.00') + index,
                effective_from=start,
                ended_at=start + timedelta(days=30),
            )
        # ...plus a current one alongside all of them.
        self._terms()

        rows = RestaurantSubscriptionTerms.objects.filter(restaurant=self.restaurant)
        self.assertEqual(rows.count(), 4)
        self.assertEqual(rows.filter(ended_at__isnull=True).count(), 1)

    def test_d_two_simultaneously_open_terms_rows_are_rejected(self):
        """
        The hard database invariant. Note the partial-index predicate consults no
        clock (it must be immutable), so "open" means only "has no ended_at" — which
        is what makes a future writer's close-then-insert step load-bearing.
        """
        self._terms()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._terms(effective_from=self.effective + timedelta(days=1))

    def test_an_ended_row_frees_the_open_slot(self):
        """The other half of D: closing the current row permits its replacement."""
        first = self._terms()
        first.ended_at = self.effective + timedelta(days=30)
        first.save(update_fields=['ended_at'])

        second = self._terms(effective_from=self.effective + timedelta(days=30))
        self.assertEqual(
            RestaurantSubscriptionTerms.objects
            .filter(restaurant=self.restaurant, ended_at__isnull=True).count(),
            1,
        )
        self.assertEqual(second.restaurant_id, self.restaurant.id)

    def test_two_restaurants_may_each_hold_an_open_row(self):
        """The uniqueness is per restaurant, not global."""
        other = Restaurant.objects.create(
            name='Other Terms Ltd', location='loc-terms-2',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        self._terms()
        self._terms(restaurant=other)
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.filter(ended_at__isnull=True).count(),
            2,
        )

    # --- E, F, G: the amount ------------------------------------------------

    def test_e_negative_recurring_amount_is_rejected(self):
        """A negative fee is Dinify paying the restaurant, which this cannot mean."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._terms(recurring_amount=Decimal('-1.00'))

    def test_f_zero_recurring_amount_is_accepted(self):
        """
        ZERO IS AN EXPLICIT, LEGITIMATE PRICE — a test tenant rehearsing the real
        onboarding path, a free pilot, a waived period. Crucially it is a DIFFERENT
        fact from "no terms exist" (test A), which is why no separate `chargeable`
        boolean is needed: the row's presence and its amount already say everything.
        """
        terms = self._terms(recurring_amount=Decimal('0.00'))
        terms.refresh_from_db()
        self.assertEqual(terms.recurring_amount, Decimal('0.00'))
        self.assertTrue(
            RestaurantSubscriptionTerms.objects
            .filter(restaurant=self.restaurant).exists()
        )

    def test_g_positive_recurring_amount_is_accepted(self):
        terms = self._terms(recurring_amount=Decimal('250000.00'))
        terms.refresh_from_db()
        self.assertEqual(terms.recurring_amount, Decimal('250000.00'))

    def test_the_amount_is_a_decimal_not_a_float(self):
        """The repository money rule; also enforced in CI by check_money_fields."""
        field = RestaurantSubscriptionTerms._meta.get_field('recurring_amount')
        self.assertEqual(field.get_internal_type(), 'DecimalField')
        self.assertEqual(field.decimal_places, 2)

    # --- H, I, J: the recurrence -------------------------------------------

    def test_h_billing_interval_count_of_zero_is_rejected(self):
        """"Every 0 months" is not a recurrence."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._terms(billing_interval_count=0)

    def test_i_invalid_billing_interval_unit_is_rejected(self):
        """
        `per_order` is refused explicitly. Dinify's revenue model is a recurring
        software subscription; reviving that word would reintroduce the
        per-order-commission framing the non-custodial posture keeps out.
        """
        for bogus in ('per_order', 'quarter', 'MONTH', 'monthly', ''):
            with self.subTest(billing_interval_unit=bogus):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._terms(billing_interval_unit=bogus)

    def test_j_every_supported_interval_unit_is_accepted(self):
        for index, unit in enumerate(BILLING_INTERVAL_UNIT_VALUES):
            with self.subTest(billing_interval_unit=unit):
                restaurant = Restaurant.objects.create(
                    name=f'Unit {unit}', location=f'loc-unit-{index}',
                    status=RestaurantStatus_Live, owner=self.owner,
                )
                terms = self._terms(restaurant=restaurant, billing_interval_unit=unit)
                terms.refresh_from_db()
                self.assertEqual(terms.billing_interval_unit, unit)

    def test_a_multi_unit_interval_is_representable(self):
        """
        `week + 2` — generic recurrence, not a frozen plan catalogue. The schema can
        carry real terms without pretending a named plan set has been chosen.
        """
        terms = self._terms(billing_interval_unit='week', billing_interval_count=2)
        terms.refresh_from_db()
        self.assertEqual((terms.billing_interval_unit, terms.billing_interval_count),
                         ('week', 2))

    # --- K, L: the effective window ----------------------------------------

    def test_k_ended_at_before_effective_from_is_rejected(self):
        """Terms that ended before they applied never had a period of effect."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._terms(ended_at=self.effective - timedelta(seconds=1))

    def test_l_ended_at_after_or_equal_to_effective_from_is_accepted(self):
        for label, ended in (
            ('equal', self.effective),
            ('after', self.effective + timedelta(days=30)),
        ):
            with self.subTest(ended_at=label):
                restaurant = Restaurant.objects.create(
                    name=f'Window {label}', location=f'loc-window-{label}',
                    status=RestaurantStatus_Live, owner=self.owner,
                )
                terms = self._terms(restaurant=restaurant, ended_at=ended)
                terms.refresh_from_db()
                self.assertEqual(terms.ended_at, ended)
                self.assertFalse(terms.is_open)

    # --- M: recording attribution ------------------------------------------

    def test_m_terms_carry_recorded_at_and_recorded_by(self):
        """
        `recorded_*` means A PLATFORM OPERATOR WROTE THIS DOWN — never that the
        restaurant agreed, signed or paid. That is why the model is called Terms and
        why there are no `agreed_at` / `agreed_by` columns: evidence of the current
        owner's consent comes from the future owner go-live approval.
        """
        terms = self._terms()
        terms.refresh_from_db()
        self.assertEqual(terms.recorded_by_id, self.actor.id)
        self.assertIsNotNone(terms.recorded_at)

        field_names = {f.name for f in RestaurantSubscriptionTerms._meta.get_fields()}
        for forbidden in ('agreed_at', 'agreed_by', 'signed_at', 'signed_by',
                          'accepted_at', 'accepted_by', 'approved_at', 'approved_by'):
            self.assertNotIn(forbidden, field_names)

    def test_effective_from_is_independent_of_recorded_at(self):
        """
        Backdating is legitimate: an operator may on Tuesday record terms that took
        effect last month. `effective_from` is never fabricated from `recorded_at`
        or from ``Restaurant.time_created``.
        """
        backdated = self.effective - timedelta(days=45)
        terms = self._terms(effective_from=backdated)
        terms.refresh_from_db()
        self.assertEqual(terms.effective_from, backdated)
        self.assertGreater(terms.recorded_at, terms.effective_from)

    # --- currency -----------------------------------------------------------

    def test_currency_is_stored_exactly_and_has_no_default(self):
        terms = self._terms(currency='KES')
        terms.refresh_from_db()
        self.assertEqual(terms.currency, 'KES')
        # No default: "somebody chose UGX" and "the column defaulted" must stay
        # distinguishable, exactly as for the two service-configuration axes.
        self.assertIs(
            RestaurantSubscriptionTerms._meta.get_field('currency').has_default(),
            False,
        )

    def test_malformed_currency_is_rejected_by_the_database(self):
        """
        SHAPE only — three uppercase ASCII letters. WHICH currencies Dinify actually
        supports is policy for the future domain writer, not a database fact.
        """
        for bogus in ('ugx', 'UG', 'U1X', 'u', ''):
            with self.subTest(currency=bogus):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._terms(currency=bogus)

    # --- N, O, P: the architectural boundaries ------------------------------

    def test_n_no_payment_or_standing_state_field_exists(self):
        """
        TERMS ARE NOT A PAYMENT-STATE MODEL. Every one of these words would need a
        maintainer, and this repository runs nothing on a schedule — a status column
        nothing updates is a lie with a timestamp on it (the
        ``subscription_validity`` lesson). "Open" is derived from ``ended_at``.
        """
        field_names = {f.name for f in RestaurantSubscriptionTerms._meta.get_fields()}
        for forbidden in ('paid', 'active', 'valid', 'validity', 'good_standing',
                          'status', 'payment_status', 'merchant_status',
                          'expired', 'expires_at', 'current', 'is_paid'):
            self.assertNotIn(forbidden, field_names)

    def test_o_no_commission_or_surcharge_field_exists(self):
        """
        Dinify earns a recurring SOFTWARE SUBSCRIPTION, never a percentage of diner
        money. A commission column here would quietly restate the custodial revenue
        model the non-custodial migration removed.
        """
        field_names = {f.name for f in RestaurantSubscriptionTerms._meta.get_fields()}
        for forbidden in ('commission_rate', 'commission', 'per_order_rate',
                          'surcharge_percentage', 'surcharge', 'gmv_percentage',
                          'take_rate'):
            self.assertNotIn(forbidden, field_names)

    def test_p_no_relation_to_an_order_or_a_transaction(self):
        """
        An FK to ``Order`` or ``DinifyTransaction`` is precisely how commercial
        TERMS would become a transaction/payment state model. The only relations
        this model may hold are its tenant and the operator who recorded it.
        """
        related = {
            field.related_model
            for field in RestaurantSubscriptionTerms._meta.get_fields()
            if field.is_relation and field.related_model is not None
        }
        self.assertEqual({model.__name__ for model in related}, {'Restaurant', 'User'})
