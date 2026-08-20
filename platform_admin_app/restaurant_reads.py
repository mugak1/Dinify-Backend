"""
The Admin restaurant directory and detail READ model (Phase 1, Step 1).

This module owns what the admin portal is told about a restaurant. The endpoints in
``endpoints/restaurants.py`` are thin: they parse query parameters and render what is
computed here, in the same way the transition endpoint delegates every rule to
``restaurants_app.controllers.lifecycle``.

━━ WHAT THIS MODULE IS FOR, AND WHAT IT REFUSES TO DO ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Step 1 exposes truth that exists TODAY. The temptation in a directory screen is to
fill every column, because a column reading "not configured" looks unfinished — and
filling them means inventing semantics the database cannot support. Three fields
would each have been easy to fake, and each is deliberately not faked:

  READINESS is delegated to ``lifecycle.check_go_live_readiness``, the one seam
  Phase 1 Step 3 will fill. Today it fails closed with ``readiness_not_configured``,
  so that is what the portal is told. A partial checklist implemented here would be
  a SECOND readiness implementation that Step 3 would then have to reconcile — and,
  worse, one that answers "ready" on inputs it has not actually checked.

  PAYMENT MODE has no authoritative persisted field at all. ``require_order_prepayments``
  is a diner-checkout toggle, not the commercial ``cash_only`` / PSP-backed mode the
  Admin spec means, and inferring one from the other would produce a confident answer
  that is wrong for any restaurant that has configured prepayment for its own reasons.
  It is reported as unconfigured, because it is.

  SUBSCRIPTION reports the LEGACY ``Restaurant`` columns under names that say so.
  ``RestaurantSubscription`` / ``SubscriptionInvoice`` / ``SubscriptionPayment`` do
  not exist yet. ``subscription_validity`` is a bare boolean with no invoice behind
  it; calling it "paid" or "current" would assert a financial fact the database
  cannot prove. Note especially that ``lifecycle.has_outstanding_receivables`` is NOT
  consulted here: the specification requires that seam to be wired in the same change
  that makes invoices capable of becoming overdue, and calling it today would return
  a cheerful ``False`` that means only "invoices do not exist".

━━ ONE DEFINITION OF ATTENTION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``needs_attention`` is computed in exactly one place, and the ``?attention=true``
filter is its SQL mirror rather than a second opinion. See ``attention_filter``.
"""
from django.db.models import Count, OuterRef, Q, Subquery

from dinify_backend.configss.string_definitions import (
    RESTAURANT_LIFECYCLE_STATES,
    RestaurantStatus_Onboarding,
)
from platform_admin_app.models import AdminAuditLog
from restaurants_app.controllers import lifecycle
from restaurants_app.models import Restaurant
from support_app.models import SupportIssue

# --- readiness ---------------------------------------------------------------

READINESS_READY = 'ready'
READINESS_NOT_READY = 'not_ready'
# Go-live readiness is a question about ONE transition: onboarding -> live. For a
# restaurant that is already live, or suspended, or offboarded, "is it ready to go
# live" has no answer — and reporting zero blockers there would read as "ready",
# which is the opposite of the truth for an offboarded tenant. A distinct state is
# the honest representation, and the portal renders it as a dash rather than a tick.
READINESS_NOT_APPLICABLE = 'not_applicable'

# The state in which go-live readiness is the live question.
READINESS_APPLICABLE_STATES = (RestaurantStatus_Onboarding,)


def readiness_summary(restaurant):
    """
    The restaurant's go-live readiness, as the portal should render it.

    A thin, stable projection of ``lifecycle.check_go_live_readiness`` — never a
    second implementation of it. When Step 3 fills that seam, this inherits the real
    checklist without changing: the shape is already blocker-list-shaped because the
    seam already returns one.

    Cheap by construction: the seam is a pure function today and this adds no query,
    so calling it per row in a directory listing costs nothing.
    """
    if restaurant.status not in READINESS_APPLICABLE_STATES:
        return {
            'state': READINESS_NOT_APPLICABLE,
            'blocker_count': 0,
            'blockers': [],
        }

    result = lifecycle.check_go_live_readiness(restaurant)
    blockers = list(result.blockers or [])
    return {
        'state': READINESS_READY if result.ready else READINESS_NOT_READY,
        'blocker_count': len(blockers),
        'blockers': blockers,
    }


# --- attention ---------------------------------------------------------------

