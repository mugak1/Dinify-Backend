"""
D04 — the one correlated answer to "what happened to my checkout?".

THE QUESTION A RECOVERING CLIENT ACTUALLY HAS. It issued an acceptance command
for a specific order, under a specific intent key, at a specific table, against
a specific quote. Something went wrong — a lost response, a timeout, a reload —
and it now needs to know, from whatever response it can get: did that command
land, is this answer about MY command, and what is true of the order now.

D04/C made the acceptance fact durable and resolvable by key. It did not make
the ANSWER verifiable, and it left one pair of states indistinguishable:

  * ``accepted`` is a boolean over "is there an evidence row", so a genuine
    DRAFT and an order accepted BEFORE the evidence table existed both read
    false. Those are opposite instructions: the first must be reviewed and may
    still be accepted, the second is already in the kitchen and must never be
    accepted again. (Separating them does not make the second case a verdict —
    see ``ACCEPTANCE_EVIDENCE_UNAVAILABLE_MEANING``.)
  * the submit results carry ``{status, message, idempotent}`` and name no
    order, key, scope or reference, so a client has nothing to validate a
    correlation against.
  * the ORIGINAL reference the diner confirmed is stored on ``OrderAcceptance``
    and published nowhere. A client could only recompute ``quote_ref`` from the
    CURRENT rows, which answers a different question and changes whenever the
    order does.

THIS MODULE IS THE SHARED PROJECTION THAT CLOSES ALL THREE. It is read by the
acceptance transition (``manage_order._submit_order``) and by the diner's own
order read (``SerializerPublicOrderDetails``), so the answer a client gets from
a mutation and the answer it gets from a later recovery cannot be two different
opinions about one checkout.

WHAT IT IS NOT.

  NOT AUTHORIZATION. It discloses the server-resolved scope, so every caller
  must already have established that this principal may see this order — the
  diner table session for the read, the session/module gate for the write. It
  performs no check of its own and must never be called on an unauthorized
  order.

  NOT A PRICING SURFACE. It reads no catalogue, recomputes no amount and builds
  no quote. The itemised quote stays with ``order_quote`` / the serializer;
  this projection carries only the REFERENCE the acceptance was bound to, read
  verbatim from the stored row.

  NOT A GENERAL ENVELOPE. The key set is fixed and small on purpose. It carries
  no order contents, no diner identity, no actor, no money and no catalogue
  data, so widening it is a deliberate edit rather than a drift.

  NOT EXACTLY-ONCE DELIVERY. A response can still be lost; this is what makes
  the retry that follows answerable.
"""
from orders_app.controllers.services.checkout_protocol import CHECKOUT_PROTOCOL
from orders_app.models import OrderAcceptance
from dinify_backend.configss.string_definitions import OrderStatus_Initiated

#: The submission landed and the server holds the evidence: when, and against
#: which quote.
ACCEPTANCE_ACCEPTED = 'accepted'

#: The order is still a DRAFT. Definitive — not "we have no record", but "this
#: row has never left the state a submission moves it out of". Safe to review
#: and safe to accept.
ACCEPTANCE_NOT_ACCEPTED = 'not_accepted'

#: The order is NOT a draft and NOTHING records an acceptance, so THIS SERVER
#: CANNOT DETERMINE whether the submission landed. See
#: ``ACCEPTANCE_EVIDENCE_UNAVAILABLE_MEANING`` for the contract; it is a
#: statement of ignorance, never of acceptance.
ACCEPTANCE_EVIDENCE_UNAVAILABLE = 'evidence_unavailable'

