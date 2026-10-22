"""
D11 E-R4 — a login resend is honoured only for the client that completed the password step.

WHAT WAS WRONG
━━━━━━━━━━━━━━

``resend-otp`` with ``purpose='login'`` was admitted whenever the account had a
password-created login challenge (``msisdn IS NULL``, the "anchor") under five minutes
old. The anchor proves that SOMEONE entered the password a moment ago. It never proved
that the requester did. Anyone who knew the phone number, or the user id (a colleague can
read an owner's id), could therefore, while the owner was signing in:

* write a newer login challenge, so the owner's correct code was refused and the owner's
  attempt was charged to the foreign row;
* after the owner had signed in, write fresh challenges, each with a fresh five-guess
  budget, and under ``ENV=dev`` (where every code is 1234) sign in as the owner;
* learn from three different answers whether an account existed and whether its owner
  had just entered the password.

Each was reproduced through the real routes on Backend 12541d5 before this change (the
private Stage A record, scenarios S02, S03, S04 and S05).

THE CONTRACT
━━━━━━━━━━━━

* A LOGIN request may name ``login_resend_protocol: 'v1'``, exactly that string. It is a
  compatibility signal, not authentication. Only then, and only on the OTP branch after
  ``make_otp`` committed its challenge and reported success, the response carries
  ``login_resend_proof``: ``'v1.'`` plus the unpadded base64url HMAC-SHA256, under the
  existing OTP pepper and a domain of its own, of the user's UUID bytes and the UUID bytes
  of the challenge THIS login's transaction committed. Nothing is stored. Any other value
  of the capability, or none, gets the response a login always got.
* A ``purpose='login'`` resend, after the unchanged purpose-allowlist and presence checks,
  needs the ``id`` selector, a well-formed bounded proof, a recent anchor, a proof that
  matches that anchor (``hmac.compare_digest`` on bounded ASCII strings), and an account
  phone. Every refusal among those is ONE answer, the sentence the anchor refusal always
  used, with nothing written, deleted, recorded or sent. An admitted resend issues through
  the unchanged ``make_otp(user, purpose='login', msisdn=user.phone_number,
  origin='login_resend')``.
* Other purposes, registration, owner claim, password reset, ``ENV=dev``'s 1234, expiry,
  the attempt cap, accounting and every E-R1/E-R2/E-R3 rule are unchanged.

WHAT THIS DOES NOT PROMISE
━━━━━━━━━━━━━━━━━━━━━━━━━━

Pinned where it can be measured (the case named in brackets) and stated otherwise:

* The check happens at admission and is a plain read. A resend already past it still
  issues if a newer login replaces the anchor or the window closes meanwhile; the code
  goes to the same account phone [C2].
* A retry with the same proof is a NEW issuance, not a repeat delivery: it replaces the
  earlier resend's row, whose code then stops working [S06, DeliveryFailure].
* While a resend row is live, the anchor's own code is shadowed at verification, because
  the newest live login row is the one selected and charged. It can become selectable
  again once that row is spent [S06].
* The proof ignores consumption, as the anchor rule always did: its holder may resend
  after signing in, until the anchor is replaced or ages out [S15].
* One status and body for every refusal is not one timing. It says nothing about a 429:
  the throttles count refused requests too, so foreign traffic can use up the
  per-identifier budget in successive windows, and no upper bound on how long is claimed.
* Verification takes a row lock on the newest live login challenge, and a resend's
  replacement DELETE can lock that same row, so the two can wait on each other. This
  change alters neither path's locking, so no new schedule is added for it.
* ``ENV=dev``'s 1234 against a LIVE anchor, and a login body naming ``source='diner'``,
  are untouched and are recorded residuals, not addressed here [S03c].

HOW THESE TESTS DISCRIMINATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Two clients with their own ``APIClient`` and address: the initiator (A) uses only its own
login response, and a foreign client (B) uses only what it can learn without it. Every
step is real HTTP through ``UsersAuthenticationEndpoint``; selection, locking, accounting
and token minting are the real code. Only delivery is replaced (SMS, e-mail and
notification are recorders; the dev dispatch thread runs inline). Under ``ENV=test`` the
codes are fixed and distinct; under ``ENV=dev`` the oracle is row identity. A refusal is
asserted together with the absence of every effect: no challenge row written, changed or
deleted, no accounting row, no send. A success is asserted as a principal (the tokens are
decoded and must name the intended account). S14 is the case that discriminates a proof
check that does not verify the MAC: there the anchor is LIVE, so only the proof stands
between B and an issuance.
"""
import ast
import base64
import contextlib
import contextvars
import datetime
import hashlib
import hmac
import inspect
import logging
import os
import re
import textwrap
import threading
import uuid
from unittest import mock

