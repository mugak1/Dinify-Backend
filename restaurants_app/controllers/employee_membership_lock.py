"""
The serialization barrier for ``RestaurantEmployee`` membership mutations.

WHAT THIS EXISTS TO PROTECT
───────────────────────────────────────────────────────────────────────────────
``platform_admin_app.onboarding.assert_owner_consistency`` asks one question of a
restaurant: among its ``active=True``, ``deleted=False`` memberships, does exactly
one carry ``RESTAURANT_OWNER``, and is that membership's user ``Restaurant.owner``?

Its own docstring is explicit that the answer "is a snapshot, valid for the instant
it was read", and that a MUTATING caller must establish the transaction and locking
discipline around it. The onboarding writers do their half — ``onboarding_adoption``
and ``onboarding_invitations`` both take the ``Restaurant`` row FIRST and call the
assertion inside that transaction. Nothing held up the other half: the customer
plane's membership writers took no ``Restaurant`` lock at all, so a membership could
change between the assertion and the consequential write it was guarding.

THE ``Restaurant`` ROW IS THE SERIALIZATION POINT, and it has to be
───────────────────────────────────────────────────────────────────────────────
Locking the membership rows the assertion read is NOT sufficient, and the reason is
worth stating rather than discovering twice:

  * every membership belongs to exactly one restaurant, so the parent row is the one
    thing every writer at that tenant is guaranteed to share;
  * a row lock over the rows that exist cannot predicate-lock a row that does not
    exist yet, and a REACTIVATION (``deleted=True -> False`` / ``active=False ->
    True``) produces a second live owner membership out of a row the assertion never
    read and never could have locked;
  * the onboarding writers already take exactly this row, so the customer plane
    joins an ordering that is already proven rather than inventing a second one.

MEASURED POSTGRESQL BEHAVIOUR, because half of this is already true by accident
───────────────────────────────────────────────────────────────────────────────
Against PostgreSQL 16, while one transaction holds ``restaurants`` row R
``FOR UPDATE``:

  ============================================  ==========  =========================
  concurrent statement on restaurant_employees  blocked?    why
  ============================================  ==========  =========================
  INSERT referencing R                          YES         referential integrity
                                                            takes ``FOR KEY SHARE``
                                                            on the parent row
  UPDATE that CHANGES the FK to R               YES         same RI re-check
  UPDATE that does not touch the FK             **NO**      keys unchanged, so the RI
                                                            trigger never fires
  DELETE of a child row                         **NO**      no parent RI check
  ============================================  ==========  =========================

So a membership INSERT is already serialized against a held ``Restaurant`` lock — but
only INCIDENTALLY, as a side effect of referential integrity, and only on PostgreSQL.
Every other membership mutation is not: role changes, deactivation, soft-deletion and
above all REACTIVATION all rewrite an existing row without touching its FK, so they
commit straight through a held parent lock. This helper makes the barrier EXPLICIT
for all of them, rather than leaving the invariant resting on an implementation
detail of one database's RI triggers.

LOCK ORDER
───────────────────────────────────────────────────────────────────────────────
``Restaurant -> RestaurantEmployee``. Never the inverse. This is a tail extension of
the documented global order, in exactly the shape ``restaurants_app.controllers.
tables`` already uses for table-number allocation (``Restaurant -> INSERT Table``):
it takes the ``Restaurant`` row and never afterwards reaches for the admission
advisory lock, so it can BLOCK the lifecycle transition (which holds the advisory
lock and then waits for this row) but can never cycle against it.

NO ADMISSION ADVISORY LOCK, deliberately. Membership management does not participate
in order admission — no order path reads a membership — and acquiring that lock after
the row lock would invert the documented ``advisory -> Restaurant`` order for nothing.

NO AUTHORIZATION LIVES HERE. Callers gate first (``check_permission``) and lock
second; this function answers "serialize me against the other writers at this tenant"
and nothing else. It never inspects the actor, never reads a role grid, and never
mutates a membership.
"""
import logging
import uuid as uuid_module

from django.db import transaction

logger = logging.getLogger(__name__)


def lock_restaurant_for_membership_mutation(restaurant_id):
    """
    Take the parent ``Restaurant`` row lock for a membership mutation.

    Returns the locked ``Restaurant``, or ``None`` when ``restaurant_id`` is absent,
    malformed or names no row.

    MUST be called inside a transaction. A ``select_for_update`` in autocommit is
    released by the very statement that took it, so the caller would hold nothing and
    be told nothing — and every test would still pass. The guard therefore lives in
    the primitive, matching ``commercial_app.mutation_context.lock_restaurant`` and
    ``restaurants_app.controllers.admission_lock``.

    ``of=('self',)`` with no ``select_related``: this locks the ``restaurants`` row
    and NOTHING else. An over-broad ``select_for_update`` over a join locks every row
    in the join on PostgreSQL, which is how the delegation-redemption ABBA cycle
    happened (PR-E). The explicit ``of=`` also means a future ``select_related`` added
    for convenience cannot silently widen the lock.

    IT RETURNS ``None`` RATHER THAN RAISING for an unresolvable target, and it does
    NOT refuse a soft-deleted restaurant. Both are deliberate: this is a
    serialization primitive dropped into paths that already have their own
    authorization gate and their own not-found posture, and inventing a new refusal
    here would change what those endpoints answer. A caller that cannot resolve the
    parent simply proceeds unserialized to its existing 404 — which is correct,
    because a membership whose parent cannot be resolved is a membership the caller
    was never going to be allowed to touch.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'lock_restaurant_for_membership_mutation must be called inside a '
            'transaction — a select_for_update taken in autocommit is released '
            'immediately and serializes nothing.'
        )

    # Imported lazily so this controller module stays importable without pulling the
    # app's model graph in at import time, matching the cross-app convention used by
    # `commercial_app.mutation_context` and `restaurants_app.controllers.lifecycle`.
    from restaurants_app.models import Restaurant

    if restaurant_id is None:
        return None
    try:
        restaurant_uuid = uuid_module.UUID(str(restaurant_id))
    except (ValueError, AttributeError, TypeError):
        logger.debug(
            'Membership barrier skipped: %r is not a restaurant UUID.', restaurant_id,
        )
        return None

    return (
        Restaurant.objects
        .select_for_update(of=('self',))
        .filter(pk=restaurant_uuid)
        .first()
    )
