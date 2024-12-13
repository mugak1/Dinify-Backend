from rest_framework.serializers import ModelSerializer, SerializerMethodField
from finance_app.models import DinifyAccount, DinifyTransaction, BankAccountRecord
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment
)


class SerializerPutAccount(ModelSerializer):
    class Meta:
        model = DinifyAccount
        fields = '__all__'


class SerializerGetRestaurantTransactionListing(ModelSerializer):
    order_number = SerializerMethodField()
    amount_in = SerializerMethodField()
    amount_out = SerializerMethodField()

    class Meta:
        model = DinifyTransaction
        fields = (
            'id', 'time_created', 'transaction_type',
            'order_number', 'amount_in', 'amount_out',
            'transaction_status', 'transaction_platform',
            'account_balances'
        )

    def get_order_number(self, record):
        if record.order:
            return record.order.order_number
        return None

    def get_amount_in(self, record):
        if record.transaction_type in [TransactionType_OrderPayment]:
            return record.transaction_amount
        return 0

    def get_amount_out(self, record):
        if record.transaction_type not in [TransactionType_OrderPayment]:
            return record.transaction_amount
        return 0


class SerializerGetDinifyTransactionListing(ModelSerializer):

    class Meta:
        model = DinifyTransaction
        fields = (
            'id', 'time_created', 'time_last_updated', 'transaction_type',
            'account', 'transaction_amount', 'transaction_status',
            'transaction_status', 'transaction_platform',
            'manual_payment', 'manual_payment_details',
            'payment_mode', 'aggregator', 'aggregator_reference',
            'account_balances'
        )


class SerializerPutBankAccountRecord(ModelSerializer):
    class Meta:
        model = BankAccountRecord
        fields = '__all__'
