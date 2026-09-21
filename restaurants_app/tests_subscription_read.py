"""
The restaurant's own subscription read (D07 / PR-2).

``GET /api/v1/restaurant-setup/subscription-details/`` now states two facts the
billing screen previously invented: whether Dinify can collect a subscription
payment in-app at all, and what terms — if any — have actually been recorded.

WHAT IS PINNED HERE, AND WHY EACH MATTERS:

  * the capability is the SERVER's, published from the same constant the collection
    boundary is pinned against, so a portal cannot offer a Pay button the server
    refuses;
  * absence of terms is an EXPLICIT successful answer, never an omitted key — a
    consumer that cannot tell "none recorded" from "the server did not say" will
    default, and the default is what put a fabricated price catalogue on the screen
    in the first place;
  * the money crosses the WIRE as a decimal string. ``response.data`` is the value
    the view BUILT, never the value a client PARSES, so the amount assertions decode
    ``response.content`` with a ``parse_float`` hook that marks every JSON float.
    Nothing else can see the difference between ``"0.00"`` and ``0.0``;
  * the legacy ``subscription_validity`` / ``subscription_expiry_date`` pair decides
    NOTHING, in either direction, and is still returned for the deployed client;
  * the authorization gate is untouched — the settings MODULE resolver, not the
    retired collector's manage-level permission — and the delegated exclusion holds.
"""
import json
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from commercial_app import subscription_terms
from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
    RESTAURANT_STAFF,
    RestaurantStatus_Live,
)
from finance_app import subscription_capability
from platform_admin_app.configs.delegation_scopes import SETUP_READABLE_RECORDS
from restaurants_app.models import Restaurant, RestaurantEmployee

User = get_user_model()

URL = '/api/v1/restaurant-setup/subscription-details/'


