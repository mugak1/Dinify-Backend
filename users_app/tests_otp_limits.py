"""
D11 enforcement — durable abuse limits, pinned at the routes.

Every refusal here is decoded from a real response. Each limit has a REGRESSION that
fails on ``02075f2`` (where no limit exists, so the request is admitted) and passes
now, and a CONTROL that holds on both. The concurrency claims (exactness of L1, L2,
L3 and L5, and L4's bounded overshoot) are in ``tests_otp_limits_concurrency``.

The limits, at the owner's defaults (``docs/engineering/d11-enforcement.md``):

* L1 issuance per subject: 3 / 15 min, 10 / 24 h
* L2 issuance per destination phone: 3 / 15 min, 10 / 24 h
* L3 issuance per client IP: 20 / hour
* L4 global issuance: 300 / 24 h, alerts at 80% and at the cap
* L5 ``msisdn-lookup`` per client IP: 30 / hour
* L6 verify attempts per challenge: 5, unchanged

A refusal of an issuance is ``429 {"status":429,"message":"Too many attempts. Please
wait and try again."}`` on login, resend and the owner-claim challenge, and E-R1's
unchanged acknowledgement on reset initiation. In every case it writes nothing.

Ledger rows standing for earlier issuances are inserted directly, with the
``created_at`` the case needs. That is the only way to place a row hours in the past,
and it is exactly what the decision reads. Everything else a case needs before it
(earlier codes, earlier lookups) is produced by real requests.

THIS MODULE LOADS ON A TREE WITHOUT THE LIMITS (``02075f2``), so that each REGRESSION
can be seen failing there on its own assertion. It imports nothing new at module level:
the reviewed key derivations are restated below, and the few cases about the module's
internals (``UNIT``) skip where it does not exist.
"""
import datetime
import hashlib
import hmac
import ipaddress
import json
import re
import unittest
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase, override_settings
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

# The reviewed derivations, restated rather than imported (the E-R3 convention), so a
# contract test can compare production with an independent statement of them.
_LOCK_KEY_DOMAIN = b'dinify.abuse-limit-lock.v1\x00'
_CLIENT_KEY_DOMAIN = b'dinify:abuse-limits:client:v1\x00'


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


def seed(count, *, subject=None, destination=None, age=datetime.timedelta(minutes=1)):
    """``count`` finalized ledger rows standing for earlier issuances, ``age`` old."""
    created = timezone.now() - age
    OtpIssuance.objects.bulk_create([
        OtpIssuance(
            id=uuid.uuid4(), subject_key=subject or f'u:{uuid.uuid4()}',
            destination_key=destination, origin=otp_accounting.ORIGIN_PASSWORD_LOGIN,
            state=otp_accounting.STATE_ACCEPTED, created_at=created, finalized_at=created,
        )
        for _ in range(count)
    ])


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

    def assertNothingWrittenBy(self, action):
        """Runs ``action`` and asserts it issued, deleted, recorded and sent nothing."""
        counter_model = _model('AbuseCounter')

        def counters():
            if counter_model is None:
                return []
            return list(counter_model.objects.order_by('pk').values_list('pk', 'hits'))

        challenges = sorted(map(str, UserOtp.objects.values_list('pk', flat=True)))
        ledger = OtpIssuance.objects.count()
        before = counters()
        sends = self.sms.call_count
        result = action()
        self.assertEqual(sorted(map(str, UserOtp.objects.values_list('pk', flat=True))),
                         challenges, 'a refusal deleted or wrote a challenge')
        self.assertEqual(OtpIssuance.objects.count(), ledger, 'a refusal wrote a ledger row')
        self.assertEqual(counters(), before, 'a refusal was counted')
        self.assertEqual(self.sms.call_count, sends, 'a refusal sent a code')
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — per subject
# ═══════════════════════════════════════════════════════════════════════════════

