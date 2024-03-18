from rest_framework.serializers import ModelSerializer
from finance_app.models import DinifyAccount


class SerializerPutAccount(ModelSerializer):
    class Meta:
        model = DinifyAccount
        fields = '__all__'
