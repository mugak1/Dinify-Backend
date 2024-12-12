from datetime import date, datetime
from django.core.management.base import BaseCommand
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    Aggregator_DPO,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
    ProcessingStatus_Pending
)
from payment_integrations_app.controllers.dpo import DpoIntegration


class Command(BaseCommand):
    help = """
    - Checks for the token information with DPO to establish the status of the payments
    """

    def handle(self, *args, **options):
        print(f'\nVerifying DPO tokens at {datetime.datetime.now()}...\n')
        pending_transactions = DinifyTransaction.objects.filter(
            aggregator=Aggregator_DPO,
            transaction_type=TransactionType_OrderPayment,
            processing_status=ProcessingStatus_Pending,
            transaction_status__in=[
                TransactionStatus_Pending,
                TransactionStatus_Initiated
            ],
        )

        for txs in pending_transactions:
            dpo_token = txs.aggregator_misc_details.get('transaction_token')
            if dpo_token is None:
                continue
            DpoIntegration(
                amount=None,
                currency=None,
                msisdn=None,
                transaction_reference=str(txs.id),
                timestamp=None,
                dpo_transaction_token=dpo_token
            ).verify_token()
