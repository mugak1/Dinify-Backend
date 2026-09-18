"""
THE BARRIER A CATALOGUE WRITE TAKES SO AN ORDER CANNOT BE ACCEPTED AGAINST A
DEFINITION THAT IS CHANGING (D06 completion, G1a).

WHAT CHANGED UNDER THE OLD REASONING. Before D06, order admission read exactly
three restaurant-level facts — ``status``, ``is_test``, ``accepting_orders`` — so
`restaurant_setup` could say, correctly, that taking a per-restaurant exclusive
lock "for a menu-item rename would queue every diner order behind an edit that no
admission reads". D06 made that premise false in the same change that wrote it:
acceptance now re-reads the CATALOGUE (`purchase_integrity`), so a menu edit IS
something an admission reads, and the two transactions once again shared no lock.

THE WINDOW. Acceptance resolves the whole purchase in one statement and then
decides. Under READ COMMITTED a catalogue write committing after that statement
is invisible to it, so an order could be accepted — irreversibly, onto a kitchen
board — against a dish that had just been withdrawn, sold out, unpublished,
re-configured or re-tagged. It is the same shape as the pause race the advisory
lock was introduced for, one level down.

HOW IT CLOSES. The order paths hold this restaurant's admission lock SHARED for
their whole transaction. A catalogue writer takes it EXCLUSIVE, so its change
either lands wholly BEFORE the acceptance snapshot (and is observed) or waits
until that acceptance has committed or rolled back. Nothing is timed and nothing
is retried.

TAKE IT FIRST OR NOT AT ALL. Like every other participant, this must be the
transaction's first lock: taking a row and then reaching for the advisory lock
inverts the documented ordering (``advisory -> rows``) and reintroduces the cycle
that ordering exists to prevent.

MULTI-TENANT CALLERS ACQUIRE IN A DETERMINISTIC ORDER. A writer touching several
restaurants takes every lock it needs up front, sorted, so two such writers can
never hold each other's locks in opposite orders. A caller that cannot enumerate
its tenants up front must not use this barrier at all — see
``CATALOGUE_WRITER_EXEMPTIONS`` in ``restaurants_app/tests_catalogue_admission.py``
for the writers that are deliberately outside it and why.

WHAT IS AND IS NOT AN ELIGIBILITY FIELD. Acceptance reads, per line: the item's
``deleted`` / ``available`` / ``in_stock`` / ``approved`` / ``enabled`` /
``section`` / ``section_group`` / ``has_extras`` / ``extras_applicable`` /
``extras_min_selections`` / ``extras_max_selections`` / ``options`` / ``is_extra``
and its allergen tag LABELS; the section's ``deleted`` / ``approved`` /
``enabled`` / ``available`` / ``availability`` / ``schedules``; and the group's
``deleted`` / ``approved`` / ``enabled`` / ``available`` / ``section``. It does
NOT read ``listing_position``, ``display_order``, ``image``, ``description``,
``calories`` or — since G2-B settled the scope — the item's own ``name``. A
writer that touches only the second list changes no verdict and needs no barrier.
"""
from restaurants_app.controllers.admission_lock import lock_admission_exclusive


def lock_catalogue_for_write(restaurant_ids) -> None:
    """Exclude in-flight order admission at every restaurant about to be edited.

    ``restaurant_ids`` may be a single id or an iterable. ``None`` entries are
    dropped rather than raising: a caller whose target could not be resolved has
    already been refused by its own permission gate, and the scoped queryset
    behind it is what answers — inventing a lock target here would be guessing.

    Deterministic order, de-duplicated, and every lock taken before the caller's
    first row lock.
    """
    if restaurant_ids is None:
        return
    if isinstance(restaurant_ids, (str, bytes)) or not hasattr(
        restaurant_ids, '__iter__'
    ):
        restaurant_ids = [restaurant_ids]

    unique = {str(rid): rid for rid in restaurant_ids if rid is not None}
    for key in sorted(unique):
        lock_admission_exclusive(unique[key])