class SubjectLimitTests(_LimitCase):

    def test_REGRESSION_a_fourth_login_code_in_15_minutes_is_refused(self):
        owner = _owner('l1-login')
        for _ in range(3):
            self.assertAdmitted(self.login(owner))
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.login(owner)))

    def test_REGRESSION_the_budget_spans_purposes_and_routes(self):
        owner = _owner('l1-purposes')
        self.assertAdmitted(self.reset(owner))
        self.assertAdmitted(self.login(owner))
        self.assertAdmitted(self.resend_reset(owner))
        self.assertRefused(self.login(owner))
        self.assertRefused(self.resend_reset(owner))

    def test_REGRESSION_ten_in_24_hours(self):
        owner = _owner('l1-day')
        seed(10, subject=subject_of(owner), age=datetime.timedelta(hours=2))
        self.assertRefused(self.login(owner))

    def test_CONTROL_nine_in_24_hours_still_admits(self):
        owner = _owner('l1-day-control')
        seed(9, subject=subject_of(owner), age=datetime.timedelta(hours=2))
        self.assertAdmitted(self.login(owner))

    def test_CONTROL_rows_older_than_the_short_window_do_not_count_there(self):
        owner = _owner('l1-aged')
        seed(3, subject=subject_of(owner), age=datetime.timedelta(minutes=16))
        self.assertAdmitted(self.login(owner))

    def test_CONTROL_rows_older_than_24_hours_do_not_count(self):
        owner = _owner('l1-old')
        seed(10, subject=subject_of(owner), age=datetime.timedelta(hours=25))
        self.assertAdmitted(self.login(owner))

    def test_CONTROL_another_account_is_unaffected(self):
        owner, other = _owner('l1-a'), _owner('l1-b')
        seed(3, subject=subject_of(owner))          # the first account's budget is spent
        self.assertAdmitted(self.login(other))

    def test_REGRESSION_a_phone_with_no_account_is_its_own_subject(self):
        phone = _phone()
        seed(3, subject=phone_key(phone))
        self.assertRefused(self.register_code(phone))

    def test_CONTROL_the_admitted_code_is_still_1234_under_dev(self):
        """ENV=dev (the suite's default) is unchanged: the limits count, the code is fixed."""
        owner = _owner('l1-dev')
        self.assertAdmitted(self.login(owner))
        cache.clear()
        verified = self.client.post(VERIFY, {'user': str(owner.pk), 'otp': DEV_OTP},
                                    format='json')
        self.assertTrue(verified.json()['data']['valid'], verified.content)


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — per destination phone, whoever asks
# ═══════════════════════════════════════════════════════════════════════════════

class DestinationLimitTests(_LimitCase):

    def test_REGRESSION_a_phone_receives_at_most_three_codes_whoever_asks(self):
        # Three sign-up codes to a number with no account (subject: the phone)...
        phone = _phone()
        for _ in range(3):
            self.assertAdmitted(self.register_code(phone))
        # ...then the account is created and its owner signs in (subject: the account,
        # which has no issuance of its own). The DESTINATION is what refuses.
        owner = _owner('l2-after-signup')
        User.objects.filter(pk=owner.pk).update(phone_number=phone, username=phone)
        owner.refresh_from_db()
        self.assertEqual(OtpIssuance.objects.filter(subject_key=subject_of(owner)).count(), 0)
        self.assertRefused(self.login(owner))

    def test_REGRESSION_ten_in_24_hours(self):
        owner = _owner('l2-day')
        seed(10, destination=phone_key(owner.phone_number), age=datetime.timedelta(hours=3))
        self.assertRefused(self.resend_reset(owner))

    def test_CONTROL_another_phone_is_unaffected(self):
        owner, other = _owner('l2-a'), _owner('l2-b')
        seed(3, destination=phone_key(owner.phone_number))   # the first phone's is spent
        self.assertAdmitted(self.login(other))


# ═══════════════════════════════════════════════════════════════════════════════
# L3 — per client IP
# ═══════════════════════════════════════════════════════════════════════════════

class ClientLimitTests(_LimitCase):

    def test_REGRESSION_one_client_issues_at_most_twenty_codes_an_hour(self):
        owners = [_owner(f'l3-{n}') for n in range(5)]
        for owner in owners:                       # 5 logins
            self.assertAdmitted(self.login(owner))
        for owner in owners:                       # 5 resets
            self.assertEqual(self.reset(owner)[0].status_code, 200)
        for _ in range(10):                        # 10 sign-up codes
            self.assertAdmitted(self.register_code(_phone()))
        self.assertEqual(OtpIssuance.objects.count(), 20)
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.register_code(_phone())))
        self.assertRefused(self.login(_owner('l3-fresh')))

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
# L4 — global, and its alerts
# ═══════════════════════════════════════════════════════════════════════════════

ALERT_EMAIL = 'ops-alerts@example.test'


