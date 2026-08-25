"""
``POST admin/v1/restaurants/`` — the Admin restaurant-creation endpoint (Step 2D).

The collection resource now answers two methods with two different authority bars, so
this suite is as much about the CONTROL-PLANE discipline around the write — per-method
elevation, CSRF, a substantive reason, exactly one audit row, the transaction that
binds the mutation to that row, and the no-store response carrying the only copy of a
credential — as about the rows themselves.

The domain rules (locking, canonicalisation, collisions, the owner invariant, the
credential) belong to ``platform_admin_app.onboarding_creation`` and are covered by
``tests_onboarding_creation``. What is pinned HERE is that they are reached through
HTTP unchanged, that the GET half is untouched, and that the adapter adds nothing of
its own to them.
"""
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commercial_app.models import (
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
    RestaurantStatus_Onboarding,
)
from platform_admin_app import sessions
from platform_admin_app.audit_actions import ADMIN_RESTAURANT_CREATED
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import assert_owner_consistency
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import (
    DiningArea,
    MenuItem,
    MenuSection,
    Restaurant,
    RestaurantEmployee,
    RestaurantRolePermission,
    Table,
)
from users_app.models import User

COLLECTION_URL = '/admin/v1/restaurants/'
PASSWORD = 'correct-horse-battery'
REASON = 'Creating the restaurant after the signed onboarding agreement.'

# Distinct phone range from every other admin suite.
_PHONE = iter(f'25670970{n:05d}' for n in range(1, 9999))

_ADMIN_OVERRIDES = dict(
    ROOT_URLCONF='dinify_backend.urls_admin',
    MIDDLEWARE=[
        'platform_admin_app.middleware.RequestIDMiddleware',
        'platform_admin_app.middleware.ClientIPMiddleware',
        *dj_settings.MIDDLEWARE,
    ],
    REST_FRAMEWORK={
        **dj_settings.REST_FRAMEWORK,
        'DEFAULT_AUTHENTICATION_CLASSES': (
            'platform_admin_app.authentication.AdminSessionAuthentication',
        ),
        'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
    },
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='UG', password=PASSWORD, roles=[],
        account_type=account_type,
    )


