"""
serializers for the users_app
"""
from rest_framework.serializers import SerializerMethodField, ModelSerializer
from users_app.models import User
from restaurants_app.models import RestaurantEmployee


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
        res_roles = RestaurantEmployee.objects.filter(user=user)
        return [
            {
                'restaurant': res_role.restaurant.name,
                'roles': res_role.roles
            }
            for res_role in res_roles
        ]
