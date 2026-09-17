"""
D06 — what THIS deployment can promise about the life of a saved quote.

WHY A SEPARATE SIGNAL FROM ``checkout_protocol``. They answer different
questions, they were built by different changes, and they will move apart.

  ``checkout_protocol``  (D04) — can an uncertain checkout be RETRIED and
                         RECOVERED safely? It is about identity, evidence and
                         correlation: which command produced which order.
  ``quote_policy``       (D06) — may a saved quote still be ACCEPTED, and what
                         happens when it may not? It is about the life of the
                         reviewed amount and of the purchase behind it.

A client can want either without the other. One that never retries still needs
to know its quote has a deadline; one that never renders a deadline still needs
the correlation fields to recover a lost response. Folding the second into
``checkout_protocol`` would have raised that level for a change that added
nothing to what it promises — #661 in a new place, and this time in the
direction that matters most, because a client pinned to level 3 is RIGHT that
level 3 said nothing about quote lifetime.

**``CHECKOUT_PROTOCOL`` IS DELIBERATELY UNCHANGED BY D06 AND STAYS 3.**

  1  ENFORCED. The server applies a named, versioned lifetime to a saved quote
     and re-checks the agreed purchase at acceptance; it publishes the deadline
     on the order read so a client can see it coming; and when a quote can no
     longer be honoured it RETIRES it durably, so a replacement quote is safe
     to mint — an acceptance still in flight for the old one can never execute
     afterwards. A client may additionally use
     ``PUT orders/retire-quote/`` to establish that, without attempting an
     acceptance and without claiming a table.

An ABSENT value is level 0 and promises NOTHING — in particular it does not
mean "quotes never expire here"; it means this server has not said. A client
must not infer the level from the presence of a ``quote_policy`` object, and
must not infer it from a 404 on the retire route: a missing route and a server
that does not implement the policy are different facts, and only the level
distinguishes them.

THE LEVEL AND THE POLICY VERSION ARE DIFFERENT NUMBERS.
``quote_policy.QUOTE_POLICY_VERSION`` names the RULE — this anchor, this
duration, this comparison — and changes when the rule changes. This names the
CONTRACT — which facts the server publishes and which operations it offers —
and changes when that changes. A shorter lifetime moves the first and not the
second.
"""

#: The lifetime is enforced, the deadline is published, a quote that can no
#: longer be honoured is retired durably, and the retire-for-review operation
#: exists.
QUOTE_PROTOCOL_ENFORCED = 1

#: WHAT THIS BUILD ACTUALLY SUPPORTS. Raised only by the change that makes the
#: next level true — never in advance of it.
QUOTE_PROTOCOL = QUOTE_PROTOCOL_ENFORCED
