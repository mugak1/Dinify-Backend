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
PRs wire up. This PR only classifies; it changes no behaviour.
"""
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
    validator actually enforces it is proven by the two-tenant behavioural tests.
    """

    path: str

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
