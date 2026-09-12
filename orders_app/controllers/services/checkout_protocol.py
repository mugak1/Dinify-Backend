"""
D04 — what THIS deployment can actually promise a checkout client.

WHY A DEDICATED SIGNAL. A client that wants to retry an uncertain mutation
safely has to know whether the server it is talking to can answer that retry
safely. Every value already on the wire is the wrong thing to ask:

  ``pricing_version``   says how the order's MONEY was calculated. #661 is the
                        standing lesson — two contract introductions were
                        collapsed into one flag and the client refused a real
                        deployable server for the width of a deploy.
  a present total       says the response has a field, not that the server can
                        bind a key or remember an acceptance.
  a fulfilment state    says what the kitchen is doing.

So the capability is stated directly, and it is a LEVEL rather than a boolean
because the two halves of D04 deploy separately and a client must not be told
the second exists when only the first does.

  1  BINDING. The idempotency key is bound to the canonical purchase and the
     server-resolved scope, every return-existing site validates that binding,
     and a successful creation replay allocates no new order number. A client
     may retry an uncertain CREATION with the same key.

  2  RECOVERABLE. Everything in 1, plus durable acceptance evidence and a
     scoped read that resolves an intent key. A client may additionally
     recover an uncertain ACCEPTANCE.

  3  CORRELATED. Everything in 2, plus a shared projection on BOTH the
     acceptance result and the scoped read that names the order, the keyed
     intent and the server-resolved scope, separates a genuine draft from an
     acceptance that predates the evidence table, publishes the ORIGINAL
     accepted reference and moment, and labels current order/fulfilment state
     apart from the acceptance. A client may additionally VALIDATE that an
     answer belongs to the command it issued, and may distinguish "never
     accepted" from "accepted, unrecorded" — which at level 2 read alike.

A client must treat an ABSENT value as level 0 and promise nothing, and must
never infer a level from a route returning 404 — a missing route and an intent
that never existed are different facts.
"""

#: The key is bound to the purchase; creation replay is safe.
CHECKOUT_PROTOCOL_BINDING = 1

#: Acceptance evidence is durable and recoverable by intent key.
CHECKOUT_PROTOCOL_RECOVERABLE = 2

#: The answer is correlated to the issued command and states the original
#: acceptance, so a client can verify it rather than assume it.
CHECKOUT_PROTOCOL_CORRELATED = 3

#: WHAT THIS BUILD ACTUALLY SUPPORTS. Raised only by the change that makes the
#: next level true — never in advance of it.
#:
#: Raised to 2 by D04/C, which added BOTH halves of that level in one change:
#: durable acceptance evidence (`OrderAcceptance`, written with the transition
#: and never moved afterwards) and the scoped recovery read
#: (`orders/journey/order-details/?intent=<client_order_id>`). Raising it for
#: one without the other would have been the #661 mistake in a new place — a
#: client told it may recover an acceptance it has no way to look up.
#:
#: Raised to 3 by the D04 completion, which publishes
#: `acceptance_result.acceptance_result` on both the acceptance result and the
#: scoped read. A NEW NUMBER RATHER THAN A NEW MEANING FOR 2: a client pinned
#: to 2 keeps exactly the promises 2 made, and only a client that recognises 3
#: may rely on the correlation fields or on the three-state verdict. Silently
#: widening 2 is the #661 mistake, and it would be worse here — a level-2
#: client reading the old `accepted` boolean is RIGHT to treat it as
#: two-valued, because for it, it is.
CHECKOUT_PROTOCOL = CHECKOUT_PROTOCOL_CORRELATED
