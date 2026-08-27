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

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from platform_admin_app import owner_claim
from users_app.controllers.otp_manager import OtpManager

logger = logging.getLogger(__name__)

# The header the raw claim token travels in, and the ONLY place it is read from.
CLAIM_TOKEN_HEADER = 'X-Owner-Claim-Token'

# The OTP purpose for owner claim. Deliberately NOT in `CUSTOMER_AUTH_OTP_PURPOSES`:
# those are the two flows that can end in a customer session or a customer password,
# and a `pending_initial_claim` identity is refused them. This purpose must REACH a
# pending identity — that is the one identity it exists for — and a verified
# owner-claim code is only EVIDENCE that Step 2F.2 will consume, never a token mint.
OWNER_CLAIM_OTP_PURPOSE = 'owner-claim'

# One sentence for every refusal. See the module docstring.
REFUSAL_MESSAGE = 'This owner claim is invalid or no longer available.'
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
        delivered = OtpManager().make_otp(
            user=preflight.invited_user,
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
