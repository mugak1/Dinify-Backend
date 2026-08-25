"""
The REQUEST CONTRACT for admin restaurant creation (Phase 1, Step 2D).

The serializers, the strict input primitives, the domain-error → HTTP status map and
the audit / response shapes for ``POST admin/v1/restaurants/``. The view itself lives
in ``endpoints/restaurants.py``, beside the ``GET`` it shares a resource with, so the
collection is described in one place; what lives here is everything about *what a
creation request is*, which is long enough to bury that view if it were inlined.

Nothing here writes. Every row is created by
``platform_admin_app.onboarding_creation.create_admin_restaurant``, which owns the
transaction, the owner lock, the invariant proof and the credential.

━━ THE OWNER SHAPE IS DISCRIMINATED, ON PURPOSE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``owner.mode`` is ``new`` or ``existing`` and the caller must say which. The tempting
alternative — accept identity fields, and quietly reuse whatever account already has
that phone — fails silently and expensively: a restaurant attached to the wrong
person looks exactly like one attached to the right person, and nobody finds out
until that person signs in. So a phone already in use is a CONFLICT, and attaching an
existing account is a separate, explicitly requested operation naming an exact UUID.

Each mode also REFUSES the other's fields rather than ignoring them. A request that
sends ``mode: "existing"`` plus a phone number believes something about what it is
asking for, and silently dropping half of it would confirm a belief that is wrong.
"""
import uuid

from rest_framework import serializers

from platform_admin_app import onboarding_creation
from platform_admin_app.endpoints.reasoned_request import ReasonedRequestSerializer
from platform_admin_app.onboarding_creation import (
    MAX_EMAIL_LENGTH,
    MAX_PERSON_NAME_LENGTH,
    MAX_RESTAURANT_TEXT_LENGTH,
    ExistingOwner,
    NewOwner,
)

OWNER_MODE_NEW = 'new'
OWNER_MODE_EXISTING = 'existing'
OWNER_MODES = (OWNER_MODE_NEW, OWNER_MODE_EXISTING)

# Fields that belong to exactly one mode. Each is refused in the other, so the
# accepted body is a property of the mode the caller named rather than of which keys
# they happened to include.
NEW_ONLY_OWNER_FIELDS = ('first_name', 'last_name', 'phone_number', 'email')
EXISTING_ONLY_OWNER_FIELDS = ('user_id',)


# --- strict input primitives -------------------------------------------------

class StrictBooleanField(serializers.Field):
    """
    A flag that MUST arrive as a JSON boolean.

    ``true`` / ``false`` are accepted; ``1``, ``0``, ``"true"``, ``"yes"``, ``""``
    and ``null`` are not.

    DRF's ``BooleanField`` would NOT do, and the reason is specific rather than
    stylistic: it treats ``1``, ``"1"``, ``"true"``, ``"yes"`` and ``"on"`` as True
    and a matching set as False, so a client that sent a string or a number would
    have the platform's TEST CLASSIFICATION decided by a coercion table. A real
    restaurant misclassified as a test tenant disappears from every revenue figure —
    ``sale_filters``, both dashboards, the transactions report — and a test tenant
    misclassified as real contaminates all of them. That decision has to be made by
    an operator saying so, which means the wire value has to be a boolean.
    """

    default_error_messages = {
        'not_a_boolean': 'Send true or false.',
    }

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail('not_a_boolean')
        return data

    def to_representation(self, value):
        return bool(value)


class StrictUUIDStringField(serializers.Field):
    """
    An account identifier that MUST arrive as a JSON string holding a UUID.

    ``"7f1c…"`` is accepted; ``42``, ``true`` and ``null`` are not.

    A DRF ``UUIDField`` would NOT do: given a JSON number it evaluates
    ``uuid.UUID(int=data)``, so ``42`` becomes a perfectly well-formed UUID no
    account has ever carried. The request would then miss in the domain and come back
    as a 409 saying the account does not exist — an answer about the platform's state
    when the truth is that the body was malformed. The same primitive, and the same
    reasoning, as the subscription-terms concurrency token.
    """

    default_error_messages = {
        'not_a_string': 'Send the owner account id as a UUID string.',
        'invalid': 'Enter a valid UUID.',
    }

    def to_internal_value(self, data):
        # `bool` is not a `str`, so True/False are refused here rather than being
        # reinterpreted as an integer and, from there, as a UUID.
        if not isinstance(data, str):
            self.fail('not_a_string')
        try:
            return uuid.UUID(data.strip())
        except (ValueError, AttributeError, TypeError):
            self.fail('invalid')

    def to_representation(self, value):
        return str(value)


