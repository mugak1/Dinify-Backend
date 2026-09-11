"""
D01 — the ONE contract for customer-supplied order input.

Pure, deterministic and DATABASE-FREE. It answers exactly one question — *is this
request structurally a well-formed order?* — and nothing about the catalogue, the
tenant, publication, stock or price. Those remain the job of
``menu_publication.validate_order_selections`` and the in-transaction re-check,
which run AFTER this and are the only checks entitled to read the database.

WHY A PLAIN MODULE RATHER THAN A DRF SERIALIZER. Not because DRF cannot express a
strict rule — a custom ``to_internal_value`` can enforce raw-type checks perfectly
well. The reasons are that this boundary is reached from an HTTP adapter AND from
two service entrypoints that have no request object, that the order path pins EXACT
query counts (``orders_app/tests_order_path_queries.py``) so validation must issue
zero queries, and that the surrounding order code already speaks in
``{'status', 'message'}`` dicts. What IS rejected is relying on DRF's STOCK
coercion: ``IntegerField.to_internal_value`` evaluates ``int(re_decimal.sub('',
str(data)))``, so it silently converts ``"3"`` to 3 and ``2.0`` to 2, and a
``min_value``/range validator sits downstream of that conversion and never sees the
original type. A quantity rule built on it would admit exactly the values this
module exists to refuse.

WHAT IS NORMALISED, AND WHY THAT IS ALL. ``item`` and each ``extras`` member are
returned as CANONICAL LOWERCASE UUID STRINGS — strings, never ``uuid.UUID``
objects, so re-validating this module's own output is stable rather than a
type error at the next boundary. Everything else is returned exactly as submitted:
modifier group and choice identifiers are OPAQUE STRINGS whose identity belongs to
the operator's catalogue, so they are length- and type-checked but never trimmed,
case-folded or parsed as UUIDs (the shipped operator UI mints UUIDs for them, but
the column is unvalidated JSON and the repository's own fixtures use ``'g-req'`` /
``'c1'``). Keys absent from the submitted line stay absent, so the supported
omitted/null forms survive untouched.

THE VALIDATOR NEVER MUTATES ITS INPUT. Every returned line is a new dict.

LIMITS ARE APPLICATION SAFETY CEILINGS — new in D01, not prior Dinify policy, not
payment-provider limits, and not a statement about stock, menu size or what a
restaurant may sell. They exist because one request drives per-line database work
inside a transaction that holds the table row and the shared admission advisory
lock. Collection ceilings are applied to RAW submitted entries, before
de-duplication and before any per-entry work.

This is POST-PARSE APPLICATION validation. It does not replace an HTTP body-size
limit, edge rate limiting, or any other denial-of-service control, and it makes no
claim about them.
"""
import uuid

from dinify_backend.configss.messages import MESSAGES
from restaurants_app.controllers.menu_publication import NOT_ON_MENU_MESSAGE

# --- request ceilings (defined once; see the module docstring) ----------------

#: Largest quantity accepted on ONE submitted order line. This is an INPUT
#: ceiling, NOT a database or merged-row maximum: several valid lines for the
#: same item legitimately merge above it, bounded only by MAX_TOTAL_UNITS.
MAX_QUANTITY_PER_LINE = 99
#: Largest number of submitted parent lines in one order.
MAX_LINES_PER_ORDER = 100
#: Largest total of all submitted line quantities in one order.
MAX_TOTAL_UNITS = 500
#: Largest number of modifier GROUPS a single submitted line may carry.
MAX_MODIFIER_GROUPS_PER_LINE = 32
#: Largest number of RAW choice entries in one submitted group (pre-de-dup).
MAX_CHOICES_PER_GROUP = 64
#: Largest number of RAW extra entries on one submitted line (pre-de-dup).
MAX_EXTRAS_PER_LINE = 64
#: Largest accepted length of an opaque modifier group/choice identifier.
MAX_MODIFIER_ID_LENGTH = 128
#: Largest total of RAW choice + extra entries across the whole request.
MAX_SELECTION_ENTRIES_PER_REQUEST = 2048

