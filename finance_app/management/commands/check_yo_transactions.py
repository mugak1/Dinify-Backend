
from django.core.management.base import BaseCommand
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment, TransactionType_Subscription,
    Aggregator_DPO,
    Aggregator_Yo,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
    ProcessingStatus_Pending
)
from payment_integrations_app.controllers.yo_integrations import YoIntegration


class Command(BaseCommand):
    help = """
    - Check transactions to get their statuses with the respective aggregators
    """

    def handle(self, *args, **options):
        pending_yo_collections = DinifyTransaction.objects.values(
            'aggregator_reference',
        ).filter(
            transaction_type=[TransactionType_OrderPayment, TransactionType_Subscription],
            aggregator=Aggregator_Yo,
            transaction_status__in=[
                TransactionStatus_Pending,
                TransactionStatus_Initiated
            ],
            processing_status__in=ProcessingStatus_Pending
        ).exclude(aggregator_reference__isnull=True)

        for txs in pending_yo_collections:
            YoIntegration().momo_check_transaction(
                yo_transaction_reference=txs['aggregator_reference']
            )
