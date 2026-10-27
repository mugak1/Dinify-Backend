"""
D11 enforcement — durable abuse limits, pinned at the routes.

Every refusal here is decoded from a real response. The limits, at the owner's defaults
(``docs/engineering/d11-enforcement.md``):

* L1 issuance per subject: 3 / 15 min, 10 / 24 h, PER CLASS
* L2 issuance per destination phone: 3 / 15 min, 10 / 24 h, PER CLASS
* L3 issuance per client IP: 20 / hour, UNVERIFIED CLASS ONLY
* L4 global issuance: 300 / 24 h refuses the UNVERIFIED CLASS ONLY; its alerts, at 80%
  and at the cap, count every issuance
* L5 ``msisdn-lookup`` per client IP: 30 / hour
* L6 verify attempts per challenge: 5, unchanged

THE TWO CLASSES. An issuance is VERIFIED when its origin follows a check of something
only the account holder has (a password login, a login resend bound to that login's
proof, an owner-claim challenge, which needs the claim token) and UNVERIFIED otherwise
(reset initiation, every other resend, and anything unattributed). The class comes from
the server-owned origin alone. #374 counted both classes in one budget, so anyone who
knew an owner's phone number could spend that owner's login budget with anonymous reset
requests, and a few addresses could spend the global cap and refuse every login.

WHICH TREE EACH CASE FAILS ON. A REGRESSION fails on the tree before the change that made
it pass: the #374 cases on ``02075f2``, where no limit exists, and the class cases on
``e50429d`` (#374), where one budget spans both classes. Those are marked ``(e50429d)``
in their docstrings. A CONTROL holds on both. Where #374 asserted what the class
correction deliberately reverses, the case is kept and says so (``CHANGED BY THE CLASS
CORRECTION``) rather than being deleted.

A refusal of an issuance is ``429 {"status":429,"message":"Too many attempts. Please
wait and try again."}`` on login, resend and the owner-claim challenge, and E-R1's
unchanged acknowledgement on reset initiation. In every case it writes nothing.

Ledger rows standing for earlier issuances are inserted directly, with the ``created_at``
and the origin the case needs. That is the only way to place a row hours in the past,
and it is exactly what the decision reads. Everything else a case needs before it
(earlier codes, earlier lookups) is produced by real requests.

THIS MODULE LOADS ON A TREE WITHOUT THE LIMITS (``02075f2``) AND ON ONE WITHOUT THE
CLASSES (``e50429d``), so that each REGRESSION can be seen failing there on its own
assertion. It imports nothing new at module level: the reviewed key derivations and the
class table are restated below, and the few cases about the module's internals (``UNIT``)
skip where they do not exist.
"""
import ast
import datetime
import hashlib
import hmac
import ipaddress
import json
import pathlib
import re
import unittest
import uuid
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from users_app import models as user_models
from users_app import otp_accounting
from users_app.controllers import otp_manager
from users_app.controllers.otp_manager import OTP_MAX_ATTEMPTS
from users_app.models import OtpIssuance, User, UserOtp
from users_app.tests_challenge_replacement import (
    CLAIM_CHALLENGE, CLAIM_HEADER, DEV_OTP, INITIATE, LOGIN, PASSWORD, RESEND, VERIFY,
    _account, _claim, _delivery_intercepted, _owner, _phone,
)


def enforcement():
    """The limits module, or ``None`` on a tree that has none."""
    try:
        from users_app import otp_limits
    except ImportError:
        return None
    return otp_limits


def _model(name):
    return getattr(user_models, name, None)


otp_limits = enforcement()

needs_enforcement = unittest.skipIf(
    enforcement() is None, 'UNIT: about the limits module itself, which this tree lacks')
needs_classes = unittest.skipIf(
    otp_limits is None or not hasattr(otp_limits, 'issuance_class'),
    'UNIT: about the issuance classes, which this tree lacks')

# The reviewed derivations, restated rather than imported (the E-R3 convention), so a
# contract test can compare production with an independent statement of them.
_LOCK_KEY_DOMAIN = b'dinify.abuse-limit-lock.v1\x00'
_CLIENT_KEY_DOMAIN = b'dinify:abuse-limits:client:v1\x00'

# The reviewed class of every origin in the vocabulary, restated the same way.
VERIFIED_ORIGINS = ('password_login', 'login_resend', 'owner_claim_challenge')
UNVERIFIED_ORIGINS = ('reset_initiation', 'resend_request', 'unattributed')
# One origin of each class, for seeding rows that stand for earlier issuances.
VERIFIED = 'password_login'
UNVERIFIED = 'reset_initiation'