#: Hard cap on how many field errors one response may carry. Validation output is
#: bounded and NEVER echoes a submitted value.
MAX_REPORTED_ERRORS = 10

# --- public messages ---------------------------------------------------------

INVALID_REQUEST_MESSAGE = 'The order request is not valid. Please try again.'
#: An id-shaped failure keeps the ESTABLISHED opaque menu refusal, imported
#: rather than restated so the two can never drift. This is load-bearing, not
#: cosmetic: every unorderable id — foreign, nonexistent, malformed, unpublished
#: — has always collapsed to ONE message precisely so a response never reveals
#: whether an id exists at another tenant. Answering a malformed id differently
#: here would have introduced exactly the distinction that contract removes.
#: (Well-formed ids are untouched by this module and still reach the catalogue
#: gate, so "foreign" and "nonexistent" remain indistinguishable from each
#: other as before.)
MENU_ID_MESSAGE = NOT_ON_MENU_MESSAGE
QUANTITY_MESSAGE = (
    f'Each item needs a whole quantity between 1 and {MAX_QUANTITY_PER_LINE}.'
)
TOO_MANY_LINES_MESSAGE = (
    f'An order can carry at most {MAX_LINES_PER_ORDER} items. '
    'Please place the rest as a second order.'
)
TOO_MANY_UNITS_MESSAGE = (
    f'An order can carry at most {MAX_TOTAL_UNITS} units in total. '
    'Please place the rest as a second order.'
)
TOO_MANY_SELECTIONS_MESSAGE = (
    'That order carries too many options and extras. Please simplify it.'
)
#: Sourced from the shared catalogue rather than restated, so the wording an
#: empty basket has always produced cannot drift away from this module.
NO_ITEMS_MESSAGE = MESSAGES['NO_ORDER_ITEMS']

# --- reason codes (stable, safe to log; never contain submitted values) -------

QUANTITY_MISSING = 'quantity_missing'
QUANTITY_NOT_AN_INTEGER = 'quantity_not_an_integer'
QUANTITY_NOT_POSITIVE = 'quantity_not_positive'
QUANTITY_ABOVE_LIMIT = 'quantity_above_limit'


