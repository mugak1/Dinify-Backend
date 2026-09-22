"""The D01 request ceilings, as a contract a client repository can agree with.

THE PROBLEM THIS EXISTS TO CLOSE. The ceilings are enforced here and mirrored in
Dinify-Frontend so a diner's basket can refuse an over-ceiling order before the
round trip instead of after it. Until now each side asserted its own copy against
its own constants, and the cross-repository assertion on this side
(``CrossRepositoryCeilingContractTests.test_the_frontend_contract_file_matches``)
``skipTest``s whenever Dinify-Frontend is not checked out beside this repository —
which, in CI, is always. Two independently checked copies are not parity: they are
two things that each agree with themselves.

WHAT CLOSES IT. One authority — this module, derived from ``order_input`` — plus a
committed export (``checkout_limits.contract.json``) that a test asserts against it
UNCONDITIONALLY, plus a DIGEST both languages can compute byte for byte. The release
gate in Dinify-Frontend records its own copy's digest in the release manifest and
refuses to publish a build whose digest disagrees with the backend peer pinned in
its compatible set. A ceiling changed on one side and not the other then cannot be
released, whichever side moved.

THE CANONICAL FORM IS THE CROSS-LANGUAGE CONTRACT: keys sorted, no insignificant
whitespace, integers only —

    json.dumps(values, sort_keys=True, separators=(',', ':'))

which is exactly what ``release/lib/canonical.mjs`` produces in the other
repository. Keys beginning ``_`` are provenance notes for a human reader and are
excluded, so the two copies may annotate themselves differently and still agree.

NOTHING HERE ENFORCES ANYTHING. The ceilings are enforced by ``order_input``; this
module only states them in a form somebody else can check against.
"""
import hashlib
import json
import pathlib

from orders_app.controllers.services import order_input

#: The committed export. Repository-relative, beside this module.
CONTRACT_FILE = pathlib.Path(__file__).with_name('checkout_limits.contract.json')

#: Repository-relative path of the client's copy, for the cross-repository test that
#: runs only when Dinify-Frontend happens to be checked out beside this one.
CLIENT_CONTRACT_PATH = 'src/app/_shared/order/checkout-limits.contract.json'

#: Ceilings a BASKET can act on. These are published to the client.
PUBLISHED_NAMES = (
    'MAX_QUANTITY_PER_LINE',
    'MAX_LINES_PER_ORDER',
    'MAX_TOTAL_UNITS',
    'MAX_MODIFIER_GROUPS_PER_LINE',
    'MAX_CHOICES_PER_GROUP',
    'MAX_EXTRAS_PER_LINE',
    'MAX_SELECTION_ENTRIES_PER_REQUEST',
)

#: Bounds this module holds that a BASKET cannot act on, with the reason. Publishing
#: these would tell the diner app to enforce something it can neither cause nor cure,
#: which is worse than not publishing them.
NOT_CLIENT_ACTIONABLE = frozenset({
    # The catalogue mints modifier ids; a basket only ever echoes them back. An
    # over-long stored id makes the ITEM unorderable however small the basket is, so
    # there is nothing for a diner to reduce. That condition is reported by
    # ``manage.py check_order_input_compatibility``, against the catalogue, where it
    # can actually be fixed.
    'MAX_MODIFIER_ID_LENGTH',
    # How many problems ONE refusal enumerates. A reporting bound on the response,
    # not a limit on what may be submitted.
    'MAX_REPORTED_ERRORS',
})


def enforced_ceiling_names():
    """Every ``MAX_*`` integer ``order_input`` actually holds, discovered not listed.

    Discovery rather than a literal list is what makes adding a ceiling force a
    decision — publish it, or record why a basket cannot act on it — instead of it
    reaching a diner as an unexplained refusal.
    """
    return {
        name for name in vars(order_input)
        if name.startswith('MAX_') and isinstance(getattr(order_input, name), int)
    }


def published_values():
    """The published ceilings, read from the constants that enforce them.

    No number in this contract is typed twice on this side.
    """
    return {name: getattr(order_input, name) for name in PUBLISHED_NAMES}


def canonical_json(values=None):
    """The exact text both repositories digest. See the module docstring."""
    return json.dumps(
        published_values() if values is None else values,
        sort_keys=True,
        separators=(',', ':'),
    )


def contract_digest(values=None):
    """``sha256:<hex>`` over the canonical text.

    The prefix is part of the value: a bare hex string says nothing about which
    algorithm produced it, and a future algorithm change must be visible in every
    record that already exists.
    """
    return 'sha256:' + hashlib.sha256(canonical_json(values).encode('utf-8')).hexdigest()


def committed_export():
    """The committed JSON, with provenance notes stripped."""
    published = json.loads(CONTRACT_FILE.read_text())
    return {k: v for k, v in published.items() if not k.startswith('_')}


def export_text():
    """The exact bytes ``checkout_limits.contract.json`` should contain.

    Indented and trailing-newline'd for a human reader; the DIGEST is taken over the
    canonical form above, never over these bytes, so formatting is free to differ
    between the two repositories' copies.
    """
    body = {
        '_source': 'orders_app/controllers/services/order_input.py',
        '_note': (
            'Source-authoritative export of the D01 request ceilings. Generated by '
            '`manage.py export_checkout_limits_contract`; asserted against the live '
            'constants by orders_app/tests_order_input.py, unconditionally.'
        ),
        **published_values(),
    }
    return json.dumps(body, indent=2) + '\n'