def _make_admin(email='rc-admin@t.com', username='rc-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def new_owner_body(**overrides):
    body = {
        'mode': 'new',
        'first_name': 'Jane',
        'last_name': 'Doe',
        'phone_number': '0772123456',
        'email': 'jane@example.com',
    }
    body.update(overrides)
    return body


def creation_body(owner=None, **overrides):
    body = {
        'restaurant': {
            'name': 'Kampala Bistro',
            'location': 'Kololo, Kampala',
            'is_test': False,
        },
        'owner': owner if owner is not None else new_owner_body(),
        'reason': REASON,
    }
    body.update(overrides)
    return body


@override_settings(**_ADMIN_OVERRIDES)
class _CreationEndpointTestCase(AuditAssertionsMixin, TestCase):
    """An authenticated, RECENTLY ELEVATED admin session on the collection route."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw
        self.counts_before = self._counts()

    def _counts(self):
        return {
            'users': User.objects.count(),
            'restaurants': Restaurant.objects.count(),
            'memberships': RestaurantEmployee.objects.count(),
            'onboardings': RestaurantOnboarding.objects.count(),
            'invitations': OwnerInvitation.objects.count(),
        }

    def post(self, body=None, **kwargs):
        # Snapshotted per request, so "created nothing" is measured against the state
        # the request actually found — not against an empty database a test fixture
        # may legitimately have populated first.
        self.counts_before = self._counts()
        return self.client.post(
            COLLECTION_URL,
            data=creation_body() if body is None else body,
            content_type='application/json',
            **kwargs,
        )

    def assertNoTenantCreated(self):
        self.assertEqual(self._counts(), self.counts_before)


# --- §30 1-2 the GET half is untouched ---------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class DirectoryReadRegressionTests(AuditAssertionsMixin, TestCase):
    """
    Adding a method to the collection must not raise the bar for reading it.

    The session in this class is deliberately NEVER elevated.
    """

    def setUp(self):
        super().setUp()
        self.admin = _make_admin(email='rc-read@t.com', username='rc-read')
        raw, self.session = sessions.create_session(self.admin)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    def test_get_still_answers_200_without_elevation(self):
        response = self.client.get(COLLECTION_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('results', response.json()['data'])

    def test_get_is_still_unaudited(self):
        self.client.get(COLLECTION_URL)
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_a_malformed_query_parameter_is_still_a_400(self):
        response = self.client.get(COLLECTION_URL, {'stats': 'live'})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('__all__', response.json()['errors'])

    def test_get_is_still_refused_for_an_anonymous_caller(self):
        self.assertEqual(Client().get(COLLECTION_URL).status_code, 401)

    def test_post_on_the_same_route_requires_elevation(self):
        """The two methods resolve different permissions off the same view."""
        response = self.client.post(
            COLLECTION_URL, data=creation_body(),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403, response.content)


# --- §30 3-26 the creation happy path ----------------------------------------

class CreateWithNewOwnerTests(_CreationEndpointTestCase):
    def setUp(self):
        super().setUp()
        self.response = self.post()
        self.assertEqual(self.response.status_code, 201, self.response.content)
        self.payload = self.response.json()
        self.data = self.payload['data']
        self.restaurant = Restaurant.objects.get()
        self.owner = User.objects.get(account_type=ACCOUNT_TYPE_RESTAURANT_USER)

    def test_status_is_201_with_the_house_envelope(self):
        self.assertEqual(self.payload['status'], 201)
        self.assertEqual(self.payload['message'], 'Restaurant created.')

    def test_exactly_one_owner_account_was_created(self):
        self.assertEqual(
            User.objects.filter(
                account_type=ACCOUNT_TYPE_RESTAURANT_USER,
            ).count(),
            1,
        )

    def test_username_is_the_canonical_msisdn(self):
        self.assertEqual(self.owner.username, '256772123456')
        self.assertEqual(self.owner.phone_number, '256772123456')

    def test_owner_is_a_restaurant_user_with_an_unusable_password(self):
        self.assertEqual(self.owner.account_type, ACCOUNT_TYPE_RESTAURANT_USER)
        self.assertFalse(self.owner.has_usable_password())

    def test_owner_holds_no_platform_authority(self):
        self.assertEqual(self.owner.roles, [])

    def test_restaurant_is_owned_by_the_new_account(self):
        self.assertEqual(self.restaurant.owner_id, self.owner.id)

    def test_restaurant_starts_onboarding(self):
        self.assertEqual(self.restaurant.status, RestaurantStatus_Onboarding)

    def test_the_explicit_test_classification_is_persisted(self):
        self.assertFalse(self.restaurant.is_test)

    def test_exactly_one_active_owner_membership_exists(self):
        memberships = RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, active=True, deleted=False,
        )
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.get().user_id, self.owner.id)

    def test_the_membership_uses_the_canonical_owner_role(self):
        self.assertEqual(
            RestaurantEmployee.objects.get(restaurant=self.restaurant).roles,
            [RESTAURANT_OWNER],
        )

    def test_owner_consistency_holds(self):
        self.assertIsNotNone(assert_owner_consistency(self.restaurant))

    def test_no_role_permission_rows_are_seeded(self):
        self.assertFalse(RestaurantRolePermission.objects.exists())

    def test_onboarding_is_admin_created_and_attributed_to_the_actor(self):
        onboarding = RestaurantOnboarding.objects.get(restaurant=self.restaurant)
        self.assertEqual(onboarding.source, ONBOARDING_SOURCE_ADMIN_CREATED)
        self.assertEqual(onboarding.created_by_id, self.admin.pk)

    def test_no_adoption_or_attestation_fields_are_written(self):
        onboarding = RestaurantOnboarding.objects.get(restaurant=self.restaurant)
        for field in ('adopted_at', 'adopted_by_id', 'owner_control_attested_at',
                      'owner_control_attested_user_id',
                      'owner_control_attested_by_id'):
            self.assertIsNone(getattr(onboarding, field), field)

    def test_exactly_one_unresolved_invitation_exists(self):
        invitation = OwnerInvitation.objects.get()
        self.assertFalse(invitation.is_resolved)
        self.assertEqual(invitation.invited_user_id, self.restaurant.owner_id)
        self.assertEqual(invitation.issued_by_id, self.admin.pk)

    def test_invitation_expires_at_the_configured_ttl(self):
        from platform_admin_app import onboarding_creation

        invitation = OwnerInvitation.objects.get()
        self.assertEqual(
            invitation.expires_at - invitation.issued_at,
            onboarding_creation.owner_invitation_ttl(),
        )

    # --- the credential ---

    def test_the_raw_claim_token_is_returned_once(self):
        token = self.data['owner_invitation']['claim_token']
        self.assertTrue(token)
        self.assertRegex(token, r'^[A-Za-z0-9_-]+$')

    def test_the_stored_hash_matches_the_returned_token(self):
        token = self.data['owner_invitation']['claim_token']
        self.assertEqual(
            OwnerInvitation.objects.get().token_hash, sessions.hash_token(token),
        )

    def test_the_raw_token_appears_in_no_persisted_text_field(self):
        """
        The token is a credential: it must exist in the response and nowhere else.

        Sweeps every text-ish column that could plausibly have captured it — the
        invitation's own hash, every audit row's reason and state blobs, and the
        owner's own profile strings.
        """
        import json

        token = self.data['owner_invitation']['claim_token']
        haystacks = [OwnerInvitation.objects.get().token_hash]
        for entry in AdminAuditLog.objects.all():
            haystacks.extend([
                entry.reason, entry.error_code, entry.actor_label,
                json.dumps(entry.before_state), json.dumps(entry.after_state),
            ])
        haystacks.extend([
            self.owner.username, self.owner.email or '', self.owner.password,
            self.restaurant.name, self.restaurant.location,
        ])
        for haystack in haystacks:
            self.assertNotIn(token, haystack or '')

    def test_the_response_is_not_cacheable(self):
        self.assertEqual(self.response['Cache-Control'], 'no-store, private')
        self.assertEqual(self.response['Pragma'], 'no-cache')

    def test_the_credential_never_travels_in_a_cookie_or_a_location_header(self):
        token = self.data['owner_invitation']['claim_token']
        self.assertNotIn('Location', self.response)
        for cookie in self.response.cookies.values():
            self.assertNotIn(token, cookie.value)

    def test_no_claim_url_is_fabricated(self):
        invitation = self.data['owner_invitation']
        self.assertEqual(
            set(invitation), {'id', 'issued_at', 'expires_at', 'claim_token'},
        )

    def test_the_owner_account_block_states_that_it_was_created(self):
        self.assertEqual(
            self.data['owner_account'],
            {'id': str(self.owner.id), 'created': True},
        )

    # --- the canonical read ---

    def test_the_restaurant_block_is_the_canonical_detail_projection(self):
        detail = self.client.get(
            f'/admin/v1/restaurants/{self.restaurant.id}/',
        ).json()['data']
        created = dict(self.data['restaurant'])
        # `recent_activity` and `last_activity_at` legitimately move on: the second
        # GET is a later moment. Everything else must be byte-identical.
        for volatile in ('recent_activity', 'last_activity_at'):
            created.pop(volatile, None)
            detail.pop(volatile, None)
        self.assertEqual(created, detail)

    def test_the_projection_reports_the_expected_onboarding_state(self):
        onboarding = self.data['restaurant']['onboarding']
        self.assertTrue(onboarding['tracked'])
        self.assertEqual(onboarding['source'], ONBOARDING_SOURCE_ADMIN_CREATED)
        self.assertEqual(onboarding['owner_relationship']['status'], 'consistent')
        self.assertEqual(onboarding['owner_control']['status'], 'not_established')
        self.assertEqual(onboarding['invitation']['status'], 'pending')

    def test_the_projection_reports_lifecycle_and_readiness_unchanged(self):
        block = self.data['restaurant']
        self.assertEqual(block['status'], RestaurantStatus_Onboarding)
        self.assertFalse(block['is_test'])
        # Unchanged and still failing closed — creation makes nothing ready.
        self.assertEqual(block['readiness']['state'], 'not_ready')
        self.assertIn('readiness_not_configured', block['readiness']['blockers'])

    def test_the_projection_invents_no_commercial_facts(self):
        commercial = self.data['restaurant']['commercial']
        self.assertFalse(commercial['payment_timing']['configured'])
        self.assertFalse(commercial['payment_collection_mode']['configured'])
        self.assertFalse(commercial['subscription_terms']['configured'])

    def test_the_projection_never_leaks_the_credential(self):
        import json

        blob = json.dumps(self.data['restaurant'])
        self.assertNotIn('token', blob)
        self.assertNotIn(OwnerInvitation.objects.get().token_hash, blob)

    # --- audit ---

    def test_exactly_one_success_audit_row(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_CREATED, result=RESULT_SUCCESS,
        )
        self.assertEqual(AdminAuditLog.objects.count(), 1)
        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.resource_type, 'Restaurant')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.reason, REASON)

    def test_the_success_audit_after_state_is_narrow_and_truthful(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_CREATED, result=RESULT_SUCCESS,
        )
        invitation = OwnerInvitation.objects.get()
        self.assertIsNone(entry.before_state)
        self.assertEqual(entry.after_state, {
            'restaurant_status': RestaurantStatus_Onboarding,
            'is_test': False,
            'admin_onboarding_source': ONBOARDING_SOURCE_ADMIN_CREATED,
            'owner_user_id': str(self.owner.id),
            'owner_account_created': True,
            'owner_invitation_id': str(invitation.id),
            'owner_invitation_expires_at': invitation.expires_at.isoformat(),
        })

    def test_the_success_audit_carries_no_owner_pii(self):
        import json

        entry = self.assertAudited(
            ADMIN_RESTAURANT_CREATED, result=RESULT_SUCCESS,
        )
        blob = json.dumps(entry.after_state) + entry.reason + entry.actor_label
        for secret in ('jane@example.com', '256772123456', 'Jane', 'Doe'):
            self.assertNotIn(secret, blob)

    def test_the_success_audit_carries_no_token_or_hash(self):
        import json

        entry = self.assertAudited(
            ADMIN_RESTAURANT_CREATED, result=RESULT_SUCCESS,
        )
        blob = json.dumps(entry.after_state)
        self.assertNotIn(self.data['owner_invitation']['claim_token'], blob)
        self.assertNotIn(OwnerInvitation.objects.get().token_hash, blob)

    def test_one_request_produces_one_entry_not_one_per_row(self):
        self.assertEqual(AdminAuditLog.objects.count(), 1)


# --- §30 11, 28 the test classification --------------------------------------

class TestClassificationEndpointTests(_CreationEndpointTestCase):
    def test_is_test_true_is_persisted_and_audited(self):
        body = creation_body()
        body['restaurant']['is_test'] = True
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(Restaurant.objects.get().is_test)
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_SUCCESS)
        self.assertTrue(entry.after_state['is_test'])

    def test_a_missing_is_test_is_a_400(self):
        body = creation_body()
        del body['restaurant']['is_test']
        response = self.post(body)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('is_test', response.json()['errors']['restaurant'])
        self.assertNoTenantCreated()

    def test_a_coercible_non_boolean_is_a_400(self):
        for value in (1, 0, 'true', 'false', 'yes', '', None):
            with self.subTest(value=value):
                body = creation_body()
                body['restaurant']['is_test'] = value
                response = self.post(body)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertNoTenantCreated()

    def test_creation_writes_no_separate_classification_audit(self):
        body = creation_body()
        body['restaurant']['is_test'] = True
        self.post(body)
        self.assertEqual(AdminAuditLog.objects.count(), 1)


# --- §30 27-31 the existing-owner path ---------------------------------------

class ExistingOwnerEndpointTests(_CreationEndpointTestCase):
    def setUp(self):
        super().setUp()
        self.existing = _make_user('existing-owner@t.com')
        self.snapshot = {
            field: getattr(self.existing, field)
            for field in ('username', 'email', 'phone_number', 'password', 'roles',
                          'first_name', 'last_name', 'is_active',
                          'prompt_password_change', 'account_type')
        }

    def attach(self, **restaurant_overrides):
        body = creation_body(owner={
            'mode': 'existing', 'user_id': str(self.existing.pk),
        })
        body['restaurant'].update(restaurant_overrides)
        return self.post(body)

    def test_an_active_restaurant_user_is_reused(self):
        response = self.attach()
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()['data']
        self.assertEqual(data['owner_account'],
                         {'id': str(self.existing.pk), 'created': False})
        self.assertEqual(Restaurant.objects.get().owner_id, self.existing.pk)

    def test_no_new_user_is_created(self):
        before = User.objects.count()
        self.attach()
        self.assertEqual(User.objects.count(), before)

    def test_the_existing_account_is_untouched(self):
        self.attach()
        self.existing.refresh_from_db()
        for field, value in self.snapshot.items():
            self.assertEqual(getattr(self.existing, field), value, field)

    def test_the_same_owner_may_hold_a_second_restaurant(self):
        self.assertEqual(self.attach().status_code, 201)
        second = self.attach(name='Second Bistro', location='Ntinda, Kampala')
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(
            Restaurant.objects.filter(owner=self.existing).count(), 2,
        )
        self.assertEqual(
            RestaurantEmployee.objects.filter(
                user=self.existing, active=True,
            ).count(),
            2,
        )
        self.assertEqual(RestaurantOnboarding.objects.count(), 2)
        self.assertEqual(OwnerInvitation.objects.count(), 2)

    def test_the_invitation_targets_the_named_account(self):
        self.attach()
        self.assertEqual(
            OwnerInvitation.objects.get().invited_user_id, self.existing.pk,
        )

    def test_the_success_audit_says_the_account_was_not_created(self):
        self.attach()
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_SUCCESS)
        self.assertFalse(entry.after_state['owner_account_created'])
        self.assertEqual(entry.after_state['owner_user_id'], str(self.existing.pk))


# --- §30 32-40 refusals ------------------------------------------------------

class ConflictTests(_CreationEndpointTestCase):
    """Well-formed requests the platform's current state contradicts. All 409."""

    def assertConflict(self, response, code):
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body['code'], code)
        self.assertNotIn('errors', body)
        return body

    def test_a_phone_collision_is_a_409_with_zero_side_effects(self):
        existing = _make_user('someone@t.com')
        User.objects.filter(pk=existing.pk).update(phone_number='256772123456')
        body = self.assertConflict(self.post(), 'owner_account_already_exists')
        self.assertEqual(body['details'], {'owner_user_id': str(existing.pk)})
        self.assertNoTenantCreated()

    def test_the_phone_conflict_never_reuses_the_existing_identity(self):
        existing = _make_user('someone@t.com')
        User.objects.filter(pk=existing.pk).update(phone_number='256772123456')
        self.post()
        self.assertFalse(RestaurantEmployee.objects.filter(user=existing).exists())

    def test_an_email_collision_is_a_409_naming_no_account(self):
        _make_user('jane@example.com')
        body = self.assertConflict(self.post(), 'owner_email_already_in_use')
        self.assertNotIn('details', body)
        self.assertNoTenantCreated()

    def test_an_inactive_existing_account_is_a_409_and_is_not_reactivated(self):
        existing = _make_user('inactive@t.com')
        User.objects.filter(pk=existing.pk).update(is_active=False)
        response = self.post(creation_body(owner={
            'mode': 'existing', 'user_id': str(existing.pk),
        }))
        self.assertConflict(response, 'owner_account_inactive')
        existing.refresh_from_db()
        self.assertFalse(existing.is_active)
        self.assertFalse(Restaurant.objects.exists())

    def test_a_platform_staff_account_is_a_409_and_gains_no_membership(self):
        response = self.post(creation_body(owner={
            'mode': 'existing', 'user_id': str(self.admin.pk),
        }))
        self.assertConflict(response, 'owner_account_not_restaurant_user')
        self.assertFalse(RestaurantEmployee.objects.filter(user=self.admin).exists())
        self.assertFalse(Restaurant.objects.exists())

    def test_an_unknown_account_is_a_409_not_a_404(self):
        """
        The route's target is the COLLECTION, and the caller explicitly named this
        UUID. A 404 would tell an authenticated administrator the wrong thing was
        missing.
        """
        response = self.post(creation_body(owner={
            'mode': 'existing',
            'user_id': '7f1c0000-0000-0000-0000-00000000dead',
        }))
        self.assertConflict(response, 'owner_account_not_found')

    def test_a_duplicate_restaurant_is_a_409(self):
        self.assertEqual(self.post().status_code, 201)
        response = self.post(creation_body(
            owner=new_owner_body(phone_number='0700000041', email=None),
        ))
        self.assertConflict(response, 'restaurant_already_exists')
        self.assertEqual(Restaurant.objects.count(), 1)

    def test_every_conflict_writes_exactly_one_failure_audit(self):
        _make_user('jane@example.com')
        self.post()
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_FAILURE)
        self.assertEqual(AdminAuditLog.objects.count(), 1)
        self.assertEqual(entry.error_code, 'owner_email_already_in_use')
        self.assertEqual(entry.reason, REASON)
        # Nothing was created, so nothing may be named.
        self.assertEqual(entry.resource_id, '')
        self.assertIsNone(entry.restaurant_id)
        self.assertIsNone(entry.before_state)
        self.assertIsNone(entry.after_state)


