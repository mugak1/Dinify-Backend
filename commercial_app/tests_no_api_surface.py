"""
The commercial models are not reachable from any API (Phase 1, Step 3B).

Step 3B is storage only: no serializer, no endpoint, no Secretary vocabulary, no
delegated route, no management command. That is easy to state and easy to lose —
a serializer added "just to read it" in some later PR is how platform-owned
commercial state becomes tenant-writable, which is precisely what happened to
``preferred_subscription_method`` before PR #296.

These tests prove the ABSENCE mechanically, so the next PR that adds a read or a
write has to do it deliberately and update the assertions here.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.edit_information import EDIT_INFORMATION
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from restaurants_app.models import Restaurant, RestaurantEmployee

User = get_user_model()

COMMERCIAL_MODELS = (RestaurantServiceConfiguration, RestaurantSubscriptionTerms)


class CommercialModelsHaveNoSerializerTests(TestCase):
    """
    The tenancy machinery (TENANT-STRUCT-00) reasons about DRF ``ModelSerializer``
    relational fields. These models have NO serializer at all, so they contribute
    nothing to discovery, nothing to ``baseline.txt`` and nothing to the ratchet —
    the cleanest possible tenancy outcome, because there is no writable relation to
    classify in the first place.
    """

    def test_no_project_serializer_targets_a_commercial_model(self):
        from dinify_backend.tenancy.discovery import all_project_serializers

        offenders = [
            serializer.__name__
            for serializer in all_project_serializers()
            if getattr(serializer.Meta, 'model', None) in COMMERCIAL_MODELS
        ]
        self.assertEqual(
            offenders, [],
            'A serializer now exposes a commercial model. Classify its tenant '
            'relations (Meta.tenant_relations) and revisit this test deliberately.',
        )

    def test_the_tenant_relation_baseline_names_no_commercial_relation(self):
        """
        The ratchet only forbids ADDITIONS, so a new baseline entry would be caught
        there too — but this states the expectation locally: these models must never
        need a baseline entry, because they must never have a writable relation.
        """
        from dinify_backend.tenancy.ratchet import load_baseline

        offenders = [key for key in load_baseline() if 'commercial_app' in key]
        self.assertEqual(offenders, [])

    def test_neither_model_is_exposed_through_secretary(self):
        """
        ``EDIT_INFORMATION`` is the only vocabulary Secretary builds a PUT payload
        from. No section names a commercial field, so the generic tenant edit path
        cannot reach one even if a serializer appeared.
        """
        commercial_keys = set()
        for model in COMMERCIAL_MODELS:
            commercial_keys |= {
                field.name for field in model._meta.get_fields()
                if not field.is_relation or field.many_to_one or field.one_to_one
            }
        # The two names that would actually be dangerous if they leaked in.
        for key in ('payment_timing', 'payment_collection_mode', 'recurring_amount',
                    'billing_interval_unit', 'billing_interval_count'):
            self.assertIn(key, commercial_keys)  # guards against a rename here
            for section, entries in EDIT_INFORMATION.items():
                with self.subTest(section=section, key=key):
                    self.assertNotIn(key, {entry['key'] for entry in entries})


class CommercialModelsAreNotReachableOverHttpTests(TestCase):
    """
    The restaurant-setup catch-all resolves a ``config_detail`` word to a record
    type. These models have no word, so an owner naming one falls through to the
    generic unmapped-resource refusal — never to a 200.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            first_name='Own', last_name='Er', email='surface-owner@test.com',
            phone_number='256772000401', username='256772000401',
            country='Uganda', password='password', roles=[],
        )
        cls.restaurant = Restaurant.objects.create(
            name='Surface Ltd', location='loc-surface',
            status=RestaurantStatus_Live, owner=cls.owner,
        )
        RestaurantEmployee.objects.create(
            user=cls.owner, restaurant=cls.restaurant, roles=[RESTAURANT_OWNER],
        )

    def _auth(self):
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def test_an_owner_cannot_read_a_commercial_record_through_restaurant_setup(self):
        for word in ('serviceconfiguration', 'service-configuration',
                     'subscriptionterms', 'subscription-terms', 'commercial'):
            with self.subTest(config_detail=word):
                response = self.client.get(
                    f'/api/v1/restaurant-setup/{word}/?restaurant={self.restaurant.id}',
                    **self._auth(),
                )
                self.assertNotEqual(response.status_code, 200, response.content)

    def test_an_owner_cannot_write_a_commercial_record_through_restaurant_setup(self):
        for word in ('serviceconfiguration', 'subscriptionterms', 'commercial'):
            with self.subTest(config_detail=word):
                response = self.client.put(
                    f'/api/v1/restaurant-setup/{word}/',
                    data={'restaurant': str(self.restaurant.id),
                          'payment_timing': 'pay_first',
                          'payment_collection_mode': 'psp_online'},
                    content_type='application/json',
                    **self._auth(),
                )
                self.assertNotEqual(response.status_code, 200, response.content)

        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 0)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 0)

    def test_a_restaurants_put_cannot_smuggle_commercial_fields(self):
        """
        The nearest real attack: ride the commercial fields along on the LIVE
        restaurant edit. Secretary only forwards EDIT_INFORMATION keys and the
        serializer only names its own model's fields, so the legitimate edit applies
        and the commercial keys land nowhere.
        """
        response = self.client.put(
            '/api/v1/restaurant-setup/restaurants/',
            data={'id': str(self.restaurant.id), 'name': 'Surface Renamed',
                  'payment_timing': 'pay_first',
                  'payment_collection_mode': 'psp_online',
                  'recurring_amount': '0.00'},
            content_type='application/json',
            **self._auth(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.name, 'Surface Renamed')
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 0)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 0)


class CommercialModelsAreOutsideDelegatedReachTests(TestCase):
    """
    A delegated administrator reaches a tenant through an explicit (route, method)
    allowlist. No commercial route exists, and the restaurant-setup entry it does
    carry is GET-only over a named record vocabulary that has no commercial word.
    """

    def test_no_delegated_route_mentions_the_commercial_domain(self):
        from platform_admin_app.configs.delegation_scopes import (
            ALLOWED_ROUTES, SETUP_READABLE_RECORDS,
        )

        for route, _method in ALLOWED_ROUTES:
            self.assertNotIn('commercial', route)
            self.assertNotIn('subscription-terms', route)
            self.assertNotIn('service-configuration', route)

        for record in SETUP_READABLE_RECORDS:
            self.assertNotIn('commercial', record)
            self.assertNotIn('subscription', record)
            self.assertNotIn('serviceconfiguration', record)