def lock_key(*parts):
    material = json.dumps(list(parts), separators=(',', ':'))
    digest = hashlib.sha256(_LOCK_KEY_DOMAIN + material.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big', signed=True)


def client_key(address):
    """IPv4 addresses only, which is all these cases use for a key."""
    network = str(ipaddress.ip_address(address))
    return 'ip1:' + hmac.new(otp_manager._otp_pepper(), _CLIENT_KEY_DOMAIN + network.encode(),
                             hashlib.sha256).hexdigest()

LOOKUP = '/api/v1/users/msisdn-lookup/'
REFUSAL = {'status': 429, 'message': 'Too many attempts. Please wait and try again.'}
ACKNOWLEDGEMENT = {
    'status': 200,
    'message': 'If these details match an eligible account, check its registered '
               'phone or email for a reset code.',
}
ADDRESS = '203.0.113.10'
OTHER_ADDRESS = '198.51.100.20'


def subject_of(user):
    return otp_accounting.user_subject_key(user.pk)


def phone_key(phone):
    return otp_accounting.phone_key(phone, otp_manager._otp_pepper())


def seed(count, *, origin, subject=None, destination=None, age=datetime.timedelta(minutes=1)):
    """
    ``count`` finalized ledger rows standing for earlier issuances of ``origin``, ``age``
    old. The origin is required: since the class correction it decides what a row counts
    against.
    """
    created = timezone.now() - age
    OtpIssuance.objects.bulk_create([
        OtpIssuance(
            id=uuid.uuid4(), subject_key=subject or f'u:{uuid.uuid4()}',
            destination_key=destination, origin=origin,
            state=otp_accounting.STATE_ACCEPTED, created_at=created, finalized_at=created,
        )
        for _ in range(count)
    ])


def issued(*, origins, subject=None, destination=None):
    """How many ledger rows of these origins name this subject or destination."""
    rows = OtpIssuance.objects.filter(origin__in=origins)
    if subject is not None:
        rows = rows.filter(subject_key=subject)
    if destination is not None:
        rows = rows.filter(destination_key=destination)
    return rows.count()


def inline(run):
    run()


class _LimitCase(TestCase):
    """Real routes, delivery replaced, the alert thread run inline."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)
        delivery = _delivery_intercepted()
        self.sms, self.email = delivery.__enter__()
        self.addCleanup(delivery.__exit__, None, None, None)
        if otp_limits is not None:
            spawn = mock.patch.object(otp_limits, '_spawn', inline)
            spawn.start()
            self.addCleanup(spawn.stop)
            otp_limits._ALERT_GATE.clear()
            self.addCleanup(otp_limits._ALERT_GATE.clear)

    # --- requests ---

    def post(self, url, body, address=ADDRESS, headers=None):
        # The per-process DRF throttles are not what these tests are about.
        cache.clear()
        return self.client.post(url, body, format='json', REMOTE_ADDR=address,
                                headers=headers or {})

    def get(self, url, address=ADDRESS):
        cache.clear()
        return self.client.get(url, REMOTE_ADDR=address)

    def issuing(self, url, body, **kw):
        """POST, and return the response with the challenge rows it wrote."""
        before = set(UserOtp.objects.values_list('pk', flat=True))
        response = self.post(url, body, **kw)
        new = list(UserOtp.objects.exclude(pk__in=before).values_list('pk', flat=True))
        return response, new

    def login(self, user, **kw):
        return self.issuing(LOGIN, {'username': user.phone_number, 'password': PASSWORD}, **kw)

    def proof_login(self, user, **kw):
        """A login that names the resend capability (D11 E-R4); its proof is in the body."""
        return self.issuing(LOGIN, {'username': user.phone_number, 'password': PASSWORD,
                                    'login_resend_protocol': 'v1'}, **kw)

    def login_resend(self, user, proof, **kw):
        return self.issuing(RESEND, {'identification': 'id', 'identifier': str(user.pk),
                                     'purpose': 'login', 'login_resend_proof': proof}, **kw)

    def reset(self, user, **kw):
        return self.issuing(INITIATE, {'identifier': user.phone_number,
                                       'identification': 'phone'}, **kw)

    def register_code(self, phone, **kw):
        """The generic resend sign-up uses: no account, a phone-only challenge."""
        return self.issuing(RESEND, {'identification': 'msisdn', 'identifier': phone,
                                     'purpose': 'register'}, **kw)

    def resend_reset(self, user, **kw):
        return self.issuing(RESEND, {'identification': 'id', 'identifier': str(user.pk),
                                     'purpose': 'reset-password'}, **kw)

    def verify_login(self, user, code=DEV_OTP):
        cache.clear()
        return self.client.post(VERIFY, {'user': str(user.pk), 'otp': code}, format='json')

    # --- assertions ---

    def assertAdmitted(self, outcome, status=200):
        response, new = outcome
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(len(new), 1, 'an admitted request writes one challenge')
        return new[0]

    def assertRefused(self, outcome):
        """The one 429 body, no Retry-After, and nothing written."""
        response, new = outcome
        self.assertEqual(response.status_code, 429, response.content)
        self.assertEqual(response.json(), REFUSAL)
        self.assertNotIn('Retry-After', response.headers)
        self.assertEqual(new, [], 'a refusal wrote a challenge')

    def assertAcknowledgedWithoutIssuing(self, outcome):
        """Reset initiation's refusal: E-R1's acknowledgement, and nothing written."""
        response, new = outcome
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), ACKNOWLEDGEMENT)
        self.assertEqual(new, [], 'a refused reset wrote a challenge')

    def assertLoginCodeWorks(self, user):
        """The login's code, ``1234`` under ENV=dev, verifies to a session."""
        verified = self.verify_login(user)
        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertTrue(verified.json()['data']['valid'], verified.content)
        self.assertIn('token', verified.json()['data'])

    def counters(self):
        counter_model = _model('AbuseCounter')
        if counter_model is None:
            return []
        return list(counter_model.objects.order_by('pk').values_list('pk', 'hits'))

    def assertNothingWrittenBy(self, action):
        """Runs ``action`` and asserts it issued, deleted, recorded and sent nothing."""
        challenges = sorted(map(str, UserOtp.objects.values_list('pk', flat=True)))
        ledger = OtpIssuance.objects.count()
        before = self.counters()
        sends = self.sms.call_count
        result = action()
        self.assertEqual(sorted(map(str, UserOtp.objects.values_list('pk', flat=True))),
                         challenges, 'a refusal deleted or wrote a challenge')
        self.assertEqual(OtpIssuance.objects.count(), ledger, 'a refusal wrote a ledger row')
        self.assertEqual(self.counters(), before, 'a refusal was counted')
        self.assertEqual(self.sms.call_count, sends, 'a refusal sent a code')
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — per subject, per class
# ═══════════════════════════════════════════════════════════════════════════════

class SubjectLimitTests(_LimitCase):

    def test_REGRESSION_a_fourth_login_code_in_15_minutes_is_refused(self):
        """A verified login is still limited by its own L1."""
        owner = _owner('l1-login')
        for _ in range(3):
            self.assertAdmitted(self.login(owner))
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.login(owner)))

    def test_REGRESSION_a_fourth_unverified_code_in_15_minutes_is_refused(self):
        """Unverified requests are still limited by L1, across their purposes and routes."""
        owner = _owner('l1-unverified')
        self.assertEqual(self.reset(owner)[0].json(), ACKNOWLEDGEMENT)
        self.assertAdmitted(self.resend_reset(owner))
        self.assertEqual(self.reset(owner)[0].json(), ACKNOWLEDGEMENT)
        self.assertEqual(issued(origins=UNVERIFIED_ORIGINS, subject=subject_of(owner)), 3)
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.resend_reset(owner)))
        self.assertAcknowledgedWithoutIssuing(self.reset(owner))

    def test_REGRESSION_each_class_has_its_own_budget_across_purposes_and_routes(self):
        """
        (e50429d) CHANGED BY THE CLASS CORRECTION. #374 asserted that one reset, one
        login and one reset resend spent the account's whole budget, so the next login
        and the next resend were both refused. Each class now has the three codes.
        """
        owner = _owner('l1-purposes')
        self.assertEqual(self.reset(owner)[0].json(), ACKNOWLEDGEMENT)
        self.assertAdmitted(self.login(owner))
        self.assertAdmitted(self.resend_reset(owner))
        self.assertAdmitted(self.login(owner))
        self.assertAdmitted(self.resend_reset(owner))
        self.assertAdmitted(self.login(owner))
        self.assertRefused(self.login(owner))
        self.assertRefused(self.resend_reset(owner))
        self.assertEqual(issued(origins=VERIFIED_ORIGINS, subject=subject_of(owner)), 3)
        self.assertEqual(issued(origins=UNVERIFIED_ORIGINS, subject=subject_of(owner)), 3)

    def test_REGRESSION_ten_in_24_hours(self):
        owner = _owner('l1-day')
        seed(10, origin=VERIFIED, subject=subject_of(owner), age=datetime.timedelta(hours=2))
        self.assertRefused(self.login(owner))

    def test_REGRESSION_ten_unverified_in_24_hours(self):
        owner = _owner('l1-day-unverified')
        seed(10, origin=UNVERIFIED, subject=subject_of(owner), age=datetime.timedelta(hours=2))
        self.assertRefused(self.resend_reset(owner))

    def test_CONTROL_nine_in_24_hours_still_admits(self):
        owner = _owner('l1-day-control')
        seed(9, origin=VERIFIED, subject=subject_of(owner), age=datetime.timedelta(hours=2))
        self.assertAdmitted(self.login(owner))

    def test_CONTROL_rows_older_than_the_short_window_do_not_count_there(self):
        owner = _owner('l1-aged')
        seed(3, origin=VERIFIED, subject=subject_of(owner), age=datetime.timedelta(minutes=16))
        self.assertAdmitted(self.login(owner))

    def test_CONTROL_rows_older_than_24_hours_do_not_count(self):
        owner = _owner('l1-old')
        seed(10, origin=VERIFIED, subject=subject_of(owner), age=datetime.timedelta(hours=25))
        self.assertAdmitted(self.login(owner))

    def test_CONTROL_another_account_is_unaffected(self):
        owner, other = _owner('l1-a'), _owner('l1-b')
        seed(3, origin=VERIFIED, subject=subject_of(owner))   # the first account's is spent
        self.assertAdmitted(self.login(other))

    def test_REGRESSION_a_phone_with_no_account_is_its_own_subject(self):
        phone = _phone()
        seed(3, origin=UNVERIFIED, subject=phone_key(phone))
        self.assertRefused(self.register_code(phone))

    def test_CONTROL_the_admitted_code_is_still_1234_under_dev(self):
        """ENV=dev (the suite's default) is unchanged: the limits count, the code is fixed."""
        owner = _owner('l1-dev')
        self.assertAdmitted(self.login(owner))
        self.assertLoginCodeWorks(owner)


