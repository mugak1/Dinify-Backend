"""
ADMIN-CREATED ONBOARDING — the authoritative primitive that brings a NEW canonical
``Restaurant`` into existence through the platform Admin control plane.

The third writer in the onboarding domain and the first that creates a tenant.
``onboarding_adoption`` records how an ALREADY-EXISTING restaurant entered Admin;
this module is the other provenance:

    an explicit, attributed platform-admin decision
              |
    User (owner)  +  Restaurant  +  owner RestaurantEmployee
              |
    RestaurantOnboarding(source='admin_created', created_by=<actor>)
              |
    OwnerInvitation(unresolved)  ->  one raw claim token, returned ONCE

ONE ADMINISTRATIVE DECISION, SIX ROWS, ONE TRANSACTION. "Create this restaurant
under this owner and issue its initial claim credential" is a single decision, so
either all of it commits or none of it does. A half-created tenant — a restaurant
with no owner authority, an owner account with no restaurant, an onboarding record
with no invitation — is not a state anybody can act on, and the operator would have
no way to tell which half survived.

━━ WHAT THIS IS NOT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NOT THE RETIRED ``admin_register_restaurant``. That flow generated an 8-character
random password, called ``self_register(skip_otp=True, send_credentials=True)`` and
emailed the credentials. It is deleted, and this module exists specifically so it is
not rebuilt. A newly created owner gets an UNUSABLE password; the invitation is the
onboarding credential, and there is no second one.

NOT A DELIVERY. This MINTS and ISSUES the invitation. It sends no SMS, no email, no
notification, and it never claims anything was delivered — the schema has no delivery
columns for exactly that reason (see ``OwnerInvitation``). Operator-mediated handoff
is the first implementation; delivery lands additively when its architecture is
chosen.

NOT A CLAIM. The invitation begins UNRESOLVED and owner control stays
``not_established`` until a future redemption CONSUMES it. Nothing here sets
``consumed_at``, and nothing here writes the legacy attestation triple — for
``admin_created`` provenance the database refuses attestation outright, because an
administrator vouching for an owner they just invented is not evidence of anything.

NOT COMMERCIAL, AND NOT OPERATIONAL. No service configuration, no subscription
terms, no invoice, no PSP, no readiness verdict, no owner go-live approval; no dining
area, table, QR credential, menu section, menu item, order or support issue. A
newborn tenant being commercially and operationally empty is CORRECT — those absences
are the readiness blockers a later step will name.

━━ THE AUDIT LIVES IN THE ADAPTER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Unlike ``onboarding_adoption``, this service writes NO ``AdminAuditLog`` row. Its one
adapter is an HTTP endpoint, and the auditable unit there is THE REQUEST — which also
has to record an elevation denial, an unreadable body and a rejected payload, none of
which this service ever sees. Splitting one request's audit vocabulary across two
layers would make "exactly one entry per unsafe request" impossible to state.

So the endpoint opens the outer ``transaction.atomic()``, calls this service (whose
own atomic block nests as a savepoint) and writes the audit row inside it — the
Step-3D.2 composition. A failed audit therefore rolls the whole creation back, and a
REFUSED creation can still be recorded, because the domain exception unwinds only its
own savepoint. ``commercial_app`` is built the same way and for the same reason.
"""
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Union

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
    RestaurantStatus_Onboarding,
)
from misc_app.controllers.msisdn import MsisdnError, normalise_msisdn
from platform_admin_app import sessions
from platform_admin_app.models import (
    ONBOARDING_SOURCE_ADMIN_CREATED,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding import assert_owner_consistency
from platform_admin_app.services import guard_membership_creation
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

# --- policy constants --------------------------------------------------------

# The owner claim window. Read through ``getattr`` with this same default so the
# service behaves identically under the base and test settings, where the
# admin-only names are undefined — the contract every other admin constant follows
# (see ``platform_admin_app.sessions``).
OWNER_INVITATION_TTL_DEFAULT = timedelta(days=7)

# Raw claim-token entropy, matching ``sessions._TOKEN_BYTES``: 48 bytes -> a 64-char
# url-safe string (~288 bits). The same standard as an admin session token, a login
# challenge and a delegation code, because this is the same kind of thing.
_TOKEN_BYTES = 48

# PHASE-1 IS UGANDA-ONLY, deliberately and not merely by omission. ``Restaurant``
# carries a ``country`` column and ``normalise_msisdn`` accepts a country argument,
# and neither is an invitation to accept one from a client here: the MSISDN
# canonicaliser supports Uganda ALONE ("there is deliberately no country -> prefix
# abstraction table"), so a restaurant recorded in another country would have an
# owner whose phone could not be canonicalised. Adding a market is an explicit,
# reviewed change in both places at once — not a request field.
ONBOARDING_COUNTRY = 'UG'

# Matches the ``Restaurant.name`` / ``Restaurant.location`` columns. A bound, not a
# product rule: an over-long value must be a named 400 rather than a database error.
MAX_RESTAURANT_TEXT_LENGTH = 255
MAX_PERSON_NAME_LENGTH = 255
MAX_EMAIL_LENGTH = 255

_WHITESPACE_RUN = re.compile(r'\s+')

# --- the owner contract ------------------------------------------------------
#
# TWO SHAPES, NOT ONE SHAPE WITH AN OPTIONAL HALF. Creating a new Dinify identity
# and attaching an existing one are different decisions with different consequences,
# and the wrong-owner failure is silent: a restaurant handed to the wrong person
# looks exactly like a restaurant handed to the right one. Making the union
# structural means an ambiguous hybrid cannot be constructed at all, rather than
# being refused by a check somebody could later forget to run.


@dataclass(frozen=True)
class NewOwner:
    """Create a brand-new canonical restaurant-user identity for this restaurant."""

    first_name: str
    last_name: str
    phone_number: str
    email: Optional[str] = None


@dataclass(frozen=True)
class ExistingOwner:
    """
    Attach an existing, explicitly named restaurant-user account.

    A ``User`` may legitimately own more than one restaurant, so this is an ordinary
    supported case — but it is REQUESTED BY UUID and never inferred. "This phone
    already exists, so that person must be who you meant" is the assumption this
    contract exists to refuse.
    """

    user_id: Union[str, uuid.UUID]


OwnerSpec = Union[NewOwner, ExistingOwner]

# --- error codes -------------------------------------------------------------
#
# Split by REMEDY, not by layer. A malformed field needs the request corrected; a
# state conflict needs the operator to look at the platform and make a different
# decision. The adapter maps the first group to 400 and the second to 409.

INVALID_RESTAURANT_NAME = 'invalid_restaurant_name'
INVALID_RESTAURANT_LOCATION = 'invalid_restaurant_location'
INVALID_TEST_CLASSIFICATION = 'invalid_test_classification'
INVALID_REASON = 'invalid_reason'
INVALID_ACTOR = 'invalid_actor'
INVALID_OWNER_SPEC = 'invalid_owner_spec'
INVALID_OWNER_USER_ID = 'invalid_owner_user_id'
INVALID_OWNER_NAME = 'invalid_owner_name'
INVALID_OWNER_PHONE = 'invalid_owner_phone'
INVALID_OWNER_EMAIL = 'invalid_owner_email'

OWNER_ACCOUNT_ALREADY_EXISTS = 'owner_account_already_exists'
OWNER_EMAIL_ALREADY_IN_USE = 'owner_email_already_in_use'
OWNER_ACCOUNT_NOT_FOUND = 'owner_account_not_found'
OWNER_ACCOUNT_INACTIVE = 'owner_account_inactive'
OWNER_ACCOUNT_NOT_RESTAURANT_USER = 'owner_account_not_restaurant_user'
RESTAURANT_ALREADY_EXISTS = 'restaurant_already_exists'

INVALID_REQUEST_CODES = frozenset({
    INVALID_RESTAURANT_NAME,
    INVALID_RESTAURANT_LOCATION,
    INVALID_TEST_CLASSIFICATION,
    INVALID_REASON,
    INVALID_ACTOR,
    INVALID_OWNER_SPEC,
    INVALID_OWNER_USER_ID,
    INVALID_OWNER_NAME,
    INVALID_OWNER_PHONE,
    INVALID_OWNER_EMAIL,
})

CONFLICT_CODES = frozenset({
    OWNER_ACCOUNT_ALREADY_EXISTS,
    OWNER_EMAIL_ALREADY_IN_USE,
    OWNER_ACCOUNT_NOT_FOUND,
    OWNER_ACCOUNT_INACTIVE,
    OWNER_ACCOUNT_NOT_RESTAURANT_USER,
    RESTAURANT_ALREADY_EXISTS,
})

CREATION_ERROR_CODES = INVALID_REQUEST_CODES | CONFLICT_CODES


class RestaurantCreationError(Exception):
    """
    Creation was refused. ``code`` is one of ``CREATION_ERROR_CODES``.

    Mirrors ``AdoptionError`` / ``OwnerConsistencyError`` deliberately: a short
    machine-readable ``code``, a sentence a human can act on, and ``details``
    carrying UUIDs ONLY.

    NO PII IN ``details``, EVER. These messages reach logs, operator screens and (in
    part) the audit log, and an owner's phone number or email has no business in any
    of them — an identifier is enough to investigate with. ``normalise_msisdn``'s own
    errors are already written to be safe to surface for the same reason.
    """

    def __init__(self, code, message='', details=None):
        self.code = code
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(self.message)

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class CreationResult:
    """
    What the operation created, stated by the operation rather than inferred after it.

    Frozen. ``owner_created`` is the fact the response reports as
    ``owner_account.created`` — an adapter must never re-derive it by comparing
    timestamps or re-querying, because by then a brand-new account and a
    long-standing one are indistinguishable.

    ``claim_token`` IS THE RAW CREDENTIAL and the only copy that will ever exist:
    only its SHA-256 hash is persisted. It lives on this object exactly long enough
    to be written into one HTTP response. It must never be logged, audited, stored,
    put in a URL or a cookie, or included in an exception.
    """

    restaurant: Restaurant
    onboarding: RestaurantOnboarding
    owner: User
    owner_created: bool
    invitation: OwnerInvitation
    claim_token: str


# --- validation --------------------------------------------------------------

def owner_invitation_ttl():
    """The configured owner claim window."""
    return getattr(
        settings, 'ADMIN_OWNER_INVITATION_TTL', OWNER_INVITATION_TTL_DEFAULT,
    )


def _collapse(raw):
    """
    Trim and collapse internal whitespace runs to single spaces.

    Deterministic normalisation, applied to every free-text identifying value so a
    stray double space cannot make ``"Kampala  Bistro"`` a second restaurant. It is
    NOT case folding and NOT title-casing: the operator types the business's real
    name, and ``str.title()`` would render "KFC" as "Kfc" and "McDonald's" as
    "Mcdonald'S". (The retired creation path title-cased restaurant names; that is
    one of the behaviours deliberately not carried over.)
    """
    return _WHITESPACE_RUN.sub(' ', str(raw or '').strip())


def _validate_text(raw, code, label):
    """One required, bounded, whitespace-normalised identifying value."""
    cleaned = _collapse(raw)
    if not cleaned:
        raise RestaurantCreationError(code, f'A {label} is required.')
    if len(cleaned) > MAX_RESTAURANT_TEXT_LENGTH:
        raise RestaurantCreationError(
            code,
            f'The {label} must be at most {MAX_RESTAURANT_TEXT_LENGTH} characters.',
        )
    return cleaned


def _validate_is_test(raw) -> bool:
    """
    The test classification, which must be an actual boolean.

    NOT coerced, and not defaulted. ``is_test`` decides whether a tenant's orders are
    commerce at all: a real restaurant misclassified TEST silently vanishes from
    every revenue figure, and a test tenant misclassified real contaminates them.
    Accepting ``1``, ``"true"`` or an omission would let that decision be made by a
    coercion rule instead of by an operator. It is never inferred from the name, the
    environment, the actor or the location.
    """
    if not isinstance(raw, bool):
        raise RestaurantCreationError(
            INVALID_TEST_CLASSIFICATION,
            'State the test classification explicitly as true or false.',
        )
    return raw


def _validate_reason(raw) -> str:
    """
    The trimmed operator reason, or ``RestaurantCreationError(INVALID_REASON)``.

    The bar is IMPORTED from ``restaurants_app.controllers.lifecycle``, which mirrors
    ``platform_admin_app.delegation.MIN_REASON_LENGTH``. Creating a tenant is at
    least as consequential as suspending one, and a fourth literal ``10`` is how
    these surfaces would start to disagree about what a reason is.
    """
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise RestaurantCreationError(INVALID_REASON, 'A reason is required.')
    if len(cleaned) < MIN_REASON_LENGTH:
        raise RestaurantCreationError(
            INVALID_REASON,
            f'A reason of at least {MIN_REASON_LENGTH} characters is required '
            '(the audit row is only as useful as this sentence).',
        )
    return cleaned


def _resolve_actor(actor) -> User:
    """
    The platform-staff ``User`` whose decision this is, re-read from the database.

    Enforced here even though the HTTP adapter arrives with an authenticated,
    elevated session — the service is what stamps ``created_by`` and ``issued_by``,
    so the service is where that attribution has to be true. A rule enforced only in
    adapters holds until the second adapter. Re-read rather than trusted, because an
    instance's ``is_active`` / ``account_type`` are whatever they were when it was
    loaded; the row is the fact.
    """
    if not isinstance(actor, User) or actor.pk is None:
        raise RestaurantCreationError(
            INVALID_ACTOR,
            'An actor is required: creation is an attributed platform decision.',
        )
    fresh = User.objects.filter(pk=actor.pk).first()
    if fresh is None:
        raise RestaurantCreationError(
            INVALID_ACTOR, 'The actor account no longer exists.',
        )
    if fresh.account_type != ACCOUNT_TYPE_PLATFORM_STAFF:
        raise RestaurantCreationError(
            INVALID_ACTOR,
            'Creation is a platform decision; a restaurant user can never be its '
            'actor.',
        )
    if not fresh.is_active:
        raise RestaurantCreationError(
            INVALID_ACTOR,
            'The actor account is deactivated and cannot be recorded as the actor.',
        )
    return fresh


def _validate_person_name(raw, label) -> str:
    """
    One normalised personal name.

    ``.strip().title()`` is the repository's existing convention for a restaurant
    user's name (``users_app.controllers.self_register``, and
    ``REQUIRED_INFORMATION['new_user']``'s ``text_presentation``). It is followed
    here so an admin-created owner is stored exactly like a self-registered one; if
    the convention is ever revisited it should be revisited repo-wide, not diverged
    from in one writer.
    """
    cleaned = _collapse(raw)
    if not cleaned:
        raise RestaurantCreationError(
            INVALID_OWNER_NAME, f"The owner's {label} is required.",
        )
    if len(cleaned) > MAX_PERSON_NAME_LENGTH:
        raise RestaurantCreationError(
            INVALID_OWNER_NAME,
            f"The owner's {label} must be at most {MAX_PERSON_NAME_LENGTH} "
            'characters.',
        )
    return cleaned.title()


def _validate_owner_phone(raw) -> str:
    """
    The owner's phone in the canonical stored form, ``256XXXXXXXXX``.

    ``normalise_msisdn`` is the single source of truth for that canonicalisation and
    is applied at every write site in the repository; applying it here is what makes
    the collision check below compare canonical-to-canonical, so ``0772123456`` and
    ``+256 772 123 456`` cannot become two accounts. Its error messages never
    include the raw number, so they are safe to surface.
    """
    try:
        return normalise_msisdn(raw, country=ONBOARDING_COUNTRY)
    except MsisdnError as exc:
        raise RestaurantCreationError(INVALID_OWNER_PHONE, str(exc))


def _validate_owner_email(raw) -> Optional[str]:
    """
    The owner's email, lower-cased and VALIDATED, or ``None``.

    OPTIONAL, because it is optional for a canonical restaurant user:
    ``REQUIRED_INFORMATION['new_user']`` has the email entry commented out, and the
    column is nullable. Phone is the identity; email is contact detail.

    Lower-cased because that is how ``self_register`` stores it and how
    ``login`` looks it up. Blank collapses to ``None`` rather than ``''`` so
    "no email" has ONE representation — an empty string would sit in a column whose
    other absent values are NULL, and would then collide with the next blank one.

    VALIDATED WITH DJANGO'S OWN ``validate_email``, not with a hand-rolled test for
    where the ``@`` sits. Nothing else on this path will catch a malformed address:
    ``Model.save()`` does NOT run field validators, and the request field is a
    ``CharField`` precisely so that blank-versus-absent stays this module's decision
    — so an address like ``'a b@example.com'`` or ``'a@-example.com'`` would be
    persisted verbatim while the API documents an invalid email as a 400. Reusing the
    validator ``EmailField`` itself uses is what makes the accepted set and the
    column's own idea of a valid address the same set.

    The refusal message is fixed and never echoes the address, so it is safe to log
    or surface — the same rule ``normalise_msisdn``'s errors follow.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip().lower()
    if not cleaned:
        return None
    # Length FIRST: an over-long value gets the bound's own message rather than a
    # generic "invalid", which is the more actionable of the two.
    if len(cleaned) > MAX_EMAIL_LENGTH:
        raise RestaurantCreationError(
            INVALID_OWNER_EMAIL,
            f'The email must be at most {MAX_EMAIL_LENGTH} characters.',
        )
    try:
        validate_email(cleaned)
    except DjangoValidationError:
        raise RestaurantCreationError(
            INVALID_OWNER_EMAIL, 'Enter a valid email address.',
        )
    return cleaned


def _validate_owner_user_id(raw) -> uuid.UUID:
    """The named account's UUID, parsed strictly and before any database access."""
    if isinstance(raw, uuid.UUID):
        return raw
    cleaned = str(raw or '').strip()
    if not cleaned:
        raise RestaurantCreationError(
            INVALID_OWNER_USER_ID, 'An owner account UUID is required.',
        )
    try:
        return uuid.UUID(cleaned)
    except (ValueError, AttributeError, TypeError):
        raise RestaurantCreationError(
            INVALID_OWNER_USER_ID,
            'The owner must be named by account UUID; creation never resolves an '
            'owner by name, email or phone.',
        )


# --- owner resolution --------------------------------------------------------

def _attach_existing_owner(user_id) -> User:
    """
    Lock and re-validate an explicitly named existing account. Writes NOTHING.

    THE ROW IS LOCKED AND RE-READ INSIDE THE TRANSACTION, not merely fetched. The
    eligibility facts this checks — ``account_type`` and ``is_active`` — are exactly
    the ones that can change between a validation pass and a write, and an account
    deactivated or promoted to platform staff in that window must refuse rather than
    become a restaurant owner. Locking also makes the owner row the serialization
    point for two concurrent creations under the same owner.

    ``of=('self',)`` and no ``select_related``: this locks the ``users`` row and
    NOTHING ELSE. An over-broad ``select_for_update`` is how the delegation-redemption
    ABBA cycle happened (PR-E), and the lesson is cheap to apply.

    NOT A REPAIR PATH. An inactive account is REFUSED, never reactivated —
    reactivating somebody's account is a consequential decision with its own actor,
    reason and audit trail, and it is not one this operation has been asked to make.
    Nothing about the account is altered: not the name, email, phone, username,
    password, roles, ``prompt_password_change`` or any other field.
    """
    owner = (
        User.objects
        .select_for_update(of=('self',))
        .filter(pk=user_id)
        .first()
    )
    if owner is None:
        # 409, never 404. The caller is an authenticated platform administrator who
        # explicitly named this UUID; answering "not found" about the RESTAURANT
        # route would tell them the wrong thing was missing.
        raise RestaurantCreationError(
            OWNER_ACCOUNT_NOT_FOUND,
            'No such account. Check the owner account id.',
            {'owner_user_id': str(user_id)},
        )
    if owner.account_type != ACCOUNT_TYPE_RESTAURANT_USER:
        # A platform administrator can create a tenant; they cannot simultaneously
        # become its restaurant authority. The plane separation is the whole point of
        # `account_type`, and `services.guard_membership_creation` refuses the
        # membership independently — this refusal exists so the operator gets a
        # sentence instead of an invariant error.
        raise RestaurantCreationError(
            OWNER_ACCOUNT_NOT_RESTAURANT_USER,
            'That account is not a restaurant user and cannot own a restaurant.',
            {'owner_user_id': str(owner.pk)},
        )
    if not owner.is_active:
        raise RestaurantCreationError(
            OWNER_ACCOUNT_INACTIVE,
            'That account is deactivated. Reactivating it is a separate decision '
            'and is not performed by creating a restaurant.',
            {'owner_user_id': str(owner.pk)},
        )
    return owner


def _create_owner(spec: NewOwner) -> User:
    """
    Create the canonical owner identity. NO PASSWORD IS GENERATED.

    The account is created with an UNUSABLE password (``password=None`` →
    ``set_unusable_password``), so no credential exists to leak, to email, to SMS or
    to have to rotate. The ``OwnerInvitation`` minted alongside it is the onboarding
    credential, and a future redemption owns whatever account-claim semantics we
    then design.

    ``prompt_password_change`` keeps its model default and is NOT touched: it is a
    UI hint, and it must never become the proof that an owner claimed their account
    — ``onboarding_reads`` counts a CONSUMED invitation and nothing else.

    PHONE IS THE IDENTITY. A canonical phone already held by an account is a
    CONFLICT, never a silent reuse: "this number exists, so that person must be who
    you meant" is precisely the quiet wrong-owner failure the explicit
    ``ExistingOwner`` mode exists to prevent. The pre-check gives the operator the
    existing account's id so they can look at it and deliberately choose that mode;
    the ``phone_number`` unique index behind the savepoint is what makes the refusal
    race-free rather than merely likely.

    EMAIL IS NOT IDENTITY and is never used to select an owner — but a duplicate one
    is still refused, for a specific reason recorded in this repository already:
    ``users_app.controllers.login`` and ``reset_password._resolve_user`` both call
    ``User.objects.get(email=...)``, which raises ``MultipleObjectsReturned`` when
    two accounts share an address. Creating that state would break email login and
    password reset with a 500 for BOTH users — the same reasoning
    ``update_user_profile`` already applies to an email change. The refusal names no
    other account: pointing at one would invite exactly the "email identifies the
    owner" inference this contract refuses.

    THE EMAIL CHECK IS BEST-EFFORT, AND UNLIKE THE PHONE CHECK IT IS NOT RACE-FREE.
    Say so plainly rather than let the paragraph above read as a guarantee: phone is
    backed by a unique index, so the savepoint below converts a lost race into a
    refusal, while ``User.email`` carries NO unique constraint — two concurrent
    creations with different phones and the same address can both miss this read and
    both insert. Nothing in the schema forbids the pair, and READ COMMITTED gives a
    ``SELECT`` no predicate lock to take, so no amount of care HERE closes it: the
    fix is a partial unique index on ``User.email``, which is a CONTRACT migration
    that fails at deploy if the existing corpus already holds a duplicate, and so
    needs the production data inspected first. It is recorded as an open seam in
    CLAUDE.md rather than closed with a new lock domain that would serialise this
    endpoint against itself while ``self_register`` and ``update_user_profile`` — which
    carry the identical non-atomic check today — went on writing around it.
    """
    existing_phone = (
        User.objects.filter(phone_number=spec.phone_number)
        .values_list('pk', flat=True)
        .first()
    )
    if existing_phone is not None:
        raise RestaurantCreationError(
            OWNER_ACCOUNT_ALREADY_EXISTS,
            'An account already uses that phone number. Review it and, if it is '
            'the intended owner, create the restaurant with that account instead.',
            {'owner_user_id': str(existing_phone)},
        )

    if spec.email and User.objects.filter(email__iexact=spec.email).exists():
        raise RestaurantCreationError(
            OWNER_EMAIL_ALREADY_IN_USE,
            'Another account already uses that email address. Email is not an '
            'identity here; supply a different address or leave it blank.',
        )

    owner = User(
        # The canonical MSISDN is BOTH the username and the phone number, which is
        # the convention every other restaurant-user write site follows.
        username=spec.phone_number,
        phone_number=spec.phone_number,
        # NULL rather than '' when absent, so "no email" has one representation in a
        # nullable column. (`User.objects.create_user` would store '' here, because
        # `BaseUserManager.normalize_email(None)` returns '' — which is why this
        # builds the instance directly.)
        email=spec.email,
        first_name=spec.first_name,
        last_name=spec.last_name,
        country=ONBOARDING_COUNTRY,
        # EMPTY. `User.roles` is not an authority vocabulary — Phase 0.5 removed the
        # last thing that read it that way — and restaurant authority is the
        # membership created alongside this account, never a string here.
        roles=[],
        account_type=ACCOUNT_TYPE_RESTAURANT_USER,
    )
    # THE WHOLE POINT, stated as one greppable line rather than inferred from
    # `make_password(None)`: no password is generated, so there is no credential to
    # email, to SMS, to leak or to rotate. Nothing can authenticate as this account
    # until a future redemption establishes one.
    owner.set_unusable_password()
    try:
        # A SAVEPOINT around the INSERT. Catching `IntegrityError` without one would
        # leave the OUTER transaction marked for rollback and every subsequent query
        # would raise `TransactionManagementError` — so the nested block is what
        # turns a lost uniqueness race into a controlled refusal instead of a 500.
        with transaction.atomic():
            owner.save()
            return owner
    except IntegrityError:
        # The pre-check above lost a race with a concurrent creation (or with any
        # other write site). `phone_number` and `username` are both unique, so this
        # is the database stating the same fact the pre-check states — reported the
        # same way, without the id, which we deliberately do not go back to read
        # under a lost race.
        raise RestaurantCreationError(
            OWNER_ACCOUNT_ALREADY_EXISTS,
            'An account already uses that phone number. Review it and, if it is '
            'the intended owner, create the restaurant with that account instead.',
        )


def _resolve_owner(spec: OwnerSpec):
    """``(owner, owner_created)`` for either owner mode."""
    if isinstance(spec, ExistingOwner):
        return _attach_existing_owner(_validate_owner_user_id(spec.user_id)), False
    if isinstance(spec, NewOwner):
        return _create_owner(spec), True
    raise RestaurantCreationError(
        INVALID_OWNER_SPEC,
        'The owner must be given explicitly as a new identity or an existing '
        'account.',
    )


def _normalise_owner_spec(spec: OwnerSpec) -> OwnerSpec:
    """Validate and canonicalise the owner facts BEFORE any database work."""
    if isinstance(spec, NewOwner):
        return NewOwner(
            first_name=_validate_person_name(spec.first_name, 'first name'),
            last_name=_validate_person_name(spec.last_name, 'last name'),
            phone_number=_validate_owner_phone(spec.phone_number),
            email=_validate_owner_email(spec.email),
        )
    if isinstance(spec, ExistingOwner):
        return ExistingOwner(user_id=_validate_owner_user_id(spec.user_id))
    raise RestaurantCreationError(
        INVALID_OWNER_SPEC,
        'The owner must be given explicitly as a new identity or an existing '
        'account.',
    )


# --- the tenant --------------------------------------------------------------

def _duplicate_restaurant_id(name, location, owner):
    """
    The id of a restaurant this creation would duplicate, or ``None``.

    TWO RULES, ONE ANSWER, and they cover different things:

      * SAME OWNER — ``Restaurant.Meta.unique_together = (name, location, owner)``.
        A database fact, so this read is a courtesy that produces a sentence; the
        constraint behind the savepoint below is what actually enforces it, and it
        counts SOFT-DELETED rows too (the index carries no ``deleted`` predicate).
      * ANY OWNER — the same name at the same location, case-insensitively, among
        LIVE (non-soft-deleted) restaurants. This is the rule the retired
        ``admin_register_restaurant`` applied, preserved because it is the strongest
        truthful duplicate statement this repository has ever made, and because an
        accidental double submission is far likelier than two genuinely different
        businesses sharing a name AND a location.

    THE SECOND RULE IS NOT RACE-FREE and no attempt is made to pretend otherwise:
    nothing in the schema forbids it, so two simultaneous requests naming the same
    restaurant under DIFFERENT owners can both pass this read. Closing that would
    need either a new global uniqueness index over live rows (a migration whose
    behaviour against existing data is not obviously safe) or a lock domain broad
    enough to serialise unrelated creations — both larger decisions than this slice.
    The same-owner case, which is the double-click an operator actually produces,
    IS race-free: the owner row is locked and the constraint is the backstop.
    """
    any_owner = (
        Restaurant.objects
        .filter(name__iexact=name, location__iexact=location, deleted=False)
        .values_list('pk', flat=True)
        .first()
    )
    if any_owner is not None:
        return any_owner
    # Deliberately WITHOUT `deleted=False`: the unique index carries no such
    # predicate, so a soft-deleted row still occupies the (name, location, owner)
    # slot and the insert would fail. Saying so is better than letting it surface
    # as an integrity error.
    return (
        Restaurant.objects
        .filter(name=name, location=location, owner=owner)
        .values_list('pk', flat=True)
        .first()
    )


def _duplicate_error(existing_id=None):
    return RestaurantCreationError(
        RESTAURANT_ALREADY_EXISTS,
        'A restaurant with that name and location already exists.',
        {'restaurant_id': str(existing_id)} if existing_id else None,
    )


def _create_restaurant(name, location, is_test, owner, actor) -> Restaurant:
    """
    Insert the canonical tenant. Every platform-owned fact is supplied by the server.

    ``status`` starts at ``onboarding`` — the lifecycle's own entry state, and never
    taken from the request; ``restaurants_app.controllers.lifecycle`` remains the
    only thing that can move it afterwards. ``country`` is the Phase-1 server value.
    ``created_by`` is the platform actor as ATTRIBUTION only: ``BaseModel.created_by``
    is not tenant authority and confers nothing (that is the owner membership's job).

    Nothing else is accepted from the caller. Payment, subscription, tax, surcharge,
    branding, prepayment, approval and availability fields all keep their model
    defaults, because a creation surface that exposed them would be a second, untyped
    edit API for a tenant that does not exist yet.
    """
    try:
        with transaction.atomic():
            return Restaurant.objects.create(
                name=name,
                location=location,
                owner=owner,
                is_test=is_test,
                status=RestaurantStatus_Onboarding,
                country=ONBOARDING_COUNTRY,
                created_by=actor,
            )
    except IntegrityError:
        # `unique_together (name, location, owner)`. Re-read to confirm that is what
        # happened rather than relabelling any integrity failure as a duplicate: an
        # unexpected one must surface as a 500 and roll back, not be tidied into a
        # 409 that misdescribes it.
        existing = _duplicate_restaurant_id(name, location, owner)
        if existing is None:
            raise
        raise _duplicate_error(existing)


# --- the operation -----------------------------------------------------------

def create_admin_restaurant(
    *, name, location, is_test, owner: OwnerSpec, actor, reason,
) -> CreationResult:
    """
    Create one restaurant, its owner authority, its provenance and its claim credential.

    Returns a ``CreationResult`` carrying the raw claim token, which the caller must
    hand to exactly one HTTP response and then forget. Raises
    ``RestaurantCreationError`` (a named refusal) or ``OwnerConsistencyError`` (the
    invariant proof below failing, which would mean a bug in this function rather
    than bad input).

    ━━ THE TRANSACTION, AND WHAT SERIALIZES IT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Everything from owner resolution to the invitation happens in ONE
    ``transaction.atomic()``. THE OWNER ``User`` ROW IS THE SERIALIZATION POINT for
    an existing owner: the restaurant does not exist yet, so there is no tenant row
    two concurrent operators could share, and the owner is the only thing they do.
    For a NEW owner the serialization point is the ``phone_number`` unique index,
    which is a database fact and needs no lock at all.

    LOCK ORDER: ``User -> (INSERT Restaurant) -> (INSERT RestaurantEmployee) ->
    (INSERT RestaurantOnboarding) -> (INSERT OwnerInvitation) -> AdminAuditLog``.
    It row-locks NOTHING but the owner. ``User`` is already the top of the admin-auth
    chain (``User -> AdminLoginChallenge -> PlatformStaffAuth``), and this
    transaction never waits on a ``Restaurant`` row — it inserts a new one — so it
    cannot cycle against the lifecycle transition, which holds ``Restaurant`` and
    waits on ``User`` only for the ``FOR KEY SHARE`` its audit insert takes.

    NO ADMISSION ADVISORY LOCK, for the reason ``onboarding_adoption`` gives and one
    more. ``lock_admission_exclusive`` exists to stop an order being ADMITTED against
    one lifecycle state or test classification and then written under another; an
    order path cannot read a restaurant that does not exist yet, and after commit it
    reads the committed row like any other. Taking the lock would additionally invert
    the documented ``advisory -> Restaurant`` order for no benefit.

    ━━ NO ORPHANS, AND NO COMPENSATING DELETES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    A failure at any stage unwinds every earlier one through the database, never
    through cleanup code. A newly created owner whose invitation insert fails leaves
    no account behind; an EXISTING owner is never deleted, reverted or "rolled back
    manually" — it was only ever read and locked, so there is nothing to undo.
    """
    # Cheap, purely local validation first: nothing here needs the database, and a
    # blank name should never reach a row lock.
    clean_name = _validate_text(name, INVALID_RESTAURANT_NAME, 'restaurant name')
    clean_location = _validate_text(
        location, INVALID_RESTAURANT_LOCATION, 'restaurant location',
    )
    clean_is_test = _validate_is_test(is_test)
    clean_reason = _validate_reason(reason)
    owner_spec = _normalise_owner_spec(owner)
    resolved_actor = _resolve_actor(actor)

    # ONE captured instant for the whole credential. Two `timezone.now()` calls would
    # put a meaningless sub-millisecond drift between issue and expiry and make the
    # window something other than exactly the configured TTL.
    now = timezone.now()

    with transaction.atomic():
        owner_user, owner_created = _resolve_owner(owner_spec)

        existing = _duplicate_restaurant_id(clean_name, clean_location, owner_user)
        if existing is not None:
            # Creation is not adoption: a request that would duplicate an existing
            # business is REFUSED, never answered by handing back the restaurant
            # somebody else created as though this request had created it.
            raise _duplicate_error(existing)

        restaurant = _create_restaurant(
            clean_name, clean_location, clean_is_test, owner_user, resolved_actor,
        )

        # OWNER AUTHORITY. `Restaurant.owner` alone is a name on a row: the customer
        # plane resolves permissions from an active, non-deleted owner-role
        # `RestaurantEmployee` and nothing else, so without this the owner could not
        # sign into their own restaurant. The canonical `RESTAURANT_OWNER` constant,
        # never a hand-typed 'owner'.
        #
        # `guard_membership_creation` is the wired platform-staff invariant. It is
        # unreachable here — `_attach_existing_owner` already refused a non
        # restaurant-user and a new owner is created as one — and it is called anyway,
        # because "unreachable" is a property of today's call graph.
        guard_membership_creation(owner_user)
        RestaurantEmployee.objects.create(
            user=owner_user,
            restaurant=restaurant,
            roles=[RESTAURANT_OWNER],
            active=True,
            created_by=resolved_actor,
        )

        # NO `RestaurantRolePermission` ROWS ARE SEEDED, and that is a decision rather
        # than an omission. `permissions_check._resolve_from_roles` short-circuits an
        # owner to full access, and every other role falls back to
        # `role_defaults.DEFAULT_ROLE_MODULES` when no override row exists — the
        # resolver is correct with zero rows, which `ensure_role_permissions`'s own
        # docstring says. Seeding four rows that exactly restate the coded defaults
        # would create state whose only future is to drift from them.

        # THE INVARIANT PROOF, checked before anything else is written and inside the
        # transaction, so a failure takes the whole creation with it: the owner of
        # record IS the sole active owner authority. It validates and never repairs.
        assert_owner_consistency(restaurant)

        onboarding = RestaurantOnboarding.objects.create(
            restaurant=restaurant,
            source=ONBOARDING_SOURCE_ADMIN_CREATED,
            created_by=resolved_actor,
            # Explicit, though the `admin_created` shape constraint enforces all of
            # it. Attestation is refused for this provenance by the database, and
            # rightly: an administrator vouching for an owner Dinify has just
            # created would be manufacturing the evidence a consumed invitation is
            # supposed to provide.
            adopted_at=None,
            adopted_by=None,
            owner_control_attested_at=None,
            owner_control_attested_user=None,
            owner_control_attested_by=None,
        )

        # THE CREDENTIAL. High-entropy, url-safe, generated with `secrets`; only its
        # SHA-256 hash is persisted, via the same `sessions.hash_token` used for admin
        # sessions and delegation codes rather than a second hashing function.
        claim_token = secrets.token_urlsafe(_TOKEN_BYTES)
        invitation = OwnerInvitation.objects.create(
            onboarding=onboarding,
            invited_user=owner_user,
            issued_by=resolved_actor,
            token_hash=sessions.hash_token(claim_token),
            issued_at=now,
            expires_at=now + owner_invitation_ttl(),
            # Left unresolved. Marking it consumed here would assert that the owner
            # confirmed control at the moment Dinify created their account, which is
            # the one thing this credential exists to find out.
            consumed_at=None,
            cancelled_at=None,
            cancelled_by=None,
            superseded_at=None,
        )

    return CreationResult(
        restaurant=restaurant,
        onboarding=onboarding,
        owner=owner_user,
        owner_created=owner_created,
        invitation=invitation,
        claim_token=claim_token,
    )