#: THE CONTRACT FOR THAT STATE, kept as a value rather than only as prose so a
#: test can pin it and a client contract can quote it.
#:
#: IT DOES NOT MEAN "A SUBMISSION LANDED", and an earlier draft of this module
#: said it did. The reasoning was that only ``_submit_order`` moves an order out
#: of ``initiated``, so leaving the draft state implied an acceptance. That is
#: FALSE: the kitchen writes resolve an order by primary key through
#: ``endpoints_kitchen._get_order_or_none``, which filters only
#: ``deleted=False``, and neither guards on ``initiated``. So a DRAFT can be
#: cancelled outright (``KitchenOrderCancelView`` — a draft's fulfilment status
#: is still ``new``, so it takes the free-void branch and needs no manager) or
#: walked ``new -> preparing -> ready -> served``
#: (``KitchenOrderFulfilmentStatusView``, whose completion step also writes
#: ``order_status``). Either leaves a non-draft order with no evidence row.
#:
#: THE STATE THEREFORE HAS TWO PRODUCERS AND NO FACT ON THE ROW SEPARATES THEM:
#: an order accepted before ``OrderAcceptance`` existed, and a draft a kitchen
#: write moved on. No ``order_status`` value is exclusive to acceptance (cancel
#: yields ``cancelled``, serve yields ``served``, recall yields ``pending`` —
#: each reachable both ways), ``cancelled_by`` is written on both paths, and
#: inferring a deploy date is not something this repository does.
#:
#: SO THE CLIENT INSTRUCTION IS THE CONSERVATIVE ONE, and it is conservative
#: BECAUSE the server does not know: never accept such an order again on the
#: strength of a missing row, because one of the two producers really is an
#: order in the kitchen. And never backfill a moment or a reference for it — a
#: fabricated receipt is indistinguishable from a real one afterwards.
#:
#: THE KITCHEN-DRAFT PRODUCER IS A PRE-EXISTING GAP, REPORTED AND NOT FIXED
#: HERE. Adding an ``initiated`` guard to the kitchen writes changes what the
#: kitchen may do to an order, which is a D05 transition decision with its own
#: blast radius; this module's duty is to stop over-claiming about it.
ACCEPTANCE_EVIDENCE_UNAVAILABLE_MEANING = (
    'The order is not a draft and no acceptance evidence exists, so the '
    'server cannot determine whether the submission landed. Two producers '
    'reach it and nothing on the row separates them: an order accepted '
    'before the evidence table existed, and a draft that a kitchen write '
    'cancelled or advanced. D05 closed the second producer for NEW rows — '
    'every kitchen command now refuses a draft — but it did NOT resolve the '
    'rows already produced, so the state stays a statement of ignorance '
    'rather than becoming a verdict. Treat it as not safe to accept again, '
    'and never backfill a moment or a reference for it.'
)

#: This response IS the result of an acceptance that just happened.
OUTCOME_NEWLY_ACCEPTED = 'newly_accepted'

#: This response is the result of a retry against an acceptance that had
#: already happened. Not a failure, and not a second acceptance.
OUTCOME_ALREADY_ACCEPTED = 'already_accepted'


class _Unset:
    """Sentinel: 'look the evidence up', distinct from 'there is no row'."""


_UNSET = _Unset()


def read_evidence(order):
    """THE ONE WAY TO READ AN ORDER'S ACCEPTANCE, and it prefers the JOIN.

    This is a CORRECTNESS rule, not a query-count one. Under READ COMMITTED
    every statement takes its own snapshot, so reading the order in one
    statement and its evidence in another can observe an acceptance that
    committed BETWEEN them — publishing ``acceptance.state == accepted``
    beside ``current.order_status == initiated``, a correlated answer
    describing a moment that never existed. ``transaction.atomic()`` does NOT
    close that: READ COMMITTED re-snapshots per statement inside a transaction
    too. Folding the two reads does, which is the same lesson
    ``catalogue_snapshot`` records, so the diner's recovery read fetches its
    order with ``select_related('acceptance')``.

    Attribute access is what makes the preference automatic rather than
    something each caller has to remember: a JOINED relation answers from the
    order's own snapshot and issues nothing, and an unjoined one falls back to
    a query. The fallback is the ORDINARY path for an ad-hoc caller and its
    window is the pre-existing behaviour, not something introduced here; the
    two hot callers pass their row in directly and reach neither.

    THE STALE DIRECTION IS SAFE AND THE FRESH ONE IS NOT, which is why the
    fallback is tolerable at all. Reading late can only ADD an acceptance the
    order row does not reflect — the incoherent pair. Reading early yields at
    worst ``evidence_unavailable``, a statement of ignorance the server is
    entitled to make, or ``not_accepted`` on a snapshot where the order really
    was still a draft, which the acceptance path's own replay protection
    covers.
    """
    try:
        return order.acceptance
    except OrderAcceptance.DoesNotExist:
        # Joined and absent: Django cached ``None``, so this costs nothing.
        # Unjoined and absent: one query, which is the fallback above.
        return None


def _moment(value):
    """A datetime as an explicit ISO-8601 string, or ``None``.

    NOT left as a ``datetime`` for the renderer to format, and the reason is
    the same one ``money.format_money`` exists for: the value a view BUILDS is
    not the value a client PARSES. This projection reaches the wire by two
    different paths — a plain dict returned from the submit controller, which
    DRF's ``JSONEncoder`` renders with ``.isoformat()``, and a serializer
    method field, which renders through ``api_settings.DATETIME_FORMAT``. Those
    two can be configured apart, and a client comparing an acceptance moment it
    stored against one it reads back would then see a difference that is purely
    a rendering artefact. Formatting here makes the two byte-identical.
    """
    return None if value is None else value.isoformat()


