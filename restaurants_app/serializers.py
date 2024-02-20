"""
the serializers for the restaurant app
"""
from rest_framework.serializers import ModelSerializer, SerializerMethodField
from restaurants_app.models import Restaurant, RestaurantEmployee


class SerializerPutRestaurant(ModelSerializer):
    """
    seriailizer for adding and editing restaurant details
    """
    class Meta:
        """
        the meta class for the serializers
        """
        model = Restaurant
        fields = '__all__'


class SerializerPublicGetRestaurant(ModelSerializer):
    """
    serializer for getting public restaurant details
    """
    class Meta:
        model = Restaurant
        fields = ("id", "name", "location", "logo", "cover_photo")


class SerializerEmployeeGetRestaurant(ModelSerializer):
    """
    serializer for getting restaurant details for the employee
    """
    class Meta:
        model = RestaurantEmployee
        fields = ("id", "name", "location")


class SerializerPutRestaurantEmployee(ModelSerializer):
    """
    serializer for adding and editing restaurant employees
    """
    class Meta:
        model = RestaurantEmployee
        fields = '__all__'


class SerializerGetRestaurantEmployee(ModelSerializer):
    """
    serializer for getting restaurant employees
    """
    name = SerializerMethodField()

    class Meta:
        model = RestaurantEmployee
        fields = (
            "id", "time_created", "time_last_updated",
            "name", "roles", "active"
        )

    def get_name(self, employee):
        """
        returns the name of the employee
        """
        return f"{employee.user.first_name} {employee.user.last_name}"
