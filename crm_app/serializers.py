from rest_framework.serializers import ModelSerializer
from crm_app.models import ServiceTicket


class SerializerPutServiceTicket(ModelSerializer):
    class Meta:
        model = ServiceTicket
        fields = '__all__'