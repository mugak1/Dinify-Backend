"""
serializers for the users_app
"""
from rest_framework.serializers import SerializerMethodField, ModelSerializer
from users_app.models import User


class SerGetUserProfile(ModelSerializer):
    """
    the serializer for the user profile

    Output-only in practice (login + profile fetch), but ``roles`` is declared
    ``read_only`` rather than left implicit: the field is emitted, so it would be
    natural for someone to later bind this serializer with ``data=`` for a profile
    write, and a writable ``roles`` there is a mass-assignment path straight into
    the account's role list. ``account_type`` is protected by being absent from
    ``fields`` at all; ``roles`` has to stay in the payload for the frontend, so it
    gets the explicit lock instead.
    """
    restaurant_roles = SerializerMethodField()

    class Meta:
        """
        the metadata for the serializer
        """
        model = User
        fields = [
            'id', 'first_name', 'last_name',
            'email', 'phone_number', 'country', 'roles',
            'prompt_password_change', 'restaurant_roles'
        ]
        read_only_fields = ['roles']

    def get_restaurant_roles(self, user):
        if 'restaurant_roles' in self.context:
            return self.context['restaurant_roles']
        # Delegate to the canonical builder so the login path (which feeds its
        # output in via context) and this profile-fetch path cannot diverge —
        # including the resolved `permissions` map on each entry. Lazy import
        # avoids any module-load coupling.
        from users_app.controllers.permissions_check import get_any_restaurant_roles
        return get_any_restaurant_roles(user)