# --- the request body --------------------------------------------------------

class RestaurantFactsSerializer(serializers.Serializer):
    """
    The small set of facts an operator actually supplies about the new tenant.

    THREE FIELDS, AND DELIBERATELY NOT THE MODEL. ``Restaurant`` carries forty-odd
    columns — lifecycle state, ownership, test classification, prepayment, surcharge,
    subscription, tax, branding, availability, approval flags. A ``ModelSerializer``
    here would make every one of them a candidate request field and turn a creation
    endpoint into an untyped edit API for a tenant that does not exist yet. The
    platform-owned facts (``status``, ``owner``, ``created_by``, ``country``,
    ``deleted``) are supplied by the service; everything else keeps its model default,
    because a newborn tenant having no commercial configuration is CORRECT and is
    what a later readiness checklist will report as a blocker.

    ``is_test`` is required and strict — see ``StrictBooleanField``. It is never
    inferred from the name, the location, the environment or the actor.
    """

    name = serializers.CharField(
        required=True, allow_blank=False, max_length=MAX_RESTAURANT_TEXT_LENGTH,
    )
    location = serializers.CharField(
        required=True, allow_blank=False, max_length=MAX_RESTAURANT_TEXT_LENGTH,
    )
    is_test = StrictBooleanField(required=True)


class OwnerSerializer(serializers.Serializer):
    """
    The discriminated owner shape. ``mode`` decides which other fields are legal.

    Every field except ``mode`` is declared optional here and made required — or
    forbidden — by ``validate()``, because DRF cannot express "required in this mode
    only" declaratively. The per-mode rules are stated once, in one place, as data.
    """

    mode = serializers.ChoiceField(choices=OWNER_MODES, required=True)

    # mode=new
    first_name = serializers.CharField(
        required=False, allow_blank=False, max_length=MAX_PERSON_NAME_LENGTH,
    )
    last_name = serializers.CharField(
        required=False, allow_blank=False, max_length=MAX_PERSON_NAME_LENGTH,
    )
    # Canonicalisation is `normalise_msisdn`'s job in the domain — the raw string is
    # passed through rather than half-normalised here, so there is exactly one place
    # that decides what a Ugandan number is.
    phone_number = serializers.CharField(
        required=False, allow_blank=False, max_length=64,
    )
    # OPTIONAL AND NULLABLE. Email is not identity for a restaurant user and is not
    # in `REQUIRED_INFORMATION['new_user']`; `allow_null` lets a client state its
    # absence explicitly rather than having to omit the key.
    email = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        max_length=MAX_EMAIL_LENGTH,
    )

    # mode=existing
    user_id = StrictUUIDStringField(required=False)

    def to_internal_value(self, data):
        """
        Capture the RAW block before DRF converts it.

        A nested serializer has no ``initial_data`` — that attribute exists only on
        the serializer a caller instantiated with ``data=`` — so the keys the client
        actually SENT have to be kept here or they are gone by ``validate``. Which
        keys were sent is not a detail: it is how a forbidden field is told apart
        from an absent one.
        """
        self._sent_keys = frozenset(data) if isinstance(data, dict) else frozenset()
        return super().to_internal_value(data)

    def validate(self, attrs):
        """
        The per-mode rules: what this mode requires, and what it refuses.

        Stated as data rather than as branches so the two modes read as one table,
        and so a future third mode has one place to be added.
        """
        mode = attrs.get('mode')
        if mode == OWNER_MODE_NEW:
            required, forbidden = (
                ('first_name', 'last_name', 'phone_number'),
                EXISTING_ONLY_OWNER_FIELDS,
            )
        else:
            required, forbidden = (
                EXISTING_ONLY_OWNER_FIELDS, NEW_ONLY_OWNER_FIELDS,
            )

        errors = {}
        for field in required:
            if attrs.get(field) in (None, ''):
                errors[field] = [f'This field is required when mode is "{mode}".']
        for field in forbidden:
            # Presence is read off the RAW keys, not ``attrs``: a key the caller sent
            # is a claim they made about the request, and it must be refused even
            # when its value would have been dropped as blank.
            if field in getattr(self, '_sent_keys', frozenset()):
                errors[field] = [f'This field is not accepted when mode is "{mode}".']
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class CreateRestaurantRequestSerializer(ReasonedRequestSerializer):
    """
    The whole creation body: the restaurant facts, the owner, and a stated reason.

    Nested rather than flat, so ``owner.mode`` scopes the owner fields and the two
    ``name`` concepts (a restaurant's and a person's) cannot collide. The reason bar
    is the house one, imported — creating a tenant is at least as consequential as
    suspending one.
    """

    restaurant = RestaurantFactsSerializer(required=True)
    owner = OwnerSerializer(required=True)

    _FIELD_ORDER = ('restaurant', 'owner', 'reason')