@override_settings(OTP_SPEND_ALERT_EMAIL=ALERT_EMAIL)
class GlobalLimitTests(_LimitCase):

    def alert_mails(self):
        return [c for c in self.email.call_args_list if c.kwargs.get('to') == [ALERT_EMAIL]]

    def test_REGRESSION_the_300th_code_in_24_hours_is_the_last(self):
        seed(299, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))
        self.assertNothingWrittenBy(lambda: self.assertRefused(self.register_code(_phone())))
        self.assertRefused(self.login(_owner('l4-login')))

    def test_REGRESSION_the_cap_alert_is_sent_once(self):
        seed(300, age=datetime.timedelta(hours=1))
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertEqual(
            [line for line in logs.output if 'OTP_SPEND_ALERT ' in line],
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

    @needs_enforcement
    def test_UNIT_the_cap_alert_is_sent_again_after_24_hours(self):
        seed(300, age=datetime.timedelta(hours=1))
        _model('OtpSpendAlert').objects.create(
            alert='cap', last_sent_at=timezone.now() - datetime.timedelta(hours=25))
        self.assertRefused(self.register_code(_phone()))
        self.assertEqual(len(self.alert_mails()), 1)

    def test_REGRESSION_the_warning_is_sent_once_at_80_percent(self):
        seed(239, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))          # the 240th
        self.assertEqual(len(self.alert_mails()), 1)
        self.assertIn('80%', self.alert_mails()[0].kwargs['subject'])
        otp_limits._ALERT_GATE.clear()
        self.assertAdmitted(self.register_code(_phone()))          # the 241st
        self.assertEqual(len(self.alert_mails()), 1)

    def test_CONTROL_no_warning_below_80_percent(self):
        seed(238, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.register_code(_phone()))          # the 239th
        self.assertEqual(self.alert_mails(), [])
        if _model('OtpSpendAlert') is not None:
            self.assertFalse(_model('OtpSpendAlert').objects.exists())

    @override_settings(OTP_SPEND_ALERT_EMAIL='')
    def test_REGRESSION_with_no_address_the_line_is_still_written_and_nothing_is_mailed(self):
        seed(300, age=datetime.timedelta(hours=1))
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertIn('WARNING:users_app.otp_limits:OTP_SPEND_ALERT threshold=cap issued=300 '
                      'cap=300 email=not_configured', logs.output)
        self.assertEqual(self.email.call_count, 0)

    def test_REGRESSION_a_failed_send_is_logged_with_its_marker(self):
        seed(300, age=datetime.timedelta(hours=1))
        self.email.return_value = False
        with self.assertLogs('users_app.otp_limits', level='WARNING') as logs:
            self.assertRefused(self.register_code(_phone()))
        self.assertIn('ERROR:users_app.otp_limits:OTP_SPEND_ALERT_EMAIL_FAILED threshold=cap',
                      logs.output)

    def test_CONTROL_rows_older_than_24_hours_do_not_count(self):
        seed(300, age=datetime.timedelta(hours=25))
        self.assertAdmitted(self.register_code(_phone()))

    def test_REGRESSION_the_alert_mail_names_no_person(self):
        owner = _owner('l4-pii')
        seed(299, age=datetime.timedelta(hours=1))
        self.assertAdmitted(self.login(owner))
        self.assertRefused(self.register_code(_phone()))
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
        counter_model = _model('AbuseCounter')

        def counters():
            return [] if counter_model is None else list(
                counter_model.objects.values_list('pk', 'hits'))

        before = counters()
        refused_held = self.lookup(held.phone_number)
        refused_free = self.lookup(_phone())
        for response in (refused_held, refused_free):
            self.assertEqual(response.status_code, 429, response.content)
            self.assertEqual(response.json(), REFUSAL)
            self.assertNotIn('Retry-After', response.headers)
        # The same bytes whether or not the number has an account.
        self.assertEqual(refused_held.content, refused_free.content)
        self.assertEqual(counters(), before, 'a refused lookup was counted')

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
            seed(10, subject=phone_key(phone), age=datetime.timedelta(hours=20))
        elif why == 'L2':   # someone else's codes to this phone
            seed(3, destination=phone_key(phone))
        elif why == 'L3':   # this client's hour
            address = OTHER_ADDRESS
            for _ in range(20):
                self.assertAdmitted(self.register_code(_phone(), address=address))
        elif why == 'L4':
            seed(300, age=datetime.timedelta(hours=1))
        before = set(UserOtp.objects.values_list('pk', flat=True))
        body = {'identification': 'msisdn', 'identifier': phone, 'purpose': 'register'}
        with CaptureQueriesContext(connection) as queries:     # the request, and only it
            response = self.post(RESEND, body, address=address)
        new = list(UserOtp.objects.exclude(pk__in=before).values_list('pk', flat=True))
        self.assertRefused((response, new))
        return response, [_shape(q['sql']) for q in queries.captured_queries]

    def test_REGRESSION_every_limit_answers_the_same_bytes(self):
        bodies = {why: self.refuse(why)[0].content for why in ('L1', 'L2', 'L3', 'L4')}
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

    def test_REGRESSION_a_limited_reset_answers_like_an_unknown_account(self):
        owner = _owner('one-reset')
        seed(3, subject=subject_of(owner))
        limited, new = self.reset(owner)
        self.assertEqual(new, [], 'a refused reset wrote a challenge')
        unknown = self.post(INITIATE, {'identifier': _phone(), 'identification': 'phone'})
        self.assertEqual(limited.status_code, 200)
        self.assertEqual(limited.json(), ACKNOWLEDGEMENT)
        self.assertEqual(limited.content, unknown.content)

    def test_REGRESSION_the_owner_claim_challenge_answers_the_429(self):
        owner, token, _ = _claim(pending=True)
        seed(3, subject=subject_of(owner))
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
        seed(3, subject=subject_of(owner))
        response = self.post(LOGIN, {'username': owner.phone_number, 'password': 'wrong'})
        self.assertEqual(response.status_code, 401, response.content)


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
