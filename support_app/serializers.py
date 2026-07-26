"""
Serializers for the support_app.

Three explicit-field serializers (never ``__all__``):
- ``SupportIssueWriteSerializer``     -> used by Secretary for create AND update.
- ``SupportIssueRestaurantReadSerializer`` -> restaurant-facing reads; EXCLUDES
  ``internal_notes`` and ``assigned_to``.
- ``SupportIssueAdminReadSerializer``  -> Dinify-staff reads; full fields.

All name helpers are NULL-SAFE — a ``get_created_by`` that dereferences a null
``created_by`` crashes; we must not repeat that.
"""
from rest_framework import serializers

from support_app.models import SupportIssue
from dinify_backend.tenancy.relations import GlobalRelation


def _full_name(user):
    if user is None:
        return None
    return f'{user.first_name or ""} {user.last_name or ""}'.strip() or None


class SupportIssueWriteSerializer(serializers.ModelSerializer):
    """
    Write serializer for create and update via Secretary.

    ``reference`` is read-only — the model generates it in ``save()``; declaring
    it writable would attach a UniqueValidator and make it required. Keeping it
    in the output means the POST response still carries the new reference.

    ``restaurant`` and ``created_by`` are SERVER-DERIVED (read_only): the
    restaurant is resolved + gated by the endpoint and set via Secretary
    ``server_values`` on create; ``created_by`` is the resolved actor injected by
    Secretary through ``save()``. ``assigned_to`` is a platform-global support
    agent (GlobalRelation) with NO writer left on this plane: the Dinify-admin
    triage endpoint that used to set it was removed with ambient administrator
    authority, and the restaurant-facing create path whitelists its own fields,
    so no request reaches it. Phase 1 rebuilds triage on /api/admin/v1.
    """

    class Meta:
        model = SupportIssue
        fields = [
            'id', 'reference', 'restaurant', 'created_by', 'category', 'impact',
            'status', 'title', 'description', 'contact_phone', 'contact_email',
            'preferred_contact_method', 'page_url', 'user_agent', 'assigned_to',
            'internal_notes', 'resolution_summary', 'resolved_at', 'closed_at',
        ]
        read_only_fields = ['id', 'reference', 'restaurant', 'created_by']
        tenant_relations = {
            'assigned_to': GlobalRelation(
                reason='Support agents are platform-global (not restaurant-scoped). '
                       'The admin triage path that wrote assigned_to is retired, so '
                       'no customer-plane request reaches this field at all; the '
                       'classification records that it is global by nature, not '
                       'tenant-scoped, for whenever Phase 1 rebuilds triage.'
            ),
        }


class SupportIssueRestaurantReadSerializer(serializers.ModelSerializer):
    """
    Restaurant-facing read serializer. Explicit allowlist (not ``exclude``) so a
    future model field is never silently exposed. Deliberately omits
    ``internal_notes`` and ``assigned_to``.
    """

    restaurant_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportIssue
        fields = [
            'id', 'reference', 'restaurant', 'restaurant_name', 'category',
            'impact', 'status', 'title', 'description', 'contact_phone',
            'contact_email', 'preferred_contact_method', 'page_url',
            'user_agent', 'resolution_summary', 'resolved_at', 'closed_at',
            'created_by_name', 'time_created', 'time_last_updated',
        ]

    def get_restaurant_name(self, obj):
        return obj.restaurant.name if obj.restaurant else None

    def get_created_by_name(self, obj):
        return _full_name(obj.created_by)


class SupportIssueAdminReadSerializer(serializers.ModelSerializer):
    """
    Dinify-staff read serializer. Full fields including ``internal_notes`` and
    ``assigned_to``.
    """

    restaurant_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportIssue
        fields = [
            'id', 'reference', 'restaurant', 'restaurant_name', 'category',
            'impact', 'status', 'title', 'description', 'contact_phone',
            'contact_email', 'preferred_contact_method', 'page_url',
            'user_agent', 'assigned_to', 'assigned_to_name', 'internal_notes',
            'resolution_summary', 'resolved_at', 'closed_at', 'created_by',
            'created_by_name', 'time_created', 'time_last_updated',
        ]

    def get_restaurant_name(self, obj):
        return obj.restaurant.name if obj.restaurant else None

    def get_created_by_name(self, obj):
        return _full_name(obj.created_by)

    def get_assigned_to_name(self, obj):
        return _full_name(obj.assigned_to)