def owner_spec(validated_owner):
    """
    The validated owner block as the domain's discriminated union.

    The mode word exists only in the HTTP contract; the service takes a type, so the
    hybrid this endpoint refuses is not merely rejected — it is unrepresentable one
    layer down.
    """
    if validated_owner['mode'] == OWNER_MODE_NEW:
        return NewOwner(
            first_name=validated_owner['first_name'],
            last_name=validated_owner['last_name'],
            phone_number=validated_owner['phone_number'],
            email=validated_owner.get('email'),
        )
    return ExistingOwner(user_id=validated_owner['user_id'])


# --- domain outcomes ---------------------------------------------------------

# {domain error code: HTTP status}. Split exactly the way the domain splits its own
# codes, and nothing is missing by accident: a code absent from this map is
# deliberately re-raised, so an unexpected internal condition surfaces as a 500 and
# rolls the transaction back rather than being relabelled a tidy client error.
#
# 400 = the request describes something malformed. 409 = the request is well-formed
# and the platform's current state contradicts it, so the fix is to look and decide
# again rather than to edit the body.
STATUS_BY_CODE = {
    **{code: 400 for code in onboarding_creation.INVALID_REQUEST_CODES},
    **{code: 409 for code in onboarding_creation.CONFLICT_CODES},
}

# Request-body paths a domain 400 may be attributed to, so an operator's client can
# highlight the field. Whitelisted rather than derived, so a future error code cannot
# silently become a response key.
#
# Dotted paths, expanded into a NESTED errors dict by ``error_body``. That nesting is
# not cosmetic: a serializer failure on the same field already answers
# ``{"owner": {"phone_number": [...]}}``, and a domain failure answering
# ``{"owner.phone_number": [...]}`` would give one field two error shapes depending
# on which layer refused it — a distinction the client has no way to care about and
# every reason to trip on.
ERROR_FIELD_BY_CODE = {
    onboarding_creation.INVALID_RESTAURANT_NAME: ('restaurant', 'name'),
    onboarding_creation.INVALID_RESTAURANT_LOCATION: ('restaurant', 'location'),
    onboarding_creation.INVALID_TEST_CLASSIFICATION: ('restaurant', 'is_test'),
    onboarding_creation.INVALID_REASON: ('reason',),
    onboarding_creation.INVALID_OWNER_USER_ID: ('owner', 'user_id'),
    onboarding_creation.INVALID_OWNER_NAME: ('owner', 'first_name'),
    onboarding_creation.INVALID_OWNER_PHONE: ('owner', 'phone_number'),
    onboarding_creation.INVALID_OWNER_EMAIL: ('owner', 'email'),
}


def _nested_error(path, message):
    """``('owner', 'phone_number')`` + a message -> ``{'owner': {'phone_number': [msg]}}``."""
    errors = [message]
    for key in reversed(path):
        errors = {key: errors}
    return errors

# Fixed operator-facing sentences for CONFLICTS, in the house style: a conflict
# message says what the platform believes and what to do about it, and never names
# internals. The two owner-account conflicts carry the domain's own message because
# it IS the remedy ("review it and choose the existing-account path"); the rest are
# stated here so a domain message change cannot silently rewrite the API.
CONFLICT_MESSAGES = {
    onboarding_creation.RESTAURANT_ALREADY_EXISTS:
        'A restaurant with that name and location already exists.',
    onboarding_creation.OWNER_ACCOUNT_NOT_FOUND:
        'No account with that id. Check the owner account id and try again.',
}


