"""
Gate + response-minimization tests for UserLookupEndpoint (BUG-P1-3).

GET /api/v1/users/user-lookup/?contact=<email|phone> previously had no
permission_classes — any authenticated user (any staff, any diner holding a
token) could resolve anyone's full identity, and the response returned the
COMPLEMENTARY contact (search by phone -> get their email, and vice-versa).

This suite pins the fix:
  * the lookup is gated to restaurant team-access holders only (team is
    owner-only, so managers/kitchen/staff/diners are denied, and so is an
    account carrying the retired dinify_admin role string);
  * the response is minimized to existence + id + name only — never
    phone_number / email;
  * missing contact -> 400, unknown contact -> 404.

A phone `contact` is compared in canonical form (MSISDN-LOOKUP-EXCEPT-00). It was
compared as sent, while the stored value is the canonical `256XXXXXXXXX` and
`self_register` (which `create-employee` delegates to) canonicalises before its
duplicate check. So `0750...` for a number with an account was 404, the staff form
offered to create a new employee, and creating one was refused because the number
was taken. The lookup now runs the same `normalise_msisdn()`: found means
registration would refuse the number, and a value that cannot be canonicalised is
the 400 registration gives it. An email `contact` is unchanged.
"""
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.messages import MESSAGES
from platform_admin_app.testing import give_legacy_platform_role
from users_app.controllers.self_register import self_register
from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RESTAURANT_OWNER, RESTAURANT_MANAGER, RESTAURANT_STAFF,
    DINER,
)

URL = '/api/v1/users/user-lookup/'

TARGET_PHONE = '256750000099'
# Other ways a person can type TARGET_PHONE. Each canonicalises to it.
TARGET_SPELLINGS = (
    '+256750000099',
    '+256 750 000 099',
    '256-750-000-099',
    '0750000099',
    '0750 000 099',
    '750000099',
)
# Values `normalise_msisdn` refuses: too short, no digits, a Kenyan number, and a
# 256 number one digit too long.
NOT_PHONE_NUMBERS = ('12345', 'not-a-number', '+254712345678', '2567500000991')
NOT_FOUND = {'status': 404, 'message': 'User not found'}
INVALID = {'status': 400, 'message': 'Please provide a valid Ugandan phone number.'}


def make_user(phone, roles=None):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )


def auth(user):
    return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}


