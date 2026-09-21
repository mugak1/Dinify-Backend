"""
Dinify subscription collection — DISABLED, at the authoritative boundary.

D07. This service used to answer "The subscription payment has been initiated.
Please confirm payment when promted", book a Pending ``DinifyTransaction`` and
contact nobody: the provider call was a comment. No aggregator integration
exists in this repository — ``notifications_app.controllers.sms`` holds the only
outbound HTTP call site in the tree — so there was never a collector behind that
sentence.

THE REFUSAL LIVES HERE, NOT AT THE ENDPOINT, and that is the whole point. The
endpoint is one caller; ``initiate`` is reachable in-process by any caller
(including one passing ``user=None``, which used to persist ``created_by=None``),
so disabling a view or hiding a button leaves the fake collection fully
available. Every caller reaches this refusal.

WHAT IT REFUSES, AND WHEN. The endpoint's authorization gate runs FIRST and is
unchanged, so a non-member still cannot learn whether a restaurant exists. The
restaurant is still resolved here — an unknown or malformed id still answers the
same opaque 404 the gate emits, rather than 500ing or disclosing. Only a request
that has cleared both gates reaches the 501.

THE LEGACY PLAN COLUMN NO LONGER DECIDES ANYTHING. ``preferred_subscription_method``
used to gate a ``per_order`` business refusal above the 200 path. There is no 200
path now, so branching on it would offer changing a plan as a way to enable
machinery that does not exist. Every authorized request gets the same answer
whatever the column says. (The column is unwritable from this plane anyway —
``restaurants_app.endpoints.restaurant_setup`` strips it, with ``flat_fee``, from
every restaurants PUT.)

501, NOT 503. RFC 9110 §15.6.2 is "the server does not support the functionality
required"; §15.6.4 is a temporary overload or maintenance. A ``Retry-After``, a
retry timer or a poll would all suggest that waiting implements a collector.
Nothing here suggests that.

ZERO EFFECTS. No ``DinifyTransaction``, no payment intent, no OTP or challenge,
no SMS or email, no provider call, no stored MSISDN, no amount or status write.
The insertion branch is REMOVED rather than parked behind an enable switch: a
switch is a working fake collection one boolean away from returning. Historical
rows, model fields and migrations are untouched — this changes what the server
will DO, never what it has recorded.

Paired frontend: the billing screen becomes read-only (no Pay/Renew/Subscribe,
no MSISDN lookup, no OTP step). Until that ships, an older client may still run
its pre-submit lookup/OTP chain before it ever reaches this refusal; that is a
release-window limitation of the old client, not a side effect of this service.
"""
import logging
from typing import Optional
from django.core.exceptions import ValidationError
from finance_app.subscription_capability import (
    IN_APP_COLLECTION_SUPPORTED,
    MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
    REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
)
from restaurants_app.models import Restaurant
from users_app.models import User

logger = logging.getLogger(__name__)

# THE CODE AND THE SENTENCE LIVE IN `finance_app.subscription_capability`, not here,
# because the restaurant's own billing read has to state the same capability and a
# second copy is how the portal ends up offering a Pay button the server refuses.
# They are re-exported so existing importers of this module keep working.
#
# `IN_APP_COLLECTION_SUPPORTED` is imported to be PINNED, never to be branched on:
# `initiate` below refuses unconditionally, and consulting the flag would turn a
# removed capability back into a switch. A test asserts the two agree.
__all__ = [
    'IN_APP_COLLECTION_SUPPORTED',
    'MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE',
    'REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE',
    'SubscriptionPaymentTransaction',
]


class SubscriptionPaymentTransaction:
    def __init__(self):
        pass

    def initiate(
        self,
        restaurant_id: str,
        transaction_platform: str,
        payment_mode: str,
        user: User,
        msisdn: Optional[str] = None,
        otp: Optional[str] = None,
    ) -> dict:
        """
        Refuse an in-app subscription collection, having performed none.

        The signature is unchanged so no caller has to be rewritten to be
        refused. ``payment_mode``, ``msisdn`` and ``otp`` are accepted and
        deliberately NOT read: there is nothing to tender, nothing to deliver a
        code to, and nothing to verify one against. (``otp`` was never read on
        the old path either — the billing screen collected a code the payment
        service discarded.)
        """
        try:
            # Resolve first, so the established non-disclosure holds: an unknown
            # UUID (DoesNotExist) and a malformed one (ValidationError out of the
            # UUIDField conversion) both answer the SAME 404 the endpoint gate
            # emits, and a bad id never 500s. A caller who cannot see the
            # restaurant must not be able to tell this capability apart from a
            # missing tenant.
            Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValidationError):
            return {'status': 404, 'message': 'Not found'}

        # Bounded, truthful diagnostic. No request body, no amount, no MSISDN,
        # no OTP, no bearer material — and it is a refusal, never a financial
        # intent. The restaurant id is the caller's own already-authorized scope.
        logger.info(
            'subscription collection refused (unimplemented) restaurant=%s',
            restaurant_id,
        )

        return {
            'status': 501,
            'reason': REASON_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
            'message': MESSAGE_SUBSCRIPTION_COLLECTION_UNAVAILABLE,
        }
