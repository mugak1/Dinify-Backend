"""HTTP-level tests for the user-profile endpoints.

``PUT users/user-profile/`` (UserProfileEndpoint -> self_update_user_profile) is
self-service: ANY signed-in user edits their own name/other-names/country/email
directly, regardless of role. Two identity guards apply — the phone number is not
self-editable (only a no-op echo of the stored value is accepted; a real change
is rejected), and an email already held by another user is rejected (email is not
unique on the model but is a password-reset lookup key).

The abandoned profile-update approval queue was deleted, and the manager-OTP path
``PUT users/user-profile/update-profile/`` (V2UserProfileEndpoint) has now been
RETIRED: the whole ``user-profile/<action>/`` route is gone, so any request to it
(update-profile, pending-approvals, ...) 404s.

Mirrors the conventions in tests_user_lookup.py (plain TestCase + JWT bearer).
"""
import json

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from platform_admin_app.testing import give_legacy_platform_role
from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
    RESTAURANT_STAFF,
)

SELF_URL = '/api/v1/users/user-profile/'
MANAGER_URL = '/api/v1/users/user-profile/update-profile/'


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


def auth(user):
    return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}


def put_json(client, url, user, payload):
    return client.put(
        url, data=json.dumps(payload),
        content_type='application/json', **auth(user),
    )


class SelfServiceProfileUpdateTests(TestCase):
    """PUT users/user-profile/ applies self-service edits for EVERY role.

    This is the bug fix: previously an owner/manager/staff/Dinify-admin got a
    200 'Kindly refer to your manager…' and nothing was saved.
    """

    def setUp(self):
        # One active restaurant to hang employee rows on; its owner is a
        # separate throwaway user.
        self.rest_owner = make_user('256750000001')
        self.restaurant = Restaurant.objects.create(
            name='R', location='loc',
            status=RestaurantStatus_Live, owner=self.rest_owner,
        )

    def _employee(self, phone, roles):
        user = make_user(phone)
        RestaurantEmployee.objects.create(
            user=user, restaurant=self.restaurant, roles=roles,
        )
        return user

    def test_restaurant_owner_self_update_is_applied(self):
        user = self._employee('256750000010', [RESTAURANT_OWNER])
        resp = put_json(self.client, SELF_URL, user, {
            'first_name': 'Owner', 'last_name': 'Edited',
            'email': 'owner.new@test.com',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Owner')
        self.assertEqual(user.last_name, 'Edited')
        self.assertEqual(user.email, 'owner.new@test.com')

    def test_restaurant_manager_self_update_is_applied(self):
        user = self._employee('256750000011', [RESTAURANT_MANAGER])
        resp = put_json(self.client, SELF_URL, user, {'first_name': 'Manager'})
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Manager')

    def test_restaurant_staff_self_update_is_applied(self):
        user = self._employee('256750000012', [RESTAURANT_STAFF])
        resp = put_json(self.client, SELF_URL, user, {'last_name': 'Staffer'})
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.last_name, 'Staffer')

    def test_legacy_platform_role_holder_self_update_is_applied(self):
        # Self-update applies for EVERY role, and carrying the retired platform
        # role string neither unlocks nor blocks it — the account is an ordinary
        # restaurant_user whichever strings its roles list happens to hold.
        user = give_legacy_platform_role(make_user('256750000013'))
        resp = put_json(self.client, SELF_URL, user, {
            'first_name': 'Legacy', 'email': 'legacy.new@test.com',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Legacy')
        self.assertEqual(user.email, 'legacy.new@test.com')

    def test_self_update_cannot_write_roles(self):
        # `roles` is read_only on SerGetUserProfile and absent from the write
        # path's field list, so a client-supplied value is ignored, not applied.
        user = self._employee('256750000015', [RESTAURANT_STAFF])
        resp = put_json(self.client, SELF_URL, user, {
            'first_name': 'Escalate', 'roles': ['dinify' '_admin'],
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Escalate')
        # Restaurant roles live on RestaurantEmployee; User.roles stays empty.
        self.assertEqual(user.roles, [])

    def test_roleless_diner_self_update_is_applied(self):
        # No regression: a role-less diner could always self-update.
        user = make_user('256750000014')
        resp = put_json(self.client, SELF_URL, user, {'first_name': 'Diner'})
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Diner')

    def test_phone_change_is_rejected_and_phone_unchanged(self):
        user = make_user('256750000020')
        resp = put_json(self.client, SELF_URL, user, {
            'first_name': 'ShouldNotApply',
            'phone_number': '256750009999',  # differs from stored
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json().get('message'), 'Phone number cannot be changed here.')
        user.refresh_from_db()
        # Phone + username untouched, and the guard short-circuits before apply.
        self.assertEqual(user.phone_number, '256750000020')
        self.assertEqual(user.username, '256750000020')
        self.assertEqual(user.first_name, 'Test')

    def test_phone_echo_of_stored_value_is_a_noop(self):
        # A form echoing the current phone in display format ('0750000021')
        # canonicalises back to the stored value, so it is a no-op and the other
        # fields still apply.
        user = make_user('256750000021')
        resp = put_json(self.client, SELF_URL, user, {
            'first_name': 'Echoed', 'phone_number': '0750000021',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Echoed')
        self.assertEqual(user.phone_number, '256750000021')
        self.assertEqual(user.username, '256750000021')

    def test_email_held_by_another_user_is_rejected(self):
        make_user('256750000030')  # already holds 256750000030@test.com
        user = make_user('256750000031')
        resp = put_json(self.client, SELF_URL, user, {
            'email': '256750000030@test.com',
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            resp.json().get('message'), 'This email is already in use.')
        user.refresh_from_db()
        self.assertEqual(user.email, '256750000031@test.com')

    def test_own_current_email_is_allowed(self):
        # Re-submitting your own email must not trip the uniqueness guard.
        user = make_user('256750000032')
        resp = put_json(self.client, SELF_URL, user, {
            'first_name': 'Same', 'email': '256750000032@test.com',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Same')
        self.assertEqual(user.email, '256750000032@test.com')


class V2UserProfileEndpointRetiredTests(TestCase):
    """The manager-OTP ``user-profile/<action>/`` route (V2UserProfileEndpoint)
    is RETIRED: the endpoint, its route, the update_user_profile controller and
    the SerPutUserProfile write serializer were all deleted. Any sub-action now
    404s (the route no longer resolves). The live self-service ``user-profile/``
    path is unaffected."""

    def setUp(self):
        self.user = make_user('256750000040')

    def test_pending_approvals_get_is_gone_returns_404(self):
        # The approval-queue GET was long gone; with the whole <action> route now
        # retired it 404s (previously 405 against the still-mounted V2 endpoint).
        resp = self.client.get(
            '/api/v1/users/user-profile/pending-approvals/', **auth(self.user))
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_manager_update_profile_is_retired(self):
        # The manager-OTP path is retired: PUT user-profile/update-profile/ no
        # longer resolves to any view -> 404, and performs no write.
        actor = make_user('256750000041')
        resp = put_json(self.client, MANAGER_URL, actor, {'first_name': 'ViaManager'})
        self.assertEqual(resp.status_code, 404, resp.content)
        actor.refresh_from_db()
        self.assertNotEqual(actor.first_name, 'ViaManager')
