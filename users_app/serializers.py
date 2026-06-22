"""
serializers for the users_app
"""
from rest_framework.serializers import SerializerMethodField, ModelSerializer
from users_app.models import User


class SerGetUserProfile(ModelSerializer):
    """
    the serializer for the user profile
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

    def get_restaurant_roles(self, user):
        if 'restaurant_roles' in self.context:
            return self.context['restaurant_roles']
        # Delegate to the canonical builder so the login path (which feeds its
        # output in via context) and this profile-fetch path cannot diverge —
        # including the resolved `permissions` map on each entry. Lazy import
        # avoids any module-load coupling.
        from users_app.controllers.permissions_check import get_any_restaurant_roles
        return get_any_restaurant_roles(user)


class SerPutUserProfile(ModelSerializer):
    class Meta:
        model = User
        fields = ('__all__')