class ValidationTests(_CreationEndpointTestCase):
    """Malformed request facts. All 400, all with field-keyed errors."""

    def assertRejected(self, response, *path):
        self.assertEqual(response.status_code, 400, response.content)
        errors = response.json()['errors']
        for key in path:
            self.assertIn(key, errors, response.content)
            errors = errors[key]
        self.assertNoTenantCreated()
        return errors

    def test_an_unknown_owner_mode_is_a_400(self):
        for mode in ('maybe', '', None, 1):
            with self.subTest(mode=mode):
                self.assertRejected(
                    self.post(creation_body(owner=new_owner_body(mode=mode))),
                    'owner', 'mode',
                )

    def test_a_missing_owner_block_is_a_400(self):
        body = creation_body()
        del body['owner']
        self.assertRejected(self.post(body), 'owner')

    def test_new_mode_rejects_an_existing_mode_field(self):
        owner = new_owner_body()
        owner['user_id'] = '7f1c0000-0000-0000-0000-00000000dead'
        self.assertRejected(self.post(creation_body(owner=owner)),
                            'owner', 'user_id')

    def test_existing_mode_rejects_every_new_mode_field(self):
        existing = _make_user('other@t.com')
        for field, value in (('first_name', 'Jane'), ('last_name', 'Doe'),
                             ('phone_number', '0772123456'),
                             ('email', 'jane@example.com')):
            with self.subTest(field=field):
                owner = {'mode': 'existing', 'user_id': str(existing.pk),
                         field: value}
                self.assertRejected(self.post(creation_body(owner=owner)),
                                    'owner', field)

    def test_a_forbidden_field_is_refused_even_when_blank(self):
        """
        A key the caller SENT is a claim they made; dropping it as blank would
        confirm a belief that is wrong.
        """
        existing = _make_user('other2@t.com')
        owner = {'mode': 'existing', 'user_id': str(existing.pk),
                 'phone_number': ''}
        self.assertRejected(self.post(creation_body(owner=owner)),
                            'owner', 'phone_number')

    def test_new_mode_requires_its_own_fields(self):
        for field in ('first_name', 'last_name', 'phone_number'):
            with self.subTest(field=field):
                owner = new_owner_body()
                del owner[field]
                self.assertRejected(self.post(creation_body(owner=owner)),
                                    'owner', field)

    def test_new_mode_email_is_optional(self):
        for owner in (new_owner_body(email=None), new_owner_body(email='')):
            with self.subTest(email=owner['email']):
                # PROTECT ordering: the credential, then the provenance, then
                # the tenant.
                OwnerInvitation.objects.all().delete()
                RestaurantOnboarding.objects.all().delete()
                RestaurantEmployee.objects.all().delete()
                Restaurant.objects.all().delete()
                User.objects.filter(
                    account_type=ACCOUNT_TYPE_RESTAURANT_USER,
                ).delete()
                owner = dict(owner, phone_number='0772123456')
                response = self.post(creation_body(owner=owner))
                self.assertEqual(response.status_code, 201, response.content)

    def test_existing_mode_requires_a_user_id(self):
        self.assertRejected(self.post(creation_body(owner={'mode': 'existing'})),
                            'owner', 'user_id')

    def test_a_numeric_user_id_is_a_400_not_a_manufactured_conflict(self):
        """
        DRF's own ``UUIDField`` would turn ``42`` into a well-formed UUID and the
        request would come back a 409 about an account that never existed.
        """
        response = self.post(creation_body(owner={'mode': 'existing',
                                                  'user_id': 42}))
        self.assertRejected(response, 'owner', 'user_id')

    def test_a_malformed_uuid_is_a_400(self):
        self.assertRejected(
            self.post(creation_body(owner={'mode': 'existing',
                                           'user_id': 'not-a-uuid'})),
            'owner', 'user_id',
        )

    def test_an_invalid_phone_is_a_400_keyed_like_a_serializer_failure(self):
        """
        The DOMAIN refuses this one (``normalise_msisdn``), not the serializer — and
        it must still answer under ``owner.phone_number`` nested exactly as a
        serializer failure on the same field would. One field, one error shape.
        """
        response = self.post(creation_body(
            owner=new_owner_body(phone_number='+15551234567'),
        ))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('phone_number', response.json()['errors']['owner'])
        self.assertEqual(response.json()['code'], 'invalid_owner_phone')
        self.assertNoTenantCreated()

    def test_a_domain_400_and_a_serializer_400_share_one_error_shape(self):
        domain = self.post(creation_body(
            owner=new_owner_body(phone_number='+15551234567'),
        )).json()['errors']
        owner = new_owner_body()
        del owner['phone_number']
        serializer = self.post(creation_body(owner=owner)).json()['errors']
        self.assertEqual(set(domain), {'owner'})
        self.assertEqual(set(serializer), {'owner'})
        self.assertIn('phone_number', domain['owner'])
        self.assertIn('phone_number', serializer['owner'])

    def test_a_blank_name_or_location_is_a_400(self):
        for field in ('name', 'location'):
            for value in ('', '   '):
                with self.subTest(field=field, value=value):
                    body = creation_body()
                    body['restaurant'][field] = value
                    self.assertRejected(self.post(body), 'restaurant', field)

    def test_a_missing_restaurant_block_is_a_400(self):
        body = creation_body()
        del body['restaurant']
        self.assertRejected(self.post(body), 'restaurant')

    def test_a_short_reason_is_a_400(self):
        self.assertRejected(self.post(creation_body(reason='too short')), 'reason')

    def test_a_missing_reason_is_a_400(self):
        body = creation_body()
        del body['reason']
        self.assertRejected(self.post(body), 'reason')

    def test_a_whitespace_only_reason_is_a_400(self):
        self.assertRejected(self.post(creation_body(reason='          ')), 'reason')

    def test_a_validation_failure_writes_exactly_one_audit_row(self):
        self.post(creation_body(reason='too short'))
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_FAILURE)
        self.assertEqual(AdminAuditLog.objects.count(), 1)
        self.assertEqual(entry.error_code, 'reason_too_short')

    def test_a_rejected_reason_is_never_recorded(self):
        self.post(creation_body(reason='   nope   '))
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_FAILURE)
        self.assertEqual(entry.reason, '')

    def test_a_valid_reason_survives_another_fields_failure_normalised(self):
        body = creation_body(reason=f'   {REASON}   ')
        body['restaurant']['name'] = ''
        self.post(body)
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_FAILURE)
        self.assertEqual(entry.reason, REASON)


