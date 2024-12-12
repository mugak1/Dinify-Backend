
from django.core.management.base import BaseCommand
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment, TransactionType_Subscription,
    Aggregator_DPO,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
    ProcessingStatus_Pending,
    ProcessingStatus_Confirmed,
    ProcessingStatus_Failed
)
from payment_integrations_app.controllers.dpo import DpoIntegration
from finance_app.controllers.tx_order_payment import OrderPaymentTransaction
from finance_app.controllers.tx_subscription import SubscriptionPaymentTransaction


class Command(BaseCommand):
    help = """
    - Process none pending transactions
    """

    def handle(self, *args, **options):
        pending_transactions = DinifyTransaction.objects.values(
            'id',
            'transaction_type',
        ).filter(
            transaction_status__in=[
                TransactionStatus_Pending,
                TransactionStatus_Initiated
            ],
            processing_status__in=[
                ProcessingStatus_Confirmed,
                ProcessingStatus_Failed
            ]
        )

        for txs in pending_transactions:
            if txs['transaction_type'] == TransactionType_OrderPayment:
                OrderPaymentTransaction().process(transaction_id=txs['id'])
            elif txs['transaction_type'] == TransactionType_Subscription:
                SubscriptionPaymentTransaction().process(transaction_id=txs['id'])
