"""
The owner-claim challenge endpoint — Step 2F.1, on the CUSTOMER plane.

    POST /api/v1/users/owner-claim/challenge/
    X-Owner-Claim-Token: <raw token>

    -> 200 {"status": 200, "message": "Verification code sent.",
            "data": {"credential_setup_required": true|false}}

WHY IT IS MOUNTED ON THE CUSTOMER PLANE
───────────────────────────────────────────────────────────────────────────────
The owner is claiming THEIR OWN restaurant identity. An ``AdminSession`` has no
authority in this operation and must not be able to acquire any: the admin plane
ISSUES the credential (Step 2D / 2E) and the owner REDEEMS it here. Putting redemption
on ``/api/admin/v1`` would mean the platform could complete an owner's claim on their
behalf, which is exactly the fact ``owner_control`` exists to record honestly.

It lives in ``platform_admin_app`` even so, because every line of invitation state does
— the same arrangement ``delegated_exchange`` uses for the customer-plane half of
delegation. ``OwnerInvitation`` does not move into ``users_app``.

THE TOKEN IS A BEARER CREDENTIAL AND TRAVELS HEADER-ONLY
───────────────────────────────────────────────────────────────────────────────
``X-Owner-Claim-Token`` and nowhere else — not a query parameter, not the URL path,
not the JSON or form body, not a cookie. The same rule the diner capability channel
and the delegation exchange code follow, for the same reasons: a credential in a URL
lands in access logs, ``Referer`` headers and browser history, and one in a body is
captured by ordinary request logging.

It is never logged, never echoed, never placed in an exception, and never returned.

NO AMBIENT AUTHENTICATION
───────────────────────────────────────────────────────────────────────────────
``authentication_classes = []`` — not merely ``AllowAny``. The claim token is the
credential and NOTHING about the caller may influence which invitation or which owner
is resolved: not a customer JWT, not a delegated session, not an admin cookie.
``request.user`` is never consulted. The invited identity comes from the STORED
invitation, which is the only thing that can say who this claim belongs to.

ONE PUBLIC FAILURE
───────────────────────────────────────────────────────────────────────────────
Unknown token, expired, cancelled, superseded, consumed, legacy tenant, deleted
restaurant, owner moved, ownership drifted, inactive account, wrong account type,
missing phone — all render the same 400 and the same sentence. Anything else is an
oracle: an anonymous caller who could tell "wrong token" from "right token, wrong
tenant state" learns which of their guesses was closest, and learns facts about a
restaurant they have no relationship with.

WHAT THIS ENDPOINT DOES NOT DO
───────────────────────────────────────────────────────────────────────────────
It writes NOTHING except the ``UserOtp`` row ``OtpManager`` creates. No invitation
stamp, no ``customer_access_state``, no password, no membership, no customer token.
Step 2F.2 is the only writer of the redemption transaction, and it is not built.
"""
import logging

from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from platform_admin_app import owner_claim, owner_claim_redemption
from users_app.controllers.otp_manager import OtpManager

logger = logging.getLogger(__name__)

# The header the raw claim token travels in, and the ONLY place it is read from.
CLAIM_TOKEN_HEADER = 'X-Owner-Claim-Token'

# The OTP purpose for owner claim. Deliberately NOT in `CUSTOMER_AUTH_OTP_PURPOSES`:
# those are the two flows that can end in a customer session or a customer password,
# and a `pending_initial_claim` identity is refused them. This purpose must REACH a
# pending identity — that is the one identity it exists for — and a verified
# owner-claim code is only EVIDENCE that redemption consumes, never a token mint.
#
# RE-EXPORTED FROM THE DOMAIN, never re-spelled: the redemption service binds its OTP
# query to this exact string, so a second literal here would let issuance and
# verification drift apart into a flow that can never be completed.
OWNER_CLAIM_OTP_PURPOSE = owner_claim_redemption.OWNER_CLAIM_OTP_PURPOSE