# ═══════════════════════════════════════════════════════════════════════════════
# The correction: what an anonymous requester can and cannot spend
# ═══════════════════════════════════════════════════════════════════════════════

class VerifiedBudgetIsolationTests(_LimitCase):
    """
    Anonymous requests count only against the unverified budget, so they cannot stop an
    account holder who proves something from getting a code, and the reverse.
    """

    def test_REGRESSION_ten_anonymous_reset_initiations_do_not_block_the_owners_login(self):
        """
        (e50429d) Regression (a). Ten reset requests naming an owner's phone, from a
        stranger: the first three issue and the rest are refused, every one answered with
        the same acknowledgement. The owner's password login is then still issued its
        code, and the code signs them in. On #374 the login answered 429.
        """
        owner = _owner('iso-ten-resets')
        for _ in range(10):
            response, _new = self.reset(owner, address=OTHER_ADDRESS)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json(), ACKNOWLEDGEMENT)
        self.assertEqual(issued(origins=UNVERIFIED_ORIGINS, subject=subject_of(owner)), 3)
        self.assertAdmitted(self.login(owner))
        self.assertLoginCodeWorks(owner)

    def test_REGRESSION_a_days_worth_of_anonymous_resets_does_not_block_the_owners_login(self):
        """
        (e50429d) The long windows too: ten unverified codes in the last day, to the
        account and to its phone, leave the owner's login budget whole.
        """
        owner = _owner('iso-day')
        seed(10, origin=UNVERIFIED, subject=subject_of(owner),
             destination=phone_key(owner.phone_number), age=datetime.timedelta(hours=2))
        for _ in range(3):
            self.assertAdmitted(self.login(owner))
        self.assertRefused(self.login(owner))       # its own L1 still holds

    def test_REGRESSION_the_owners_logins_do_not_show_in_an_anonymous_answer(self):
        """
        (e50429d) The reverse. Three sign-ins by the owner leave the anonymous budget
        whole, so an anonymous resend is still issued. On #374 it answered 429, which
        told anyone asking that the account had just received codes.
        """
        owner = _owner('iso-reverse')
        for _ in range(3):
            self.assertAdmitted(self.login(owner))
        self.assertAdmitted(self.resend_reset(owner))
        self.assertEqual(self.reset(owner)[0].json(), ACKNOWLEDGEMENT)
        self.assertEqual(issued(origins=UNVERIFIED_ORIGINS, subject=subject_of(owner)), 2)

    def test_REGRESSION_a_login_resend_is_verified(self):
        """
        (e50429d) A login resend, bound to its password step's proof, counts with the
        login: three anonymous codes do not refuse it, and the fourth verified code is
        refused.
        """
        owner = _owner('iso-resend')
        seed(3, origin=UNVERIFIED, subject=subject_of(owner),
             destination=phone_key(owner.phone_number))
        response, _new = self.proof_login(owner)
        self.assertEqual(response.status_code, 200, response.content)
        proof = response.json()['data']['login_resend_proof']
        self.assertAdmitted(self.login_resend(owner, proof))
        self.assertAdmitted(self.login_resend(owner, proof))
        self.assertRefused(self.login_resend(owner, proof))
        self.assertEqual(issued(origins=('login_resend',), subject=subject_of(owner)), 2)

    def test_REGRESSION_anonymous_codes_do_not_refuse_an_owner_claim_challenge(self):
        """(e50429d) The owner-claim challenge, which needs the claim token, is verified."""
        owner, token, _ = _claim(pending=True)
        seed(3, origin=UNVERIFIED, subject=subject_of(owner),
             destination=phone_key(owner.phone_number))
        response, new = self.issuing(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(new), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — per destination phone, whoever asks, per class
# ═══════════════════════════════════════════════════════════════════════════════

class DestinationLimitTests(_LimitCase):

    def test_REGRESSION_a_phone_receives_at_most_three_unverified_codes_whoever_asks(self):
        """
        CHANGED BY THE CLASS CORRECTION. #374 refused the account owner's LOGIN here; the
        login is verified and is now issued, and the owner's own reset resend, which is
        unverified, is what the destination refuses.
        """
        # Three sign-up codes to a number with no account (subject: the phone)...
        phone = _phone()
        for _ in range(3):
            self.assertAdmitted(self.register_code(phone))
        # ...then the account is created and asks for a reset code (subject: the account,
        # which has no issuance of its own). The DESTINATION is what refuses.
        owner = _owner('l2-after-signup')
        User.objects.filter(pk=owner.pk).update(phone_number=phone, username=phone)
        owner.refresh_from_db()
        self.assertEqual(OtpIssuance.objects.filter(subject_key=subject_of(owner)).count(), 0)
        self.assertRefused(self.resend_reset(owner))
        # (e50429d) The owner's login is verified, and its destination budget is whole.
        self.assertAdmitted(self.login(owner))

    def test_REGRESSION_ten_in_24_hours(self):
        owner = _owner('l2-day')
        seed(10, origin=UNVERIFIED, destination=phone_key(owner.phone_number),
             age=datetime.timedelta(hours=3))
        self.assertRefused(self.resend_reset(owner))

    def test_REGRESSION_ten_verified_in_24_hours(self):
        owner = _owner('l2-day-verified')
        seed(10, origin=VERIFIED, destination=phone_key(owner.phone_number),
             age=datetime.timedelta(hours=3))
        self.assertRefused(self.login(owner))

    def test_CONTROL_another_phone_is_unaffected(self):
        owner, other = _owner('l2-a'), _owner('l2-b')
        seed(3, origin=VERIFIED, destination=phone_key(owner.phone_number))
        self.assertAdmitted(self.login(other))


# ═══════════════════════════════════════════════════════════════════════════════
# L3 — per client IP, unverified class only
# ═══════════════════════════════════════════════════════════════════════════════

class ClientLimitTests(_LimitCase):

    def test_REGRESSION_one_client_issues_at_most_twenty_unverified_codes_an_hour(self):
        """
        CHANGED BY THE CLASS CORRECTION. #374 spent this client's hour with five logins,
        five resets and ten sign-up codes; logins no longer count, so the twenty are all
        unverified here.
        """
        owners = [_owner(f'l3-{n}') for n in range(5)]
        for owner in owners:                       # 5 resets
            self.assertEqual(self.reset(owner)[0].status_code, 200)
        for owner in owners:                       # 5 reset resends
            self.assertAdmitted(self.resend_reset(owner))
        for _ in range(10):                        # 10 sign-up codes
            self.assertAdmitted(self.register_code(_phone()))
        self.assertEqual(OtpIssuance.objects.count(), 20)
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.register_code(_phone())))
        self.assertRefused(self.resend_reset(_owner('l3-fresh')))
        self.assertAcknowledgedWithoutIssuing(self.reset(_owner('l3-fresh-reset')))

    def test_REGRESSION_an_unverified_flood_from_one_client_does_not_refuse_a_login_from_it(self):
        """
        (e50429d) Regression (c). Carrier-grade NAT puts strangers behind one address.
        Once one of them has spent its hour, a password login from the same address is
        still issued its code, and writes no hit to the client's counter.
        """
        for _ in range(20):
            self.assertAdmitted(self.register_code(_phone()))
        self.assertRefused(self.register_code(_phone()))
        owner = _owner('l3-login')
        before = self.counters()
        self.assertAdmitted(self.login(owner))
        self.assertEqual(self.counters(), before, 'a verified issuance was counted under L3')
        self.assertLoginCodeWorks(owner)
        self.assertRefused(self.register_code(_phone()))       # the flood stays refused

    def test_REGRESSION_logins_from_a_shared_address_do_not_spend_its_anonymous_budget(self):
        """(e50429d) Twenty owners sign in from one address; its sign-up codes still issue."""
        for n in range(20):
            self.assertAdmitted(self.login(_owner(f'l3-shared-{n}')))
        self.assertEqual(self.counters(), [], 'a verified issuance was counted under L3')
        self.assertAdmitted(self.register_code(_phone()))

    def test_CONTROL_another_client_is_unaffected(self):
        for _ in range(20):                         # the first client's hour is spent
            self.assertAdmitted(self.register_code(_phone()))
        self.assertAdmitted(self.register_code(_phone(), address=OTHER_ADDRESS))

    def test_REGRESSION_a_client_is_refused_then_forgotten_after_the_window(self):
        for _ in range(20):
            self.assertAdmitted(self.register_code(_phone()))
        self.assertRefused(self.register_code(_phone()))
        later = timezone.now() + datetime.timedelta(minutes=66)
        with mock.patch.object(otp_limits, '_now', return_value=later):
            self.assertAdmitted(self.register_code(_phone()))

    def test_REGRESSION_an_ipv6_client_is_counted_by_its_64(self):
        for n in range(20):
            self.assertAdmitted(self.register_code(_phone(), address=f'2001:db8:1:2::{n + 1:x}'))
        self.assertRefused(self.register_code(_phone(), address='2001:db8:1:2:ffff::9'))
        self.assertAdmitted(self.register_code(_phone(), address='2001:db8:1:3::1'))

    @needs_enforcement
    def test_UNIT_the_counter_stores_no_address(self):
        self.assertAdmitted(self.register_code(_phone()))
        row = _model('AbuseCounter').objects.get()
        self.assertRegex(row.client_key, r'^ip1:[0-9a-f]{64}$')
        for value in (row.scope, row.client_key):
            self.assertNotIn(ADDRESS, value)
        self.assertEqual((row.scope, row.hits), ('otp_issue', 1))


