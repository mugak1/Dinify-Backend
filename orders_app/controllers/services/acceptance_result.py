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
    accepted again.
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

#: The order is NOT a draft, so a submission did land, but nothing records it.
#: The honest answer for an order accepted before ``OrderAcceptance`` existed.
#: NEVER accept such an order again on the strength of a missing row, and never
#: backfill a moment or a reference for it — a fabricated receipt is
#: indistinguishable from a real one afterwards.
ACCEPTANCE_EVIDENCE_UNAVAILABLE = 'evidence_unavailable'

#: This response IS the result of an acceptance that just happened.
OUTCOME_NEWLY_ACCEPTED = 'newly_accepted'

#: This response is the result of a retry against an acceptance that had
#: already happened. Not a failure, and not a second acceptance.
OUTCOME_ALREADY_ACCEPTED = 'already_accepted'


class _Unset:
    """Sentinel: 'look the evidence up', distinct from 'there is no row'."""


_UNSET = _Unset()


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

    The discriminator is whether the row is still a DRAFT, because that is the
    only fact that can separate "never accepted" from "accepted, unrecorded":

      evidence row present       -> ``accepted``
      no row, still ``initiated``-> ``not_accepted``   (definitive)
      no row, no longer a draft  -> ``evidence_unavailable``

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
        evidence = OrderAcceptance.objects.filter(order=order).first()

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