def needs_attention(restaurant) -> bool:
    """
    Does this restaurant need the operator to do something? ROW-LEVEL TRUTH.

    Deliberately NARROW. Step 1 recognises exactly one condition, because exactly
    one is derivable from authoritative data today:

        an ONBOARDING restaurant whose go-live readiness says not ready.

    Everything else the Home inbox will eventually surface — an overdue invoice, an
    expiring owner invitation, an aged support issue — depends on a model that does
    not exist or a threshold nobody has decided. Inventing them here would produce an
    attention list that is confidently wrong, which is worse than a short one: an
    operator who learns the flag lies stops reading it.

    NOT extended to open support issues. Support is reachable in every lifecycle
    state and an open issue is normal traffic, not an exception; without a decided
    ageing or impact threshold, counting it would make nearly every restaurant
    permanently "needs attention" and drain the flag of meaning. The count is
    surfaced as its own column instead, which is what the operator actually sorts on.

    New conditions belong HERE and in ``attention_filter`` together.
    """
    return readiness_summary(restaurant)['state'] == READINESS_NOT_READY


def attention_filter() -> Q:
    """
    The SQL mirror of ``needs_attention``, for the ``?attention=true`` filter.

    THESE TWO MUST AGREE, and a test pins that they do across every lifecycle state
    (``tests_restaurant_directory.AttentionDefinitionTests``). The row value and the
    filter cannot be allowed to drift: a directory that omits a restaurant the row
    would have flagged is a restaurant the operator never sees.

    Why a mirror rather than one shared expression: readiness is a Python seam that
    Step 3 will fill with checks over related models, and not all of them will be
    expressible as a single ``Q``. Rather than pretend otherwise, the two forms are
    written side by side and the equivalence is asserted — so whoever fills the seam
    is told by a failing test that they owe this filter an update too.

    Today the seam is state-only (it fails closed for every restaurant), so the
    mirror is exactly "is onboarding".
    """
    return Q(status__in=READINESS_APPLICABLE_STATES)


# --- payment mode ------------------------------------------------------------

def payment_mode_summary(restaurant):
    """
    The commercial payment mode. UNCONFIGURED — there is no field for it yet.

    ``restaurant`` is accepted and deliberately unused: the signature is the seam.
    When an authoritative payment-mode field lands (Step 2/3), this reads it and
    every caller inherits the change; nothing else has to learn a new shape.

    Explicitly NOT inferred from ``require_order_prepayments`` (a diner-checkout
    toggle), nor from ``preferred_subscription_method`` (how Dinify bills the
    restaurant, not how the restaurant takes money), nor from transaction history.
    """
    return {
        'payment_mode': None,
        'payment_mode_configured': False,
    }


# --- subscription ------------------------------------------------------------

# The Phase-1 commercial models (RestaurantSubscription / SubscriptionInvoice /
# SubscriptionPayment) do not exist. This flag is in the payload so the portal can
# branch on a fact rather than on the shape of the object it received.
SUBSCRIPTION_SOURCE_LEGACY = 'legacy_restaurant_fields'


def subscription_summary(restaurant):
    """
    A TRANSITIONAL view of the legacy subscription columns on ``Restaurant``.

    Every key is named for what the column IS, not for what a commercial
    subscription record would mean. ``legacy_validity_flag`` is a bare boolean that
    defaults True and that nothing currently maintains — it is not "paid", not "in
    good standing", and not evidence that any invoice exists. The portal should
    render this as a legacy/not-configured state, not as a billing status.

    ``has_commercial_subscription`` is False for every restaurant today and is the
    key the portal should branch on: when the real models land, it becomes True and
    a proper subscription object joins it.
    """
    expiry = restaurant.subscription_expiry_date
    return {
        'source': SUBSCRIPTION_SOURCE_LEGACY,
        'has_commercial_subscription': False,
        'legacy_validity_flag': bool(restaurant.subscription_validity),
        'legacy_expiry_at': expiry.isoformat() if expiry else None,
        'preferred_method': restaurant.preferred_subscription_method,
    }


# --- support -----------------------------------------------------------------

# Statuses that mean somebody still owes this issue work. Taken from the model's own
# TextChoices rather than respelled as literals, so a change to the state machine
# reaches this count instead of silently bypassing it.
OPEN_SUPPORT_STATUSES = (
    SupportIssue.Status.OPEN,
    SupportIssue.Status.IN_PROGRESS,
)


# --- querysets ---------------------------------------------------------------