class _SubscriptionReadBase(TestCase):
    """Fixture only — a base carrying tests would re-run them under each subclass."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Sub', last_name='Owner', email='subread-owner@test.com',
            phone_number='256774000301', username='256774000301',
            country='Uganda', password='password', roles=[],
        )
        self.actor = User.objects.create_user(
            first_name='Plat', last_name='Staff', email='subread-actor@test.com',
            phone_number='256774000302', username='256774000302',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Subscription Read Ltd', location='loc-sr',
            status=RestaurantStatus_Live, owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
        )

    # -- helpers --------------------------------------------------------------

    def _token(self, user):
        return str(RefreshToken.for_user(user).access_token)

    def _get(self, user=None, restaurant=None):
        target = self.restaurant.pk if restaurant is None else restaurant
        return self.client.get(
            f'{URL}?restaurant={target}',
            HTTP_AUTHORIZATION=f'Bearer {self._token(user or self.owner)}',
        )

    def _data(self, response):
        self.assertEqual(response.status_code, 200)
        return response.json()['data']

    def _wire(self, response):
        """
        The response as a CLIENT parses it, with every JSON float marked.

        DRF renders a ``Decimal`` through ``float()``, so asserting on
        ``response.data`` compares the value the view assembled rather than the one
        that reaches the browser — the one place the money contract can actually
        break. ``parse_float`` is the only way to see it.
        """
        self.assertEqual(response.status_code, 200)
        return json.loads(
            response.content.decode(),
            parse_float=lambda raw: ('FLOAT_ON_THE_WIRE', raw),
        )['data']

    def _record(self, amount='150000.00', currency='UGX', unit='month', count=1):
        return subscription_terms.record_subscription_terms(
            restaurant_id=str(self.restaurant.pk),
            recurring_amount=Decimal(amount),
            currency=currency,
            billing_interval_unit=unit,
            billing_interval_count=count,
            effective_from=timezone.now() - timedelta(days=30),
            actor=self.actor,
        ).terms


class TheCollectionCapabilityIsPublishedTests(_SubscriptionReadBase):

    def test_the_read_states_that_in_app_collection_is_unsupported(self):
        self.assertIs(self._data(self._get())['in_app_collection_supported'], False)

    def test_it_publishes_the_capability_constant_rather_than_a_local_literal(self):
        # The portal and the collection boundary read ONE fact. Restating the
        # boolean here (or there) is how a Pay button outlives its collector.
        self.assertIs(
            self._data(self._get())['in_app_collection_supported'],
            subscription_capability.IN_APP_COLLECTION_SUPPORTED,
        )

    def test_the_capability_is_a_real_boolean_on_the_wire(self):
        # Not `"false"`, not `0`, not absent: a client branching on it must not have
        # to guess which falsy spelling it received.
        self.assertIs(
            self._wire(self._get())['in_app_collection_supported'], False,
        )


class RecordedTermsAreReportedTests(_SubscriptionReadBase):

    def test_the_canonical_terms_reach_the_restaurant(self):
        self._record(amount='150000.00', unit='month', count=2)
        terms = self._data(self._get())['subscription_terms']
        self.assertTrue(terms['recorded'])
        self.assertEqual(terms['current']['recurring_amount'], '150000.00')
        self.assertEqual(terms['current']['currency'], 'UGX')
        self.assertEqual(
            terms['current']['billing_interval'], {'unit': 'month', 'count': 2},
        )

    def test_the_effect_date_is_an_explicit_iso_string(self):
        recorded = self._record()
        current = self._data(self._get())['subscription_terms']['current']
        self.assertEqual(
            current['effective_from'], recorded.effective_from.isoformat(),
        )

    def test_the_amount_crosses_the_wire_as_a_decimal_string(self):
        self._record(amount='150000.00')
        current = self._wire(self._get())['subscription_terms']['current']
        self.assertEqual(current['recurring_amount'], '150000.00')

    def test_a_zero_price_survives_the_wire_as_zero_point_zero_zero(self):
        # THE case the `parse_float` hook exists for: a float `0.00` renders as
        # `0.0` and loses the stored scale, and a zero price is a real, deliberate
        # decision (a free pilot, a waived period) rather than an absence.
        self._record(amount='0.00')
        current = self._wire(self._get())['subscription_terms']['current']
        self.assertEqual(current['recurring_amount'], '0.00')

    def test_the_restaurant_is_not_handed_admin_only_fields(self):
        self._record()
        current = self._data(self._get())['subscription_terms']['current']
        self.assertEqual(
            set(current),
            {'recurring_amount', 'currency', 'billing_interval', 'effective_from'},
        )

    def test_ended_terms_are_not_reported_as_current(self):
        recorded = self._record()
        subscription_terms.end_subscription_terms(
            restaurant_id=str(self.restaurant.pk),
            expected_terms_id=str(recorded.pk),
            ended_at=timezone.now(),
        )
        self.assertEqual(
            self._data(self._get())['subscription_terms'],
            {'recorded': False, 'current': None},
        )


class AbsenceIsAnAnswerTests(_SubscriptionReadBase):

    def test_no_recorded_terms_is_a_successful_explicit_state(self):
        self.assertEqual(
            self._data(self._get())['subscription_terms'],
            {'recorded': False, 'current': None},
        )

    def test_the_key_is_present_rather_than_omitted(self):
        # Omission would leave the client to invent a default. It is the same
        # defect class as reporting an outage as a rejected credential.
        self.assertIn('subscription_terms', self._data(self._get()))


class NothingIsInferredFromTheLegacyColumnsTests(_SubscriptionReadBase):
    """
    The legacy pair is returned and decides nothing. Both directions are pinned,
    because each one on its own looks like the safe reading.
    """

    def test_a_valid_looking_legacy_flag_does_not_manufacture_terms(self):
        # THE DANGEROUS DIRECTION. `subscription_validity` defaults to True and no
        # supported writer maintains it, so reading it as evidence would tell almost
        # every restaurant it has a subscription nobody ever recorded.
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            subscription_validity=True,
            subscription_expiry_date=timezone.now() + timedelta(days=365),
        )
        data = self._data(self._get())
        self.assertIs(data['subscription_validity'], True)
        self.assertEqual(
            data['subscription_terms'], {'recorded': False, 'current': None},
        )

    def test_a_false_legacy_flag_does_not_suppress_recorded_terms(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            subscription_validity=False,
        )
        self._record(amount='75000.00')
        data = self._data(self._get())
        self.assertIs(data['subscription_validity'], False)
        self.assertTrue(data['subscription_terms']['recorded'])
        self.assertEqual(
            data['subscription_terms']['current']['recurring_amount'], '75000.00',
        )

    def test_flat_fee_is_never_reported_as_the_price(self):
        # `flat_fee` is platform-owned, stripped from every tenant PUT, and is NOT
        # the canonical terms. Reconstructing a price from it would be the exact
        # fabrication this work removes.
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            flat_fee=Decimal('999999.00'),
            preferred_subscription_method='monthly',
        )
        data = self._data(self._get())
        self.assertEqual(
            data['subscription_terms'], {'recorded': False, 'current': None},
        )
        self.assertNotIn('999999', json.dumps(data))

    def test_the_legacy_pair_is_still_returned_for_the_deployed_client(self):
        # ADDITIVE: expand now, contract once the portal has migrated. Removing
        # these in the same change that stopped reading them is the one thing the
        # expand-then-contract rule forbids.
        data = self._data(self._get())
        self.assertIn('subscription_validity', data)
        self.assertIn('subscription_expiry_date', data)


class TheReadIsReadOnlyTests(_SubscriptionReadBase):

    def test_it_creates_no_commercial_rows(self):
        self._get()
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 0)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 0)

    def test_every_statement_it_issues_is_a_select(self):
        # Asserted generally rather than table by table, so a write to something
        # nobody thought of fails too.
        # Mint the token OUTSIDE the capture: `RefreshToken.for_user` INSERTs an
        # OutstandingToken, so capturing it would report the TEST HARNESS's write as
        # the read's.
        token = self._token(self.owner)
        with CaptureQueriesContext(connection) as captured:
            self.client.get(
                f'{URL}?restaurant={self.restaurant.pk}',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
        offenders = [
            q['sql'] for q in captured.captured_queries
            if not q['sql'].lstrip().upper().startswith('SELECT')
        ]
        self.assertEqual(offenders, [], 'the read issued a non-SELECT statement')
        # A CONTROL for the assertion itself: a capture that saw nothing would
        # satisfy the check above without proving anything.
        self.assertTrue(captured.captured_queries)

    def test_recorded_terms_are_not_mutated_by_being_read(self):
        recorded = self._record()
        before = RestaurantSubscriptionTerms.objects.get(pk=recorded.pk)
        self._get()
        after = RestaurantSubscriptionTerms.objects.get(pk=recorded.pk)
        for field in (
            'recurring_amount', 'currency', 'billing_interval_unit',
            'billing_interval_count', 'effective_from', 'ended_at',
            'recorded_at', 'recorded_by_id',
        ):
            self.assertEqual(getattr(before, field), getattr(after, field), field)


class TheAuthorizationGateIsUnchangedTests(_SubscriptionReadBase):
    """
    Read permission is the SETTINGS MODULE resolver, deliberately NOT the retired
    collector's manage-level permission: reading what your restaurant pays and
    attempting to pay it are different decisions.
    """

    def test_a_manager_may_read(self):
        manager = User.objects.create_user(
            first_name='Sub', last_name='Manager', email='subread-mgr@test.com',
            phone_number='256774000303', username='256774000303',
            country='Uganda', password='password', roles=[],
        )
        RestaurantEmployee.objects.create(
            user=manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER],
        )
        self.assertEqual(self._get(user=manager).status_code, 200)

    def test_a_staff_member_without_the_settings_module_is_refused(self):
        # The default grid gives `restaurant_staff` tables only. A read that
        # answered here would mean the module resolver had been bypassed.
        staff = User.objects.create_user(
            first_name='Sub', last_name='Staff', email='subread-staff@test.com',
            phone_number='256774000304', username='256774000304',
            country='Uganda', password='password', roles=[],
        )
        RestaurantEmployee.objects.create(
            user=staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF],
        )
        self.assertEqual(self._get(user=staff).status_code, 404)

    def test_another_tenants_terms_are_not_disclosed(self):
        # The new field must not become the first thing that leaks across the
        # boundary. 404, not 403, so existence is not confirmed either.
        self._record(amount='150000.00')
        outsider = User.objects.create_user(
            first_name='Out', last_name='Sider', email='subread-out@test.com',
            phone_number='256774000305', username='256774000305',
            country='Uganda', password='password', roles=[],
        )
        other = Restaurant.objects.create(
            name='Outsider Ltd', location='loc-out',
            status=RestaurantStatus_Live, owner=outsider,
        )
        RestaurantEmployee.objects.create(
            user=outsider, restaurant=other, roles=[RESTAURANT_OWNER],
        )
        response = self._get(user=outsider)          # asks for OUR restaurant
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('150000', response.content.decode())

    def test_an_anonymous_caller_is_refused(self):
        self.assertIn(self.client.get(f'{URL}?restaurant={self.restaurant.pk}')
                      .status_code, (401, 403))

    def test_a_delegated_session_cannot_reach_this_record(self):
        # Preserved, not newly asserted: `subscription-details` is absent from the
        # delegated read allowlist, so the middleware refuses it before dispatch.
        # Adding it there would hand a delegated administrator a tenant's
        # commercial terms as an ordinary consequence of opening a screen.
        self.assertNotIn('subscription-details', SETUP_READABLE_RECORDS)


class MalformedInputStillFailsClosedTests(_SubscriptionReadBase):

    def test_an_unknown_restaurant_is_refused_without_a_terms_read(self):
        import uuid
        response = self._get(restaurant=uuid.uuid4())
        self.assertEqual(response.status_code, 404)

    def test_a_malformed_restaurant_id_never_500s(self):
        response = self._get(restaurant='not-a-uuid')
        self.assertEqual(response.status_code, 404)

    def test_an_absent_restaurant_parameter_never_500s(self):
        response = self.client.get(
            URL, HTTP_AUTHORIZATION=f'Bearer {self._token(self.owner)}',
        )
        self.assertIn(response.status_code, (400, 404))
