"""
``fields='__all__'`` policy for project serializers (TENANT-STRUCT-00).

WHY this matters: a WRITE serializer built with ``fields='__all__'`` silently
re-exposes EVERY current and FUTURE model field — including tenant FKs — as
client-writable, so a model field added later (a new FK, a ``restaurant`` column)
becomes a write surface with no code change and no review of the serializer. The
FK meta-test still forces each *relation* to be classified/baselined, but ``__all__``
is the mechanism by which new writable relations appear in the first place.

POLICY (fail-closed): every project serializer that exposes ``__all__`` (or
``Meta.exclude``, which also auto-expands) must be an EXPLICIT, review-visible
exception listed below. A NEW ``__all__`` serializer that is not listed fails the
meta-test — the author must give it an explicit field list (required for write
serializers), or, if it is genuinely read/archival, add it here with a justification.

We do NOT blindly ban ``__all__`` everywhere: read and archival serializers only
serialize OUT (no client-driven ORM write), so ``__all__`` does not widen a write
surface there. The two sets below encode that classification.
"""

# Production WRITE serializers that still expose ``fields='__all__'``. This is
# LEGACY DEBT: each should migrate to an explicit field list, because ``__all__``
# on a write serializer auto-exposes future model fields (incl. tenant FKs) to
# client writes. The nested-FK tenant boundary is currently re-imposed at runtime
# in each one's ``validate()`` (see restaurants_app/controllers/tenant_scope.py),
# NOT by the field list. This set may ONLY SHRINK — a NEW write serializer must use
# an explicit field list and must never be added here.
WRITE_ALL_FIELDS_DEBT = frozenset({
    "orders_app.serializers.SerializerPutOrder",  # NOTE: unused in prod (create_order bypasses it)
    "orders_app.serializers.SerializerPutOrderItem",
    "restaurants_app.serializers.SerializerPutDiningArea",
    "restaurants_app.serializers.SerializerPutMenuItem",
    "restaurants_app.serializers.SerializerPutMenuSection",
    "restaurants_app.serializers.SerializerPutReservation",
    "restaurants_app.serializers.SerializerPutRestaurant",
    "restaurants_app.serializers.SerializerPutRestaurantEmployee",
    "restaurants_app.serializers.SerializerPutSectionGroup",
    "restaurants_app.serializers.SerializerPutTable",
    "restaurants_app.serializers.SerializerPutWaitlistEntry",
    "users_app.serializers.SerPutUserProfile",  # __all__ over the platform-global User model
})

# READ / ARCHIVAL serializers that expose ``fields='__all__'``. ALLOWED: they only
# serialize OUT — never ``.save()`` from client input — so ``__all__`` does not
# create a write surface. Read serializers back GET responses; ``SerArc*`` archival
# serializers are driven by post_save signals to snapshot a row to MongoDB.
READ_ARCHIVAL_ALL_FIELDS_ALLOWED = frozenset({
    # read (output-only)
    "restaurants_app.serializers.SerializerGetRestaurantDetail",
    "restaurants_app.serializers.SerializerPublicGetTable",
    # archival (serialize-out to Mongo via archive_record)
    "restaurants_app.models.SerArcRestaurant",
    "restaurants_app.models.SerArcMenuSection",
    "restaurants_app.models.SerArcSectionGroup",
    "restaurants_app.models.SerArcMenuItem",
    "restaurants_app.models.SerArcDiningArea",
    "restaurants_app.models.SerArcTable",
    "restaurants_app.models.SerArcRestaurantEmployee",
    "restaurants_app.models.SerArcUpsellConfig",
    "restaurants_app.models.SerArcUpsellItem",
    "restaurants_app.models.SerArcReservation",
    "restaurants_app.models.SerArcWaitlistEntry",
    "users_app.models.SerArcUser",
})

# The union: every serializer allowed to use ``__all__`` today. Anything else that
# uses ``__all__`` is a NEW, unapproved exception and fails the meta-test.
ALL_FIELDS_ALLOWED = WRITE_ALL_FIELDS_DEBT | READ_ARCHIVAL_ALL_FIELDS_ALLOWED


def uses_all_fields(cls) -> bool:
    """
    True iff ``cls`` exposes every model field automatically — ``Meta.fields ==
    '__all__'`` (the paren form ``('__all__')`` is just that string) or a non-empty
    ``Meta.exclude`` (which also auto-includes every non-excluded field, present and
    future). Both widen the field set without an explicit, reviewed list.
    """
    meta = getattr(cls, "Meta", None)
    if meta is None:
        return False
    if getattr(meta, "fields", None) == "__all__":
        return True
    if getattr(meta, "exclude", None):
        return True
    return False


def all_fields_violations(all_fields_keys, allowed_keys=ALL_FIELDS_ALLOWED):
    """
    Human-readable violations: serializer keys that use ``__all__`` but are not an
    approved exception. Pure set logic (testable with fixtures) — the meta-test
    passes the discovered ``__all__`` keys and the ``ALL_FIELDS_ALLOWED`` union.
    """
    return [
        f"{key}: exposes fields='__all__' (or Meta.exclude) but is not an approved "
        f"exception. A write serializer MUST use an explicit field list (never "
        f"__all__, which auto-exposes future model fields incl. tenant FKs to "
        f"client writes). If it is genuinely read-only/archival, add it to "
        f"all_fields_policy.READ_ARCHIVAL_ALL_FIELDS_ALLOWED with a justification."
        for key in sorted(set(all_fields_keys) - set(allowed_keys))
    ]
