"""
D06 — HOW LONG A SAVED QUOTE MAY STILL BE ACCEPTED, AND WHAT THAT PROMISES.

THE PROMISE, STATED EXACTLY. For up to ``QUOTE_LIFETIME`` after the draft was
saved, the server will honour the MONETARY figures the diner reviewed, provided
the attempt still clears authorization, the operational rules and the
purchase-integrity rules. It is deliberately NOT:

  * a stock reservation — nothing is held for the diner, and the dish can sell
    out inside the window (``purchase_integrity`` is what answers that);
  * a promise to prepare food under obsolete preparation or allergen
    information — a quoted line whose meaning changed is sent for review;
  * a statement that the restaurant is still open, the table still usable, or
    the diner still authorized — those are separate, later checks;
  * a guarantee that a specific request will be accepted, since another diner
    may claim the table first.

It is ONE thing: the amount stays good for half an hour.

THE ANCHOR IS ``Order.time_created`` AND NOTHING ELSE. It is ``auto_now_add``,
so the database stamps it once at the INSERT and no later write moves it — not a
D04 replay (which returns the existing row untouched), not the submit save, not a
kitchen command's ``update_fields`` save, not ``determine-customers``' full save.
``time_last_updated`` is ``auto_now`` and moves on every one of those, which is
exactly why it is not the anchor. The pricing instant itself (``snapshot.now``,
captured after the table lock) is never persisted; ``time_created`` is stamped a
few statements later in the same transaction, so it is always at or after the
priced instant — the window it opens is therefore never LONGER than the promise.

NO NEW COLUMN. The deadline is DERIVED, so an existing draft gets the same rule
from the timestamp it already carries, and a rollback removes the rule rather
than stranding data. That means the duration is policy, not history, and §"THE
VERSION IS FROZEN" below is what stops a later edit rewriting the past.

THE VERSION IS FROZEN. ``QUOTE_POLICY_VERSION`` names ONE complete rule: this
anchor, this duration, this strict comparison. A future change of duration is a
NEW version with its own number and its own compatibility decision — it must not
re-open an attempt that version 1 already refused by editing a constant under the
same version. The version travels on every answer so a client can tell which rule
it is being held to, and it is deliberately SEPARATE from ``pricing_version``
(how the money was calculated) and from ``checkout_protocol`` (D04's correlation
and recovery contract). None of the three implies another.

THE COMPARISON IS AT THE DECISION, NOT AT THE READ. ``assess`` takes ``now`` as
an argument and never reads the clock itself, because the only defensible
current time at an acceptance is one sampled AFTER every lock wait, immediately
before the transition decides. A timestamp taken before a ``select_for_update``
that then blocked for a second describes a moment that has passed. What this
establishes is a decision-time boundary: the deadline had not passed when the
server decided. It does not prove the transaction committed before the deadline,
and nothing here claims it does.

EQUALITY IS EXPIRED. ``now < anchor + lifetime`` accepts; ``now == anchor +
lifetime`` does not. A boundary needs one side, and refusing the exact instant is
the side that never accepts a quote whose promise has run out.

UNUSABLE ANCHOR DATA IS ITS OWN ANSWER. A missing timestamp, or one far enough
in the future that it cannot be a real stamp of this row, is reported
``UNAVAILABLE`` — never silently treated as fresh (which would grant an
indefinite quote) and never silently treated as expired (which would refuse a
diner over a data fault they did not cause). The caller decides what an
unavailable anchor means at its own boundary; at acceptance it is a controlled
review-required refusal, which is the conservative reading without the false
claim that the quote is definitively dead.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

#: The complete rule below, named. Bump it for ANY change to the anchor, the
#: duration or the comparison — never edit those under this number.
QUOTE_POLICY_VERSION = 1

#: Version 1: thirty minutes. Long enough for a slow table, a lost signal and a
#: D04 retry; short enough that a price is not carried across a service or a
#: menu change by a forgotten draft. Deliberately unrelated to the kitchen's
#: ten-minute recall window and to the six-hour diner session TTL — neither
#: answers "how long is this amount good for".
QUOTE_LIFETIME = timedelta(minutes=30)

#: How far ahead of ``now`` an anchor may sit before it is treated as unusable
#: rather than as a live quote. Small clock skew between the application server
#: and the database is ordinary; a timestamp minutes ahead is not a stamp of
#: this row and must not be allowed to extend the window.
FUTURE_ANCHOR_TOLERANCE = timedelta(seconds=60)

#: The quote is inside its window and the amount still stands.
QUOTE_LIVE = 'live'
#: The window has run out. Definitive, and monotone: time only moves forward, so
#: a quote that is expired at one instant is expired at every later one. That
#: property is what makes expiry safe to act on irreversibly.
QUOTE_EXPIRED = 'expired'
#: The anchor cannot be read as a real moment. NOT a verdict that the quote is
#: dead, and NOT a verdict that it is alive.
QUOTE_UNAVAILABLE = 'unavailable'

QUOTE_AGE_STATUSES = frozenset({QUOTE_LIVE, QUOTE_EXPIRED, QUOTE_UNAVAILABLE})

#: The refusal vocabulary for the two answers that are not ``live``. They live
#: here, with the rule, so the code a client branches on and the rule that
#: produces it cannot be edited apart.
REASON_QUOTE_EXPIRED = 'quote_expired'
#: DELIBERATELY NOT ``quote_expired``. An unusable anchor is not a statement that
#: the quote is dead — it is a statement that this server cannot say how old the
#: quote is — so it gets its own code, and (unlike expiry) it never closes
#: anything. Reporting it as expiry would file a data fault as a clock fact and
#: would permanently retire a quote on evidence that does not support it.
REASON_QUOTE_UNVERIFIABLE = 'quote_unverifiable'

MESSAGE_QUOTE_EXPIRED = (
    'This order was priced a while ago, so the prices need checking again. '
    'Please review it and place it again.'
)
MESSAGE_QUOTE_UNVERIFIABLE = (
    'We could not confirm that this order is still current. Please review it '
    'and place it again.'
)


@dataclass(frozen=True)
class QuoteAge:
    """What the deadline rule says about one saved quote at one instant.

    ``expires_at`` is None exactly when ``status`` is ``unavailable`` — there is
    no derived deadline to publish for an anchor that could not be read.
    """

    status: str
    policy_version: int
    anchor: Optional[datetime]
    expires_at: Optional[datetime]
    observed_at: datetime

    @property
    def is_live(self) -> bool:
        return self.status == QUOTE_LIVE

    @property
    def is_expired(self) -> bool:
        return self.status == QUOTE_EXPIRED

    @property
    def is_unavailable(self) -> bool:
        return self.status == QUOTE_UNAVAILABLE


def _usable_anchor(value, now) -> Optional[datetime]:
    """The anchor if it is a real aware moment that could belong to this row.

    Naive datetimes are refused rather than localised: guessing a timezone for a
    stored stamp is how a deadline ends up hours out. ``USE_TZ`` is on, so every
    real ``auto_now_add`` value is aware.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    if value > now + FUTURE_ANCHOR_TOLERANCE:
        return None
    return value


def deadline_for(anchor, *, now):
    """The derived deadline for ``anchor``, or None when it is unusable."""
    usable = _usable_anchor(anchor, now)
    return None if usable is None else usable + QUOTE_LIFETIME


def assess(order, now) -> QuoteAge:
    """Apply version 1 to ``order`` at ``now``.

    ``now`` MUST be the aware current time sampled after the caller's locks, at
    the point the decision is made. This function reads no clock of its own so
    that requirement cannot be quietly bypassed.
    """
    anchor = getattr(order, 'time_created', None)
    usable = _usable_anchor(anchor, now)
    if usable is None:
        return QuoteAge(
            status=QUOTE_UNAVAILABLE,
            policy_version=QUOTE_POLICY_VERSION,
            anchor=anchor if isinstance(anchor, datetime) else None,
            expires_at=None,
            observed_at=now,
        )

    expires_at = usable + QUOTE_LIFETIME
    return QuoteAge(
        # `<` and not `<=`: the exact deadline instant is expired.
        status=QUOTE_LIVE if now < expires_at else QUOTE_EXPIRED,
        policy_version=QUOTE_POLICY_VERSION,
        anchor=usable,
        expires_at=expires_at,
        observed_at=now,
    )
