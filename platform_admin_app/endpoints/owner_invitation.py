"""
Admin OWNER-INVITATION lifecycle endpoints (Phase 1, Step 2E).

Two routes, one per Step-2E domain operation::

    POST admin/v1/restaurants/<uuid>/owner-invitation/reissue/
    POST admin/v1/restaurants/<uuid>/owner-invitation/cancel/

TWO EXPLICIT ROUTES, never one with an ``action`` segment. Rotating a credential and
terminating one are opposite decisions — one hands out live authority, the other
withdraws it — and an ``owner-invitation/<str:action>/`` route would make "what did
this operator do?" a question about a path segment. The same reasoning that gave the
subscription-terms domain three routes rather than one.

AND THE VERB IS ``reissue``. Not ``resend``, not ``resend-invite``, not
``invite-owner``. This system performs NO DELIVERY of any kind — no email, no SMS, no
notification, no delivery column on the schema, no provider — so a route promising a
delivery event would be a promise the platform cannot keep, made in the URL, where an
operator is most likely to believe it. What happens is ROTATION: the old credential
dies and a new raw token is handed to the authenticated, elevated operator who asked
for it.

━━ THE ENDPOINTS ARE ADAPTERS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nothing here creates, saves or deletes an ``OwnerInvitation``. Every mutation goes
through ``platform_admin_app.onboarding_invitations``, which owns the ``Restaurant``
lock, the head-invitation selection, the concurrency token, owner binding, the
supersede-before-insert step, token minting and the TTL. What lives here is
authority, the request contract, the audit row and the HTTP translation.

━━ THE BODY THE OPERATOR MUST SEND ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

::

    {"expected_invitation_id": "<UUID>", "reason": "..."}

``expected_invitation_id`` is REQUIRED on both, and there is deliberately no "act on
whatever is current when the POST arrives" fallback. The failure it prevents is
concrete: an operator reviews invitation A, another operator reissues A into B, and
the first operator's Cancel click terminates a credential they have never seen. With
the token that is a 409; without it, it succeeds and nobody finds out.

What the token asserts EXACTLY — identity, not status — is stated in
``onboarding_invitations._head_under_lock``, which is where it is enforced.
"""
import uuid

from django.db import transaction
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from platform_admin_app import onboarding_invitations
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED,
    ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED,
)
from platform_admin_app.endpoints.reasoned_request import (
    ReasonedRequestSerializer,
    read_request_body,
)
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import (
    OWNER_CONSISTENCY_CODES,
    OwnerConsistencyError,
)
from platform_admin_app.onboarding_reads import (
    invitation_projection,
    onboarding_summary,
    select_head_invitation,
)
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.models import Restaurant


def _not_found():
    """404 for a missing OR soft-deleted restaurant — the plane never distinguishes."""
    return Response({'status': 404, 'message': 'Restaurant not found.'}, status=404)


# --- the request contract ----------------------------------------------------

class StrictUUIDStringField(serializers.Field):
    """
    A concurrency token that MUST arrive as a JSON string holding a UUID.

    ``"7f1c…"`` is accepted; ``42``, ``true``, ``""`` and ``null`` are not.

    A DRF ``UUIDField`` would NOT do, and the reason is specific to what this field
    IS. Given a JSON number it evaluates ``uuid.UUID(int=data)``, so ``42`` becomes a
    perfectly well-formed UUID no invitation has ever carried. The request would then
    miss under the lock and come back **409 "the owner invitation changed since it was
    loaded"** — an answer about the platform's state when the truth is that the body
    was malformed. A conflict is the one error on these routes that means THE WORLD
    MOVED, and it must never be manufactured by a coercion table.

    The same primitive and the same reasoning as the subscription-terms token and the
    creation endpoint's owner id.
    """

    default_error_messages = {
        'not_a_string': 'Send the invitation id as a UUID string.',
        'invalid': 'Enter a valid UUID.',
    }

    def to_internal_value(self, data):
        # `bool` is not a `str`, so True/False are refused here rather than being
        # reinterpreted as an integer and, from there, as a UUID.
        if not isinstance(data, str):
            self.fail('not_a_string')
        try:
            return uuid.UUID(data.strip())
        except (ValueError, AttributeError, TypeError):
            self.fail('invalid')

    def to_representation(self, value):
        return str(value)