def _last_activity_subquery():
    """
    Timestamp of the most recent admin action against this restaurant, or NULL.

    ``AdminAuditLog.restaurant_id`` is a plain ``UUIDField``, NOT a foreign key — the
    log deliberately survives the deletion of the row it describes — so there is no
    reverse relation to aggregate over and this is a correlated subquery. The
    ``(restaurant_id, created_at)`` index on that table is exactly this query's shape.

    "Last activity" means PLATFORM-ADMIN activity, not ``time_last_updated``: the
    operator is asking when this tenant was last acted upon from the control plane,
    and a menu edit by restaurant staff is not that. It is read-only — nothing here
    writes or denormalises the audit log to make the column cheaper.
    """
    return Subquery(
        AdminAuditLog.objects
        .filter(restaurant_id=OuterRef('pk'))
        .order_by('-created_at')
        .values('created_at')[:1]
    )


def directory_queryset():
    """
    The base directory queryset: visible restaurants, with the row columns annotated.

    ONE query for the page, whatever its size. The two columns that would otherwise
    be a query per row — the open support count and the last admin activity — are an
    aggregate and a correlated subquery respectively. ``owner`` is joined rather than
    lazily loaded for the same reason.

    Soft-deleted restaurants are excluded here rather than at each call site, so
    "invisible to the directory" is a property of the queryset and not something a
    future caller has to remember.

    Ordering is ``name`` then ``id``: operator-friendly (the directory is scanned by
    name) and total, because ``name`` is not unique — ``Restaurant`` has a
    ``unique_together`` on (name, location, owner), so two restaurants can share a
    name. Without the ``id`` tiebreak, pagination could show or skip a row across
    pages, which is the classic non-deterministic-pagination bug.
    """
    return (
        Restaurant.objects
        .filter(deleted=False)
        .select_related('owner')
        .annotate(
            open_issue_count=Count(
                'support_issues',
                filter=Q(
                    support_issues__status__in=OPEN_SUPPORT_STATUSES,
                    support_issues__deleted=False,
                ),
                distinct=True,
            ),
            last_activity_at=_last_activity_subquery(),
        )
        .order_by('name', 'id')
    )


# --- serialization -----------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def serialize_row(restaurant):
    """
    One directory row.

    ``id`` is the ``Restaurant`` UUID. There is deliberately NO human-readable
    reference (a "REST-0018") — the backend has no such column, and minting a
    sequential identifier to match a design mock would be inventing a persistent
    business key in a read endpoint. See the PR description; it is a product
    decision, not a serialization one.

    Reads ``open_issue_count`` and ``last_activity_at`` off the annotations, so this
    must be given a row from ``directory_queryset()``.
    """
    return {
        'id': str(restaurant.id),
        'name': restaurant.name,
        'location': restaurant.location,
        'status': restaurant.status,
        'is_test': restaurant.is_test,
        'readiness': readiness_summary(restaurant),
        **payment_mode_summary(restaurant),
        'subscription': subscription_summary(restaurant),
        'open_issue_count': restaurant.open_issue_count,
        'last_activity_at': _iso(restaurant.last_activity_at),
        'needs_attention': needs_attention(restaurant),
    }


def serialize_owner(owner):
    """
    The owner identity an operator needs to make contact. Existing User fields only.

    NO owner-claim status. There is no owner-invitation or claim model yet, so the
    portal is told ``claim_status: None`` and ``claim_tracked: False`` rather than
    being handed a guess. An account existing is not the same as an owner having
    claimed it, and Step 2 builds the difference.
    """
    if owner is None:
        return None
    full_name = ' '.join(
        part for part in (owner.first_name, owner.last_name) if part
    ).strip()
    return {
        'id': str(owner.id),
        'name': full_name or None,
        'email': owner.email,
        'phone_number': owner.phone_number,
        'is_active': owner.is_active,
        # Explicitly unavailable rather than absent — the portal renders "not
        # tracked yet" instead of inferring that an unclaimed owner is claimed.
        'claim_tracked': False,
        'claim_status': None,
    }


def serialize_activity_entry(entry):
    """
    One audit row for the Overview's recent-activity strip.

    NARRATIVE SUBSTRATE ONLY. No ``before_state`` / ``after_state`` blobs, no
    ``source_ip``, no ``user_agent``, no ``request_id`` — the Overview answers "what
    has been happening here lately", and the full Activity screen (spec §12) is where
    forensic detail belongs. Keeping them out means this response cannot become an
    accidental egress path for redacted state.
    """
    actor = entry.actor
    if actor is not None:
        actor_display = (
            ' '.join(p for p in (actor.first_name, actor.last_name) if p).strip()
            or actor.email
            or str(actor.id)
        )
    else:
        # A failed authentication has no resolved user; `actor_label` is what was
        # typed, stored verbatim for forensics. Surfacing it is not an assertion
        # that the account exists.
        actor_display = entry.actor_label or None

    return {
        'id': str(entry.id),
        'timestamp': _iso(entry.created_at),
        'action': entry.action,
        'result': entry.result,
        'actor': actor_display,
    }