@needs_enforcement
class ClientNetworkUnitTests(TestCase):
    """UNIT: what a client is counted as. Pure; no database."""

    def test_ipv4_is_the_address(self):
        self.assertEqual(otp_limits.client_network('203.0.113.10'), '203.0.113.10')

    def test_ipv6_is_its_64(self):
        self.assertEqual(otp_limits.client_network('2001:db8:1:2:3:4:5:6'), '2001:db8:1:2::/64')

    def test_an_ipv4_mapped_address_is_the_ipv4_address(self):
        self.assertEqual(otp_limits.client_network('::ffff:203.0.113.10'), '203.0.113.10')

    def test_anything_unusable_shares_one_key(self):
        for value in (None, '', 'not-an-ip', '256.1.1.1', 12):
            self.assertEqual(otp_limits.client_network(value), 'unknown')

    def test_the_key_is_an_hmac_of_the_network_never_the_address(self):
        pepper = b'pepper'
        key = otp_limits.client_key(otp_limits.Client('203.0.113.10'), pepper)
        self.assertRegex(key, r'^ip1:[0-9a-f]{64}$')
        self.assertEqual(key, otp_limits.client_key(otp_limits.Client(' 203.0.113.10 '), pepper))
        self.assertNotEqual(key, otp_limits.client_key(otp_limits.Client('203.0.113.11'), pepper))
        self.assertNotEqual(key, otp_limits.client_key(otp_limits.Client('203.0.113.10'), b'x'))


