"""
The per-restaurant admission lock — a PostgreSQL advisory lock, shared or exclusive.

This is the synchronisation primitive that lets an order path and a lifecycle
transition agree about a restaurant's state. It lives in ``restaurants_app``
because the thing it locks is a RESTAURANT: ``lifecycle.transition_restaurant``
takes the exclusive side, the order paths in ``orders_app`` take the shared side,
and putting it here means the lifecycle service depends on its own app rather than
importing back out of ``orders_app``. The RULE about who may order — which is an
orders concern — lives separately, in
``orders_app.controllers.services.order_admission``.

WHAT IT IS FOR. Order admission has to read ``Restaurant.status`` and still have
that answer be true when the order is written moments later. Nothing in the order
path touches the ``restaurants`` row (it locks the ``Table``), so before this lock
existed a transition could commit in the middle of an order and neither side would
notice: the order was admitted, and classified commercial or rehearsal, against a
state that no longer existed.

WHY ADVISORY AND NOT A ROW LOCK. A ``select_for_update`` on the restaurant is
exclusive — Django cannot express ``FOR SHARE`` — so it would convert per-table
serialisation into per-restaurant serialisation and make every diner at a busy
restaurant queue behind every other (a typical order holds its locks ~70-90 ms) to
guard against a transition that happens perhaps once in that restaurant's
lifetime. Shared/exclusive costs admissions nothing against each other and blocks
them only for the few milliseconds a real transition takes.

It also spans PROCESSES, which no row lock on a customer-plane table could: the
transition arrives on the ADMIN plane, in a different mod_wsgi daemon. Advisory
locks are database-global, which is exactly the scope the two planes share.

``_xact_`` IS MANDATORY, not stylistic. ``CONN_MAX_AGE`` is 600, so connections are
reused for ten minutes; a session-scoped ``pg_advisory_lock`` leaked by an error
path would poison a worker for that long. The transaction-scoped variant is released
by the COMMIT or ROLLBACK that ends the caller's transaction, with nothing to
remember to unlock.

LOCK ORDER — this lock is a single TOP level, taken before any row lock, so the
ordering across every transaction that takes it stays acyclic by construction:

    order create           SHARED    -> Table -> Counter -> Order -> OrderItem
    order submit           SHARED    -> Table -> Order
    retire-for-review      SHARED    -> Table -> Order
    lifecycle transition   EXCLUSIVE -> Restaurant -> AdminAuditLog
    test classification    EXCLUSIVE -> Restaurant -> AdminAuditLog
    restaurants PUT        EXCLUSIVE -> Restaurant
    menu PUT / DELETE      EXCLUSIVE -> MenuSection | SectionGroup | MenuItem
    kitchen 86 (in_stock)  EXCLUSIVE -> MenuItem
    tag rename / delete    EXCLUSIVE -> RestaurantTag (-> MenuItemTag cascade)

Take it FIRST or not at all. A transaction that takes a row lock and then reaches
for this one reintroduces the cycle this ordering exists to prevent. A caller
holding locks for SEVERAL restaurants takes every advisory lock it needs up
front, in a deterministic (sorted) order — see ``catalogue_admission``.

``manage.py mark_restaurant_test``, the operator command that writes
``Restaurant.is_test``, takes the same locks in the same order as the
transition, so it joins an ordering already proven acyclic rather than adding a
level. It needs the lock for the same reason the transition does and for a reason
worth stating plainly, because the row lock beside it looks sufficient and is not:
``order_admission.admit`` reads ``is_test`` with a PLAIN ``values_list().get()``,
never a ``select_for_update``, so under MVCC that read does not block on a row held
FOR UPDATE. The advisory lock is the only thing the two transactions share.

THE LAST FOUR ENTRIES ARE THE D06 COMPLETION (G1a), and they exist because D06
changed what an admission READS. Until then this lock guarded three
restaurant-level columns; ``purchase_integrity`` made acceptance re-read the
CATALOGUE, so a menu edit became something an admission reads and the two
transactions once again shared nothing. ``retire-for-review`` joined on the
SHARED side for the sharper version of the same reason: it decides on the same
catalogue facts and writes an irreversible closure from the result, so a stale
read there destroys a quote that was never stale. It takes the lock for
SYNCHRONISATION and still applies no admission POLICY — those are different
questions and ``lock_admission_shared`` / ``order_admission.admit`` are
deliberately different functions.

WHICH CATALOGUE WRITERS ARE IN, AND WHICH ARE OUT WITH REASONS, is an inventory
with a test per exemption: ``restaurants_app/tests_catalogue_admission.py``
(CATALOGUE-ADMISSION-00). A module that starts taking this lock without being
named there fails that scan.

KEEP THAT TABLE EXHAUSTIVE — it has been wrong before. Delegation redemption used to
take an undeclared EXCLUSIVE lock on a ``Restaurant`` row — and on a ``User`` row —
because ``select_for_update()`` was chained with a multi-table ``select_related()``
and PostgreSQL locks the whole join when no ``OF`` clause is given. It was a
transaction the table did not list, and it closed a real cycle against the lifecycle
transition. PR-E scoped that lock with ``of=('self',)``. The transactions that take a
``Restaurant`` row lock EXCLUSIVELY are therefore exactly the two listed above, both
of them behind this lock. Before adding a row lock anywhere, check what your
``select_related`` is quietly locking — and if the row you are locking is a
restaurant whose state an order reads, take this lock first as well.
"""
import uuid as uuid_module