# --- detail: operational summary ---------------------------------------------

# Mirrors the model's own authoritative definition of a usable table,
# ``Table.is_available_for_scan``: not soft-deleted, enabled, active, and not out of
# service. Expressed here in SQL so the count is one aggregate rather than a property
# evaluated per row — but it is a MIRROR, not a new definition, and
# ``tests_restaurant_directory`` asserts the two agree. `Table.status` is non-null
# with a default, so the negated Q is safe.
TABLE_OUT_OF_SERVICE = 'out_of_service'


def operations_summary(restaurant):
    """
    Floor and trading facts for the Overview tab. Existing data only.

    Three bounded aggregates, never a query per table or per order. No invented
    semantics: ``usable_table_count`` mirrors the model's own scan-availability rule
    rather than picking whichever boolean looked closest.
    """
    from orders_app.models import Order
    from restaurants_app.models import DiningArea, Table

    tables = Table.objects.filter(restaurant=restaurant, deleted=False).aggregate(
        total=Count('id'),
        usable=Count(
            'id',
            filter=Q(enabled=True, is_active=True) & ~Q(status=TABLE_OUT_OF_SERVICE),
        ),
    )
    dining_area_count = DiningArea.objects.filter(
        restaurant=restaurant, deleted=False,
    ).count()

    latest_order = (
        Order.objects
        .filter(restaurant=restaurant, deleted=False)
        .order_by('-time_created')
        .values('id', 'time_created', 'order_status', 'is_test')
        .first()
    )

    return {
        'table_count': tables['total'] or 0,
        # "Usable" in the diner's sense: a table a QR scan can actually start an
        # order at. Distinct from `table_count`, which includes tables the operator
        # has disabled or taken out of service.
        'usable_table_count': tables['usable'] or 0,
        'dining_area_count': dining_area_count,
        'latest_order': {
            'id': str(latest_order['id']),
            'created_at': _iso(latest_order['time_created']),
            'order_status': latest_order['order_status'],
            'is_test': latest_order['is_test'],
        } if latest_order else None,
    }


def recent_activity(restaurant, limit=4):
    """
    The newest admin audit entries for this restaurant, newest first.

    ``select_related('actor')`` because the strip renders a name per row and this is
    otherwise a query per entry. Bounded by ``limit``, so it cannot grow with history.
    """
    entries = (
        AdminAuditLog.objects
        .filter(restaurant_id=restaurant.id)
        .select_related('actor')
        .order_by('-created_at')[:limit]
    )
    return [serialize_activity_entry(entry) for entry in entries]


def serialize_detail(restaurant):
    """
    The restaurant workspace header + Overview payload.

    Shares ``readiness_summary`` / ``payment_mode_summary`` / ``subscription_summary``
    with the directory rather than restating them — the header and the row must never
    disagree about whether a restaurant is ready. Expects a row from
    ``directory_queryset()`` so the shared annotations are present.
    """
    return {
        'id': str(restaurant.id),
        'name': restaurant.name,
        'location': restaurant.location,
        'status': restaurant.status,
        'is_test': restaurant.is_test,
        # Read off the lifecycle service, never a matrix restated here — the same
        # source the transition endpoint answers with.
        'allowed_transitions': lifecycle.allowed_targets(restaurant.status),
        'created_at': _iso(restaurant.time_created),
        'owner': serialize_owner(restaurant.owner),
        'readiness': readiness_summary(restaurant),
        **payment_mode_summary(restaurant),
        'subscription': subscription_summary(restaurant),
        'support': {'open_issue_count': restaurant.open_issue_count},
        'operations': operations_summary(restaurant),
        'last_activity_at': _iso(restaurant.last_activity_at),
        'recent_activity': recent_activity(restaurant),
        'needs_attention': needs_attention(restaurant),
    }


# --- query-parameter validation ----------------------------------------------

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# Accepted spellings for the attention flag. Strict: anything else is a 400 rather
# than a silent false, because a filter that quietly ignores what it was asked
# returns a plausible-looking page that answers a different question.
_TRUE_VALUES = frozenset({'true', '1', 'yes'})
_FALSE_VALUES = frozenset({'false', '0', 'no'})

