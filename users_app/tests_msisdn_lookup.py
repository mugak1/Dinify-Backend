"""
`GET users/msisdn-lookup/` answers "no account" only when it looked and found none
(MSISDN-LOOKUP-EXCEPT-00).

`MsisdnLookupEndpoint` is the anonymous existence check the public sign-up page makes
before it sends an OTP: 200 means "this number has an account" (sign-up goes straight
to `register`, which refuses it), 404 means "no account" (the page sends the OTP and
starts sign-up). It wrapped the whole lookup in `except Exception` and answered every
exception with that same 404 envelope, so two things that are not "no account" said
"no account":

  * A DATABASE FAILURE. An outage, a timeout or a broken query was reported to the
    client as `{status: 404, message: 'User not found'}`, so the page sent an OTP for a
    number that may well have an account, and the server logged it as an ordinary
    lookup error.
  * A MISSING `msisdn`. `request.GET.get('msisdn')` is `None`, and Django turns
    `.get(phone_number=None)` into `phone_number IS NULL`. `phone_number` is nullable
    for platform-staff accounts (see the model), so the answer depended on how many
    of those exist: none gave 404 "User not found", exactly one gave 200 "User found",
    and two or more raised `MultipleObjectsReturned`, which the broad except turned
    into 404 again. None of the three is an answer about a phone number.

Now a missing or blank `msisdn` is a 400, the lookup is `filter(...).exists()` (which
cannot raise `DoesNotExist` or `MultipleObjectsReturned`), and nothing is caught: a
database failure propagates and Django answers 500.

THE NUMBER IS COMPARED IN CANONICAL FORM. It was compared as sent, while the stored
value is the canonical `256XXXXXXXXX` and `self_register` canonicalises before its
duplicate check. So `0701700001` or `+256 701 700 001` for a number that has an
account was told "no account", and a value that is not a phone number at all was
told "no account" too. The lookup now runs the same `normalise_msisdn()` as
registration, so it answers "found" exactly when registration would refuse the
number as taken, and a value that cannot be canonicalised is the 400 registration
gives for it.

THE TWO ANSWERS THE FRONTEND READS ARE PINNED BYTE FOR BYTE. The sign-up page treats
an HTTP 404 as "no account" only when the body is this endpoint's own envelope with
`status: 404` (`_helpers/account-lookup.ts` in Dinify-Frontend #742), so the 404 body
must not drift, and a 500 must never carry it.

The database failure is injected at the cursor, for any statement touching the user
table, so the test does not depend on which ORM call the view makes. The client is
built with `raise_request_exception=False` so a 500 arrives as a response to assert
on, as it would for a real caller.
"""
from unittest import mock

from django.db import OperationalError
from django.db.backends.utils import CursorWrapper
from django.test import TestCase
from rest_framework.test import APIClient

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_PLATFORM_STAFF
from users_app.controllers.self_register import self_register
from users_app.models import User

URL = '/api/v1/users/msisdn-lookup/'
KNOWN = '256701700001'
UNKNOWN = '256701700099'

FOUND = {'status': 200, 'message': 'User found', 'data': {'found': True}}
NOT_FOUND = {'status': 404, 'message': 'User not found', 'data': {'found': False}}
MISSING = {'status': 400, 'message': 'A msisdn query parameter is required.'}
INVALID = {'status': 400, 'message': 'Please provide a valid Ugandan phone number.'}

# Other ways a person can type KNOWN. Each canonicalises to it.
KNOWN_SPELLINGS = (
    '+256701700001',
    '+256 701 700 001',
    '256-701-700-001',
    '0701700001',
    '0701 700 001',
    '701700001',
)
# Values `normalise_msisdn` refuses: too short, no digits, a Kenyan number, and a
# 256 number one digit too long.
NOT_PHONE_NUMBERS = ('12345', 'not-a-number', '+254712345678', '2567017000011')

CANARY = 'msisdn-lookup-canary-db-failure'


def _customer(phone):
    return User.objects.create_user(
        first_name='Lookup', last_name='Customer', email=f'{phone}@msisdn-lookup.test',
        phone_number=phone, username=phone, country='UG', password='password',
    )