# One sentence for every refusal. See the module docstring.
REFUSAL_MESSAGE = 'This owner claim is invalid or no longer available.'
# Redemption collapses claim-state AND verification failures into one sentence. A caller
# who could tell "unknown token" from "right token, wrong code" would learn which of the
# two factors they had, which is exactly the thing two factors are for.
REDEEM_REFUSAL_MESSAGE = (
    'This owner claim or verification code is invalid or no longer available.'
)
REDEEM_SUCCESS_MESSAGE = 'Owner claim completed.'
# The two request-shape refusals. Reachable ONLY behind a resolved, eligible claim (see
# `OwnerClaimRedeemView.post`), so they are actionable guidance to somebody who holds the
# credential rather than an oracle for somebody who does not.
PASSWORD_REQUIRED_MESSAGE = 'Please choose a password to finish claiming your account.'
PASSWORD_NOT_REQUIRED_MESSAGE = (
    'This account already has a password. Remove new_password and try again.'
)
OTP_REQUIRED_MESSAGE = 'Please provide the verification code that was sent to you.'
DELIVERY_FAILURE_MESSAGE = (
    "We couldn't send your verification code. Please try again."
)
SUCCESS_MESSAGE = 'Verification code sent.'


def claim_token_from_request(request):
    """
    The raw claim token — header only, never anywhere else.

    The one canonical extractor, so "where may this credential come from?" has a
    single answer that a reviewer can check in one place. Mirrors
    ``delegated_middleware.code_from_request`` and the diner capability channel's
    ``credential_from_request``.
    """
    return request.headers.get(CLAIM_TOKEN_HEADER)


class OwnerClaimChallengeThrottle(AnonRateThrottle):
    """
    Per-IP cap on claim-challenge attempts.

    **Not the security boundary, and it must not be described as one.** The claim
    token is ~288 bits and the OTP verifier has its own per-challenge attempt cap and
    per-identifier throttle. What this buys is that a flood of attempts — each of
    which can cost an SMS — is not free.

    Keyed on the client IP, never on the token: DRF throttle cache keys are visible in
    diagnostics, and a raw credential has no business in one.
    """

    scope = 'owner_claim_challenge'