from django.db import connection, transaction


def advisory_key(restaurant_id) -> int:
    """
    Fold a restaurant id into the signed 64-bit integer ``pg_advisory_*`` takes.

    Deterministic and SECRET-FREE. It is deliberately NOT peppered with
    ``SECRET_KEY``: an advisory key is a coordination address, not a credential —
    knowing it grants nothing, since taking the lock requires an authenticated
    database session that could take any lock anyway. Peppering it would make the
    key deployment-dependent, so two workers reading different settings
    (mid-deploy, or across a rotation) would coordinate on different keys and
    silently stop serialising — a safety mechanism that fails open and looks fine.

    Truncating to 8 bytes cannot produce a wrong answer, only a rare shared one:
    two restaurants folding to the same key serialise against each other
    unnecessarily for the few milliseconds a transition takes.
    """
    if not isinstance(restaurant_id, uuid_module.UUID):
        restaurant_id = uuid_module.UUID(str(restaurant_id))
    return int.from_bytes(restaurant_id.bytes[:8], 'big', signed=True)


def _lock(restaurant_id, *, exclusive: bool) -> None:
    """
    Take the transaction-scoped advisory lock for ``restaurant_id``.

    MUST be called inside a transaction. A ``pg_advisory_xact_lock`` taken in
    autocommit is released by the very statement that took it, so the caller holds
    nothing and is told nothing — the guard lives HERE, in the primitive, rather than
    only in ``order_admission.admit()``, because a silently ineffective lock is worse
    than no lock at all: every test would still pass. Asserted BEFORE the vendor check
    below, deliberately, so the misuse is caught on the SQLite unit run too and not
    only where the lock is real.

    A no-op on any backend that is not PostgreSQL — the unit suite runs on SQLite
    unless the Postgres environment is exported, and there is no advisory-lock
    equivalent there. That is why every concurrency test in this repo skips itself
    when ``connection.vendor != 'postgresql'``: without the guard the races would
    pass by not running. The same idiom guards the Postgres-only audit query in
    ``restaurants_app/migrations/0046_remove_menuitem__legacy_tags.py``.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'admission_lock must be taken inside a transaction — a '
            'transaction-scoped advisory lock taken in autocommit is released '
            'immediately and protects nothing.'
        )
    if connection.vendor != 'postgresql':
        return
    key = advisory_key(restaurant_id)
    with connection.cursor() as cursor:
        # Two literal statements rather than an interpolated function name, so no
        # part of this SQL is ever assembled from a variable.
        if exclusive:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])
        else:
            cursor.execute('SELECT pg_advisory_xact_lock_shared(%s)', [key])


def lock_admission_shared(restaurant_id) -> None:
    """
    Hold this restaurant's lifecycle steady for the rest of the transaction.

    Taken by the ORDER paths. Shared, so concurrent admissions never block each
    other — which is the whole point of the mechanism, and what a ``Restaurant``
    row lock could not have given.
    """
    _lock(restaurant_id, exclusive=False)


def lock_admission_exclusive(restaurant_id) -> None:
    """
    Exclude every in-flight admission for the rest of the transaction.

    Taken by ``lifecycle.transition_restaurant``. Once it is held, no order can be
    admitted against a status the transition is about to invalidate, and any
    admission already past its own lock has committed or rolled back.
    """
    _lock(restaurant_id, exclusive=True)