# --- §30 41-48 authority, parse failures, audit ------------------------------

class AuthorityTests(_CreationEndpointTestCase):
    def test_anonymous_is_401_and_writes_no_audit_row(self):
        self.counts_before = self._counts()
        response = Client().post(
            COLLECTION_URL, data=creation_body(),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(AdminAuditLog.objects.count(), 0)
        self.assertNoTenantCreated()

    def test_a_never_elevated_session_is_403_with_one_denial_audit(self):
        raw, _session = sessions.create_session(
            _make_admin(email='rc-plain@t.com', username='rc-plain'),
        )
        client = Client()
        client.cookies[cookie_name()] = raw
        self.counts_before = self._counts()
        response = client.post(
            COLLECTION_URL, data=creation_body(),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403, response.content)
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_DENIED)
        self.assertEqual(AdminAuditLog.objects.count(), 1)
        self.assertEqual(entry.error_code, 'elevation_required')
        self.assertNoTenantCreated()

    def test_a_stale_elevation_is_403(self):
        self.session.elevated_at = timezone.now() - timedelta(hours=3)
        self.session.save(update_fields=['elevated_at'])
        self.assertEqual(self.post().status_code, 403)
        self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_DENIED)
        self.assertNoTenantCreated()

    def test_a_denial_audit_records_nothing_from_the_body(self):
        """A denial is recorded from the request's AUTHORITY, never from a payload."""
        raw, _session = sessions.create_session(
            _make_admin(email='rc-plain2@t.com', username='rc-plain2'),
        )
        client = Client()
        client.cookies[cookie_name()] = raw
        client.post(COLLECTION_URL, data=creation_body(reason='SECRET REASON TEXT'),
                    content_type='application/json')
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_DENIED)
        self.assertNotIn('SECRET REASON TEXT', entry.reason)
        self.assertEqual(entry.resource_id, '')
        self.assertIsNone(entry.restaurant_id)

    def test_a_denied_get_is_never_audited(self):
        """``GET`` is a safe request the one-entry convention does not cover."""
        Client().get(COLLECTION_URL)
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_a_restaurant_user_session_cannot_reach_the_plane(self):
        """
        The admin authenticator refuses a non-platform account outright, so this is
        a 401/403 about identity and there is nothing to audit.
        """
        raw, _session = sessions.create_session(_make_user('diner@t.com'))
        client = Client()
        client.cookies[cookie_name()] = raw
        self.counts_before = self._counts()
        response = client.post(COLLECTION_URL, data=creation_body(),
                               content_type='application/json')
        self.assertIn(response.status_code, (401, 403), response.content)
        self.assertNoTenantCreated()