from django.core.cache import cache
from django.db import DatabaseError
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from dinify_backend.configss.messages import MESSAGES
from dinify_backend.configss.string_definitions import (
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM, RESTAURANT_MANAGER, RESTAURANT_OWNER,
    RestaurantStatus_Live,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app import customer_access, otp_accounting
from users_app.controllers import otp_manager
from users_app.controllers.otp_manager import (
    LOGIN_RESEND_PROTOCOL, LOGIN_RESEND_REFUSAL, OtpManager, login_resend_proof,
    wants_login_resend_proof,
)
from users_app.models import OtpIssuance, OtpVerificationFailure, User, UserOtp
from users_app.tests_challenge_issuance_serialization import (
    INVALID_OTP, WAIT, _LoginBucket, bucket_lock,
)
from users_app.tests_challenge_replacement import (
    LOGIN as LOGIN_URL, PASSWORD as PASSWORD_ER3,
)

LOGIN = '/api/v1/users/auth/login/'
RESEND = '/api/v1/users/auth/resend-otp/'
VERIFY = '/api/v1/users/auth/verify-otp/'
DETAILS = '/api/v1/restaurant-setup/details/'

PROOF = 'login_resend_proof'
CAPABILITY = {'login_resend_protocol': 'v1'}
PASSWORD = 'Er4-Synthetic-Passw0rd!'
DEV_OTP = '1234'

# The answers, restated as literals rather than imported, so a change to any of them
# fails here.
REFUSAL = {'status': 400,
           'message': 'Please provide your username and password again to get a login OTP'}
SENT = {'status': 200, 'message': 'OTP sent successfully'}
SEND_FAILED = {'status': 500,
               'message': "We couldn't send your verification code. Please try again."}
INVALID = {'status': 200, 'message': 'Invalid OTP', 'data': {'valid': False}}
LEGACY_OTP_KEYS = {'require_otp', 'prompt_password_change', 'user_id', 'profile'}

# The proof's derivation, restated independently of the production code so the contract
# test below compares the two rather than the code with itself.
SPEC_DOMAIN = b'dinify.login-resend-proof.v1\x00'
PROOF_SHAPE = re.compile(r'^v1\.[A-Za-z0-9_-]{43}$')


def spec_proof(user_pk, anchor_pk):
    mac = hmac.new(
        otp_manager._otp_pepper(),
        SPEC_DOMAIN + uuid.UUID(str(user_pk)).bytes + uuid.UUID(str(anchor_pk)).bytes,
        hashlib.sha256,
    ).digest()
    return 'v1.' + base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')


# A synthetic phone range no other suite uses. Never dialled: every sender is a recorder.
_PHONES = iter(f'2567726{n:05d}' for n in range(10000, 99999))
_ACTOR = contextvars.ContextVar('er4_actor', default='fixture')


def _phone():
    return next(_PHONES)


def _account(tag, **extra):
    phone = extra.pop('phone_number', None) or _phone()
    # prompt_password_change=False: the supported login screen reaches the code entry
    # (and its Resend button) only with False. create_user's default is True.
    extra.setdefault('prompt_password_change', False)
    return User.objects.create_user(
        first_name='Er', last_name=tag.title(), email=f'er4-{tag}-{phone}@example.test',
        phone_number=phone, username=phone, country='UG', password=PASSWORD, roles=[],
        **extra,
    )


def _owner(tag):
    """An owner of a live restaurant: a password login requires, and writes, a login OTP."""
    user = _account(tag)
    restaurant = Restaurant.objects.create(
        name=f'Er4 House {user.phone_number}', location='Synthetic',
        status=RestaurantStatus_Live, owner=user,
    )
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
    )
    return user, restaurant


def _manager_of(restaurant, tag='colleague'):
    user = _account(tag)
    RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=[RESTAURANT_MANAGER], active=True,
    )
    return user


class _Codes:
    """Stands in for ``secrets`` inside ``otp_manager``: the digit draw only."""

    def __init__(self, *codes):
        self._codes = iter(codes)

    def randbelow(self, n):
        return int(next(self._codes)) - 1000

    @staticmethod
    def token_hex(nbytes=None):
        import secrets
        return secrets.token_hex(nbytes)


