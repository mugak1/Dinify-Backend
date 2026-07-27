"""
The restaurant lifecycle transition service — the ONLY writer of ``Restaurant.status``.

No other code path may assign that field. It is read_only on
``SerializerPutRestaurant`` and absent from ``EDIT_INFORMATION``, so the generic
Secretary edit path cannot reach it either; those two are the enforcement, this
module is the door.

WHAT A TRANSITION IS. Four things happen together or not at all: the matrix is
checked against the row's CURRENT state, the state-specific preconditions are
checked, the row is written, and an ``AdminAuditLog`` entry is recorded. All of it
runs inside one ``transaction.atomic()`` under ``select_for_update``, so two
administrators transitioning the same restaurant serialize rather than racing, and
a failed audit write unwinds the transition it would have described (the audit
service raises rather than swallowing — see ``platform_admin_app.audit``).

DENIALS ARE AUDITED TOO. A refused transition writes ``transition_denied`` and
then raises. An attempt to suspend a tenant that was rejected on a stale from-state
is exactly the event the log exists to hold; returning a bare 400 with no trace
would lose it.

THE TWO SEAMS. Go-live readiness and outstanding receivables are Phase-1 concerns
whose models do not exist yet. Both are single named functions with a real call
site, deliberately NOT stubbed inline at the point of use — when Phase 1 lands, it
fills in the function body and every caller inherits it.
"""
import logging

from django.db import transaction

from restaurants_app.controllers.admission_lock import lock_admission_exclusive
from dinify_backend.configss.string_definitions import (
    RESTAURANT_LIFECYCLE_STATES,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)

logger = logging.getLogger(__name__)

# Minimum substance for a stated reason. Mirrors
# ``platform_admin_app.delegation.MIN_REASON_LENGTH`` byte-for-byte — a reason is a
# reason, and the two surfaces should not disagree about how long one has to be.
MIN_REASON_LENGTH = 10

# The blocker `check_go_live_readiness` reports while its checklist is unbuilt. A
# named constant because it travels to the caller inside `errors['blockers']` and the
# tests assert on it — Phase 1 removes it when it fills the seam in.
BLOCKER_READINESS_NOT_CONFIGURED = 'readiness_not_configured'

# --- the matrix -------------------------------------------------------------
#
# | From       | To         | Allowed                                            |
# |------------|------------|----------------------------------------------------|
# | onboarding | live       | Yes — readiness-gated                              |
# | onboarding | offboarded | Yes                                                |
# | live       | suspended  | Yes — reason required (as all transitions are)     |
# | suspended  | live       | Yes                                                |
# | live       | offboarded | Yes — elevated confirmation, no receivables        |
# | suspended  | offboarded | Yes — same conditions                              |
# | offboarded | live       | NO — restoration is re-onboarding, not a transition |
#
# Anything absent is refused. In particular there is no self-transition: moving a
# restaurant to the state it is already in is a no-op dressed as an action, and
# would put a misleading row in the audit log.
ALLOWED_TRANSITIONS = frozenset({
    (RestaurantStatus_Onboarding, RestaurantStatus_Live),
    (RestaurantStatus_Onboarding, RestaurantStatus_Offboarded),
    (RestaurantStatus_Live, RestaurantStatus_Suspended),
    (RestaurantStatus_Suspended, RestaurantStatus_Live),
    (RestaurantStatus_Live, RestaurantStatus_Offboarded),
    (RestaurantStatus_Suspended, RestaurantStatus_Offboarded),
})

# Transitions the caller must have recently re-authenticated for. Enforced at the
# endpoint by ``IsRecentlyElevated`` (a permission class cannot see the target's
# from-state, so it gates the whole endpoint); named here so the requirement is
# discoverable from the service and can be asserted in a test.
ELEVATION_REQUIRED_TARGETS = frozenset({RestaurantStatus_Offboarded})