def _is_strict_int(value):
    """True only for a real ``int``. ``bool`` is a subclass of ``int`` and is
    NOT one for our purposes — ``True`` must never be read as a quantity of 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def quantity_error(value):
    """
    THE quantity rule, in one place.

    Returns a stable reason code, or ``None`` when ``value`` is an acceptable
    submitted quantity: a real positive ``int`` no greater than
    ``MAX_QUANTITY_PER_LINE``. Missing/``None``, ``bool``, ``float`` (``2.0``
    included — a JSON floating representation is refused rather than coerced),
    numeric strings, containers and out-of-range values all yield a code.

    Every customer-facing quantity boundary calls THIS — the request validator,
    the order-item chokepoint and the merge helper — so none of them can drift.
    """
    if value is None:
        return QUANTITY_MISSING
    if not _is_strict_int(value):
        return QUANTITY_NOT_AN_INTEGER
    if value < 1:
        return QUANTITY_NOT_POSITIVE
    if value > MAX_QUANTITY_PER_LINE:
        return QUANTITY_ABOVE_LIMIT
    return None


def canonical_uuid(value):
    """
    Canonical lowercase-hyphenated UUID string, or ``None`` when ``value`` is not
    a UUID supplied as a string.

    STRING INPUT ONLY, deliberately. ``uuid.UUID(int=...)`` happily turns the
    integer 5 into a well-formed identifier no row has ever carried; accepting
    that would let a malformed request miss in the catalogue and come back as a
    confusing rejection about the menu. Its own output re-canonicalises to
    itself, which is what makes re-validation stable.
    """
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _reject(message, errors):
    """Bounded 400 envelope. ``message`` stays a usable top-level string for
    clients that read only that; ``errors`` is additive and capped."""
    trimmed = dict(list(errors.items())[:MAX_REPORTED_ERRORS])
    return {'status': 400, 'message': message, 'errors': trimmed}


def is_submittable_identifier(value):
    """Can a client actually put this identifier in a request?

    Exposed so the catalogue inspector can be handed THE request contract
    instead of restating it. A stored id that fails this is unreachable: no
    well-formed request can name it, so a group or choice that REQUIRES one
    cannot be ordered at all — which is a compatibility fact the preflight has
    to be able to see.
    """
    return _opaque_id_error(value) is None


def _opaque_id_error(value):
    """Opaque catalogue identifier rule: a non-empty string within the length
    ceiling. Identity is preserved exactly — no trim, no case fold, no UUID
    parse."""
    if not isinstance(value, str):
        return 'not_a_string'
    if not value:
        return 'empty'
    if len(value) > MAX_MODIFIER_ID_LENGTH:
        return 'too_long'
    return None


def validate_order_items(items):
    """
    Validate the submitted item collection.

    Returns ``{'status': 200, 'items': [<new dicts>]}`` or a bounded 400 envelope.
    Pure: no database access, no mutation of ``items`` or of any line in it.
    """
    if items is None or not isinstance(items, list):
        return _reject(NO_ITEMS_MESSAGE, {'items': ['A list of items is required.']})
    if not items:
        return _reject(NO_ITEMS_MESSAGE, {'items': ['At least one item is required.']})
    if len(items) > MAX_LINES_PER_ORDER:
        return _reject(TOO_MANY_LINES_MESSAGE, {
            'items': [f'At most {MAX_LINES_PER_ORDER} items are allowed.'],
        })

    errors = {}
    #: Deterministic message priority: a quantity problem is the most
    #: actionable and discloses nothing, so it wins; otherwise an id-shaped
    #: problem takes the opaque menu refusal; otherwise the generic one.
    saw_quantity_error = False
    saw_menu_id_error = False
    validated = []
    total_units = 0
    total_selection_entries = 0

    for index, entry in enumerate(items):
        prefix = f'items.{index}'
        if not isinstance(entry, dict):
            errors[prefix] = ['Each item must be an object.']
            continue

        line = {}

        # -- item id -------------------------------------------------------
        item_id = canonical_uuid(entry.get('item'))
        if item_id is None:
            errors[f'{prefix}.item'] = ['A valid item id is required.']
            saw_menu_id_error = True
        else:
            line['item'] = item_id

        # -- quantity ------------------------------------------------------
        code = quantity_error(entry.get('quantity'))
        if code is not None:
            errors[f'{prefix}.quantity'] = [QUANTITY_MESSAGE]
            saw_quantity_error = True
        else:
            line['quantity'] = entry['quantity']
            total_units += entry['quantity']

        # -- selected_modifiers (absent / None preserved exactly) ----------
        if 'selected_modifiers' in entry:
            raw = entry['selected_modifiers']
            if raw is None:
                line['selected_modifiers'] = None
            elif not isinstance(raw, dict):
                errors[f'{prefix}.selected_modifiers'] = [
                    'Modifier selections must be an object.',
                ]
            elif len(raw) > MAX_MODIFIER_GROUPS_PER_LINE:
                errors[f'{prefix}.selected_modifiers'] = [
                    f'At most {MAX_MODIFIER_GROUPS_PER_LINE} option groups '
                    'are allowed.',
                ]
            else:
                groups = {}
                for group_id, choices in raw.items():
                    if _opaque_id_error(group_id) is not None:
                        errors[f'{prefix}.selected_modifiers'] = [
                            'An option group id is not valid.',
                        ]
                        break
                    if not isinstance(choices, list):
                        errors[f'{prefix}.selected_modifiers.{group_id}'] = [
                            'Option choices must be a list.',
                        ]
                        break
                    # Ceilings apply to RAW entries, before de-duplication and
                    # before any per-member work.
                    if len(choices) > MAX_CHOICES_PER_GROUP:
                        errors[f'{prefix}.selected_modifiers.{group_id}'] = [
                            f'At most {MAX_CHOICES_PER_GROUP} choices are '
                            'allowed in one option group.',
                        ]
                        break
                    total_selection_entries += len(choices)
                    bad_member = any(
                        _opaque_id_error(choice) is not None for choice in choices
                    )
                    if bad_member:
                        errors[f'{prefix}.selected_modifiers.{group_id}'] = [
                            'An option choice id is not valid.',
                        ]
                        break
                    groups[group_id] = list(choices)
                else:
                    line['selected_modifiers'] = groups

        # -- extras (absent / None preserved exactly) ----------------------
        if 'extras' in entry:
            raw_extras = entry['extras']
            if raw_extras is None:
                line['extras'] = None
            elif not isinstance(raw_extras, list):
                errors[f'{prefix}.extras'] = ['Extras must be a list.']
                saw_menu_id_error = True
            elif len(raw_extras) > MAX_EXTRAS_PER_LINE:
                errors[f'{prefix}.extras'] = [
                    f'At most {MAX_EXTRAS_PER_LINE} extras are allowed on '
                    'one item.',
                ]
            else:
                total_selection_entries += len(raw_extras)
                canonical_extras = [canonical_uuid(member) for member in raw_extras]
                if any(member is None for member in canonical_extras):
                    errors[f'{prefix}.extras'] = ['A valid extra id is required.']
                    saw_menu_id_error = True
                else:
                    line['extras'] = canonical_extras

        # A validated line carries EXACTLY the contract fields and nothing else.
        # Public request parsing stays distinct from the trusted internal
        # representation: an unrecognised key is dropped here rather than
        # forwarded, so no downstream reader can ever grow a dependency on raw
        # client input that never passed through this rule. (Nothing reads one
        # today — `add_order_item` builds its persisted `item_data` entirely from
        # server-resolved values — so this drops nothing that is used.)
        validated.append(line)
        if len(errors) >= MAX_REPORTED_ERRORS:
            break

    if errors:
        if saw_quantity_error:
            message = QUANTITY_MESSAGE
        elif saw_menu_id_error:
            message = MENU_ID_MESSAGE
        else:
            message = INVALID_REQUEST_MESSAGE
        return _reject(message, errors)
    if total_units > MAX_TOTAL_UNITS:
        return _reject(TOO_MANY_UNITS_MESSAGE, {
            'items': [f'At most {MAX_TOTAL_UNITS} units are allowed in total.'],
        })
    if total_selection_entries > MAX_SELECTION_ENTRIES_PER_REQUEST:
        return _reject(TOO_MANY_SELECTIONS_MESSAGE, {
            'items': [
                f'At most {MAX_SELECTION_ENTRIES_PER_REQUEST} option and extra '
                'entries are allowed in total.',
            ],
        })
    return {'status': 200, 'items': validated}


def validate_order_request(payload):
    """
    Validate the OUTER request body before any unsafe mapping access.

    Returns ``{'status': 200, 'items': [...], 'client_order_id': str | None}`` or
    a bounded 400 envelope. ``client_order_id`` keeps its supported optional form:
    absent and ``null`` both yield ``None``; anything present must be a UUID
    STRING (an integer is refused rather than coerced by ``uuid.UUID(int=...)``).
    Making idempotency mandatory is out of scope.
    """
    if not isinstance(payload, dict):
        return _reject(INVALID_REQUEST_MESSAGE, {
            '__all__': ['The request body must be an object.'],
        })

    client_order_id = payload.get('client_order_id')
    if client_order_id is not None:
        client_order_id = canonical_uuid(client_order_id)
        if client_order_id is None:
            return _reject(INVALID_REQUEST_MESSAGE, {
                'client_order_id': ['A valid client_order_id is required.'],
            })

    result = validate_order_items(payload.get('items'))
    if result.get('status') != 200:
        return result
    return {
        'status': 200,
        'items': result['items'],
        'client_order_id': client_order_id,
    }