# ═══════════════════════════════════════════════════════════════════════════════
# L4 — global: refuses the unverified class, and its alerts count every issuance
# ═══════════════════════════════════════════════════════════════════════════════

ALERT_EMAIL = 'ops-alerts@example.test'


@override_settings(OTP_SPEND_ALERT_EMAIL=ALERT_EMAIL)
class GlobalLimitTests(_LimitCase):

    def alert_mails(self):
        return [c for c in self.email.call_args_list if c.kwargs.get('to') == [ALERT_EMAIL]]

    def alert_lines(self, logs):
        return [line for line in logs.output if 'OTP_SPEND_ALERT ' in line]

    def test_REGRESSION_the_300th_unverified_code_in_24_hours_is_the_last(self):
        seed(299, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.register_code(_phone())))
        self.assertRefused(self.resend_reset(_owner('l4-resend')))
        self.assertAcknowledgedWithoutIssuing(self.reset(_owner('l4-reset')))

    def test_REGRESSION_the_cap_reached_by_unverified_codes_does_not_refuse_a_password_login(self):
        """
        (e50429d) Regression (b). A flood of anonymous requests reaches the cap; every
        unverified request is refused, and a password login is still issued its code and
        signs in. On #374 the login answered 429.
        """
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        self.assertRefused(self.register_code(_phone()))
        owner = _owner('l4-login')
        self.assertAdmitted(self.login(owner))
        self.assertLoginCodeWorks(owner)
        claimed, token, _ = _claim(pending=True)
        response, new = self.issuing(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertEqual((response.status_code, len(new)), (200, 1), response.content)

    def test_REGRESSION_verified_codes_do_not_spend_the_cap(self):
        """(e50429d) 300 verified codes in the window leave the unverified cap whole."""
        seed(300, origin=VERIFIED, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))

    def test_REGRESSION_the_cap_alert_is_sent_once(self):
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertEqual(
            self.alert_lines(logs),
            ['WARNING:users_app.otp_limits:OTP_SPEND_ALERT threshold=cap issued=300 cap=300 '
             'email=sending'])
        self.assertEqual(len(self.alert_mails()), 1)
        mail = self.alert_mails()[0].kwargs
        self.assertIn('cap', mail['subject'])
        self.assertIn('300', mail['message'])
        # A second refusal, even past this process's local gate, is not re-sent.
        otp_limits._ALERT_GATE.clear()
        self.assertRefused(self.register_code(_phone()))
        self.assertEqual(len(self.alert_mails()), 1)
        self.assertTrue(_model('OtpSpendAlert').objects.filter(alert='cap').exists())

    def test_CONTROL_the_cap_alert_reports_verified_spend_too(self):
        """The cap refuses on the unverified count; the alert reports every issuance."""
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        seed(40, origin=VERIFIED, age=datetime.timedelta(hours=1))
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertEqual(
            self.alert_lines(logs),
            ['WARNING:users_app.otp_limits:OTP_SPEND_ALERT threshold=cap issued=340 cap=300 '
             'email=sending'])
        self.assertIn('issued 340 one-time codes', self.alert_mails()[0].kwargs['message'])

    @needs_enforcement
    def test_UNIT_the_cap_alert_is_sent_again_after_24_hours(self):
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        _model('OtpSpendAlert').objects.create(
            alert='cap', last_sent_at=timezone.now() - datetime.timedelta(hours=25))
        self.assertRefused(self.register_code(_phone()))
        self.assertEqual(len(self.alert_mails()), 1)

    def test_REGRESSION_the_warning_is_sent_once_at_80_percent(self):
        """The 239 earlier codes are verified: the warning counts every issuance."""
        seed(239, origin=VERIFIED, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))          # the 240th
        self.assertEqual(len(self.alert_mails()), 1)
        self.assertIn('80%', self.alert_mails()[0].kwargs['subject'])
        otp_limits._ALERT_GATE.clear()
        self.assertAdmitted(self.register_code(_phone()))          # the 241st
        self.assertEqual(len(self.alert_mails()), 1)

    def test_CONTROL_a_verified_code_can_be_the_one_that_reaches_the_warning(self):
        seed(239, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertAdmitted(self.login(_owner('l4-warning')))  # the 240th
        self.assertEqual(
            self.alert_lines(logs),
            ['WARNING:users_app.otp_limits:OTP_SPEND_ALERT threshold=warning issued=240 '
             'cap=300 email=sending'])
        self.assertEqual(len(self.alert_mails()), 1)

    def test_CONTROL_no_warning_below_80_percent(self):
        seed(238, origin=VERIFIED, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))          # the 239th
        self.assertEqual(self.alert_mails(), [])
        if _model('OtpSpendAlert') is not None:
            self.assertFalse(_model('OtpSpendAlert').objects.exists())

    @override_settings(OTP_SPEND_ALERT_EMAIL='')
    def test_REGRESSION_with_no_address_the_line_is_still_written_and_nothing_is_mailed(self):
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertIn('WARNING:users_app.otp_limits:OTP_SPEND_ALERT threshold=cap issued=300 '
                      'cap=300 email=not_configured', logs.output)
        self.assertEqual(self.email.call_count, 0)

    def test_REGRESSION_a_failed_send_is_logged_with_its_marker(self):
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        self.email.return_value = False
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertIn('ERROR:users_app.otp_limits:OTP_SPEND_ALERT_EMAIL_FAILED threshold=cap',
                      logs.output)

    def test_CONTROL_rows_older_than_24_hours_do_not_count(self):
        seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=25))
        self.assertAdmitted(self.register_code(_phone()))

    def test_REGRESSION_the_alert_mail_names_no_person(self):
        owner = _owner('l4-pii')
        seed(299, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.resend_reset(owner))              # the 300th: the warning
        self.assertRefused(self.resend_reset(owner))               # the cap
        self.assertEqual(len(self.alert_mails()), 2)
        for call in self.alert_mails():
            text = call.kwargs['subject'] + call.kwargs['message']
            for value in (owner.phone_number, owner.email, str(owner.pk), ADDRESS):
                self.assertNotIn(value, text)