@contextlib.contextmanager
def _env(name, *codes):
    """``ENV=test`` draws the listed codes; ``ENV=dev`` keeps its 1234."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, {'ENV': name}))
        if codes:
            stack.enter_context(mock.patch.object(otp_manager, 'secrets', _Codes(*codes)))
        yield


class _InlineThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


class _Client:
    """One client context: its own APIClient and address, nothing shared."""

    def __init__(self, label, ip):
        self.label = label
        self.api = APIClient(raise_request_exception=False, REMOTE_ADDR=ip)

    def post(self, url, body, bearer=None):
        cache.clear()   # the throttles are not under test here; clearing is symmetric
        token = _ACTOR.set(self.label)
        try:
            headers = {'HTTP_AUTHORIZATION': f'Bearer {bearer}'} if bearer else {}
            return self.api.post(url, body, format='json', **headers)
        finally:
            _ACTOR.reset(token)

    def get(self, url, params, bearer):
        cache.clear()
        return self.api.get(url, params, HTTP_AUTHORIZATION=f'Bearer {bearer}')


class _Case(TestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.rows = []    # (actor, pk) for every challenge row written
        self.sends = []   # (actor, destination, code) for every SMS attempted
        self.sms_ok = True
        self.email_ok = True

        def created(sender, instance, created, **kwargs):
            if created:
                self.rows.append((_ACTOR.get(), str(instance.pk)))

        uid = f'er4-rows-{id(self)}'
        post_save.connect(created, sender=UserOtp, weak=False, dispatch_uid=uid)
        self.addCleanup(post_save.disconnect, sender=UserOtp, dispatch_uid=uid)

        def sms(message, msisdn, **kwargs):
            self.sends.append((_ACTOR.get(), msisdn, re.search(r'(\d{4})', message).group(1)))
            return self.sms_ok

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(otp_manager, 'send_sms', side_effect=sms))
        stack.enter_context(mock.patch(
            'notifications_app.controllers.messenger.Messenger.send_email',
            side_effect=lambda *a, **k: self.email_ok))
        stack.enter_context(mock.patch(
            'misc_app.controllers.notifications.notification.Notification.create_notification',
            return_value=None))
        stack.enter_context(mock.patch.object(otp_manager, 'threading',
                                              mock.Mock(Thread=_InlineThread)))

        self.user, self.restaurant = _owner('owner')
        self.A = _Client('A:initiator', '192.0.2.10')
        self.B = _Client('B:foreign', '198.51.100.20')

    # --- requests ---

    def login(self, client=None, user=None, extra=CAPABILITY):
        """A password login; returns ``(proof or None, the challenge row it wrote)``."""
        client, user = client or self.A, user or self.user
        before = len(self.rows)
        response = client.post(LOGIN, {'username': user.phone_number, 'password': PASSWORD,
                                       **extra})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertTrue(data['require_otp'], data)
        written = [pk for _, pk in self.rows[before:]]
        self.assertEqual(len(written), 1, 'a password login writes exactly one challenge')
        return data.get(PROOF), written[0]

    def resend(self, client, proof=None, user_id=None, bearer=None, **extra):
        body = {'identification': 'id', 'identifier': str(user_id or self.user.pk),
                'purpose': 'login', **extra}
        if proof is not None:
            body[PROOF] = proof
        return client.post(RESEND, body, bearer=bearer)

    def verify(self, client, code, user_id=None):
        return client.post(VERIFY, {'user': str(user_id or self.user.pk), 'otp': code})

    # --- state ---

    def written_since(self, n):
        return [pk for _, pk in self.rows[n:]]

    @staticmethod
    def row(pk):
        return UserOtp.objects.filter(pk=pk).first()

    def effects(self):
        """Everything a refused resend must leave exactly as it was."""
        return (
            sorted((str(r.pk), r.user_id, r.msisdn, r.purpose, r.consumed_at, r.attempts,
                    r.time_created) for r in UserOtp.objects.all()),
            sorted((str(i.pk), i.state) for i in OtpIssuance.objects.all()),
            OtpVerificationFailure.objects.count(),
            len(self.sends),
        )

    # --- assertions ---

    def assertRefusedWithNoEffect(self, call, msg=None):
        before = self.effects()
        response = call()
        self.assertEqual((response.status_code, response.json()), (400, REFUSAL), msg)
        self.assertEqual(self.effects(), before,
                         msg or 'a refused login resend wrote, deleted, recorded or sent')
        return response

    def assertSent(self, response):
        self.assertEqual((response.status_code, response.json()), (200, SENT), response.content)

    def assertSignedIn(self, response, user=None):
        user = user or self.user
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        self.assertEqual(set(data), {'valid', 'token', 'refresh'}, data)
        self.assertEqual(str(AccessToken(data['token'])['user_id']), str(user.pk))
        self.assertEqual(str(RefreshToken(data['refresh'])['user_id']), str(user.pk))

    def assertNotSignedIn(self, response):
        self.assertEqual((response.status_code, response.json()), (200, INVALID))


# ═══════════════════════════════════════════════════════════════════════════════
# The proof and the capability, as values
# ═══════════════════════════════════════════════════════════════════════════════

class ProofContractTests(SimpleTestCase):
    """Pure: the derivation, its shape and the capability's one recognised value."""

    USER = uuid.UUID('00000000-0000-4000-8000-0000000000a1')
    ANCHOR = uuid.UUID('00000000-0000-4000-8000-0000000000b2')

    def test_the_proof_matches_an_independent_statement_of_the_derivation(self):
        for user, anchor in ((self.USER, self.ANCHOR), (uuid.uuid4(), uuid.uuid4())):
            proof = login_resend_proof(user, anchor)
            self.assertEqual(proof, spec_proof(user, anchor))
            self.assertRegex(proof, PROOF_SHAPE)

    def test_the_proof_binds_both_ids_and_not_their_spelling(self):
        proof = login_resend_proof(self.USER, self.ANCHOR)
        self.assertNotEqual(proof, login_resend_proof(self.ANCHOR, self.USER))
        self.assertNotEqual(proof, login_resend_proof(self.USER, uuid.uuid4()))
        self.assertNotEqual(proof, login_resend_proof(uuid.uuid4(), self.ANCHOR))
        self.assertEqual(proof, login_resend_proof(str(self.USER), str(self.ANCHOR).upper()))

    def test_only_the_exact_capability_string_is_recognised(self):
        self.assertEqual(LOGIN_RESEND_PROTOCOL, 'v1')
        self.assertTrue(wants_login_resend_proof('v1'))
        for value in ('V1', 'v2', ' v1', 'v1 ', '', 'v1.0', 'v1\x00', None, 1, 1.0, True,
                      ['v1'], {'v1': True}, b'v1'):
            with self.subTest(value=value):
                self.assertFalse(wants_login_resend_proof(value))

    def test_the_refusal_is_the_sentence_the_anchor_refusal_always_used(self):
        self.assertEqual(LOGIN_RESEND_REFUSAL, REFUSAL)

    def test_the_comparison_is_constant_time(self):
        """
        No behavioural test can tell ``hmac.compare_digest`` from ``==``: both give the
        same answers. They are not equivalent for a secret, so the source is pinned: the
        proof is compared through ``hmac.compare_digest`` and through no ``==``, ``!=``,
        ``in`` or ``not in`` anywhere in the resend decision.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(OtpManager._resend_login)))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'compare_digest'
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == 'hmac']
        self.assertEqual(len(calls), 1)
        equality = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)
                    and any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn))
                            for op in node.ops)]
        self.assertEqual(
            [ast.unparse(node) for node in equality], ["identification != 'id'"],
            'the only equality test in the decision is the selector, never the proof')


# ═══════════════════════════════════════════════════════════════════════════════
# The login response: who is given a proof, and which row it names
# ═══════════════════════════════════════════════════════════════════════════════

class LoginCapabilityTests(_Case):

    def test_the_exact_capability_returns_a_proof_for_the_row_this_login_committed(self):
        with _env('test', '6101'):
            response = self.A.post(LOGIN, {'username': self.user.phone_number,
                                           'password': PASSWORD, **CAPABILITY})
        data = response.json()['data']
        self.assertEqual(set(data), LEGACY_OTP_KEYS | {PROOF})
        anchor = UserOtp.objects.get(user=self.user, purpose='login', msisdn=None)
        self.assertEqual(data[PROOF], spec_proof(self.user.pk, anchor.pk))
        self.assertEqual(OtpIssuance.objects.get(pk=anchor.pk).origin, 'password_login')

    def test_without_the_capability_the_response_is_the_one_a_login_always_got(self):
        with _env('test', '6201'):
            proof, anchor = self.login(extra={})
            self.assertIsNone(proof)
            self.assertSignedIn(self.verify(self.A, '6201'))
        self.assertEqual([(a, d) for a, d, _ in self.sends], [('A:initiator', self.user.phone_number)])

    def test_an_unrecognised_capability_is_ignored_not_refused(self):
        for value in ('V1', 'v2', ' v1', 'v1 ', '', 'v1.0', None, 1, True, ['v1'], {'v': 'v1'}):
            with self.subTest(value=value), _env('dev'):
                response = self.A.post(LOGIN, {'username': self.user.phone_number,
                                               'password': PASSWORD,
                                               'login_resend_protocol': value})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(set(response.json()['data']), LEGACY_OTP_KEYS)

    def test_the_token_branch_never_carries_a_proof(self):
        diner = _account('diner')   # no restaurant role: no OTP step
        with _env('dev'):
            response = self.A.post(LOGIN, {'username': diner.phone_number,
                                           'password': PASSWORD, **CAPABILITY})
        data = response.json()['data']
        self.assertFalse(data['require_otp'])
        self.assertNotIn(PROOF, data)
        self.assertEqual(str(AccessToken(data['token'])['user_id']), str(diner.pk))
        self.assertEqual(self.rows, [])

    def test_the_diner_branch_never_carries_a_proof(self):
        # RESIDUAL, NOT ADDRESSED HERE: `source='diner'` skips the OTP step for an owner.
        # This case pins only that no proof is disclosed on that branch.
        with _env('dev'):
            response = self.A.post(LOGIN, {'username': self.user.phone_number,
                                           'password': PASSWORD, 'source': 'diner',
                                           **CAPABILITY})
        data = response.json()['data']
        self.assertFalse(data['require_otp'])
        self.assertNotIn(PROOF, data)
        self.assertEqual(self.rows, [])

    def test_a_failed_login_never_carries_a_proof(self):
        wrong = {'status': 401, 'message': MESSAGES.get('WRONG_PASSWORD')}
        with _env('dev'):
            for username, password in ((self.user.phone_number, 'not-the-password'),
                                       (_phone(), PASSWORD)):
                response = self.A.post(LOGIN, {'username': username, 'password': password,
                                               **CAPABILITY})
                self.assertEqual((response.status_code, response.json()), (401, wrong))
        self.assertEqual(self.rows, [])

    def test_an_undelivered_login_code_carries_no_proof_and_a_new_login_recovers(self):
        self.sms_ok = self.email_ok = False
        with _env('test', '6301', '6302', '6303'):
            response = self.A.post(LOGIN, {'username': self.user.phone_number,
                                           'password': PASSWORD, **CAPABILITY})
            self.assertEqual((response.status_code, response.json()), (500, SEND_FAILED))
            self.sms_ok = self.email_ok = True
            proof, anchor = self.login()
            self.assertEqual(proof, spec_proof(self.user.pk, anchor))
            self.assertSent(self.resend(self.A, proof))
            self.assertSignedIn(self.verify(self.A, '6303'))

    def test_the_proof_names_the_committed_row_even_if_a_newer_one_exists_by_delivery(self):
        """
        Another password login for the same account commits a newer anchor while THIS
        login's code is being delivered. The proof names this login's own row, which the
        newer login has replaced, so it admits nothing. A proof derived from whichever
        anchor is newest after delivery would name the other client's row and be admitted.
        """
        interloper = {}

        def during_delivery(message, msisdn, **kwargs):
            self.sends.append(('A:initiator', msisdn, message[-5:-1]))
            if not interloper:
                # Marked BEFORE the nested issuance, whose own delivery re-enters here.
                interloper['started'] = True
                manager = OtpManager()
                interloper['made'] = manager.make_otp(
                    user=self.user, purpose='login', origin='password_login')
                interloper['pk'] = manager.issued_otp_id
            return True

        with _env('test', '6401', '6402'), \
                mock.patch.object(otp_manager, 'send_sms', side_effect=during_delivery):
            response = self.A.post(LOGIN, {'username': self.user.phone_number,
                                           'password': PASSWORD, **CAPABILITY})
        proof = response.json()['data'][PROOF]
        mine = self.rows[0][1]
        self.assertTrue(interloper['made'])
        self.assertNotEqual(str(interloper['pk']), mine)
        self.assertIsNone(self.row(mine), 'the newer login replaced this login\'s row')
        self.assertEqual(proof, spec_proof(self.user.pk, mine))
        self.assertNotEqual(proof, spec_proof(self.user.pk, interloper['pk']))
        with _env('test'):
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A, proof))


class IssuedChallengeIdTests(_Case):
    """``issued_otp_id`` names only the row the call's own transaction committed."""

    def test_it_is_cleared_before_every_issuance_attempt(self):
        manager = OtpManager()
        with _env('dev'):
            self.assertTrue(manager.make_otp(user=self.user, purpose='login'))
            first = manager.issued_otp_id
            self.assertIsInstance(first, uuid.UUID)
            self.assertEqual(str(first), self.rows[-1][1])

            pending = _account('pending', customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
            self.assertFalse(manager.make_otp(user=pending, purpose='login'))
            self.assertIsNone(manager.issued_otp_id, 'a refused issuance kept an old id')

            self.assertTrue(manager.make_otp(user=self.user, purpose='login'))
            self.assertIsInstance(manager.issued_otp_id, uuid.UUID)
            with mock.patch.object(otp_accounting, 'record_issuance',
                                   side_effect=DatabaseError('synthetic')):
                self.assertFalse(manager.make_otp(user=self.user, purpose='login'))
            self.assertIsNone(manager.issued_otp_id, 'a rolled-back issuance left an id')


# ═══════════════════════════════════════════════════════════════════════════════
# The resend, two clients
# ═══════════════════════════════════════════════════════════════════════════════

class S01InitiatorTests(_Case):
    """CONTROL. The initiator's own resend issues, and its code signs the initiator in."""

    def test_the_initiator_resends_and_signs_in_with_the_latest_code(self):
        with _env('test', '7101', '7102'):
            proof, anchor = self.login()
            before = len(self.rows)
            self.assertSent(self.resend(self.A, proof))
            resent = self.written_since(before)
            self.assertEqual(len(resent), 1)
            row = self.row(resent[0])
            self.assertEqual((row.user_id, row.msisdn, row.purpose),
                             (self.user.pk, self.user.phone_number, 'login'))
            ledger = OtpIssuance.objects.get(pk=resent[0])
            self.assertEqual((ledger.origin, ledger.state), ('login_resend', 'accepted'))
            self.assertSignedIn(self.verify(self.A, '7102'))
        self.assertIsNotNone(self.row(resent[0]).consumed_at)
        self.assertIsNone(self.row(anchor).consumed_at, 'the anchor is another bucket (E-R2)')
        self.assertEqual([(a, d) for a, d, _ in self.sends],
                         [('A:initiator', self.user.phone_number)] * 2)


class S02ForeignPhoneOnlyTests(_Case):
    """B knows the phone number and never sees A's login response."""

    def test_a_phone_or_msisdn_resend_is_refused_and_the_initiators_code_still_works(self):
        with _env('test', '7201', '7202'):
            proof, anchor = self.login()
            for selector in ('phone', 'msisdn'):
                self.assertRefusedWithNoEffect(lambda: self.B.post(RESEND, {
                    'identification': selector, 'identifier': self.user.phone_number,
                    'purpose': 'login'}), selector)
            self.assertSignedIn(self.verify(self.A, '7201'))
        self.assertIsNotNone(self.row(anchor).consumed_at)
        self.assertEqual([a for a, _, _ in self.sends], ['A:initiator'])


class S03ColleagueAfterSignInTests(_Case):
    """
    ``ENV=dev``. A has signed in, so A's anchor is consumed. B is a manager at the same
    restaurant and reads the owner's user id from a route a manager may read.
    """

    def test_a_colleague_cannot_obtain_an_owner_session_through_a_resend(self):
        colleague = _manager_of(self.restaurant)
        bearer = str(customer_access.issue_customer_tokens(colleague).access_token)
        with _env('dev'):
            proof, anchor = self.login()
            self.assertSignedIn(self.verify(self.A, DEV_OTP))
            details = self.B.get(DETAILS, {'record': 'restaurants',
                                           'id': str(self.restaurant.pk)}, bearer)
            self.assertEqual(details.status_code, 200, details.content)
            self.assertIn(str(self.user.pk), details.content.decode())
            self.assertRefusedWithNoEffect(lambda: self.B.post(RESEND, {
                'identification': 'id', 'identifier': str(self.user.pk), 'purpose': 'login',
                'skip_auth': 'yes'}))
            self.assertNotSignedIn(self.verify(self.B, DEV_OTP))
        self.assertEqual(OtpVerificationFailure.objects.count(), 0)


class S03cDevCodeAgainstALiveAnchorTests(_Case):
    """
    CONTROL, OUTSIDE THIS CHANGE. Under ``ENV=dev`` every code is 1234 by design, so whoever
    knows the user id can verify against a LIVE anchor before its owner does. E-R4 binds
    the RESEND; it does not bind verification and does not change the intentional code.
    """

    def test_dev_1234_against_a_live_anchor_is_unchanged(self):
        with _env('dev'):
            self.login()
            self.assertSignedIn(self.verify(self.B, DEV_OTP))


class S04GuessBudgetTests(_Case):
    """``ENV=test``. A has signed in. B knows the user id but not the code."""

    def test_a_foreign_client_cannot_create_a_fresh_guessing_budget(self):
        with _env('test', '7401'):
            proof, anchor = self.login()
            self.assertSignedIn(self.verify(self.A, '7401'))
            for _ in range(2):
                self.assertRefusedWithNoEffect(lambda: self.resend(self.B))
                for guess in ('9990', '9991', '9992', '9993', '9994', '9995'):
                    self.assertNotSignedIn(self.verify(self.B, guess))
        self.assertEqual(OtpVerificationFailure.objects.count(), 0,
                         'no live login challenge, so nothing to charge')


class S05OneAnswerTests(_Case):
    """Every login-purpose refusal is one answer; other purposes keep their own."""

    def test_existence_and_recent_password_use_are_not_disclosed(self):
        stranger = _phone()
        calls = {
            'phone, before any login': {'identification': 'phone',
                                        'identifier': self.user.phone_number},
            'unknown phone': {'identification': 'phone', 'identifier': stranger},
            'unknown msisdn': {'identification': 'msisdn', 'identifier': stranger},
            'unparseable msisdn': {'identification': 'msisdn', 'identifier': '12'},
            'unknown email': {'identification': 'email', 'identifier': 'nobody@example.test'},
            'known email': {'identification': 'email', 'identifier': self.user.email},
            'unknown id': {'identification': 'id', 'identifier': str(uuid.uuid4())},
        }
        with _env('test', '7501'):
            answers = {}
            for name, body in calls.items():
                answers[name] = self.assertRefusedWithNoEffect(
                    lambda body=body: self.B.post(RESEND, {**body, 'purpose': 'login'}), name)
            proof, anchor = self.login()
            for name, body in list(calls.items()) + [
                    ('id, no proof, right after the password',
                     {'identification': 'id', 'identifier': str(self.user.pk)})]:
                answers[f'{name} (after login)'] = self.assertRefusedWithNoEffect(
                    lambda body=body: self.B.post(RESEND, {**body, 'purpose': 'login'}), name)
        self.assertEqual({(r.status_code, r.content) for r in answers.values()},
                         {(400, answers['unknown id'].content)})

    def test_control_other_purposes_keep_their_answers(self):
        with _env('dev'):
            reset = self.B.post(RESEND, {'identification': 'phone', 'identifier': _phone(),
                                         'purpose': 'reset-password'})
            self.assertEqual((reset.status_code, reset.json()),
                             (400, {'status': 400, 'message': 'User not found'}))
            self.assertSent(self.B.post(RESEND, {'identification': 'id',
                                                 'identifier': str(self.user.pk),
                                                 'purpose': 'register'}))
            self.assertSent(self.B.post(RESEND, {'identification': 'msisdn',
                                                 'identifier': _phone()}))


class S06RepeatedGenuineResendTests(_Case):
    """
    CONTROL. Repeated resends with one proof are new issuances that replace each other;
    the anchor is never refreshed. While a resend row is live it shadows the anchor's own
    code, which can be selected again once that row is spent.
    """

    def test_three_resends_with_one_proof(self):
        with _env('test', '7601', '7602', '7603', '7604'):
            proof, anchor = self.login()
            anchored_at = self.row(anchor).time_created
            resent = []
            for _ in range(3):
                before = len(self.rows)
                self.assertSent(self.resend(self.A, proof))
                resent += self.written_since(before)
            self.assertIsNone(self.row(resent[0]), 'a same-proof retry replaced the earlier resend')
            self.assertIsNone(self.row(resent[1]))
            self.assertEqual(self.row(anchor).time_created, anchored_at)
            self.assertEqual(
                [OtpIssuance.objects.get(pk=pk).origin for pk in resent],
                ['login_resend'] * 3)

            # The anchor's code is shadowed while the newest resend row is live: the
            # newest row is selected, and the attempt is charged to it.
            self.assertNotSignedIn(self.verify(self.A, '7601'))
            self.assertEqual(self.row(resent[2]).attempts, 1)
            self.assertEqual(self.row(anchor).attempts, 0)
            self.assertSignedIn(self.verify(self.A, '7604'))
            # RESIDUAL, UNCHANGED: once that row is spent, the anchor's code is selectable.
            self.assertSignedIn(self.verify(self.A, '7601'))


class S07AnchorWindowTests(_Case):
    """CONTROL. No anchor, or one past five minutes, refuses; four minutes admits."""

    def _age(self, pk, minutes):
        UserOtp.objects.filter(pk=pk).update(
            time_created=timezone.now() - datetime.timedelta(minutes=minutes))

    def test_missing_then_fresh_then_expired(self):
        with _env('test', '7701', '7702'):
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A, 'v1.' + 'A' * 43))
            proof, anchor = self.login()
            self._age(anchor, 4)
            self.assertSent(self.resend(self.A, proof))
            self._age(anchor, 6)
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A, proof))


