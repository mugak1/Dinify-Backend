"""
Tenant-scope guards shared by the tables-domain write serializers.

The reservations / waitlist / tables write paths authorize only the PARENT
record's restaurant (the endpoint gate) and never re-scope the NESTED foreign
keys. Because those serializers are ``fields='__all__'`` with the default,
globally-unfiltered ``PrimaryKeyRelatedField`` queryset, DRF happily resolves any
restaurant's UUID to an instance. This helper re-imposes the tenant boundary in
the serializers' ``validate()`` — every nested FK must resolve to the same
restaurant the caller was gated against.
"""
from rest_framework import serializers


def assert_fks_belong_to_restaurant(restaurant_id, attrs, fields):
    """
    Every named FK present and non-None in ``attrs`` must resolve to
    ``restaurant_id``.

    DRF has already resolved each ``PrimaryKeyRelatedField`` to a model instance,
    and every tables-domain FK target (Table, RestaurantEmployee, DiningArea)
    carries a direct ``restaurant_id``, so the check is a single-hop equality.
    Fields ABSENT from ``attrs`` (a partial update that did not touch them) and
    fields explicitly set to ``None`` (all are SET_NULL and must stay clearable)
    are skipped. The message is generic — it never reveals whether the foreign id
    exists, so it cannot be used to enumerate other tenants.
    """
    for field in fields:
        obj = attrs.get(field)
        if obj is None:
            continue
        if obj.restaurant_id != restaurant_id:
            raise serializers.ValidationError(
                {field: 'Does not belong to this restaurant.'}
            )