# ═══════════════════════════════════════════════════════════════════════════════
# L5 — the msisdn lookup
# ═══════════════════════════════════════════════════════════════════════════════

class LookupLimitTests(_LimitCase):

    def lookup(self, phone, address=ADDRESS):
        return self.get(f'{LOOKUP}?msisdn={phone}', address=address)

    def test_REGRESSION_the_31st_lookup_in_an_hour_is_refused(self):
        held = _account('l5-held')
        for n in range(30):
            status = self.lookup(held.phone_number if n % 2 else _phone()).status_code
            self.assertIn(status, (200, 404))
        before = self.counters()
        refused_held = self.lookup(held.phone_number)
        refused_free = self.lookup(_phone())
        for response in (refused_held, refused_free):
            self.assertEqual(response.status_code, 429, response.content)
            self.assertEqual(response.json(), REFUSAL)
            self.assertNotIn('Retry-After', response.headers)
        # The same bytes whether or not the number has an account.
        self.assertEqual(refused_held.content, refused_free.content)
        self.assertEqual(self.counters(), before, 'a refused lookup was counted')

    def test_REGRESSION_the_refusal_is_decided_before_the_request_is_read(self):
        for _ in range(30):
            self.lookup(_phone())
        response = self.get(LOOKUP)                     # no msisdn at all
        self.assertEqual(response.status_code, 429, response.content)

    def test_CONTROL_another_client_is_unaffected(self):
        for _ in range(30):                         # the first client's hour is spent
            self.lookup(_phone())
        self.assertEqual(self.lookup(_phone(), address=OTHER_ADDRESS).status_code, 404)

    def test_CONTROL_lookups_do_not_spend_the_issuance_budget(self):
        for _ in range(30):
            self.lookup(_phone())
        self.assertAdmitted(self.register_code(_phone()))


# ═══════════════════════════════════════════════════════════════════════════════
# One refusal: the same body, the same statements, no new oracle
# ═══════════════════════════════════════════════════════════════════════════════

def _shape(sql):
    """A statement with its literals removed, so two refusals can be compared."""
    sql = re.sub(r"'[^']*'", '?', sql)
    sql = re.sub(r'-?\d+(\.\d+)?', '?', sql)
    # psycopg writes a negative integer with a leading space; spacing is not a shape.
    sql = re.sub(r'\s+', ' ', sql)
    return sql