class S08NewLoginSupersedesOldTabTests(_Case):
    """A second password login replaces the first anchor, so the first tab's proof dies."""

    def test_the_old_tab_is_refused_and_the_new_tab_works(self):
        A2 = _Client('A2:second-tab', '192.0.2.11')
        with _env('test', '7801', '7802', '7803'):
            proof1, anchor1 = self.login(self.A)
            proof2, anchor2 = self.login(A2)
            self.assertIsNone(self.row(anchor1))
            self.assertNotEqual(proof1, proof2)
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A, proof1))
            self.assertSent(self.resend(A2, proof2))
            self.assertSignedIn(self.verify(A2, '7803'))


class S09UnrelatedAccountTests(_Case):
    """A proof admits only its own account, and nothing in the body redirects delivery."""

    def test_unrelated_account_borrowed_proof_and_a_fixed_destination(self):
        other, _ = _owner('other')
        elsewhere = _phone()
        with _env('test', '7901', '7902', '7903'):
            proof, anchor = self.login()
            other_proof, other_anchor = self.login(self.B, other)
            self.assertRefusedWithNoEffect(lambda: self.resend(self.B, user_id=other.pk))
            self.assertRefusedWithNoEffect(lambda: self.resend(self.B, proof, user_id=other.pk))
            self.assertRefusedWithNoEffect(lambda: self.resend(self.B, other_proof))
            before = len(self.rows)
            self.assertSent(self.resend(self.A, proof, msisdn=elsewhere, phone_number=elsewhere,
                                        email='elsewhere@example.test'))
        written = self.written_since(before)
        self.assertEqual(self.row(written[0]).msisdn, self.user.phone_number)
        self.assertEqual(self.sends[-1][1], self.user.phone_number)
        self.assertNotIn(elsewhere, [d for _, d, _ in self.sends])