@override_settings(**_ADMIN_OVERRIDES)
class CsrfTests(AuditAssertionsMixin, TestCase):
    """
    The existing admin CSRF policy applies and is not reimplemented here.

    ``Client(enforce_csrf_checks=True)`` is load-bearing: the DEFAULT test client
    sets ``_dont_enforce_csrf_checks``, which short-circuits the check before it
    looks at anything, so a suite that used it would prove nothing at all.
    """

    def setUp(self):
        super().setUp()
        self.admin = _make_admin(email='rc-csrf@t.com', username='rc-csrf')
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.raw = raw

    def _client(self):
        client = Client(enforce_csrf_checks=True)
        client.cookies[cookie_name()] = self.raw
        return client

    def _token_for(self, client):
        response = client.get('/admin/v1/auth/session/')
        self.assertEqual(response.status_code, 200, response.content)
        return client.cookies[dj_settings.CSRF_COOKIE_NAME].value

    def test_a_missing_csrf_token_is_refused_and_unaudited(self):
        response = self._client().post(
            COLLECTION_URL, data=creation_body(),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Restaurant.objects.exists())
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_a_wrong_csrf_token_is_refused(self):
        client = self._client()
        self._token_for(client)
        response = client.post(
            COLLECTION_URL, data=creation_body(),
            content_type='application/json',
            HTTP_X_CSRFTOKEN='not-the-token-the-server-issued',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Restaurant.objects.exists())

    def test_the_server_issued_token_is_accepted(self):
        client = self._client()
        token = self._token_for(client)
        response = client.post(
            COLLECTION_URL, data=creation_body(),
            content_type='application/json', HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_get_needs_no_csrf_token(self):
        self.assertEqual(self._client().get(COLLECTION_URL).status_code, 200)


class UnreadableBodyTests(_CreationEndpointTestCase):
    def test_malformed_json_is_a_400_in_the_house_envelope(self):
        response = self.client.post(
            COLLECTION_URL, data='{"restaurant": ', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body['message'], 'The restaurant could not be created.')
        self.assertIn('__all__', body['errors'])
        self.assertNoTenantCreated()

    def test_malformed_json_writes_exactly_one_failure_audit(self):
        self.client.post(
            COLLECTION_URL, data='{"restaurant": ', content_type='application/json',
        )
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_FAILURE)
        self.assertEqual(AdminAuditLog.objects.count(), 1)
        self.assertEqual(entry.error_code, 'malformed_body')
        # No reason can be read from a body that would not parse.
        self.assertEqual(entry.reason, '')

    def test_an_unsupported_media_type_is_a_415(self):
        response = self.client.post(
            COLLECTION_URL, data='name=Kampala', content_type='application/xml',
        )
        self.assertEqual(response.status_code, 415, response.content)
        self.assertIn('__all__', response.json()['errors'])
        self.assertNoTenantCreated()

    def test_an_unsupported_media_type_writes_its_own_audit_code(self):
        self.client.post(
            COLLECTION_URL, data='name=Kampala', content_type='application/xml',
        )
        entry = self.assertAudited(ADMIN_RESTAURANT_CREATED, result=RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'unsupported_media_type')

    def test_drfs_bare_detail_shape_never_escapes(self):
        for data, content_type in (('{', 'application/json'),
                                   ('x', 'application/xml')):
            with self.subTest(content_type=content_type):
                body = self.client.post(
                    COLLECTION_URL, data=data, content_type=content_type,
                ).json()
                self.assertNotIn('detail', body)
                self.assertIn('errors', body)