def error_body(exc, status):
    """
    The response body for a refused creation.

    A 400 is about the caller's own input, so the domain's explanation is passed
    through and attributed to the field it names. A 409 is about the platform, so the
    body carries a code the client can branch on, a sentence, and — for the two owner
    conflicts — the UUID of the account involved, which is the ONE thing that lets an
    operator act on it (open that account, then deliberately request
    ``mode: "existing"``).

    ``details`` carries UUIDs only. It never carries the existing account's name,
    phone or email: the caller already supplied the phone, an identifier is enough to
    look the account up with the directory reads they already have, and this endpoint
    must not become a user-directory search API. The EMAIL conflict deliberately
    names no account at all — pointing at one would invite exactly the "email
    identifies the owner" inference the contract refuses.
    """
    if status == 409:
        body = {
            'status': status,
            'message': CONFLICT_MESSAGES.get(exc.code, exc.message),
            'code': exc.code,
        }
        if exc.details:
            body['details'] = exc.details
        return body
    path = ERROR_FIELD_BY_CODE.get(exc.code, ('__all__',))
    return {
        'status': status,
        'message': 'The restaurant could not be created.',
        'code': exc.code,
        'errors': _nested_error(path, exc.message),
    }


# --- what the decision recorded, and what the caller is handed ---------------

def after_state(result):
    """
    The narrow platform facts this decision established, for the audit row.

    NO OWNER PII — no name, phone or email; the owner's UUID is the identity and is
    all an investigator needs. NO CREDENTIAL — not the raw claim token and not its
    hash, because an audit log is read by more people, for longer, than a credential
    should be reachable by, and a hash in the log would be a standing invitation to
    treat it as one. (``audit.redact`` would scrub a key containing "token" anyway;
    this does not rely on that.)

    There is no ``before_state``: the restaurant did not exist. Recording ``{}`` or a
    row of nulls would imply a prior state that can be compared against.
    """
    invitation = result.invitation
    return {
        'restaurant_status': result.restaurant.status,
        'is_test': result.restaurant.is_test,
        'admin_onboarding_source': result.onboarding.source,
        'owner_user_id': str(result.owner.id),
        'owner_account_created': result.owner_created,
        'owner_invitation_id': str(invitation.id),
        'owner_invitation_expires_at': invitation.expires_at.isoformat(),
    }


def success_body(result, restaurant_detail):
    """
    The 201 payload: the canonical restaurant projection, the owner, the credential.

    ``restaurant`` is the SAME projection ``GET admin/v1/restaurants/<id>/`` returns —
    not a second "created restaurant" shape — so the portal can navigate straight to
    the workspace and see exactly what it was just told, and so the onboarding state
    the client reads (``tracked``, ``admin_created``, ``consistent``,
    ``not_established``, ``pending``) is derived by the existing evidence rules from
    the rows that were actually written, never asserted by this endpoint.

    ``claim_token`` IS THE RAW CREDENTIAL AND THIS IS THE ONLY TIME IT IS EVER
    RETURNED. Only its SHA-256 hash is persisted, so a lost response is unrecoverable
    by design: the remedy is a future reissue that supersedes the unresolved
    invitation and mints a fresh token, never recoverable plaintext storage and never
    handing back the stored hash as though it were a credential.

    There is deliberately NO claim URL. A token is a real credential; a URL is a
    product promise, and the customer-plane redemption route does not exist yet.
    """
    invitation = result.invitation
    return {
        'status': 201,
        'message': 'Restaurant created.',
        'data': {
            'restaurant': restaurant_detail,
            'owner_account': {
                'id': str(result.owner.id),
                # Stated by the operation, never re-derived. A brand-new account and
                # a long-standing one are indistinguishable a moment later.
                'created': result.owner_created,
            },
            'owner_invitation': {
                'id': str(invitation.id),
                'issued_at': invitation.issued_at.isoformat(),
                'expires_at': invitation.expires_at.isoformat(),
                'claim_token': result.claim_token,
            },
        },
    }