class S10OlderClientTests(_Case):
    """
    A login screen from before this change: no capability, no proof. Its resend is refused
    with the sentence it already shows, nothing is written, and its login code still works.
    """

    def test_the_legacy_body_is_refused_and_the_login_code_still_signs_in(self):
        with _env('test', '8001'):
            proof, anchor = self.login(extra={})
            self.assertIsNone(proof)
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A))
            self.assertEqual(self.row(anchor).attempts, 0)
            self.assertSignedIn(self.verify(self.A, '8001'))

    def test_a_legacy_re_login_restores_a_login_code_but_not_the_resend(self):
        with _env('test', '8101', '8102', '8103', '8104'):
            self.login(extra={})
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A))
            proof, anchor = self.login(extra={})          # the user re-enters the password
            self.assertIsNone(proof)
            self.assertRefusedWithNoEffect(lambda: self.resend(self.A))
            proof, anchor = self.login()                   # the updated login screen
            self.assertSent(self.resend(self.A, proof))
            self.assertSignedIn(self.verify(self.A, '8104'))


class S11ReloadedTabTests(_Case):
    """A reloaded tab holds no login response, so it can present only what anyone can."""

    def test_a_client_without_the_login_response_is_refused(self):
        C = _Client('C:reloaded-tab', '192.0.2.12')
        with _env('test', '8201'):
            self.login()
            self.assertRefusedWithNoEffect(lambda: C.post(RESEND, {
                'identification': 'phone', 'identifier': self.user.phone_number,
                'purpose': 'login'}))
            self.assertRefusedWithNoEffect(lambda: self.resend(C))