class OwnerInvitationRequestSerializer(ReasonedRequestSerializer):
    """
    The body both operations take: the exact invitation reviewed, plus why.

    Shared by reissue and cancel because the CONTRACT is genuinely identical — unlike
    the subscription-terms operations, which differ in the facts they carry. What the
    two do with the named invitation could hardly be more different, and that
    difference lives in the two ``post`` methods, not in the shape of the request.

    ``required=True`` with no default: an omitted token is a 400, never "act on
    whatever is current". A default here would silently hand a forgetful client the
    very assertion the field exists to make them state.
    """

    expected_invitation_id = StrictUUIDStringField(required=True)

    # Fixed order, so a body with several problems always produces the same audit
    # error code rather than one that depends on dict iteration.
    _FIELD_ORDER = ('expected_invitation_id', 'reason')


# --- domain outcomes ---------------------------------------------------------

# {domain error code: HTTP status}. A code absent from this map is deliberately
# re-raised: an unexpected internal condition should surface as a 500 and roll the
# transaction back, not be relabelled a tidy client error to keep the error rate low.
#
# 400 = the request describes something malformed. 409 = the request is well-formed
# and the platform's current state contradicts it, so the fix is to look and decide
# again rather than to edit the body.
STATUS_BY_CODE = {
    **{code: 400 for code in onboarding_invitations.INVALID_REQUEST_CODES},
    **{code: 409 for code in onboarding_invitations.CONFLICT_CODES},
    # Owner consistency, flattened into the same map so a drifted tenant is a 409 on
    # the reissue route rather than an unhandled exception. The three canonical codes
    # keep their own spelling — an operator can search the audit log, the read
    # projection and the codebase for the same word.
    **{code: 409 for code in OWNER_CONSISTENCY_CODES},
}

# Fixed operator-facing sentences for CONFLICTS, in the house style: a conflict
# message says what the platform believes and what to do about it, and never names
# internals. Stated here rather than passed through from the domain so a domain
# message change cannot silently rewrite the API.
CONFLICT_MESSAGES = {
    onboarding_invitations.ONBOARDING_NOT_TRACKED:
        'This restaurant is not represented in the Admin onboarding domain.',
    onboarding_invitations.OWNER_INVITATION_NOT_APPLICABLE:
        'This restaurant did not enter Dinify through a claim flow, so it has no '
        'owner invitation.',
    onboarding_invitations.STALE_OWNER_INVITATION:
        'The owner invitation changed since it was loaded.',
    onboarding_invitations.OWNER_INVITATION_NOT_ISSUED:
        'This restaurant has no owner invitation.',
    onboarding_invitations.OWNER_CONTROL_ALREADY_ESTABLISHED:
        'This restaurant\'s owner has already claimed it.',
    onboarding_invitations.OWNER_ACCOUNT_NOT_FOUND:
        'This restaurant has no usable owner account to invite.',
    onboarding_invitations.OWNER_ACCOUNT_INACTIVE:
        'The owner account is deactivated.',
    onboarding_invitations.OWNER_ACCOUNT_NOT_RESTAURANT_USER:
        'The owner account cannot be invited to claim a restaurant.',
    onboarding_invitations.OWNER_INVITATION_ALREADY_RESOLVED:
        'This owner invitation has already resolved and cannot be cancelled.',
}

# The one owner-consistency sentence. All three codes share it: the remedy is the
# same ("somebody has to fix who owns this restaurant"), and the code beside it says
# which way the two answers disagree.
_OWNER_CONSISTENCY_MESSAGE = (
    'This restaurant\'s owner of record and owner authority disagree. Resolve that '
    'before issuing a new claim credential.'
)

# Request-body fields a domain 400 may be attributed to. Whitelisted rather than
# derived, so a future error code cannot silently become a response key.
ERROR_FIELD_BY_CODE = {
    onboarding_invitations.INVALID_EXPECTED_INVITATION_ID: 'expected_invitation_id',
    onboarding_invitations.INVALID_REASON: 'reason',
}


def invitation_snapshot(head):
    """
    One head invitation as an audit event should record it, or ``None``.

    NARROW ON PURPOSE, and identical in shape to what the read publishes: the id, the
    state word, and the two instants that make ``pending`` and ``expired`` legible.

    Deliberately absent: ``token_hash`` and the raw token above all — an audit log is
    read by more people, for longer, than a credential should be reachable by, and a
    hash in the log is a standing invitation to treat it as one. Also absent are the
    invited user's identity (the restaurant id already implies who the owner is, and
    an owner's name, phone or email has no business in a credential-rotation record),
    ``issued_by`` and ``cancelled_by`` (the audit row already says who made THIS
    request), and any whole-object serialization of the restaurant.

    (``audit.redact`` would scrub a key containing "token" anyway; this does not rely
    on that.)
    """
    if head is None or head.invitation is None:
        return None
    return invitation_projection(head)


