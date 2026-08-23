"""
The two preconditions every commercial mutation shares (Phase 1, Step 3C).

Both services in this app begin the same way: lock the canonical ``Restaurant`` row,
and resolve the persisted ``User`` whose decision is being recorded. They live here so
the rule is stated once and both writers demonstrably obey the same one.

━━ THE RESTAURANT ROW IS THE SERIALIZATION POINT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every commercial mutation serializes on the tenant row, exactly as
``platform_admin_app.onboarding_adoption`` does and for the same reason: the
commercial child rows may not exist yet, so the ``Restaurant`` is the only thing two
concurrent operators are guaranteed to share. Locking it turns every race in this
domain — two operators setting a value, two recording first terms, two replacing the
same terms — into a queue with a well-defined winner and a clean domain refusal for
the loser, rather than a lost update or a leaked ``IntegrityError``.

LOCK ORDER, and why it is compatible with what already exists:

    commercial mutation      Restaurant -> RestaurantServiceConfiguration
    commercial mutation      Restaurant -> RestaurantSubscriptionTerms
    lifecycle transition     advisory(restaurant) EXCLUSIVE -> Restaurant -> AdminAuditLog
    legacy adoption          Restaurant -> RestaurantOnboarding -> AdminAuditLog

A commercial mutation takes the ``Restaurant`` row and then only its own child
tables, which nothing else locks — so it extends the documented global order at its
tail and cannot cycle against it.

**IT MUST NEVER REACH FOR THE ADMISSION ADVISORY LOCK AFTERWARDS.** The lifecycle
transition takes that lock FIRST and the ``Restaurant`` row second; a transaction that
took the row first and then reached for the advisory lock would invert that order and
reintroduce exactly the cycle ``restaurants_app.controllers.admission_lock`` documents.
Take it first or not at all — and this domain does not need it at all:

WHY NO ADMISSION BARRIER TODAY. ``lock_admission_exclusive`` exists to stop an order
being ADMITTED against one lifecycle state or test classification and then WRITTEN
under another. Nothing in the order path reads payment timing or collection mode —
they have no runtime order-admission effect yet — so taking that lock would enrol this
domain in a lock-ordering domain it has no business in, and every lock a transaction
holds is a lock some future transaction can deadlock against. WHEN payment timing is
eventually enforced in the order/kitchen path, changing it while live WILL need
integration with the barrier; that is a future runtime concern and must not be
pretended into existence now.

THE PAYOFF FOR WHAT COMES NEXT. Because every commercial write holds this row, a
future lifecycle go-live (which already locks it) can evaluate commercial readiness
without the answer changing between the check and the commit, and a future owner
go-live approval that uses the same serialization point cannot snapshot a moving
target.

━━ WHAT THIS MODULE DELIBERATELY DOES NOT DO ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

It does not authorize. ``resolve_actor`` answers *who performed this operation*, never
*was this caller allowed to*. Authorization — platform-staff account type, an
``AdminSession``, recent elevation, CSRF, a reason — belongs to the control-plane
adapter that will wrap these services, and encoding one plane's answer here would make
a reusable business mutation usable from exactly one caller.
"""
import uuid as uuid_module

from django.db import transaction

from commercial_app import errors
from commercial_app.errors import CommercialMutationError


def parse_uuid(raw, *, code, message):
    """
    Parse an identifier strictly, BEFORE any database access.

    Targeting is always one immutable UUID: no name, no fuzzy match, no ``.first()``
    over candidates. The wrong-tenant failure is silent — a commercial fact recorded
    against the wrong restaurant does not error, it just permanently misstates that
    tenant's terms — so the identifier is never allowed to be approximate.
    """
    if isinstance(raw, uuid_module.UUID):
        return raw
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise CommercialMutationError(code, message)
    try:
        return uuid_module.UUID(cleaned)
    except (ValueError, AttributeError, TypeError):
        raise CommercialMutationError(code, message)


def lock_restaurant(restaurant_id):
    """
    Resolve and LOCK the target restaurant. Returns the locked ``Restaurant``.

    MUST be called inside a transaction. A ``select_for_update`` in autocommit is
    released by the very statement that took it, so the caller would hold nothing and
    be told nothing — the guard lives here, in the primitive, rather than in each
    service, because a silently ineffective lock is worse than no lock at all: every
    test would still pass. The same reasoning
    ``restaurants_app.controllers.admission_lock`` gives for its own assertion.

    NO ``select_related`` IS CHAINED, deliberately. On PostgreSQL a
    ``select_for_update`` over a join locks every row in the join unless ``of=`` is
    given — the defect PR-E found in delegation redemption, where a
    ``select_related('administrator', 'restaurant')`` silently held two extra row
    locks and closed a real ABBA cycle. This locks the ``restaurants`` row and
    nothing else.

    THE SOFT-DELETE CHECK IS ON THE LOCKED ROW, not on a copy read earlier: the row is
    fetched without a ``deleted`` filter precisely so a soft-deleted tenant can be
    refused with its own code instead of collapsing into "not found".
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'lock_restaurant must be called inside a transaction — a '
            'select_for_update taken in autocommit is released immediately and '
            'serializes nothing.'
        )

    # Imported lazily so this module stays importable without pulling the restaurant
    # app's model graph in at import time, matching the cross-app convention on the
    # order path and in `restaurants_app.controllers.lifecycle`.
    from restaurants_app.models import Restaurant

    restaurant_uuid = parse_uuid(
        restaurant_id,
        code=errors.INVALID_RESTAURANT_ID,
        message='The target must be a restaurant UUID.',
    )

    restaurant = (
        Restaurant.objects.select_for_update().filter(pk=restaurant_uuid).first()
    )
    if restaurant is None:
        raise CommercialMutationError(
            errors.RESTAURANT_NOT_FOUND,
            'No such restaurant.',
            {'restaurant_id': str(restaurant_uuid)},
        )
    if restaurant.deleted:
        raise CommercialMutationError(
            errors.RESTAURANT_DELETED,
            'This restaurant is deleted and its commercial configuration cannot '
            'be changed.',
            {'restaurant_id': str(restaurant_uuid)},
        )
    return restaurant


def resolve_actor(actor):
    """
    The persisted ``User`` whose decision this is, re-read from the database.

    RE-READ RATHER THAN TRUSTED: the caller hands in an instance, and an instance can
    be unsaved, or can name a row that has since gone. Letting either reach the
    ``PROTECT`` foreign key would surface as a raw ``IntegrityError`` or a
    ``ValueError`` from an unsaved related object — an ugly 500 where a named domain
    refusal belongs.

    IT IS NOT AN AUTHORIZATION CHECK, and the absences are deliberate: no
    ``account_type``, no ``is_active``, no session, no elevation. The adoption writer
    checks those because it writes the audit row and therefore owns the attribution;
    these services write no audit row and are not Admin-specific, so the control-plane
    adapter owns eligibility. Encoding "must be platform staff" here would also
    pre-empt the open question of whether a restaurant eventually gets a say in its
    own service model.
    """
    from users_app.models import User

    if not isinstance(actor, User) or actor.pk is None:
        raise CommercialMutationError(
            errors.INVALID_ACTOR,
            'An actor is required: a commercial mutation is an attributed decision.',
        )

    fresh = User.objects.filter(pk=actor.pk).first()
    if fresh is None:
        raise CommercialMutationError(
            errors.INVALID_ACTOR,
            'The actor account no longer exists.',
            {'actor_id': str(actor.pk)},
        )
    return fresh