class OneRefusalTests(_LimitCase):

    def refuse(self, why):
        """A sign-up resend refused by exactly ONE limit, and the statements it ran."""
        phone, address = _phone(), ADDRESS
        if why == 'L1':     # the phone's own 24-hour subject budget; nothing else is near
            seed(10, origin=UNVERIFIED, subject=phone_key(phone),
                 age=datetime.timedelta(hours=20))
        elif why == 'L2':   # someone else's codes to this phone
            seed(3, origin=UNVERIFIED, destination=phone_key(phone))
        elif why == 'L3':   # this client's hour
            address = OTHER_ADDRESS
            for _ in range(20):
                self.assertAdmitted(self.register_code(_phone(), address=address))
        elif why == 'L4':
            seed(300, origin=UNVERIFIED, age=datetime.timedelta(hours=1))
        before = set(UserOtp.objects.values_list('pk', flat=True))
        body = {'identification': 'msisdn', 'identifier': phone, 'purpose': 'register'}
        with CaptureQueriesContext(connection) as queries:     # the request, and only it
            response = self.post(RESEND, body, address=address)
        new = list(UserOtp.objects.exclude(pk__in=before).values_list('pk', flat=True))
        self.assertRefused((response, new))
        return response, [_shape(q['sql']) for q in queries.captured_queries]

    def refuse_login(self, why):
        """A password login refused by exactly ONE limit, and the statements it ran."""
        owner = _owner(f'one-login-{why}')
        if why == 'L1':
            seed(10, origin=VERIFIED, subject=subject_of(owner),
                 age=datetime.timedelta(hours=20))
        elif why == 'L2':
            seed(3, origin=VERIFIED, destination=phone_key(owner.phone_number))
        before = set(UserOtp.objects.values_list('pk', flat=True))
        body = {'username': owner.phone_number, 'password': PASSWORD}
        with CaptureQueriesContext(connection) as queries:
            response = self.post(LOGIN, body)
        new = list(UserOtp.objects.exclude(pk__in=before).values_list('pk', flat=True))
        self.assertRefused((response, new))
        raw = [q['sql'] for q in queries.captured_queries]
        return response, [_shape(sql) for sql in raw], raw

    def test_REGRESSION_every_limit_answers_the_same_bytes(self):
        bodies = {why: self.refuse(why)[0].content for why in ('L1', 'L2', 'L3', 'L4')}
        bodies.update({f'login-{why}': self.refuse_login(why)[0].content
                       for why in ('L1', 'L2')})
        self.assertEqual(len(set(bodies.values())), 1, bodies)

    def test_REGRESSION_every_limit_runs_the_same_statements(self):
        shapes = {}
        for why in ('L1', 'L2', 'L3', 'L4'):
            # The cap alert's own statements run in its thread, not the request; here the
            # thread is inline, so this process's gate is closed to keep it out.
            if otp_limits is not None:
                otp_limits._ALERT_GATE[otp_limits.OTP_SPEND_ALERT_CAP] = float('inf')
            shapes[why] = self.refuse(why)[1]
            OtpIssuance.objects.all().delete()
            if _model('AbuseCounter') is not None:
                _model('AbuseCounter').objects.all().delete()
        self.assertEqual(shapes['L1'], shapes['L2'])
        self.assertEqual(shapes['L1'], shapes['L3'])
        self.assertEqual(shapes['L1'], shapes['L4'])
        for why, statements in shapes.items():
            ledger = [s for s in statements if 'FROM "otp_issuances"' in s]
            counter = [s for s in statements if 'FROM "account_abuse_counters"' in s]
            self.assertEqual((len(ledger), len(counter)), (1, 1), why)

    def test_CONTROL_a_refused_login_runs_the_same_statements_whichever_limit_fired(self):
        shapes = {why: self.refuse_login(why)[1] for why in ('L1', 'L2')}
        self.assertEqual(shapes['L1'], shapes['L2'])
        ledger = [s for s in shapes['L1'] if 'FROM "otp_issuances"' in s]
        self.assertEqual(len(ledger), 1)

    def test_REGRESSION_a_login_reads_and_writes_no_client_counter(self):
        """
        (e50429d) L3 does not apply to a verified issuance: it takes no client lock and
        neither reads nor writes the client's counter, refused or admitted.
        """
        refused = self.refuse_login('L1')[2]
        owner = _owner('one-login-admitted')
        with CaptureQueriesContext(connection) as queries:
            self.assertAdmitted(self.login(owner))
        admitted = [q['sql'] for q in queries.captured_queries]
        client_lock = f'pg_advisory_xact_lock({0x4F545043},'
        for statements in (refused, admitted):
            # The one statement allowed is the housekeeping after any admitted send: a
            # bounded DELETE of expired rows, which reads no client's hits.
            self.assertEqual([s for s in statements if 'account_abuse_counters' in s
                              and not s.startswith('DELETE FROM "account_abuse_counters"')], [])
            self.assertEqual([s for s in statements if client_lock in s.replace(' ', '')], [])
            # ...while the subject and destination locks are still taken.
            self.assertEqual(len([s for s in statements if 'pg_advisory_xact_lock' in s]), 3)
        self.assertEqual(self.counters(), [])

    def test_REGRESSION_a_limited_reset_answers_like_an_unknown_account(self):
        owner = _owner('one-reset')
        seed(3, origin=UNVERIFIED, subject=subject_of(owner))
        limited, new = self.reset(owner)
        self.assertEqual(new, [], 'a refused reset wrote a challenge')
        unknown = self.post(INITIATE, {'identifier': _phone(), 'identification': 'phone'})
        self.assertEqual(limited.status_code, 200)
        self.assertEqual(limited.json(), ACKNOWLEDGEMENT)
        self.assertEqual(limited.content, unknown.content)

    def test_REGRESSION_the_owner_claim_challenge_answers_the_429(self):
        """Its verified budget is shared with the account's password logins."""
        owner, token, _ = _claim(pending=True)
        seed(3, origin=VERIFIED, subject=subject_of(owner))
        outcome = self.issuing(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertRefused(outcome)
        self.assertEqual(outcome[0]['Cache-Control'], 'no-store, private')

    def test_CONTROL_the_owner_claim_challenge_is_admitted_below_the_limits(self):
        owner, token, _ = _claim(pending=True)
        response, new = self.issuing(CLAIM_CHALLENGE, {}, headers={CLAIM_HEADER: token})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(new), 1)

    def test_CONTROL_a_wrong_password_is_still_the_401_whatever_the_budget(self):
        owner = _owner('one-401')
        seed(3, origin=VERIFIED, subject=subject_of(owner))
        response = self.post(LOGIN, {'username': owner.phone_number, 'password': 'wrong'})
        self.assertEqual(response.status_code, 401, response.content)


# ═══════════════════════════════════════════════════════════════════════════════
# The class table, and every production call that reaches the limits
# ═══════════════════════════════════════════════════════════════════════════════