class S12MalformedInputTests(_Case):
    """Every malformed proof, selector or account id is the one refusal, never a 500."""

    def test_malformed_proofs(self):
        with _env('test', '8301', '8302'):
            proof, anchor = self.login()
            values = {
                'non-ASCII': 'v1.ééé', 'number': 123, 'float': 1.5, 'boolean': True,
                'list': [proof], 'object': {'proof': proof}, 'null': None, 'empty': '',
                'prefix only': 'v1.', 'huge': 'v1.' + 'A' * 5000,
                'leading space': ' ' + proof, 'trailing space': proof + ' ',
                'padded': proof + '=', 'upper-cased': proof.upper(),
                'other version': 'v2.' + proof[3:], 'no prefix': proof[3:],
                'NUL inside': proof[:10] + '\x00' + proof[11:],
            }
            for name, value in values.items():
                with self.subTest(proof=name):
                    self.assertRefusedWithNoEffect(lambda value=value: self.A.post(RESEND, {
                        'identification': 'id', 'identifier': str(self.user.pk),
                        'purpose': 'login', PROOF: value}))
            self.assertSent(self.resend(self.A, proof))   # CONTROL: the real proof works

    def test_malformed_account_ids_and_selectors(self):
        with _env('test', '8401', '8402'):
            proof, anchor = self.login()
            for identifier in ('not-a-uuid', 123, True, ['x'], {'a': 1}, '',
                               '00000000-0000-0000-0000-000000000000', 'z' * 5000):
                with self.subTest(identifier=identifier):
                    self.assertRefusedWithNoEffect(lambda identifier=identifier: self.A.post(
                        RESEND, {'identification': 'id', 'identifier': identifier,
                                 'purpose': 'login', PROOF: proof}))
            for identification in ('phone', 'msisdn', 'email', 'ID', ' id', ['id'], {'id': 1}):
                with self.subTest(identification=identification):
                    self.assertRefusedWithNoEffect(lambda identification=identification: (
                        self.A.post(RESEND, {'identification': identification,
                                             'identifier': str(self.user.pk),
                                             'purpose': 'login', PROOF: proof})))
            self.assertSent(self.resend(self.A, proof))

    def test_the_early_answers_are_unchanged(self):
        with _env('test', '8501'):
            proof, anchor = self.login()
            before = self.effects()
            for body, answer in (
                ({'identification': 'id', 'identifier': str(self.user.pk),
                  'purpose': 'owner-claim', PROOF: proof},
                 {'status': 400, 'message': 'Invalid purpose'}),
                ({'identification': 'id', 'purpose': 'login', PROOF: proof},
                 {'status': 400, 'message': 'Please provide both identification and identifier'}),
                ({'identification': None, 'identifier': str(self.user.pk),
                  'purpose': 'login', PROOF: proof},
                 {'status': 400, 'message': 'Please provide both identification and identifier'}),
            ):
                response = self.A.post(RESEND, body)
                self.assertEqual((response.status_code, response.json()), (400, answer))
            self.assertEqual(self.effects(), before)


