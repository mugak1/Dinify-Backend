from rest_framework.serializers import ModelSerializer, SerializerMethodField
from crm_app.models import ServiceTicket


class SerializerPutServiceTicket(ModelSerializer):
    class Meta:
        model = ServiceTicket
        fields = '__all__'


class SerializerGetServiceTicket(ModelSerializer):
    created_by = SerializerMethodField()
    restaurant = SerializerMethodField()
    assigned_to = SerializerMethodField()
    assigned_by = SerializerMethodField()

    class Meta:
        model = ServiceTicket
        fields = '__all__'

    def get_created_by(self, obj):
        names = f"{obj.created_by.first_name} {obj.created_by.last_name}"
        return names

    def get_restaurant(self, obj):
        if obj.restaurant is None:
            return None
        return obj.restaurant.name

    def get_assigned_to(self, obj):
        if obj.assigned_to is None:
            return None
        return f"{obj.assigned_to.first_name} {obj.assigned_to.last_name}"

    def get_assigned_by(self, obj):
        if obj.assigned_by is None:
            return None
        return f"{obj.assigned_by.first_name} {obj.assigned_by.last_name}"