class AuditAtomicityTests(_CreationEndpointTestCase):
    """
    An administrative action that cannot be attributed must not be allowed to stand.

    The fault is injected at ``AdminAuditLog.objects.create`` — i.e. AFTER the domain
    service has genuinely written every row — so this proves the outer transaction,
    not merely that an exception propagates.
    """

    def test_a_failed_audit_rolls_the_whole_creation_back(self):
        with patch.object(
            AdminAuditLog.objects, 'create', side_effect=RuntimeError('audit down'),
        ):
            with self.assertRaises(RuntimeError):
                self.post()
        self.assertNoTenantCreated()
        self.assertEqual(AdminAuditLog.objects.count(), 0)

    def test_the_guard_really_did_reach_the_domain_write(self):
        """Without this, the test above could pass because nothing ran at all."""
        seen = {}

        original = AdminAuditLog.objects.create

        def explode(*args, **kwargs):
            seen['restaurants'] = Restaurant.objects.count()
            seen['invitations'] = OwnerInvitation.objects.count()
            raise RuntimeError('audit down')

        with patch.object(AdminAuditLog.objects, 'create', side_effect=explode):
            with self.assertRaises(RuntimeError):
                self.post()
        self.assertEqual(seen, {'restaurants': 1, 'invitations': 1})
        self.assertEqual(original, AdminAuditLog.objects.create)


