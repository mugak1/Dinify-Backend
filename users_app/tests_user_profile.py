"""HTTP-level tests for V2UserProfileEndpoint (user-profile/<action>/).

Covers the GET handler's fallback for an unknown intention and confirms the
pending-approvals intention still routes into get_pending_profile_updates.
Mirrors the conventions in tests_user_lookup.py (plain TestCase + JWT bearer).
"""
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


def auth(user):
    return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}


class V2UserProfileGetTests(TestCase):
    def setUp(self):
        self.user = make_user('256750000010')

    def test_unknown_intention_returns_400_not_500(self):
        # Any intention other than the handled one(s) must fall through to a
        # clean 400 'Invalid intention.' — not a 500.
        resp = self.client.get(
            '/api/v1/users/user-profile/something-else/', **auth(self.user))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json().get('message'), 'Invalid intention.')

    def test_pending_approvals_routes_to_controller(self):
        # A plain user lacks approval permission, so get_pending_profile_updates
        # returns its 401 dict before any Mongo call. Reaching that branch (401)
        # rather than the fallback (400 'Invalid intention.') proves the
        # pending-approvals intention still works and does not 500.
        resp = self.client.get(
            '/api/v1/users/user-profile/pending-approvals/', **auth(self.user))
        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertNotEqual(resp.json().get('message'), 'Invalid intention.')
