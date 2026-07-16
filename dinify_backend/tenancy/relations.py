"""
Tenant-relation classifications (TENANT-STRUCT-00).

Three constrained types that declare, beside a serializer, how each writable
relational field relates to the caller's restaurant. They exist so a writable
relation cannot be left as an unstated default: a bare ``True`` / ``"validated"``
is impossible to express — you must pick one of these, and for the non-trivial
ones justify it.

Declared on a serializer's ``Meta``::

    class Meta:
        model = Reservation
        fields = '__all__'
        tenant_relations = {
            'restaurant': SameTenant('id'),
            'table':      SameTenant('restaurant_id'),
            'server':     SameTenant('restaurant_id'),
            'created_by': ServerDerived(),
            'customer':   GlobalRelation(reason='Users are platform-global identities'),
        }

This module is imported ONLY by the tenancy meta-test / baseline tooling — never
by a request path. It adds NO runtime validation on its own; enforcing a
``SameTenant`` at request time is the job of a serializer ``validate()`` (see
``restaurants_app/controllers/tenant_scope.py``), which the per-domain migration
PRs wire up. This module only classifies; it changes no behaviour.

RUNTIME ASSURANCE: because the classification is inert, ``SameTenant(path)`` on its
own proves NOTHING — the path is only typo-checked. To stop a classification from
LOOKING like a guarantee it is not, every PRODUCTION ``SameTenant`` must declare
``verified_by`` — a dotted reference (or tuple) to the two-tenant behavioural
test(s) that actually exercise the runtime enforcement. The meta-test verifies the
reference resolves (``resolve_test_ref``); it does not (and cannot) re-run the
proof, but it makes the proof's existence a reviewable, machine-checked link.
"""
import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SameTenant:
    """
    The relation MUST belong to the caller's restaurant.

    ``path`` is the ORM lookup FROM the related model TO its restaurant id, e.g.
    ``"restaurant_id"`` (the related model has a direct ``restaurant`` FK) or
    ``"section__restaurant_id"`` (reached via an intermediate FK). It is a
    typo-checkable declaration, NOT a proof of correctness — the meta-test only
    verifies the path RESOLVES against the related model; whether a request-time
    validator actually enforces it is proven by the two-tenant behavioural tests
    named in ``verified_by``.

    ``verified_by`` is a dotted reference (or tuple of them) to the two-tenant
    behavioural test that proves this SameTenant is enforced at runtime, e.g.
    ``"restaurants_app.tests.MenuFkTenantBoundaryTests"``. It is REQUIRED for every
    production classification (the meta-test asserts it resolves) — the path alone
    is inert. It defaults to ``None`` so tooling fixtures need not set it; the
    requirement bites only on serializers reached by real discovery.
    """

    path: str
    verified_by: object = None

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError(
                "SameTenant requires a non-empty ORM path from the related model "
                "to its restaurant id (e.g. 'restaurant_id' or "
                "'section__restaurant_id'). A bare True/'validated' is not allowed."
            )


@dataclass(frozen=True)
class ServerDerived:
    """
    The relation is set by the server and is never client-writable — e.g. the
    audit ``created_by`` / ``deleted_by`` FKs. A migration PR that classifies a
    field ``ServerDerived()`` is asserting the serializer must not accept it from
    the request body (typically by marking it ``read_only`` or popping it).
    """


@dataclass(frozen=True)
class GlobalRelation:
    """
    The relation is legitimately cross-tenant or platform-global — e.g. a FK to
    ``User`` (identities are shared across restaurants) or a Django auth M2M. It
    is deliberately NOT restaurant-scoped. ``reason`` is REQUIRED and non-empty:
    a global relation is the dangerous classification (it opts a field OUT of
    tenant scoping), so every use must carry an explicit, reviewable justification.
    """

    reason: str

    def __post_init__(self):
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError(
                "GlobalRelation requires a non-empty reason justifying why this "
                "relation is legitimately cross-tenant / platform-global."
            )


# The only value types permitted in a ``Meta.tenant_relations`` mapping.
CLASSIFICATION_TYPES = (SameTenant, ServerDerived, GlobalRelation)


def is_classification(value) -> bool:
    """True iff ``value`` is one of the constrained classification instances."""
    return isinstance(value, CLASSIFICATION_TYPES)


def resolve_test_ref(ref) -> bool:
    """
    True iff ``ref`` — a dotted path like ``"pkg.module.TestClass"`` or
    ``"pkg.module.TestClass.test_method"`` — resolves to a real, importable object.

    Import the longest importable module prefix, then ``getattr`` the remaining
    segments (class, then optional method). Proves the linked behavioural test
    EXISTS; it does not run it.
    """
    if not isinstance(ref, str) or not ref.strip():
        return False
    parts = ref.strip().split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:split])
        try:
            obj = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - not a module prefix; try a shorter one
            continue
        try:
            for attr in parts[split:]:
                obj = getattr(obj, attr)
            return True
        except AttributeError:
            return False
    return False


def same_tenant_assurance_violations(classified):
    """
    Human-readable violations for the runtime-assurance policy. ``classified`` is an
    iterable of ``(key, classification)``. Every ``SameTenant`` must declare a
    ``verified_by`` that resolves to an importable behavioural test — else the
    classification is asserting a guarantee nothing proves. Pure logic (testable
    with fixtures); the meta-test feeds it the production classifications.
    """
    violations = []
    for key, classification in classified:
        if not isinstance(classification, SameTenant):
            continue
        refs = classification.verified_by
        if not refs:
            violations.append(
                f"{key}: SameTenant declares no verified_by. Link the two-tenant "
                f"behavioural test(s) that prove runtime enforcement — the path "
                f"alone proves nothing."
            )
            continue
        refs = [refs] if isinstance(refs, str) else list(refs)
        unresolved = [r for r in refs if not resolve_test_ref(r)]
        if unresolved:
            violations.append(
                f"{key}: SameTenant.verified_by does not resolve to an importable "
                f"test: {unresolved}."
            )
    return violations