class LifecycleTransitionError(ValueError):
    """
    A refused transition.

    ``errors`` is the field-keyed dict an endpoint renders; ``code`` is the short
    machine-readable reason that goes into the audit entry's ``error_code``, matching
    ``platform_admin_app.delegation.DelegationValidationError``.
    """

    def __init__(self, errors, code=''):
        self.errors = errors if isinstance(errors, dict) else {'detail': str(errors)}
        self.code = code
        super().__init__(self.errors)


# --- Phase-1 seams ----------------------------------------------------------

class ReadinessResult:
    """The outcome of a go-live readiness evaluation: ``ready`` plus the blockers."""

    __slots__ = ('ready', 'blockers')

    def __init__(self, ready, blockers=None):
        self.ready = bool(ready)
        self.blockers = list(blockers or [])


def check_go_live_readiness(restaurant) -> ReadinessResult:
    """
    Whether ``restaurant`` may move ``onboarding -> live``. SEAM — always ready today.

    PHASE 1 WIRES THIS. The hard blockers are specified but their prerequisites do
    not exist yet: owner credentials established, at least one published and
    available menu item, at least one enabled table with a valid current-``qr_version``
    QR, a successful end-to-end test order, payment mode set, and a subscription
    agreement created (``RestaurantSubscription`` is a Phase-1 model). Implementing
    the checklist against half its inputs would produce a gate that is wrong in both
    directions, so this PR builds the CALL SITE and leaves the body.

    Returning a ``ReadinessResult`` rather than a bool is the point of the seam: the
    blocker list is what the portal renders, and callers already handle it.

    IT FAILS CLOSED. Until that checklist exists this returns NOT ready, so
    ``onboarding -> live`` is refused on every path. It used to return ready
    unconditionally, which meant the one gate standing between a half-built
    restaurant and real diners was a stub that always said yes — a safety seam that
    fails open is worse than no seam, because it reads as protection.

    Nothing is stranded by this: there is currently no API path that creates a
    restaurant at all, and the one production restaurant is already ``live``.
    Deliberately there is no override — a bypass built "just until Phase 1" is
    exactly the kind that outlives its reason. See BACKGROUND_TASKS.md.
    """
    return ReadinessResult(ready=False, blockers=[BLOCKER_READINESS_NOT_CONFIGURED])


def has_outstanding_receivables(restaurant) -> bool:
    """
    Whether ``restaurant`` still owes Dinify money. SEAM — always ``False`` today.

    PHASE 1 WIRES THIS to overdue ``SubscriptionInvoice`` rows. Those models do not
    exist yet (§8 of the specification), and ``DinifyTransaction`` is explicitly never
    the receivable — its status axis is dead and it carries custodial residue — so
    there is nothing correct to query today.

    Named and called rather than inlined at the two offboarding branches so Phase 1
    changes one function body instead of hunting call sites.
    """
    return False


# --- validation -------------------------------------------------------------

def _validate_reason(reason):
    cleaned = (reason or '').strip()
    if not cleaned:
        raise LifecycleTransitionError(
            {'reason': 'A reason is required.'}, code='reason_required',
        )
    if len(cleaned) < MIN_REASON_LENGTH:
        raise LifecycleTransitionError(
            {'reason': f'Please state a reason of at least {MIN_REASON_LENGTH} characters.'},
            code='reason_too_short',
        )
    return cleaned


def _validate_target(to_state):
    if to_state not in RESTAURANT_LIFECYCLE_STATES:
        raise LifecycleTransitionError(
            {'to_state': f'Must be one of: {", ".join(RESTAURANT_LIFECYCLE_STATES)}.'},
            code='unknown_state',
        )
    return to_state


def is_allowed(from_state, to_state) -> bool:
    """Whether ``from_state -> to_state`` is in the matrix. Pure; no DB access."""
    return (from_state, to_state) in ALLOWED_TRANSITIONS


