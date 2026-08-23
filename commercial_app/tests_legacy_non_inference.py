"""
The commercial domain infers NOTHING from legacy fields (Phase 1, Step 3B).

Step 3B adds authoritative storage ALONGSIDE the legacy payment/subscription
columns and derives nothing from them: no data migration, no signal, no
``post_save``, no ``get_or_create`` on a read path, no default row.

WHY THIS MATTERS ENOUGH TO TEST. Each legacy field looks like it answers one of the
new questions and none of them does:

  ``require_order_prepayments``  has ZERO runtime readers — a stored intention that
      nothing enforces cannot establish a service model.
  ``Table.prepayment_required``  is copied onto an order and then never gated on.
  ``preferred_subscription_method`` / ``flat_fee`` belong to the old restaurant ->
      Dinify billing flow, and were tenant-reachable until recently.
  ``subscription_validity``      is a bare boolean defaulting True whose only
      writer was deleted, so True is indistinguishable from "never touched".
  ``subscription_expiry_date``   was never compared to a clock by anything.

A backfill from any of them would fabricate authoritative commercial facts for
every restaurant that has ever existed — which is exactly the failure the new
domain is built to avoid.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
)
from restaurants_app.models import Restaurant, Table

User = get_user_model()


class LegacyFieldsCreateNoCommercialFactsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='legacy-owner@test.com',
            phone_number='256772000301', username='256772000301',
            country='Uganda', password='password', roles=[],
        )

    def _assert_no_commercial_rows(self, restaurant):
        self.assertFalse(
            RestaurantServiceConfiguration.objects
            .filter(restaurant=restaurant).exists(),
            'A service configuration was created without an explicit decision.',
        )
        self.assertFalse(
            RestaurantSubscriptionTerms.objects
            .filter(restaurant=restaurant).exists(),
            'Subscription terms were created without an explicit decision.',
        )

    def test_maximally_suggestive_legacy_state_creates_no_commercial_rows(self):
        """
        Every legacy field set to the value a naive backfill would have translated.
        The new domain stays empty.
        """
        restaurant = Restaurant.objects.create(
            name='Suggestive Ltd', location='loc-suggestive',
            status=RestaurantStatus_Live, owner=self.owner,
            require_order_prepayments=True,
            preferred_subscription_method='monthly',
            flat_fee=Decimal('50000.00'),
            subscription_validity=True,
            subscription_expiry_date=timezone.now() + timedelta(days=365),
        )
        # A prepayment-required table too — the flag that actually flows onto orders.
        Table.objects.create(
            restaurant=restaurant, number=1, prepayment_required=True,
        )
        self._assert_no_commercial_rows(restaurant)

    def test_a_test_tenant_gets_no_automatic_configuration(self):
        """
        ``is_test`` IS NOT A SCHEMA SHORTCUT. A test restaurant is meant to rehearse
        the same readiness path a real one walks, so it gets no auto-created
        configuration, no defaulted `offline`, and no free terms row. Zero-priced
        terms are perfectly legitimate — but only once somebody records them.
        """
        for is_test in (True, False):
            with self.subTest(is_test=is_test):
                restaurant = Restaurant.objects.create(
                    name=f'Flagged {is_test}', location=f'loc-flag-{is_test}',
                    status=RestaurantStatus_Live, owner=self.owner,
                    is_test=is_test,
                )
                self._assert_no_commercial_rows(restaurant)

    def test_creating_a_restaurant_normally_creates_no_commercial_rows(self):
        """
        No signal, no post_save hook, no default profile row — for any lifecycle
        state. Absence is meaningful: it means the commercial domain has not yet
        been asked about this tenant.
        """
        for status in (RestaurantStatus_Onboarding, RestaurantStatus_Live):
            with self.subTest(status=status):
                restaurant = Restaurant.objects.create(
                    name=f'Plain {status}', location=f'loc-plain-{status}',
                    status=status, owner=self.owner,
                )
                self._assert_no_commercial_rows(restaurant)

        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 0)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 0)

    def test_no_signal_receivers_are_registered_for_these_models(self):
        """
        Proves the mechanism, not just the outcome: a receiver added later would
        make the assertions above pass for the wrong reason on some other path.
        """
        from django.db.models.signals import (
            post_delete, post_save, pre_delete, pre_save,
        )

        for model in (RestaurantServiceConfiguration, RestaurantSubscriptionTerms):
            for signal in (pre_save, post_save, pre_delete, post_delete):
                with self.subTest(model=model.__name__, signal=signal):
                    self.assertEqual(signal._live_receivers(model)[0], [])

    def test_the_initial_migration_carries_no_data_operations(self):
        """
        EXPAND-ONLY, and provably so. The migration creates two tables and nothing
        else: no RunPython, no RunSQL, and no touch of any existing table — so
        rolled-back application code simply ignores the new tables.
        """
        from importlib import import_module

        from django.db import migrations as migrations_module

        migration = import_module(
            'commercial_app.migrations.0001_initial'
        ).Migration

        forbidden = (
            migrations_module.RunPython,
            migrations_module.RunSQL,
            migrations_module.AlterField,
            migrations_module.RemoveField,
            migrations_module.RenameField,
            migrations_module.AddField,
            migrations_module.DeleteModel,
        )
        for operation in migration.operations:
            self.assertNotIsInstance(operation, forbidden)

        created = {
            operation.name for operation in migration.operations
            if isinstance(operation, migrations_module.CreateModel)
        }
        self.assertEqual(
            created,
            {'RestaurantServiceConfiguration', 'RestaurantSubscriptionTerms'},
        )
        self.assertEqual(len(migration.operations), 2)