# The complete set of parameters this endpoint understands. Anything else is a 400.
#
# Without this an unrecognised key is simply never read: `?stats=live` (a typo for
# `status`) returns a cheerful unfiltered 200, which is precisely the "plausible page
# answering a different question" the strict validation above exists to prevent — and
# the worst version of it, because the operator believes they filtered. Deny-by-default
# on the query string matches the deny-by-default posture of every route on this plane.
#
# Adding a parameter means adding it HERE as well as parsing it; a test asserts the two
# stay in step, so a new filter cannot ship silently rejected.
KNOWN_PARAMS = frozenset({'search', 'status', 'attention', 'page', 'page_size'})

# An upper bound on `page`, so a page number cannot become an unrepresentable OFFSET.
#
# `page_size` was always bounded; `page` was not, and the two multiply. On PostgreSQL
# `(page - 1) * page_size` is emitted as an OFFSET literal, so a page number just
# past `bigint` overflowed it and raised `DataError` — a 500 from the one path whose
# contract is that a bad parameter is a 400. The cap is the fix rather than catching the
# DataError, because a page number that large is a malformed request, not a deep read.
#
# 1,000,000 pages is beyond any real portfolio by several orders of magnitude (even at
# `page_size=1`), and keeps the largest reachable offset at 10^8 — comfortably inside
# `bigint` on every backend.
MAX_PAGE = 1_000_000


class QueryParamError(Exception):
    """Invalid query parameters. ``errors`` is field-keyed for the 400 body."""

    def __init__(self, errors):
        super().__init__('Invalid query parameters.')
        self.errors = errors


def _positive_int(raw, field, errors, *, default, maximum=None):
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        errors[field] = ['Must be a positive integer.']
        return default
    if value < 1:
        errors[field] = ['Must be a positive integer.']
        return default
    if maximum is not None and value > maximum:
        errors[field] = [f'Must not exceed {maximum}.']
        return default
    return value


def parse_directory_params(query):
    """
    Validate the directory's query parameters, or raise ``QueryParamError``.

    EVERY problem is collected before raising, so an operator fixing a bookmarked URL
    is told about all of it at once rather than one round trip per mistake.

    A malformed filter is a 400, never an empty 200. An empty list is a meaningful
    answer — "no restaurants match" — and returning it for a typo would let the
    operator conclude something false about the portfolio.
    """
    errors = {}

    # Checked FIRST so a typo is reported even when nothing else is wrong, and
    # collected like every other problem rather than short-circuiting: an operator
    # fixing a bookmarked URL should be told about the bad key AND the bad value in
    # one response. Sorted so the message is deterministic across dict orderings.
    unknown = sorted(set(query.keys()) - KNOWN_PARAMS)
    if unknown:
        errors['__all__'] = [
            'Unknown query parameter(s): ' + ', '.join(unknown)
            + '. Supported: ' + ', '.join(sorted(KNOWN_PARAMS)) + '.'
        ]

    search = (query.get('search') or '').strip()

    status = (query.get('status') or '').strip()
    if status and status not in RESTAURANT_LIFECYCLE_STATES:
        errors['status'] = [
            'Must be one of: ' + ', '.join(RESTAURANT_LIFECYCLE_STATES) + '.'
        ]

    attention = None
    raw_attention = query.get('attention')
    if raw_attention is not None and raw_attention != '':
        lowered = str(raw_attention).strip().lower()
        if lowered in _TRUE_VALUES:
            attention = True
        elif lowered in _FALSE_VALUES:
            attention = False
        else:
            errors['attention'] = ['Must be a boolean (true or false).']

    page = _positive_int(
        query.get('page'), 'page', errors, default=1, maximum=MAX_PAGE,
    )
    page_size = _positive_int(
        query.get('page_size'), 'page_size', errors,
        default=DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE,
    )

    if errors:
        raise QueryParamError(errors)

    return {
        'search': search,
        'status': status or None,
        'attention': attention,
        'page': page,
        'page_size': page_size,
    }


def apply_directory_filters(queryset, params):
    """Apply the validated filters. Search matches name or location, case-insensitive."""
    search = params['search']
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) | Q(location__icontains=search)
        )
    if params['status']:
        queryset = queryset.filter(status=params['status'])
    if params['attention'] is True:
        queryset = queryset.filter(attention_filter())
    elif params['attention'] is False:
        queryset = queryset.exclude(attention_filter())
    return queryset