def allowed_targets(from_state):
    """The states reachable from ``from_state``, sorted — what a portal offers."""
    return sorted(
        target for source, target in ALLOWED_TRANSITIONS if source == from_state
    )


def _check_transition_preconditions(restaurant, from_state, to_state):
    """
    The state-specific conditions layered on top of the matrix.

    Runs INSIDE the transaction, against the locked row, so a readiness or
    receivables answer cannot go stale between the check and the write.
    """
    if to_state == RestaurantStatus_Live and from_state == RestaurantStatus_Onboarding:
        readiness = check_go_live_readiness(restaurant)
        if not readiness.ready:
            raise LifecycleTransitionError(
                {'to_state': 'This restaurant is not ready to go live.',
                 'blockers': readiness.blockers},
                code='not_ready_for_go_live',
            )

    if to_state == RestaurantStatus_Offboarded and has_outstanding_receivables(restaurant):
        raise LifecycleTransitionError(
            {'to_state': 'This restaurant has outstanding receivables and cannot be '
                         'offboarded until they are settled.'},
            code='outstanding_receivables',
        )


# --- audit ------------------------------------------------------------------

def _audit(request, action, *, restaurant, actor, result, reason='',
           before_state=None, after_state=None, error_code=''):
    """
    Write the audit entry for a transition or a refusal.

    Imported lazily: ``platform_admin_app.audit`` pulls in the admin models, and this
    controller is imported by the customer plane (the endpoint, and the tests that
    drive transitions directly). Keeping the import at call time avoids binding the
    two app graphs together at module scope.
    """
    from platform_admin_app.audit import record, record_from_request

    payload = dict(
        result=result,
        resource_type='Restaurant',
        resource_id=str(restaurant.id),
        restaurant_id=restaurant.id,
        reason=reason,
        before_state=before_state,
        after_state=after_state,
        error_code=error_code,
    )
    if request is not None:
        return record_from_request(request, action, **payload)
    # No request (a management command, or a service-level caller): the actor still
    # has to be named, so it is passed explicitly rather than left to the request.
    return record(action=action, actor=actor, **payload)


def _notify_owner_of_go_live(restaurant):
    """
    Tell the owner their restaurant is live.

    Preserves the notification that used to fire from ``Secretary.make_notification``
    on a pending->active status edit. That branch is unreachable now that ``status``
    has left ``EDIT_INFORMATION``, so the behaviour moves here rather than being
    silently dropped — going live is precisely when the owner wants to hear from us.

    Best-effort by design: a notification backend problem must not roll back a
    completed lifecycle transition. This runs AFTER the transaction commits.

    There is no counterpart for the old ``restaurant-rejected`` message: ``rejected``
    is one of the states the new vocabulary does not have.
    """
    from misc_app.controllers.notifications.notification import Notification

    owner = getattr(restaurant, 'owner', None)
    if owner is None:
        logger.warning(
            "Go-live notification skipped (restaurant_id=%s): owner is missing.",
            restaurant.id,
        )
        return
    try:
        Notification(msg_data={
            'msg_type': 'restaurant-activated',
            # Production rows exist with an empty/null first name; the greeting is
            # not worth failing a notification over.
            'first_name': owner.first_name or 'there',
            'restaurant_id': str(restaurant.id),
            'restaurant_name': restaurant.name,
            'user_id': str(owner.id),
        }).create_notification()
    except Exception as error:  # noqa: BLE001 - never let a notification break a transition
        logger.error(
            "Go-live notification failed (restaurant_id=%s): %s", restaurant.id, error,
        )


# --- the transition ---------------------------------------------------------

