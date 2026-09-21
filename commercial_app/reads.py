"""
Reusable, PURE reads over the commercial domain (D07).

Steps 3B/3C made a restaurant's commercial facts storable and writable, and
``platform_admin_app.commercial_reads`` projects them for the Admin control plane.
This module is the part that is NOT Admin-specific: the canonical rule for which
terms row is the current one, and a projection of it narrow enough for a consumer
that is not a platform operator.

It exists because the restaurant's OWN billing screen must be able to state its
subscription terms truthfully, and the only two ways to reach them otherwise were
both wrong. Reconstructing them from ``Restaurant.flat_fee`` /
``preferred_subscription_method`` / ``subscription_validity`` would read columns
that are NOT canonical — no supported writer maintains them, the tenant plane
strips two of them from every PUT, and the third defaults to ``True`` so it cannot
distinguish a decision from a column that was never touched. And importing the
Admin projection would hand a restaurant an operator's view of itself.

━━ ONE SELECTION RULE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``open_terms`` is THE answer to "which terms are in force", and
``subscription_terms`` imports it as its own ``_open_terms`` rather than keeping a
second copy — so the writers that refuse a second open row and the readers that
report the current one cannot form different opinions about which row that is. A
test asserts the two names are the SAME OBJECT, because two functions that merely
happen to agree today are exactly what drifts.

Open means ``ended_at IS NULL`` and nothing else — never ``effective_from <= now``,
never the latest ``recorded_at``, never a validity flag. Two layers already
guarantee that is unambiguous: ``one_open_subscription_terms_per_restaurant``
makes "at most one open row" a database fact, and the Step 3C writers refuse
future-dated terms, so an open row is never one that has not taken effect yet.

━━ THE PROJECTION IS DELIBERATELY NARROWER THAN THE ADMIN ONE ━━━━━━━━━━━━━━━━━━━

``project_terms`` carries the price, the currency, the recurrence and the date the
terms took effect. It deliberately omits:

  ``id``          the ``expected_terms_id`` an Admin replace/end write asserts
                  against. There is no customer-plane writer to assert it, so
                  publishing it would hand out a concurrency token for an
                  operation this caller cannot perform.
  ``recorded_at`` WHEN A PLATFORM OPERATOR WROTE THIS DOWN, which is an internal
                  bookkeeping moment, not a commercial fact about the restaurant.
                  ``effective_from`` is the date that answers "since when?".
  ``recorded_by`` WHO wrote it down. That is ``AdminAuditLog``'s question, and the
                  answer is a platform staff member's identity.
  ``ended_at``    NULL by construction for an open row.

The two projections are therefore NOT unified, and that is the decision rather than
an omission: they answer the same question for different readers. What is shared is
the SELECTION RULE, which is the part that could be wrong.

━━ WHAT AN OPEN TERMS ROW PROVES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

That Dinify has RECORDED these terms. Not that they are active, paid, invoiced,
collected, agreed by the owner, or in good standing — there is no invoice model, no
receivable and no collection path in this repository, so no word implying any of
that has anything behind it. The caller is told ``recorded: true`` and the terms
themselves; deriving a billing verdict from that would be the same fabrication this
work exists to remove.

━━ PURE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

No write, no ``get_or_create``, no ``save``, no lock, no transaction, no audit row,
no legacy synchronisation. Reading a restaurant that has never had terms recorded
creates nothing, and a restaurant whose terms have all ended opens nothing.
"""
from commercial_app.models import RestaurantSubscriptionTerms


def open_terms(restaurant):
    """
    The restaurant's OPEN terms row, or ``None``.

    THE canonical selector — ``subscription_terms`` imports this as its private
    ``_open_terms`` so the writers and the readers share one definition.

    At most one open row can exist: ``one_open_subscription_terms_per_restaurant``
    guarantees it at the database, and that constraint remains the final backstop
    behind every check that reads this.

    ``restaurant`` may be a ``Restaurant`` instance or its primary key — Django
    resolves either identically for a forward FK filter. A malformed value raises
    rather than answering ``None``, which is correct: "I cannot tell which
    restaurant you mean" must never be reported as "this restaurant has no terms".
    """
    return (
        RestaurantSubscriptionTerms.objects
        .filter(restaurant=restaurant, ended_at__isnull=True)
        .first()
    )


def project_terms(terms):
    """
    One terms row as the restaurant's own surfaces may see it, or ``None``.

    Pure: takes a model instance, touches no database, and is total over ``None``
    so a caller need not branch before calling it.

    ``recurring_amount`` is serialized with ``str()`` and that is load-bearing
    rather than stylistic. DRF's JSON encoder renders a bare ``Decimal`` through
    ``float()``, which is precisely the conversion this repository's money rule
    forbids — the stored scale is lost, so ``0.00`` reaches a client as ``0.0`` and
    an amount that is not representable in binary floating point reaches it changed.
    A price that renders differently from how it is stored is a price nobody can
    reconcile. ``str()`` on the ``Decimal`` the database returned preserves it
    exactly, and a zero is a real, deliberate price (a free pilot, a waived period,
    a rehearsing test tenant) rather than "free", "trial" or an absence.

    ``effective_from`` is an EXPLICIT ISO-8601 string for the reason ``format_money``
    exists: the value a view BUILDS is not the value a client PARSES. A ``datetime``
    left in the payload is rendered by whichever encoder the surface happens to use,
    and ``api_settings.DATETIME_FORMAT`` is configurable, so formatting it here is
    what keeps one fact one string.
    """
    if terms is None:
        return None
    return {
        # The price and the currency exactly as recorded. No rounding, no symbol,
        # no locale formatting — display is the client's job.
        'recurring_amount': str(terms.recurring_amount),
        'currency': terms.currency,
        # The recurrence as two machine facts rather than a plan name: this domain
        # has no plan catalogue, and inventing one here would freeze a product
        # decision nobody has made.
        'billing_interval': {
            'unit': terms.billing_interval_unit,
            'count': terms.billing_interval_count,
        },
        # WHEN THE TERMS TOOK EFFECT — not when they were written down.
        'effective_from': terms.effective_from.isoformat(),
    }


def subscription_terms_summary(restaurant):
    """
    ``{'recorded': bool, 'current': {...} | None}`` for one restaurant.

    ``recorded`` states ONE thing: an open terms row exists. It is deliberately not
    called ``configured``, ``active`` or ``valid`` — see the module docstring for
    what an open row does and does not prove.

    **ABSENCE IS AN ANSWER, NOT A FAILURE.** A restaurant with no recorded terms is
    an ordinary, truthful state — most are — so it is reported explicitly as
    ``recorded: false`` rather than by omitting the key, leaving the client to
    invent a default, or falling back to a legacy column. A consumer that cannot
    tell "no terms recorded" from "the server did not say" will eventually guess,
    and the guess is what puts an invented price on a screen.

    ONE query, and only when a restaurant was resolvable in the first place.
    """
    terms = open_terms(restaurant)
    return {
        'recorded': terms is not None,
        'current': project_terms(terms),
    }