class OwnerClaimChallengeView(APIView):
    """``POST`` a raw claim token in ``X-Owner-Claim-Token`` → an owner-claim OTP."""

    # No authenticator at all — see the module docstring. This is load-bearing, not
    # a convenience: an ambient credential must have zero influence on the outcome.
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [OwnerClaimChallengeThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        """
        Stamp every response — success, refusal and throttle alike — ``no-store``.

        The body carries no credential and never will, but this is a step in a claim
        flow and its responses have no business in a shared cache or a browser's back
        button. Set on the VIEW rather than at one return site so a later branch
        cannot forget it.
        """
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, private'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    def _refused(self):
        return Response({'status': 400, 'message': REFUSAL_MESSAGE}, status=400)

    def post(self, request):
        raw_token = claim_token_from_request(request)

        try:
            preflight = owner_claim.resolve_claim_token(raw_token)
        except owner_claim.ClaimRefused as refusal:
            # The CODE goes to the log so an operator can distinguish a stale
            # credential from a drifted tenant. The response cannot.
            logger.info('owner-claim challenge refused: %s', refusal.code)
            return self._refused()

        # DELIVERY IS OUTSIDE ANY TRANSACTION AND HOLDS NO LOCK. The resolution above
        # is a pure read that opened neither, deliberately — see `owner_claim`'s module
        # docstring on the preflight/authoritative split and on what PR #306 measured
        # about holding a Restaurant row across notification I/O.
        #
        # `make_otp` owns entropy, hashing, salt, expiry and delivery. There is exactly
        # one OTP implementation and this is not a second one.
        #
        # THE DESTINATION IS PASSED EXPLICITLY (Step 2F.2), and it is load-bearing rather
        # than tidy. Without `msisdn`, `make_otp` sends to `user.phone_number` but stores
        # `UserOtp.msisdn = NULL`, so the row does not record where the code went — and
        # redemption has to compare the factor against the CURRENT owner's phone to know
        # that a code delivered to a since-replaced number cannot be spent. A destination
        # that is not stored cannot be compared.
        #
        # `preflight.canonical_phone` was already proved equal to the stored
        # `phone_number` byte for byte, and `normalise_msisdn` is idempotent, so this
        # sends to exactly the same number as before and merely records it.
        delivered = OtpManager().make_otp(
            user=preflight.invited_user,
            msisdn=preflight.canonical_phone,
            purpose=OWNER_CLAIM_OTP_PURPOSE,
        )
        if not delivered:
            # Fail CLOSED, exactly as login and password reset do on a falsy make_otp:
            # telling the caller a code is on its way when it is not leaves them
            # waiting for something that will never arrive. Nothing was mutated —
            # the invitation, the access state and the password are all untouched.
            logger.error('owner-claim challenge: OTP delivery failed')
            return Response(
                {'status': 500, 'message': DELIVERY_FAILURE_MESSAGE}, status=500,
            )

        # ONE boolean of context, and it is safe to give: the caller already holds the
        # claim credential for this invitation. It says whether Step 2F.2 will ask
        # them to choose a password (a brand-new owner) or not (an established owner
        # claiming an additional restaurant). No owner PII, no invitation id, no
        # restaurant identity, no token, no customer session.
        return Response(
            {
                'status': 200,
                'message': SUCCESS_MESSAGE,
                'data': {
                    'credential_setup_required': preflight.credential_setup_required,
                },
            },
            status=200,
        )


class OwnerClaimRedeemThrottle(AnonRateThrottle):
    """
    Per-IP cap on redemption attempts.

    **DEFENCE IN DEPTH, AND NOT THE DURABLE BOUNDARY.** DRF throttle counters live in
    ``LocMemCache``, which is per-mod_wsgi-worker and trivially bypassed by rotating
    source IPs. The boundary that actually holds is
    ``OwnerInvitation.claim_failed_attempts`` — five failed verifications for the whole
    life of the credential, held in PostgreSQL under the ``Restaurant`` lock, unaffected
    by IP, worker or a fresh OTP.

    ``5/min``, matching the challenge route and ``auth_otp`` rather than the looser
    ``10/min`` sibling: an honest claimant has five attempts in TOTAL, so this cannot
    inconvenience them, and it keeps a flood from costing the database a locked
    transaction per request.

    Keyed on the client IP, never on the claim token: DRF throttle cache keys surface in
    diagnostics, and a raw credential has no business in one.
    """

    scope = 'owner_claim_redeem'


class OwnerClaimRedeemView(APIView):
    """
    ``POST`` a raw claim token plus its owner-claim OTP → the completed claim.

        POST /api/v1/users/owner-claim/redeem/
        X-Owner-Claim-Token: <raw token>
        {"otp": "1234", "new_password": "<chosen>"}   # password only when required

    The password is required exactly when the identity is ``pending_initial_claim``, and
    REFUSED otherwise — an established owner claiming an additional restaurant must not
    have their credential rewritten, and silently ignoring the field would hide that from
    somebody who believed they had just changed their password.

    NO AMBIENT AUTHENTICATION — ``authentication_classes = []``, not merely ``AllowAny``.
    A customer JWT, a delegated session and an admin cookie all have ZERO influence on
    which invitation is redeemed, which identity is established and which restaurant is
    affected. ``request.user`` is never consulted. The claim token names the invitation;
    the invitation names the identity.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [OwnerClaimRedeemThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        """
        Stamp EVERY response ``no-store`` — and here it carries customer tokens.

        Set on the view rather than at one return site so a later branch cannot forget
        it. No cookie is set, no ``Location`` header is emitted and no claim URL is
        fabricated: the session is handed to the caller in the body, once.
        """
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store, private'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response

    def _refused(self):
        """The ONE public failure for every claim-state and verification outcome."""
        return Response(
            {'status': 400, 'message': REDEEM_REFUSAL_MESSAGE}, status=400,
        )

    def _bad_request(self, message, errors=None):
        """
        A shaped 400 for the narrow set of failures a legitimate claimant must be able
        to act on — a missing code, a missing or rejected password, a password supplied
        where none is wanted.

        Reachable ONLY after the claim token has resolved to a real, currently-eligible
        claim, so it tells somebody who already holds the credential how to use it and
        tells a guesser nothing. That ordering is the whole reason password validation
        runs where it does.
        """
        body = {'status': 400, 'message': message}
        if errors:
            body['errors'] = errors
        return Response(body, status=400)

    def post(self, request):
        raw_token = claim_token_from_request(request)

        # ━━ A. READ-ONLY PREFLIGHT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #
        # The SAME resolver the challenge uses, so the two halves of the flow can never
        # disagree about what a claimable state is. It takes no lock and opens no
        # transaction, and NOTHING it returns is trusted for authority — the redemption
        # service re-queries and re-checks every fact under the `Restaurant` lock.
        #
        # It exists here for exactly two reasons: to refuse an unknown or ineligible
        # token BEFORE any password-shaped response could become an oracle, and to learn
        # whether a password is needed so the expensive hash can be computed outside the
        # lock.
        try:
            preflight = owner_claim.resolve_claim_token(raw_token)
        except owner_claim.ClaimRefused as refusal:
            logger.info('owner-claim redemption refused: %s', refusal.code)
            return self._refused()

        data = request.data if isinstance(request.data, dict) else {}

        # NOT parsed as an integer and not coerced: a leading zero is significant, and
        # `int('01234')` would silently make a different code. Not `.strip()`ped either —
        # the stored hash is over the exact digits that were generated.
        otp = data.get('otp')
        if otp is None or not isinstance(otp, str) or not otp:
            return self._bad_request(OTP_REQUIRED_MESSAGE)

        # DELIBERATELY NOT `.strip()`ped: whitespace can be part of a password, and
        # trimming it here would store a credential the user never typed.
        new_password = data.get('new_password')

        # ━━ B. THE PASSWORD, VALIDATED AND HASHED OUTSIDE THE TRANSACTION ━━━━━━━━━
        encoded_password = None
        if preflight.credential_setup_required:
            if new_password is None or not isinstance(new_password, str):
                return self._bad_request(PASSWORD_REQUIRED_MESSAGE)
            try:
                # DJANGO'S CONFIGURED POLICY, not a second one invented here.
                # `AUTH_PASSWORD_VALIDATORS` is the policy; `user=` is what lets
                # `UserAttributeSimilarityValidator` compare against this owner's own
                # name, username and email.
                validate_password(new_password, user=preflight.invited_user)
            except ValidationError as error:
                return self._bad_request(
                    PASSWORD_REQUIRED_MESSAGE, {'new_password': list(error.messages)},
                )
            # THE EXPENSIVE PART, AND IT HAPPENS HERE ON PURPOSE. ~258ms of PBKDF2 at
            # Django 5.2's default iteration count. The redemption transaction holds the
            # `Restaurant` row, which is both the membership barrier and a row the
            # lifecycle transition waits on while holding the exclusive order-admission
            # advisory lock — so a quarter-second of CPU under it would stall every diner
            # order at that restaurant. Hashing needs no tenant serialization.
            encoded_password = make_password(new_password)
        elif new_password is not None:
            # REFUSED, never ignored and never applied. This identity already has a
            # password; rewriting it during a restaurant claim would treat a
            # restaurant-scoped credential as global account onboarding, and quietly
            # dropping the field would leave the caller believing they had changed it.
            return self._bad_request(PASSWORD_NOT_REQUIRED_MESSAGE)

        # ━━ C. THE AUTHORITATIVE TRANSACTION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        try:
            outcome = owner_claim_redemption.redeem_owner_claim(
                raw_token=raw_token,
                otp=otp,
                encoded_password=encoded_password,
            )
        except owner_claim_redemption.RedemptionRefused as refusal:
            logger.info('owner-claim redemption refused: %s', refusal.code)
            return self._refused()

        if isinstance(outcome, owner_claim_redemption.RedemptionAttemptFailed):
            # A WRONG CODE. Its attempt counters have already COMMITTED — see the
            # service's module docstring on why this is a return value rather than an
            # exception — and the caller gets the same sentence as every claim-state
            # refusal, so the two factors cannot be probed independently.
            logger.info(
                'owner-claim redemption refused: %s (attempts remaining=%s)',
                owner_claim_redemption.INVALID_OTP, outcome.remaining,
            )
            return self._refused()

        # ━━ D. SUCCESS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #
        # The restaurant id is safe to return now and only now: the caller has proved
        # BOTH factors and is this restaurant's confirmed owner. No claim token, no token
        # hash, no OTP, no password, no invitation id, no owner PII, no admin actor.
        #
        # No `require_otp`: this transaction has just consumed the claim-specific second
        # factor. No profile either — the frontend fetches the ordinary authenticated
        # profile with the token it was just handed.
        return Response(
            {
                'status': 200,
                'message': REDEEM_SUCCESS_MESSAGE,
                'data': {
                    'token': str(outcome.tokens.access_token),
                    'refresh': str(outcome.tokens),
                    'restaurant_id': str(outcome.restaurant.pk),
                },
            },
            status=200,
        )