class S14ForgedOrBorrowedProofTests(_Case):
    """
    THE DISCRIMINATING CASE. The anchor is LIVE and inside the window, so only the proof
    stands between B and an issuance. B presents everything it could construct or obtain
    without A's login response, plus two proofs the test computes with the server key (a
    proof for this anchor under another user, and for this user under another anchor),
    which B cannot compute. Every one is refused with no effect, and A's real proof still
    works afterwards: the anchor really was live.
    """

    def test_every_proof_but_the_real_one_is_refused_while_the_anchor_is_live(self):
        other, _ = _owner('other')
        with _env('test', '8601', '8602', '8603'):
            proof, anchor = self.login()
            other_proof, other_anchor = self.login(self.B, other)
            candidates = {
                'fabricated, well formed': 'v1.' + 'A' * 43,
                'prefix + user id': 'v1.' + str(self.user.pk),
                'user id': str(self.user.pk),
                'anchor id': anchor,
                'prefix + anchor id': 'v1.' + anchor,
                'truncated real proof': proof[:-1],
                'altered last character': proof[:-1] + ('A' if proof[-1] != 'A' else 'B'),
                'altered first MAC character': proof[:3] + ('A' if proof[3] != 'A' else 'B') + proof[4:],
                'B\'s own login proof': other_proof,
                'this anchor, another user': login_resend_proof(other.pk, anchor),
                'this user, another anchor': login_resend_proof(self.user.pk, other_anchor),
                'this user, a random anchor': login_resend_proof(self.user.pk, uuid.uuid4()),
            }
            for name, value in candidates.items():
                with self.subTest(proof=name):
                    self.assertRefusedWithNoEffect(lambda value=value: self.resend(self.B, value))
            self.assertSent(self.resend(self.A, proof))
            self.assertSignedIn(self.verify(self.A, '8603'))
        self.assertEqual([a for a, _, _ in self.sends], ['A:initiator', 'B:foreign', 'A:initiator'],
                         'B\'s only send is its own login code to its own phone')


class S15ProofAfterSignInTests(_Case):
    """
    MEASURED, NOT A DEFECT FIX. The proof ignores consumption, so its HOLDER may resend
    after signing in until the anchor is replaced or ages out. Nobody else can.
    """

    def test_the_holder_may_resend_after_sign_in_and_nobody_else_can(self):
        with _env('test', '8701', '8702', '8703'):
            proof, anchor = self.login()
            response = self.verify(self.A, '8701')
            self.assertSignedIn(response)
            bearer = response.json()['data']['token']
            self.assertSent(self.resend(self.A, proof))
            # Signed in, the server still names the signed-in account (skip_auth defaults
            # to 'no'), and the proof is that account's.
            self.assertSent(self.resend(self.A, proof, user_id=uuid.uuid4(), bearer=bearer))
            self.assertRefusedWithNoEffect(lambda: self.resend(self.B))


class SignedInIdentityOverrideTests(_Case):
    """The server keeps naming the signed-in account, whatever the body says."""

    def test_a_signed_in_colleague_cannot_spend_a_leaked_proof_for_another_account(self):
        colleague = _manager_of(self.restaurant)
        bearer = str(customer_access.issue_customer_tokens(colleague).access_token)
        with _env('test', '8801'):
            proof, anchor = self.login()
            # Even holding the owner's proof, a signed-in request is about the signed-in
            # colleague, and the proof is not theirs.
            self.assertRefusedWithNoEffect(
                lambda: self.resend(self.B, proof, user_id=self.user.pk, bearer=bearer))