# --- §30 52-57 scope ---------------------------------------------------------

class ScopeTests(_CreationEndpointTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.post().status_code, 201)

    def test_no_commercial_rows_are_created(self):
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())

    def test_no_operational_rows_are_created(self):
        self.assertFalse(DiningArea.objects.exists())
        self.assertFalse(Table.objects.exists())
        self.assertFalse(MenuSection.objects.exists())
        self.assertFalse(MenuItem.objects.exists())

    def test_no_delegation_grant_or_support_issue_is_created(self):
        from platform_admin_app.models import DelegationGrant
        from support_app.models import SupportIssue

        self.assertFalse(DelegationGrant.objects.exists())
        self.assertFalse(SupportIssue.objects.exists())

    def test_creation_is_absent_from_the_delegated_route_allowlist(self):
        """
        A delegated administrator can never create a tenant: the collection route is
        not on ``ALLOWED_ROUTES`` for POST, and delegation is a customer-plane
        credential the admin urlconf does not even carry.
        """
        from platform_admin_app.configs.delegation_scopes import ALLOWED_ROUTES

        for route, method in ALLOWED_ROUTES:
            self.assertNotEqual(
                (route, method), ('admin-restaurant-list', 'POST'),
            )


class CreationIsNotDeliveryTests(_CreationEndpointTestCase):
    """
    ISSUANCE IS NOT DELIVERY. Nothing is sent, and creation must not become coupled
    to the notification stack — which this repository documents as unreliable and
    unscheduled.
    """

    def test_creation_succeeds_with_every_delivery_path_booby_trapped(self):
        def explode(*args, **kwargs):
            raise AssertionError('creation must not deliver anything')

        with patch('notifications_app.controllers.sms.send_sms', side_effect=explode), \
                patch('misc_app.controllers.save_action_log.save_action',
                      side_effect=explode), \
                patch('users_app.controllers.self_register.self_register',
                      side_effect=explode), \
                patch('users_app.controllers.otp_manager.OtpManager',
                      side_effect=explode), \
                patch('misc_app.controllers.notifications.notification.Notification',
                      side_effect=explode):
            response = self.post()
        self.assertEqual(response.status_code, 201, response.content)

    def test_no_otp_row_is_manufactured(self):
        from users_app.models import UserOtp

        self.post()
        self.assertFalse(UserOtp.objects.exists())

    def test_the_response_never_carries_a_password(self):
        import json

        blob = json.dumps(self.post().json())
        self.assertNotIn('password', blob)
        self.assertNotIn('temp_password', blob)
