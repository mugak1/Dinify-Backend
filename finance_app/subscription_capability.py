"""
WHAT DINIFY CAN ACTUALLY DO ABOUT A SUBSCRIPTION PAYMENT — one fact, one place.

D07. This module states, for the build that is running, whether Dinify can collect
a restaurant's subscription payment IN-APP. Today it cannot: there is no aggregator
integration anywhere in this repository, and the one route that claimed otherwise
answered "The subscription payment has been initiated. Please confirm payment when
promted" while contacting nobody.

━━ WHY IT IS A LEAF MODULE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TWO SURFACES have to agree about this and they live in different apps: the
authoritative refusal (``finance_app.controllers.tx_subscription``) and the
restaurant's own read of its billing state
(``restaurants_app.controllers.subscriptions``). A constant restated in both is a
constant that eventually disagrees with itself, and the disagreement would present
as a portal offering a Pay button that the server refuses.

So the fact lives here, imported by both. It imports NOTHING — no Django, no
models, no settings — for the same reason ``delegation_scopes`` stays import-light:
a module that two apps depend on must not drag either one's graph into the other.

━━ IT IS A DISCLOSURE, NEVER A SWITCH ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``IN_APP_COLLECTION_SUPPORTED`` DESCRIBES what the collection boundary does. It does
not DECIDE it, and ``tx_subscription`` deliberately does not branch on it: that
service removed its insertion branch outright rather than parking it behind a
boolean, because a switch is a working fake collection one flag away from returning.
Flipping this to ``True`` therefore implements nothing — it would only make the
portal advertise a collector that still does not exist.

The pairing is pinned instead of enforced: a test asserts that WHILE this reads
``False``, ``initiate`` refuses. Building a real collector means changing the
service AND this constant, and that test is what makes the second half impossible
to forget.

This is the same shape as ``reports_app``'s ``PAYMENT_TRACKING_ENABLED``: a module
constant that flags a figure as a placeholder rather than a measurement, flipped by
the change that makes it true.

━━ SCOPE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

It is about the DINIFY SUBSCRIPTION (restaurant -> Dinify) and about IN-APP
collection specifically. It says nothing about whether a restaurant has paid, has
ever paid, owes anything, or has settled by some entirely external means — Dinify
has no invoice model, no receivable and no collection record, so none of those
questions has an answer here. And it is unrelated to DINER -> RESTAURANT payment:
that is ``commercial_app``'s ``payment_collection_mode``, a different axis with a
different vocabulary (see the four-concepts table in CLAUDE.md).
"""

#: Whether this build can collect a Dinify subscription payment in-app.
#:
#: FALSE, and a disclosure rather than a switch — see the module docstring.
IN_APP_COLLECTION_SUPPORTED = False

#: Stable machine code a client branches on when a collection is attempted anyway.
REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE = 'subscription_collection_unavailable'

#: The sentence an operator reads. It is about ONE REQUEST and nothing else — it
#: does not claim the restaurant has never paid, that no external or prior charge
#: exists, or that no billing OTP was issued by an older client before the request
#: landed. It names no contact, bank account or payment instruction, because
#: inventing one would be the same class of untruth this change removes.
MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE = (
    'In-app subscription payment collection is not available. '
    'This request did not create or send a payment request.'
)