def _platform_staff(tag):
    # A real account shape with no phone number: the admin plane signs in by
    # password + TOTP and never needs one.
    return User.objects.create_user(
        first_name='Platform', last_name='Staff', email=f'{tag}@msisdn-lookup.test',
        phone_number=None, username=f'msisdn-lookup-{tag}', country='UG',
        password='password', roles=[], account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


class MsisdnLookupEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient(raise_request_exception=False)
        _customer(KNOWN)

    def lookup(self, query):
        return self.client.get(URL + query)

    def lookup_value(self, msisdn):
        # Lets the client encode '+' and spaces, as a browser would.
        return self.client.get(URL, {'msisdn': msisdn})

    # --------------------------------------------------------- the two answers

    def test_known_number_is_found(self):
        # CONTROL: anonymous, as the sign-up page calls it.
        response = self.lookup(f'?msisdn={KNOWN}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), FOUND)

    def test_unknown_number_is_not_found_with_the_envelope_the_frontend_reads(self):
        # CONTROL: the one answer that means "no account".
        response = self.lookup(f'?msisdn={UNKNOWN}')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), NOT_FOUND)

    def test_phone_less_accounts_do_not_change_a_real_lookup(self):
        # CONTROL: the guard below is about the request, not about who exists.
        _platform_staff('a')
        _platform_staff('b')
        self.assertEqual(self.lookup(f'?msisdn={KNOWN}').json(), FOUND)
        self.assertEqual(self.lookup(f'?msisdn={UNKNOWN}').json(), NOT_FOUND)

    # ------------------------------------------------- a failure is not "no account"

    def test_a_database_failure_is_a_server_error_not_user_not_found(self):
        # REGRESSION: `except Exception` answered this with the 404 envelope.
        real_execute = CursorWrapper.execute

        def failing_execute(cursor, sql, params=None):
            if 'users' in sql:
                raise OperationalError(CANARY)
            return real_execute(cursor, sql, params)

        with mock.patch.object(CursorWrapper, 'execute', autospec=True,
                               side_effect=failing_execute), \
                self.assertLogs('django.request', level='ERROR'):
            response = self.lookup(f'?msisdn={KNOWN}')

        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertNotIn('User not found', body)
        self.assertNotIn('"found"', body)
        self.assertNotIn(CANARY, body)

    # ------------------------------------------------- a missing msisdn is a 400

    def test_missing_msisdn_is_refused_when_no_account_lacks_a_phone(self):
        # REGRESSION: answered 404 "User not found" about no number at all.
        response = self.lookup('')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), MISSING)

    def test_missing_msisdn_is_refused_when_one_account_lacks_a_phone(self):
        # REGRESSION: `phone_number IS NULL` matched the staff account, so a request
        # naming no number was told "User found".
        _platform_staff('only')
        response = self.lookup('')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), MISSING)

    def test_missing_msisdn_is_refused_when_several_accounts_lack_a_phone(self):
        # REGRESSION: MultipleObjectsReturned, which the broad except made a 404.
        _platform_staff('one')
        _platform_staff('two')
        response = self.lookup('')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), MISSING)

    def test_blank_msisdn_is_refused(self):
        # REGRESSION: `?msisdn=` and a whitespace-only value were looked up as
        # numbers and answered 404 "User not found".
        for query in ('?msisdn=', '?msisdn=%20%20'):
            with self.subTest(query=query):
                response = self.lookup(query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), MISSING)

    # ------------------------------------------- the number is compared canonically

    def test_every_spelling_of_a_known_number_is_found(self):
        # REGRESSION: compared as sent, so each of these was 404 "User not found"
        # for a number that has an account.
        for spelling in KNOWN_SPELLINGS:
            with self.subTest(msisdn=spelling):
                response = self.lookup_value(spelling)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), FOUND)

    def test_found_is_exactly_when_registration_refuses_the_number(self):
        # REGRESSION: the sign-up page's next step depends on the lookup and
        # `self_register`'s duplicate check giving the same answer. Before, they
        # disagreed for every spelling but the canonical one.
        for spelling in (KNOWN,) + KNOWN_SPELLINGS:
            with self.subTest(msisdn=spelling):
                self.assertEqual(self.lookup_value(spelling).json(), FOUND)
                refusal = self_register({
                    'first_name': 'New', 'last_name': 'Person', 'country': 'UG',
                    'phone_number': spelling, 'password': 'password',
                })
                self.assertEqual(refusal, {
                    'status': 400, 'message': MESSAGES['PHONE_NUMBER_EXISTS'],
                })

    def test_an_unknown_number_is_not_found_however_it_is_spelled(self):
        # CONTROL: canonicalising must not turn "no account" into anything else.
        for spelling in ('0701700099', '+256 701 700 099', '701700099'):
            with self.subTest(msisdn=spelling):
                response = self.lookup_value(spelling)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), NOT_FOUND)

    def test_a_value_that_is_not_a_ugandan_number_is_refused(self):
        # REGRESSION: each of these was 404 "User not found", so the sign-up page
        # sent an OTP to it. Registration refuses it with this same message.
        for value in NOT_PHONE_NUMBERS:
            with self.subTest(msisdn=value):
                response = self.lookup_value(value)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), INVALID)