def transition_restaurant(*, restaurant, to_state, reason, actor=None, request=None):
    """
    Move ``restaurant`` to ``to_state``. Returns the refreshed ``Restaurant``.

    Raises ``LifecycleTransitionError`` — having first written a ``transition_denied``
    audit entry — when the target is unknown, the reason is missing or too short, the
    pair is not in the matrix, or a state-specific precondition fails.

    The from-state is read from the LOCKED row inside the transaction, never from the
    instance the caller happened to be holding: two administrators acting at once must
    not both see ``live`` and both succeed.

    ``actor`` names who did it when there is no request (a management command). With a
    request, actor / session / request-id / IP / user-agent come from it automatically.
    """
    from restaurants_app.models import Restaurant
    from platform_admin_app.audit_actions import (
        ADMIN_RESTAURANT_LIFECYCLE_TRANSITION,
        ADMIN_RESTAURANT_TRANSITION_DENIED,
    )
    from platform_admin_app.models import RESULT_DENIED, RESULT_SUCCESS

    def _deny(exc, from_state):
        # The refusal and its audit entry share a transaction of their own, so a
        # failed audit write cannot leave a denial unrecorded.
        with transaction.atomic():
            _audit(
                request, ADMIN_RESTAURANT_TRANSITION_DENIED,
                restaurant=restaurant, actor=actor, result=RESULT_DENIED,
                reason=(reason or '').strip(),
                before_state={'status': from_state},
                after_state={'requested_status': to_state},
                error_code=exc.code,
            )
        raise exc

    # Cheap, request-shaped validation first, against the caller's instance: a bad
    # target or a missing reason needs no row lock to refuse.
    try:
        target = _validate_target(to_state)
        cleaned_reason = _validate_reason(reason)
    except LifecycleTransitionError as exc:
        _deny(exc, restaurant.status)

    went_live = False
    with transaction.atomic():
        # THE ADMISSION BARRIER, taken FIRST — before the row lock below, because
        # the advisory lock is the single top level of the documented order
        # (advisory -> Restaurant -> AdminAuditLog) and the order paths take the
        # same lock first too.
        #
        # Exclusive here, shared there: any number of orders may be admitted at
        # once, but a transition excludes them all. Once this returns, every
        # admission is either finished (committed or rolled back) or has not yet
        # read a status — so none can be admitted against the state this
        # transaction is about to invalidate. Without it, suspending a restaurant
        # left orders already past their gate still writing themselves into the
        # kitchen, and the operator was told trading had stopped when it had not.
        #
        # A row lock could not have done this job: the order path does not touch
        # the `restaurants` row, and this transition arrives on the ADMIN plane in
        # a different mod_wsgi daemon process. Advisory locks are database-global,
        # which is exactly the scope the two planes share.
        lock_admission_exclusive(restaurant.pk)

        locked = Restaurant.objects.select_for_update().get(pk=restaurant.pk)
        from_state = locked.status

        if not is_allowed(from_state, target):
            # A refusal is CAPTURED here and raised only after this block exits.
            # _deny writes its audit row in an atomic block of its own, and raising
            # from inside this one would mark the outer transaction for rollback and
            # take the denial entry down with it.
            error = LifecycleTransitionError(
                {'to_state': f'A restaurant cannot move from {from_state} to {target}.',
                 'allowed': allowed_targets(from_state)},
                code='transition_not_allowed',
            )
        else:
            error = None
            try:
                _check_transition_preconditions(locked, from_state, target)
            except LifecycleTransitionError as exc:
                error = exc

        if error is None:
            locked.status = target
            # `time_last_updated` is auto_now, so Django stamps it itself — but a
            # field omitted from update_fields is not written, so it has to be named.
            locked.save(update_fields=['status', 'time_last_updated'])

            _audit(
                request, ADMIN_RESTAURANT_LIFECYCLE_TRANSITION,
                restaurant=locked, actor=actor, result=RESULT_SUCCESS,
                reason=cleaned_reason,
                before_state={'status': from_state},
                after_state={'status': target},
            )
            went_live = (
                from_state == RestaurantStatus_Onboarding
                and target == RestaurantStatus_Live
            )

    if error is not None:
        _deny(error, from_state)

    if went_live:
        _notify_owner_of_go_live(locked)

    restaurant.status = locked.status
    return locked