class AdmissionChecksTests(_Case):
    """What the account itself must be is unchanged, and reached only with a valid proof."""

    def test_an_account_without_a_phone_is_refused_even_with_a_matching_proof(self):
        phoneless = User.objects.create_user(
            first_name='No', last_name='Phone', email='er4-no-phone@example.test',
            username='er4-no-phone', country='UG', password=PASSWORD, roles=[],
        )
        manager = OtpManager()
        with _env('dev'):
            self.assertTrue(manager.make_otp(user=phoneless, purpose='login'))
            proof = login_resend_proof(phoneless.pk, manager.issued_otp_id)
            self.assertRefusedWithNoEffect(
                lambda: self.resend(self.A, proof, user_id=phoneless.pk))

    def test_the_customer_access_gate_still_refuses_issuance(self):
        with _env('test', '8901'):
            proof, anchor = self.login()
            User.objects.filter(pk=self.user.pk).update(
                customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
            before = self.effects()
            response = self.resend(self.A, proof)
        # make_otp's own gate (Step 2D.1), reached unchanged: the delivery-failure answer
        # it has always given, and nothing written.
        self.assertEqual((response.status_code, response.json()), (500, SEND_FAILED))
        self.assertEqual(self.effects(), before)


class DeliveryFailureTests(_Case):
    """An undelivered resend is answered as before, and a retry is a new issuance."""

    def test_a_failed_send_then_a_retry_with_the_same_proof(self):
        with _env('test', '9001', '9002', '9003'):
            proof, anchor = self.login()
            self.sms_ok = self.email_ok = False
            before = len(self.rows)
            response = self.resend(self.A, proof)
            self.assertEqual((response.status_code, response.json()), (500, SEND_FAILED))
            failed = self.written_since(before)
            self.assertEqual(OtpIssuance.objects.get(pk=failed[0]).state, 'unknown')
            self.sms_ok = self.email_ok = True
            before = len(self.rows)
            self.assertSent(self.resend(self.A, proof))
            retried = self.written_since(before)
            self.assertIsNone(self.row(failed[0]), 'the retry replaced the undelivered row')
            self.assertSignedIn(self.verify(self.A, '9003'))
        self.assertIsNotNone(self.row(retried[0]).consumed_at)


class TheProofIsNotRepeatedTests(_Case):
    """The proof appears once, in its own login response: never in a log or another answer."""

    def test_no_other_response_and_no_log_record_carries_it(self):
        records = []
        handle = logging.Logger.handle

        def capture(logger, record):
            # Every record any logger handles, whatever its handlers or propagation: the
            # project loggers do not propagate to the root, so a root handler saw nothing.
            records.append(record.getMessage() + ' ' + repr(record.args))
            return handle(logger, record)

        patcher = mock.patch.object(logging.Logger, 'handle', capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        with _env('test', '9101', '9102'):
            proof, anchor = self.login()
            responses = [
                self.resend(self.A, proof),
                self.resend(self.B, proof[:-1]),
                self.resend(self.B, proof, user_id=uuid.uuid4()),
                self.verify(self.A, '0000'),
                self.verify(self.A, '9102'),
            ]
        for response in responses:
            self.assertNotIn(proof.encode(), response.content)
        self.assertTrue(records, 'the capture saw nothing, so it proves nothing')
        self.assertFalse([r for r in records if proof in r or proof[3:] in r])


# ═══════════════════════════════════════════════════════════════════════════════
# Two connections — PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════════

C0, CA, CB = '6101', '6202', '6303'


class _ResendBucket(_LoginBucket):
    """The E-R3 schedule harness, with a login that names the capability."""
    CODES = (C0, CA, CB)

    def setUp(self):
        super().setUp()
        self.proof = self.proof_login()
        self.anchor = str(UserOtp.objects.get(user=self.user, purpose='login', msisdn=None).pk)

    def foreign_resends(self):
        """B: the phone selector, then the id selector, neither with a proof."""
        return [
            self.post(RESEND, {'identification': 'phone', 'identifier': self.user.phone_number,
                               'purpose': 'login'}),
            self.post(RESEND, {'identification': 'id', 'identifier': str(self.user.pk),
                               'purpose': 'login'}),
        ]


class C1ForeignResendsDuringAGenuineResendTests(_ResendBucket):
    """
    A's resend is parked inside its issuance transaction straight after its INSERT, holding
    the ``(user, phone, 'login')`` bucket lock. B then asks for a login resend. B is
    refused without waiting on that lock and without touching any row, and A's code works.
    """

    def test_the_foreign_resends_neither_wait_nor_replace(self):
        seen, outcomes = self.race(lambda: self.login_resend(self.proof), self.foreign_resends,
                                   'after_insert')
        self.assertEqual(seen, {'state': 'finished'},
                         'a refused foreign resend waited on the bucket lock')
        self.assertOk(outcomes['issuer'])
        self.assertEqual(outcomes['second'], ('ok', [(400, REFUSAL), (400, REFUSAL)]))
        ra = self.created_by('issuer')
        self.assertEqual([t for t, _ in self.created if t == 'second'], [])
        self.assertEqual(self.live(user=self.user, msisdn=self.user.phone_number,
                                   purpose='login'), [ra])
        status, body = self.verify(CA)
        self.assertEqual(status, 200, body)
        self.assertTokensFor(body['data'], self.user)
        self.assertSpent(ra)
        anchor = UserOtp.objects.get(pk=self.anchor)
        self.assertEqual((anchor.consumed_at, anchor.attempts), (None, 0))
        self.assertEqual(OtpIssuance.objects.get(pk=ra).origin, otp_accounting.ORIGIN_LOGIN_RESEND)
        self.assertNothingHeldWhenSent(['MainThread', 'issuer'], self.user.phone_number)


class C2AnchorReplacedAfterAdmissionTests(_ResendBucket):
    """
    THE STATED LIMIT, MEASURED. A's resend has been admitted and is parked before
    ``make_otp`` (no transaction open, no lock held). A second password login for the same
    account (a new tab) replaces the anchor and commits. The parked resend is then released
    and still issues, to the same account phone: admission is a plain read, not repeated
    inside the issuance transaction. Afterwards the old proof admits nothing.
    """
    CODES = (C0, CA, CB, '6404')

    @contextlib.contextmanager
    def park_resend_before_issuance(self, parked, resume):
        real = otp_manager.OtpManager.make_otp

        def wrapper(manager, *args, **kwargs):
            if (threading.current_thread().name == 'issuer'
                    and kwargs.get('origin') == otp_accounting.ORIGIN_LOGIN_RESEND
                    and not parked.is_set()):
                parked.set()
                if not resume.wait(timeout=WAIT):
                    raise RuntimeError('the resend seam was never released')
            return real(manager, *args, **kwargs)

        with mock.patch.object(otp_manager.OtpManager, 'make_otp', wrapper):
            yield

    def race_with(self, park_cm, first, second):
        parked, resume = threading.Event(), threading.Event()
        first_done, second_done, outcomes = threading.Event(), threading.Event(), {}
        with park_cm(parked, resume):
            issuer = self.worker('issuer', first, outcomes, first_done)
            issuer.start()
            self.assertTrue(parked.wait(WAIT), 'the issuer never reached its seam')
            other = self.worker('second', second, outcomes, second_done)
            other.start()
            try:
                seen = self.watch('second', second_done, holder='issuer')
            finally:
                resume.set()
            self.join(issuer, other)
        return seen, outcomes

    def test_an_admitted_resend_issues_after_its_anchor_was_replaced(self):
        old_anchor, old_proof = self.anchor, self.proof
        seen, outcomes = self.race_with(
            self.park_resend_before_issuance,
            lambda: self.login_resend(old_proof),
            lambda: self.post(LOGIN_URL, {'username': self.user.phone_number,
                                          'password': PASSWORD_ER3,
                                          'login_resend_protocol': 'v1'}),
        )
        self.assertEqual(seen, {'state': 'finished'}, 'nothing is held while parked')
        self.assertOk(outcomes['second'])
        self.assertOk(outcomes['issuer'])
        self.assertFalse(self.exists(old_anchor), 'the new login replaced the anchor')
        new_anchor, resent = self.created_by('second'), self.created_by('issuer')
        self.assertEqual(self.live(user=self.user, purpose='login'), sorted([new_anchor, resent]))
        status, body = self.verify(CB)
        self.assertTokensFor(body['data'], self.user)
        self.assertEqual(self.login_resend(old_proof), (400, REFUSAL))
        self.assertEqual({s['msisdn'] for s in self.sends}, {self.user.phone_number})

