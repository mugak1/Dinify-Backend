"""
Production write-surface manifest (TENANT-ISO-PR5).

Names the exact serializers that accept PRODUCTION input and are ``.save()``d, so
the tenancy meta-test has a precise, review-visible answer to "which serializers
are load-bearing write surfaces?". This complements — it does NOT replace —
global discovery (discovery.py still finds any newly-introduced serializer or
relation and forces it to be classified or baselined).

For every serializer in the manifest the meta-test asserts:
  - it imports;
  - it does NOT use ``fields='__all__'`` / ``Meta.exclude`` (all_fields_policy);
  - it is NOT in any legacy ``__all__`` debt set (which must stay empty);
  - every genuinely-writable relational field is CLASSIFIED in
    ``Meta.tenant_relations`` (never left as an unclassified baseline entry);
  - every ``SameTenant`` classification carries a resolvable ``verified_by``.

Keep this data-oriented and small — it is a manifest, not an authorization DSL.
"""
import importlib

from dinify_backend.tenancy.all_fields_policy import (
    uses_all_fields,
    WRITE_ALL_FIELDS_DEBT,
)
from dinify_backend.tenancy.discovery import (
    is_writable_relation,
    read_classifications,
    field_key,
)
from dinify_backend.tenancy.relations import (
    is_classification,
    SameTenant,
    resolve_test_ref,
)


# Dotted paths to every production input serializer that is validated + saved.
# SerializerPutOrder was DELETED (dead); order rows are written by the service
# via Order.objects.create + the internal SerializerPutOrderItem.
# SerPutUserProfile was DELETED with the manager-OTP V2 user-profile endpoint
# (dead); the live self-service profile path uses a plain User.save(), not a
# write serializer.
PRODUCTION_WRITE_SERIALIZERS = (
    "restaurants_app.serializers.SerializerPutRestaurant",
    "restaurants_app.serializers.SerializerPutRestaurantEmployee",
    "restaurants_app.serializers.SerializerPutMenuSection",
    "restaurants_app.serializers.SerializerPutSectionGroup",
    "restaurants_app.serializers.SerializerPutMenuItem",
    "restaurants_app.serializers.SerializerPutTable",
    "restaurants_app.serializers.SerializerPutDiningArea",
    "restaurants_app.serializers.SerializerPutReservation",
    "restaurants_app.serializers.SerializerPutWaitlistEntry",
    "restaurants_app.serializers.SerializerRestaurantTag",
    "restaurants_app.serializers.UpsellConfigUpdateSerializer",
    "orders_app.serializers.SerializerPutOrderItem",
    "reviews_app.serializers.ReviewWriteSerializer",
    "support_app.serializers.SupportIssueWriteSerializer",
)


def _import_serializer(dotted):
    module_path, _, cls_name = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, cls_name)


def write_surface_violations(serializers=PRODUCTION_WRITE_SERIALIZERS):
    """
    Human-readable violations for the production write-surface policy. Pure logic
    driven by the manifest (testable in isolation); the meta-test feeds it the
    default tuple.
    """
    violations = []
    for dotted in serializers:
        try:
            cls = _import_serializer(dotted)
        except Exception as error:  # noqa: BLE001
            violations.append(f"{dotted}: cannot import ({error!r}).")
            continue

        if uses_all_fields(cls):
            violations.append(
                f"{dotted}: a production write serializer must not use "
                f"fields='__all__' / Meta.exclude."
            )
        if dotted in WRITE_ALL_FIELDS_DEBT:
            violations.append(
                f"{dotted}: present in WRITE_ALL_FIELDS_DEBT — the debt set must "
                f"stay empty; use an explicit field list instead."
            )

        classifications = read_classifications(cls)
        try:
            fields = cls().fields
        except Exception as error:  # noqa: BLE001
            violations.append(f"{dotted}: cannot introspect fields ({error!r}).")
            continue

        for name, field in fields.items():
            if not is_writable_relation(field):
                continue
            classification = classifications.get(name)
            if classification is None:
                violations.append(
                    f"{field_key(cls, name)}: writable relation is neither "
                    f"read_only nor classified in Meta.tenant_relations."
                )
                continue
            if not is_classification(classification):
                violations.append(
                    f"{field_key(cls, name)}: tenant_relations value is not a "
                    f"valid classification instance."
                )
                continue
            if isinstance(classification, SameTenant):
                refs = classification.verified_by
                refs = [refs] if isinstance(refs, str) else list(refs or [])
                if not refs or any(not resolve_test_ref(r) for r in refs):
                    violations.append(
                        f"{field_key(cls, name)}: SameTenant needs a resolvable "
                        f"verified_by two-tenant test."
                    )
    return violations
