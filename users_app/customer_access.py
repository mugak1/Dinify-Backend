"""
THE CUSTOMER-PLANE ACCESS GATE (Step 2D.1) — one axis, one policy, one token sink.

━━ THE BYPASS THIS CLOSES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Step 2D creates a new owner with an unusable password and hands the operator an
``OwnerInvitation`` as the account-claim credential. The intended architecture is
that customer access does not exist until that invitation is redeemed — but nothing
enforced it. Generic password reset needed only a phone number or an email address:

    initiate-reset-password(identifier)  ->  OTP
    reset-password(identifier, otp)      ->  set_password() + RefreshToken.for_user()

(``identifier`` is the email or phone the request names; an older client sends it as
``phone_number``), so anybody who knew either could establish a password and take a
customer session, leaving the platform in a state that contradicts itself:
``owner_control: not_established`` and ``invitation: pending``, while the account was
already exercising owner authority over the restaurant.

AN UNUSABLE PASSWORD WAS NEVER THE INVARIANT. It is the thing being protected, not
the protection: password reset exists precisely to replace one. The invariant has to
be a durable fact about the IDENTITY, checked at every door.

━━ WHY A NEW FIELD RATHER THAN AN EXISTING ONE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nothing already on ``User`` can say this truthfully:

* ``is_active`` means ADMINISTRATIVELY DEACTIVATED and is read by Django and
  SimpleJWT throughout. Reusing it would make every existing "account disabled"
  message and any future reactivation path lie about what happened.
* ``prompt_password_change`` defaults to ``True`` for every account ever created, so
  it distinguishes nothing, and it is a UX hint that a claim flow must never become.
* ``has_usable_password()`` is the protected thing (see above).
* ``last_login`` is null for plenty of legitimate accounts.
* a ``UserOtp`` row is transient and purpose-scoped.

AND NOT THE INVITATION EITHER, which is the important one: ``OwnerInvitation`` and
``RestaurantOnboarding`` are RESTAURANT-scoped. One ``User`` may own several
restaurants, so an established owner of restaurant A who is named as the owner of a
new restaurant B holds a PENDING invitation for B. Reading invitation state as a
global gate would revoke their access to A — a live tenant losing its owner because
Dinify created a second one. The gate is therefore an identity fact, and invitation
state stays what it always was: restaurant-scoped evidence.

━━ WHAT THIS MODULE IS, AND IS NOT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

It answers ONE question — may this identity be admitted onto the customer plane at
all? — and deliberately does not absorb the neighbouring ones. ``is_active``,
``account_type``, restaurant roles, module permissions, OTP validity and password
correctness keep their existing owners and their existing gates. There is no
``can_authenticate(user)`` here, because a helper with that name accumulates meanings
until nobody can say what a call to it proves.

``issue_customer_tokens`` is the second half: the single sanctioned mint. It is not
policy — it is the chokepoint that makes the policy unavoidable, so that the next
direct ``RefreshToken.for_user(...)`` someone adds is caught by a structural test
rather than by an incident.
"""
import logging

from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import CUSTOMER_ACCESS_ESTABLISHED

logger = logging.getLogger(__name__)


class CustomerAccessRefused(Exception):
    """
    A customer token was requested for an identity that may not hold one.

    Raised only by ``issue_customer_tokens``, and unreachable through every route
    that exists today: every caller checks the policy first and answers with its own
    surface's generic refusal. It exists as the BACKSTOP — a future caller that
    forgets the check gets an exception rather than a token, so the failure direction
    is closed rather than open.

    Carries no identifier: it is going to end up in a log, and which account was
    refused is not something a stack trace needs to say.
    """


def is_established(user) -> bool:
    """
    Whether ``user`` may be admitted onto the customer plane.

    FAILS CLOSED on anything that is not explicitly established — ``None``, an
    ``AnonymousUser``, an object with no such attribute, or a value outside the
    vocabulary. A value outside the vocabulary is unreachable behind the
    ``user_customer_access_state_vocabulary`` check constraint; treating it as
    refused anyway is the right direction for a gate, and the constraint is what
    stops that fail-closed reading from silently locking a real account out.

    Note what is NOT consulted: ``is_active``, ``account_type``, password usability,
    roles, invitations. Those are other questions with other owners.
    """
    return getattr(user, 'customer_access_state', None) == CUSTOMER_ACCESS_ESTABLISHED


def is_refused(user) -> bool:
    """The inverse of ``is_established``, for call sites that read better that way."""
    return not is_established(user)


def issue_customer_tokens(user) -> RefreshToken:
    """
    Mint a customer refresh token for ``user``. THE ONLY SANCTIONED CUSTOMER MINT.

    Every production ``RefreshToken.for_user`` call for the customer plane goes
    through here, and ``users_app.tests_customer_access_gate`` AST-scans the tree to
    keep it that way. That structural rule is the point: the bypass this module
    exists for was one innocent-looking direct call in a flow nobody thought of as
    authentication, and the next one should fail a test rather than ship.

    Raises ``CustomerAccessRefused`` for an identity that is not established. Callers
    are still expected to check the policy FIRST and answer with their own surface's
    generic refusal — reaching this exception means a gate was forgotten, and a 500 is
    the correct outcome of that, because it is loud and it is not a token.

    Scope: this checks the Step-2D.1 axis and nothing else. The ``account_type``
    refusals stay at their three existing sites (``login``, ``verify_otp``,
    ``reset_password._resolve_user``), where they already are and are already pinned;
    moving them in here would turn a small chokepoint into the mega-helper the module
    docstring refuses to write.
    """
    if not is_established(user):
        logger.info('customer token mint: refused (customer access not established)')
        raise CustomerAccessRefused(
            'This identity may not hold a customer session.'
        )
    return RefreshToken.for_user(user)