def acceptance_state(order, evidence):
    """THE THREE-STATE VERDICT. Derived; nothing is stored for it.

    The discriminator is whether the row is still a DRAFT, because it is the
    only fact available that can rule "never accepted" IN:

      evidence row present       -> ``accepted``        (definitive)
      no row, still ``initiated``-> ``not_accepted``    (definitive)
      no row, no longer a draft  -> ``evidence_unavailable``  (a non-answer)

    ONLY THE FIRST TWO ARE VERDICTS. The third is the server saying it does not
    know, and it is DELIBERATELY NOT called "accepted, unrecorded": leaving the
    draft state does not imply a submission landed, because a kitchen write can
    cancel or advance a draft with no acceptance. See
    ``ACCEPTANCE_EVIDENCE_UNAVAILABLE_MEANING`` for both producers and why
    nothing on the row separates them.

    A CANCELLED OR SERVED ORDER IS NOT PUSHED BACK TO ``not_accepted``. The
    kitchen's later progress says nothing about whether the diner's submission
    landed, and reporting a cancelled legacy order as a draft is exactly the
    answer that invites a client to accept it a second time.
    """
    if evidence is not None:
        return ACCEPTANCE_ACCEPTED
    if order.order_status == OrderStatus_Initiated:
        return ACCEPTANCE_NOT_ACCEPTED
    return ACCEPTANCE_EVIDENCE_UNAVAILABLE


def acceptance_result(order, *, outcome=None, evidence=_UNSET):
    """The correlated projection for ONE order.

    ``outcome`` is set only by a caller that just performed an acceptance
    attempt (``newly_accepted`` / ``already_accepted``). A READ leaves it
    ``None`` — an observation is not the result of an attempt, and inventing a
    third word for "I merely looked" would put a claim in the response that no
    caller made. The key is always present so the shape never varies.

    ``evidence`` lets a caller that already holds the ``OrderAcceptance`` row
    pass it in, and both hot callers do. That is not a micro-optimisation: the
    acceptance path's query cost is pinned to the exact integer by
    ``tests_order_acceptance.WhatTheEvidenceCostsTests`` and
    ``tests_order_path_queries``, so a projection that quietly re-read the row
    would fail those. Omitting it looks the row up, which is correct for an
    ad-hoc caller and is what those pinned counts will catch if a hot path ever
    starts doing it.
    """
    if isinstance(evidence, _Unset):
        evidence = read_evidence(order)

    return {
        # WHICH order and WHICH keyed intent. A client validates both against
        # the command it issued before believing anything else here.
        'order_id': str(order.pk),
        'intent_key': (
            None if order.client_order_id is None
            else str(order.client_order_id)
        ),
        # The scope the SERVER resolved, never one the request asserted. This
        # is what lets a client confirm the answer belongs to the table it is
        # actually sitting at. Disclosed only because every caller has already
        # authorized this principal for this order — see the module docstring.
        'scope': {
            'restaurant': (
                None if order.restaurant_id is None
                else str(order.restaurant_id)
            ),
            'table': None if order.table_id is None else str(order.table_id),
        },
        'acceptance': {
            'state': acceptance_state(order, evidence),
            'outcome': outcome,
            # THE ORIGINAL, READ FROM THE STORED ROW. Never `quote_ref(order)`
            # recomputed from the current rows: that is a description of the
            # order NOW, and it moves whenever anything about the order does,
            # so a client checking what it accepted would be told it accepted
            # something else.
            'quote_ref': None if evidence is None else evidence.quote_ref,
            'accepted_at': (
                None if evidence is None else _moment(evidence.accepted_at)
            ),
        },
        # LABELLED SEPARATELY FROM THE ACCEPTANCE, deliberately. "Did my
        # submission land" and "what is the kitchen doing" are independent
        # facts, and collapsing them is how a cancelled-but-accepted order
        # ends up reported as never placed.
        'current': {
            'order_status': order.order_status,
            'fulfilment_status': order.fulfilment_status,
            'cancelled_at': _moment(order.cancelled_at),
            'served_at': _moment(order.served_at),
        },
        # What THIS build can promise, stated rather than inferred.
        'checkout_protocol': CHECKOUT_PROTOCOL,
    }