def _state(head):
    return {'owner_invitation': invitation_snapshot(head)}


# --- the shared view body ----------------------------------------------------

class _OwnerInvitationWriteView(AdminAPIView):
    """
    ABSTRACT. Authority, target resolution, body parsing, audit and status mapping —
    everything the two operations share before and after the part that differs.

    Subclasses supply ``audit_action`` and a ``post()`` that calls exactly one Step-2E
    domain writer. They do NOT share a generic ``mutate()`` hook: reissue returns a
    credential and cancel must never do so, reissue's before/after states describe two
    different invitations while cancel's describe one, and burying those differences
    behind a parameter is precisely how the credential half would end up on the wrong
    route.
    """

    permission_classes = [IsAuthenticated, IsRecentlyElevated]

    audit_action = None

    # --- authority ---

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused credential operation before DRF turns it into a 403.

        DRF evaluates permissions in ``check_permissions``, BEFORE the handler runs, so
        a stale-elevation request would otherwise 403 with no entry — breaking
        ``AdminAPIView``'s "exactly one entry per unsafe request, including denials".
        Mirrors the creation, transition, commercial and delegation-mint views.

        Only for a request that AUTHENTICATED. An anonymous call is a 401 about
        identity, and a CSRF failure is rejected inside authentication itself; both are
        refused before any administrative decision could exist, and neither may
        manufacture an audit actor the plane cannot name. THE BODY IS NEVER READ here
        either: a denial is recorded from the request's authority, not from a payload
        the endpoint has just refused to act on.
        """
        if getattr(request, 'successful_authenticator', None):
            restaurant_id = self.kwargs.get('restaurant_id')
            self.audit(
                request,
                self.audit_action,
                result=RESULT_DENIED,
                resource_type='Restaurant',
                resource_id=str(restaurant_id or ''),
                restaurant_id=restaurant_id,
                reason=message or 'Recent re-authentication required.',
                error_code='elevation_required',
            )
        return super().permission_denied(request, message=message, code=code)

    # --- request plumbing ---

    def resolve_restaurant(self, restaurant_id):
        """The live target, or ``None``. Soft-deleted rows are excluded here."""
        return Restaurant.objects.filter(id=restaurant_id, deleted=False).first()

    def prepare(self, request, restaurant_id):
        """
        Resolve, parse and validate. Returns ``(restaurant, data, error_response)``.

        ORDERING IS LOAD-BEARING and matches the commercial and subscription-terms
        endpoints: the TARGET is resolved FIRST, so an unreadable or invalid body aimed
        at a restaurant that does not exist is still a silent 404 rather than an audit
        row about a tenant that was never touched — and so this route cannot be used as
        a media-type or body-validation oracle for which restaurant ids are real.
        """
        restaurant = self.resolve_restaurant(restaurant_id)
        if restaurant is None:
            return None, None, _not_found()

        payload, unreadable = read_request_body(request)
        if unreadable is not None:
            status, error_code, detail = unreadable
            self._audit_failure(
                request, restaurant,
                # No reason can be read from a body that would not parse, and one must
                # never be invented.
                reason='', error_code=error_code, before=None,
            )
            return restaurant, None, Response(
                {
                    'status': status,
                    'message': 'The request could not be applied.',
                    'errors': {'__all__': [detail]},
                },
                status=status,
            )

        serializer = OwnerInvitationRequestSerializer(data=payload)
        if not serializer.is_valid():
            # `audit_reason()` records the reason ONLY if the reason field itself
            # validated, and then its NORMALIZED value — never the raw one the endpoint
            # has just refused.
            self._audit_failure(
                request, restaurant,
                reason=serializer.audit_reason(),
                error_code=serializer.audit_error_code(),
                before=None,
            )
            return restaurant, None, Response(
                {
                    'status': 400,
                    'message': 'The request could not be applied.',
                    'errors': serializer.errors,
                },
                status=400,
            )
        return restaurant, serializer.validated_data, None

    # --- audit ---

    def _audit_failure(self, request, restaurant, *, reason, error_code, before):
        """
        One failure row for a refused operation.

        ``before`` is THIS restaurant's actual head invitation, read by the caller —
        never anything reconstructed from ``exc.details``, which can legitimately name
        the CURRENT invitation the caller has no business being handed as though it
        were the one they asked about. It is ``None`` on the paths where the request
        never got far enough to have looked (an unreadable body, a rejected payload):
        recording a state there would imply the endpoint inspected something it did
        not.

        No ``after_state`` on any failure: nothing was applied, and inventing one would
        record a change that never happened.
        """
        self.audit(
            request,
            self.audit_action,
            result=RESULT_FAILURE,
            resource_type='Restaurant',
            resource_id=str(restaurant.id),
            restaurant_id=restaurant.id,
            reason=reason,
            error_code=error_code,
            before_state=_state(before) if before is not None else None,
        )

    def _audit_success(self, request, restaurant, *, reason, before, after):
        """One success row. Equal states on a no-op — the request happened, nothing moved."""
        self.audit(
            request,
            self.audit_action,
            result=RESULT_SUCCESS,
            resource_type='Restaurant',
            resource_id=str(restaurant.id),
            restaurant_id=restaurant.id,
            reason=reason,
            before_state=_state(before),
            after_state=_state(after),
        )

    def current_head(self, restaurant):
        """
        This restaurant's head invitation for a FAILURE audit's before-state, or ``None``.

        Only ever called on a refusal path, where the mutation did not happen, so an
        unlocked read cannot disagree with anything that was written. It goes through
        the SAME ``select_head_invitation`` the projection and the writers use, rather
        than a fourth opinion about which invitation is current.

        Fails soft: a restaurant with no onboarding row, or legacy provenance, has no
        head to report and the audit simply carries no before-state. That is the whole
        point of those two refusals — there was no invitation lifecycle to be in.
        """
        onboarding = (
            RestaurantOnboarding.objects.filter(restaurant=restaurant).first()
        )
        if onboarding is None or onboarding.source != ONBOARDING_SOURCE_ADMIN_CREATED:
            return None
        return select_head_invitation(onboarding, restaurant)

    # --- domain outcomes ---

    def domain_error_response(self, exc, status):
        """
        The refusal, as an HTTP response.

        A 400 is about the CALLER'S OWN input, so the domain's explanation is passed
        through and attributed to the request field it names — but only when that field
        is one this endpoint actually accepts, so a future detail key cannot leak into
        the response by default.

        A 409 gets a FIXED sentence instead, and carries no ``details``. The domain's
        conflict details legitimately name the id of the invitation that IS current,
        which is exactly the value a stale client would then use to retry blindly
        rather than reloading and looking at what changed. The client's remedy is
        ``GET admin/v1/restaurants/<id>/``, which shows the id beside the state that
        makes it meaningful.
        """
        if status == 409:
            return Response(
                {
                    'status': status,
                    'message': CONFLICT_MESSAGES.get(exc.code, exc.message),
                    'code': exc.code,
                },
                status=status,
            )
        field = ERROR_FIELD_BY_CODE.get(exc.code, '__all__')
        return Response(
            {
                'status': status,
                'message': 'The request could not be applied.',
                'code': exc.code,
                'errors': {field: [exc.message]},
            },
            status=status,
        )

    def refuse(self, request, restaurant, exc, reason):
        """
        Map, audit and answer a refused domain call. Returns ``(response, reraise)``.

        A code absent from ``STATUS_BY_CODE`` is signalled back for the caller to
        re-raise, so an unmapped internal condition surfaces as a 500 and rolls the
        outer transaction back rather than being relabelled a client error.
        """
        status = STATUS_BY_CODE.get(exc.code)
        if status is None:
            return None, True
        self._audit_failure(
            request, restaurant, reason=reason, error_code=exc.code,
            before=self.current_head(restaurant),
        )
        return self.domain_error_response(exc, status), False

    # --- the canonical projection ---

    def read_onboarding(self, restaurant):
        """
        The canonical Step-2C ``onboarding`` object for one restaurant.

        Delegates to ``onboarding_reads`` rather than assembling a write-path shape: one representation for reads and successful writes, so the
        ``expected_invitation_id`` a client gets back from a write is byte-identical to
        the one a subsequent GET would have given it, derived by the same evidence
        rules from the rows that were just written.
        """
        return onboarding_summary(restaurant)


class AdminOwnerInvitationReissueView(_OwnerInvitationWriteView):
    """
    ``POST`` to rotate this restaurant's owner claim credential.

    Supersedes whatever unresolved invitation the onboarding represents, mints a fresh
    one for the CURRENT canonical owner, and returns its raw claim token EXACTLY ONCE.

    ━━ THE RESPONSE CARRIES THE ONLY COPY OF A CREDENTIAL ━━━━━━━━━━━━━━━━━━━━━━━━

    Only the token's SHA-256 hash is persisted, so if this response is lost the
    platform holds a valid credential nobody knows. THE REMEDY IS TO REISSUE AGAIN —
    which supersedes that unknown credential and mints a known one — never to recover
    plaintext that was never stored, and never to hand back the hash as though it were
    a token. That is what makes rotation the right primitive and "resend" the wrong
    word for it.

    NO CLAIM URL IS FABRICATED. A token is a credential; a URL is a product promise,
    and the customer-plane redemption route does not exist yet (Step 2F).
    """

    audit_action = ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED

    def post(self, request, restaurant_id):
        restaurant, data, error = self.prepare(request, restaurant_id)
        if error is not None:
            return error

        reason = data['reason']

        # ONE OUTER TRANSACTION: DOMAIN + AUDIT. The service's own atomic block nests
        # as a savepoint inside it, which is exactly why the audit write does not live
        # in the service: if the audit insert fails, the supersede AND the new
        # invitation roll back with it, because a credential rotation nobody can be
        # shown to have decided must not be allowed to stand. The same structure is
        # what lets a REFUSED reissue still be recorded — the domain exception unwinds
        # only its own savepoint.
        with transaction.atomic():
            try:
                result = onboarding_invitations.reissue_owner_invitation(
                    restaurant_id=restaurant.id,
                    expected_invitation_id=data['expected_invitation_id'],
                    actor=request.user,
                    reason=reason,
                )
            except onboarding_invitations.OwnerInvitationError as exc:
                if exc.code in onboarding_invitations.NOT_FOUND_CODES:
                    # Unreachable in practice — the target was resolved before the
                    # body was read — but a soft-delete committing in between lands
                    # here. 404 and NOT audited, matching the transition endpoint:
                    # nothing was denied and no tenant was touched.
                    return _not_found()
                response, reraise = self.refuse(request, restaurant, exc, reason)
                if reraise:
                    raise
                return response
            except OwnerConsistencyError as exc:
                # A drifted tenant is a 409, and the reissue is refused rather than
                # repaired. `assert_owner_consistency` validates and never fixes; who
                # owns a business is a decision with an actor and a reason behind it.
                self._audit_failure(
                    request, restaurant, reason=reason, error_code=exc.code,
                    before=self.current_head(restaurant),
                )
                return Response(
                    {
                        'status': 409,
                        'message': _OWNER_CONSISTENCY_MESSAGE,
                        'code': exc.code,
                    },
                    status=409,
                )

            # THE STATES. `before` is the invitation this rotation replaced, as the
            # request found it — reconstructed from the result rather than re-read,
            # because by now the row carries its `superseded_at` stamp and re-reading
            # would record the outcome in the before-state. `after` is the new one.
            before = _reissued_before_state(result)
            after = {
                'owner_invitation': {
                    'status': _PENDING,
                    'id': str(result.invitation.id),
                    'issued_at': result.invitation.issued_at.isoformat(),
                    'expires_at': result.invitation.expires_at.isoformat(),
                },
            }
            self.audit(
                request,
                self.audit_action,
                result=RESULT_SUCCESS,
                resource_type='Restaurant',
                resource_id=str(restaurant.id),
                restaurant_id=restaurant.id,
                reason=reason,
                before_state=before,
                after_state=after,
            )

            onboarding = self.read_onboarding(restaurant)

        response = Response(
            {
                'status': 200,
                'message': 'Owner invitation reissued.',
                'data': {
                    'changed': True,
                    'onboarding': onboarding,
                    'owner_invitation': {
                        'id': str(result.invitation.id),
                        'issued_at': result.invitation.issued_at.isoformat(),
                        'expires_at': result.invitation.expires_at.isoformat(),
                        # THE RAW CREDENTIAL, in the one place it will ever appear.
                        # Kept in its own object rather than merged into `onboarding`
                        # so no future change to the canonical projection can start
                        # carrying it by accident.
                        'claim_token': result.claim_token,
                    },
                },
            },
            status=200,
        )
        # Stamped explicitly rather than through `misc_app.controllers.http.no_store`,
        # which also varies on the diner capability headers — meaningless on this
        # plane, and a Vary header describing a credential this response does not use
        # would be a small lie in a place that should be exact. Matches the creation
        # endpoint, which carries the same kind of payload.
        response['Cache-Control'] = 'no-store, private'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response


class AdminOwnerInvitationCancelView(_OwnerInvitationWriteView):
    """
    ``POST`` to terminate this restaurant's unresolved owner claim credential.

    Stamps ``cancelled_at``/``cancelled_by`` and creates NO replacement. Reopening the
    onboarding later is a separate, deliberate decision — reissue — and conflating the
    two would make "cancel" quietly mean "rotate".

    THE RESPONSE CARRIES NO CREDENTIAL and never will, so it needs no no-store header
    of its own: there is nothing in it a cache could leak. It is also why an EXACT
    RETRY is safe here and impossible on reissue — repeating a cancellation returns
    ``changed=false`` with the original timestamp and actor intact, because the
    terminal event happened once and the record must not be able to say it happened
    twice.

    CANCELLATION DOES NOT REQUIRE OWNER CONSISTENCY, deliberately. See
    ``onboarding_invitations.cancel_owner_invitation``: revoking a credential must stay
    possible exactly when a tenant's state is messy.
    """

    audit_action = ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED

    def post(self, request, restaurant_id):
        restaurant, data, error = self.prepare(request, restaurant_id)
        if error is not None:
            return error

        reason = data['reason']

        with transaction.atomic():
            try:
                result = onboarding_invitations.cancel_owner_invitation(
                    restaurant_id=restaurant.id,
                    expected_invitation_id=data['expected_invitation_id'],
                    actor=request.user,
                    reason=reason,
                )
            except onboarding_invitations.OwnerInvitationError as exc:
                if exc.code in onboarding_invitations.NOT_FOUND_CODES:
                    return _not_found()
                response, reraise = self.refuse(request, restaurant, exc, reason)
                if reraise:
                    raise
                return response

            # THE STATES, both describing THE SAME INVITATION as this request found it
            # and left it. A real cancellation moves it from its prior state to
            # `cancelled`; an EXACT RETRY moves nothing, so both read `cancelled` and
            # carry the ORIGINAL timestamps. Replaying the prior state on a retry would
            # write the cancellation into the log a second time, as though the
            # credential had been terminated twice.
            invitation = result.invitation
            after = _cancelled_state(invitation)
            before = (
                _prior_state(invitation, result.changed) if result.changed else after
            )
            self.audit(
                request,
                self.audit_action,
                result=RESULT_SUCCESS,
                resource_type='Restaurant',
                resource_id=str(restaurant.id),
                restaurant_id=restaurant.id,
                reason=reason,
                before_state=before,
                after_state=after,
            )

            onboarding = self.read_onboarding(restaurant)

        return Response(
            {
                'status': 200,
                'message': 'Owner invitation cancelled.',
                'data': {'changed': result.changed, 'onboarding': onboarding},
            },
            status=200,
        )


# --- audit state helpers -----------------------------------------------------
#
# Built from the RESULT rather than by re-reading, because by the time these run the
# rows carry the stamps this request just wrote — and a before-state re-read after the
# write would describe the outcome rather than what the request found.

_PENDING = 'pending'
_EXPIRED = 'expired'
_CANCELLED = 'cancelled'


def _snapshot(invitation, status):
    """One invitation, in the same shape the read publishes. No credential material."""
    return {
        'owner_invitation': {
            'status': status,
            'id': str(invitation.id),
            'issued_at': invitation.issued_at.isoformat(),
            'expires_at': invitation.expires_at.isoformat(),
        },
    }


def _reissued_before_state(result):
    """
    The invitation a reissue replaced, as the request FOUND it.

    ``head`` and ``head_status`` are captured by the service under the lock, BEFORE any
    stamp it wrote, and are used here rather than a re-read for a specific reason: a
    superseded row can no longer say whether it was a live link or an expired one a
    moment ago, and "I killed a live credential" and "I replaced a dead one" are
    different operational events an operator will want to tell apart a year later.

    A reissue from a CANCELLED or historically-CONSUMED head superseded nothing, and
    the before-state is that head exactly as it stands — unmodified, because reissue
    never edits a resolved row.
    """
    return _snapshot(result.head, result.head_status)


def _cancelled_state(invitation):
    return _snapshot(invitation, _CANCELLED)


def _prior_state(invitation, changed):
    """
    The state a just-cancelled invitation was in a moment ago: ``pending`` or
    ``expired``, decided against the SAME ``expires_at`` the domain compared.

    Only ever called for a real cancellation — a retry's before-state is its
    after-state, and there is no prior transition to describe.
    """
    status = (
        _EXPIRED if invitation.expires_at <= invitation.cancelled_at else _PENDING
    )
    return _snapshot(invitation, status)