class ClassTableTests(SimpleTestCase):

    def test_CONTROL_the_restated_table_covers_the_origin_vocabulary(self):
        self.assertEqual(sorted(VERIFIED_ORIGINS + UNVERIFIED_ORIGINS),
                         sorted(otp_accounting.ORIGINS))

    @needs_classes
    def test_UNIT_the_class_of_every_origin(self):
        self.assertEqual(otp_limits.VERIFIED_ORIGINS, frozenset(VERIFIED_ORIGINS))
        for origin in VERIFIED_ORIGINS:
            self.assertEqual(otp_limits.issuance_class(origin), 'verified', origin)
        for origin in UNVERIFIED_ORIGINS:
            self.assertEqual(otp_limits.issuance_class(origin), 'unverified', origin)

    @needs_classes
    def test_UNIT_anything_else_is_unverified(self):
        for value in (None, '', 'PASSWORD_LOGIN', ' password_login', 'owner-claim', 7,
                      ['password_login'], {'password_login': 1}, b'password_login'):
            self.assertEqual(otp_limits.issuance_class(value), 'unverified', repr(value))

    @needs_classes
    def test_UNIT_the_decision_requires_an_origin(self):
        with self.assertRaises(TypeError):
            otp_limits.decide_issuance(subject_key=None, destination_key=None, client_key=None)


def _production_make_otp_calls():
    """
    ``(path, line, origin)`` for every ``make_otp(...)`` call in production code.
    ``origin`` is the string the call passes, resolved from a literal or an
    ``otp_accounting.ORIGIN_*`` name; ``None`` when it passes none.
    """
    root = pathlib.Path(settings.BASE_DIR)
    # The repository's own packages only, so a virtualenv inside the checkout is not read.
    paths = [
        path
        for top in sorted(root.iterdir())
        if top.is_dir() and not top.name.startswith('.') and (top / '__init__.py').exists()
        for path in sorted(top.rglob('*.py'))
    ]
    calls = []
    for path in paths:
        relative = path.relative_to(root)
        if 'migrations' in relative.parts:
            continue
        if path.name.startswith(('tests', 'test_', 'qualify_')):
            continue
        source = path.read_text(encoding='utf-8')
        if 'make_otp' not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'make_otp'):
                continue
            origin = next((k.value for k in node.keywords if k.arg == 'origin'), None)
            if isinstance(origin, ast.Constant):
                value = origin.value
            elif isinstance(origin, ast.Attribute) and origin.attr.startswith('ORIGIN_'):
                value = getattr(otp_accounting, origin.attr)
            else:
                value = None if origin is None else ast.dump(origin)
            calls.append((str(relative), node.lineno, value))
    return calls


class OriginInventoryTests(SimpleTestCase):
    """
    Every production call that reaches the limits names its own origin, so none is
    decided as ``unattributed`` (which is unverified). A new call fails here until its
    class is a deliberate entry in this table.
    """
    EXPECTED = {
        ('users_app/controllers/login.py', 'password_login'): 'verified',
        ('users_app/controllers/otp_manager.py', 'login_resend'): 'verified',
        ('platform_admin_app/endpoints/owner_claim.py', 'owner_claim_challenge'): 'verified',
        ('users_app/controllers/reset_password.py', 'reset_initiation'): 'unverified',
        ('users_app/controllers/otp_manager.py', 'resend_request'): 'unverified',
    }

    def test_CONTROL_every_production_call_names_a_known_origin(self):
        calls = _production_make_otp_calls()
        self.assertEqual(len(calls), 6, calls)
        for path, line, origin in calls:
            self.assertIn(origin, otp_accounting.ORIGINS, f'{path}:{line}')
            self.assertNotEqual(origin, 'unattributed', f'{path}:{line}')
        self.assertEqual(sorted({(path, origin) for path, _, origin in calls}),
                         sorted(self.EXPECTED))
        for (path, origin), expected in self.EXPECTED.items():
            self.assertEqual('verified' if origin in VERIFIED_ORIGINS else 'unverified',
                             expected, (path, origin))


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration, and L6
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigurationTests(_LimitCase):

    @needs_enforcement
    def test_UNIT_the_defaults_are_the_owners(self):
        limits = otp_limits.current()
        self.assertEqual(
            (limits.subject_short, limits.subject_long, limits.destination_short,
             limits.destination_long, limits.client, limits.global_, limits.lookup),
            (otp_limits.Window(3, 900), otp_limits.Window(10, 86400),
             otp_limits.Window(3, 900), otp_limits.Window(10, 86400),
             otp_limits.Window(20, 3600), otp_limits.Window(300, 86400),
             otp_limits.Window(30, 3600)))
        self.assertEqual((limits.alert_percent, limits.warning_at), (80, 240))
        self.assertEqual(otp_limits.configuration_errors(), [])

    def test_REGRESSION_every_limit_is_adjustable(self):
        owner = _owner('config-raised')
        # One account, one phone: both of its short windows are raised.
        with override_settings(OTP_LIMIT_SUBJECT_SHORT_COUNT=5,
                               OTP_LIMIT_DESTINATION_SHORT_COUNT=5):
            for _ in range(5):
                self.assertAdmitted(self.login(owner))
            self.assertRefused(self.login(owner))

    def test_REGRESSION_a_bad_value_fails_the_system_check(self):
        for name, value in (('OTP_LIMIT_GLOBAL_COUNT', 0), ('OTP_LIMIT_CLIENT_SECONDS', 30),
                            ('OTP_LIMIT_SUBJECT_LONG_SECONDS', 8 * 86400),
                            ('OTP_SPEND_ALERT_PERCENT', 0), ('OTP_SPEND_ALERT_EMAIL', 'nobody'),
                            ('MSISDN_LOOKUP_LIMIT_CLIENT_COUNT', True)):
            with self.subTest(name=name), override_settings(**{name: value}):
                errors = [e for e in run_checks() if e.id == 'users_app.E001']
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(name, errors[0].msg)
                with self.assertRaises(ImproperlyConfigured):
                    otp_limits.current()

    @override_settings(OTP_LIMIT_GLOBAL_COUNT=0)
    def test_REGRESSION_a_bad_value_fails_closed_at_the_route(self):
        owner = _owner('config-closed')
        response, new = self.login(owner)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(new, [])

    def test_CONTROL_L6_is_unchanged(self):
        self.assertEqual(OTP_MAX_ATTEMPTS, 5)