class UserLookupEndpointTests(TestCase):
    def setUp(self):
        # The person being looked up — whose PII must not leak.
        self.target = make_user(TARGET_PHONE)

        # Owner + their active restaurant. The owner needs the employee row too:
        # the resolver reads RestaurantEmployee, not the Restaurant.owner FK.
        self.owner = make_user('256750000001')
        self.restaurant = Restaurant.objects.create(
            name='R', location='loc', status=RestaurantStatus_Live,
            owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])

        # Employed, but without team access (team is owner-only).
        self.manager = make_user('256750000002')
        RestaurantEmployee.objects.create(
            user=self.manager, restaurant=self.restaurant, roles=[RESTAURANT_MANAGER])
        self.staff = make_user('256750000003')
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])

        # An account carrying the RETIRED platform role, plus a plain user and a
        # diner (no employment). The role used to open this lookup on its own.
        self.legacy_role_holder = give_legacy_platform_role(
            make_user('256750000004'))
        self.plain = make_user('256750000005')
        self.diner = make_user('256750000006', roles=[DINER])

    # ------------------------------------------------------------------ gate
    def test_owner_allowed(self):
        resp = self.client.get(URL, {'contact': self.target.email}, **auth(self.owner))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_legacy_platform_role_denied(self):
        # Was `test_dinify_admin_allowed`. The gate is now team-module only, so the
        # role string resolves a person's identity from a raw contact no more than
        # a plain user does.
        resp = self.client.get(
            URL, {'contact': self.target.email}, **auth(self.legacy_role_holder))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_legacy_platform_role_is_indistinguishable_from_a_plain_user(self):
        holder = self.client.get(
            URL, {'contact': self.target.email}, **auth(self.legacy_role_holder))
        plain = self.client.get(
            URL, {'contact': self.target.email}, **auth(self.plain))
        self.assertEqual(holder.status_code, plain.status_code)
        self.assertEqual(holder.content, plain.content)

    def test_plain_user_denied(self):
        resp = self.client.get(URL, {'contact': self.target.email}, **auth(self.plain))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_diner_denied(self):
        resp = self.client.get(URL, {'contact': self.target.email}, **auth(self.diner))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_manager_denied(self):
        # team is owner-only, so an employed manager still cannot use it.
        resp = self.client.get(URL, {'contact': self.target.email}, **auth(self.manager))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_staff_denied(self):
        resp = self.client.get(URL, {'contact': self.target.email}, **auth(self.staff))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_unauthenticated_denied(self):
        # Inherited IsAuthenticated blocks the anonymous caller before the view.
        resp = self.client.get(URL, {'contact': self.target.email})
        self.assertEqual(resp.status_code, 401, resp.content)

    # -------------------------------------------------------------- minimize
    def test_response_minimized_when_searching_by_email(self):
        resp = self.client.get(URL, {'contact': self.target.email}, **auth(self.owner))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertEqual(data['id'], str(self.target.id))
        self.assertEqual(data['first_name'], self.target.first_name)
        self.assertEqual(data['last_name'], self.target.last_name)
        self.assertNotIn('phone_number', data)
        self.assertNotIn('email', data)

    def test_response_minimized_when_searching_by_phone(self):
        # Searching by phone must not disclose the email (the complementary leak).
        resp = self.client.get(URL, {'contact': self.target.phone_number}, **auth(self.owner))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertEqual(data['id'], str(self.target.id))
        self.assertNotIn('phone_number', data)
        self.assertNotIn('email', data)

    # ------------------------------------------------------------ robustness
    def test_missing_contact_returns_400(self):
        resp = self.client.get(URL, **auth(self.owner))
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_unknown_contact_returns_404(self):
        resp = self.client.get(URL, {'contact': 'nobody@nowhere.test'}, **auth(self.owner))
        self.assertEqual(resp.status_code, 404, resp.content)

    # ------------------------------------------ a phone is compared canonically
    def test_every_spelling_of_a_known_number_is_found(self):
        # REGRESSION: compared as sent, so each of these was 404 and the staff form
        # offered to create an account the number already has.
        for spelling in TARGET_SPELLINGS:
            with self.subTest(contact=spelling):
                resp = self.client.get(URL, {'contact': spelling}, **auth(self.owner))
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(resp.json()['data']['id'], str(self.target.id))

    def test_found_is_exactly_when_registration_refuses_the_number(self):
        # REGRESSION: create-employee delegates to `self_register`, so the lookup
        # and its duplicate check must give the same answer. Before, they
        # disagreed for every spelling but the canonical one.
        for spelling in (TARGET_PHONE,) + TARGET_SPELLINGS:
            with self.subTest(contact=spelling):
                resp = self.client.get(URL, {'contact': spelling}, **auth(self.owner))
                self.assertEqual(resp.status_code, 200, resp.content)
                refusal = self_register({
                    'first_name': 'New', 'last_name': 'Person', 'country': 'UG',
                    'phone_number': spelling, 'password': 'password',
                })
                self.assertEqual(refusal, {
                    'status': 400, 'message': MESSAGES['PHONE_NUMBER_EXISTS'],
                })

    def test_an_unknown_number_is_not_found_however_it_is_spelled(self):
        # CONTROL: canonicalising must not turn "no account" into anything else.
        # The body is the one the staff form reads as "no account".
        for spelling in ('0750000098', '+256 750 000 098', '750000098'):
            with self.subTest(contact=spelling):
                resp = self.client.get(URL, {'contact': spelling}, **auth(self.owner))
                self.assertEqual(resp.status_code, 404, resp.content)
                self.assertEqual(resp.json(), NOT_FOUND)

    def test_a_value_that_is_not_a_ugandan_number_is_refused(self):
        # REGRESSION: each of these was 404, so the staff form offered to create
        # an employee that registration then refused with this same message.
        for value in NOT_PHONE_NUMBERS:
            with self.subTest(contact=value):
                resp = self.client.get(URL, {'contact': value}, **auth(self.owner))
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), INVALID)

    def test_the_gate_runs_before_the_number_is_read(self):
        # CONTROL: a caller without team access learns nothing from the contact,
        # not even whether it is a valid number.
        for value in ('12345', '0750000099'):
            with self.subTest(contact=value):
                resp = self.client.get(URL, {'contact': value}, **auth(self.manager))
                self.assertEqual(resp.status_code, 403, resp.content)

    def test_an_email_contact_is_compared_as_an_email(self):
        # CONTROL: only a phone contact is canonicalised. This email's digits
        # spell the OWNER's phone number, so canonicalising it would find the
        # wrong person.
        holder = User.objects.create_user(
            first_name='Email', last_name='Holder',
            email='alias0750000001@test.com', phone_number='256750000007',
            username='256750000007', country='Uganda', password='password',
        )
        resp = self.client.get(
            URL, {'contact': 'alias0750000001@test.com'}, **auth(self.owner))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['id'], str(holder.id))
