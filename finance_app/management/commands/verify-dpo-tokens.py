
from django.core.management.base import BaseCommand
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    Aggregator_DPO,
    TransactionStatus_Pending
)
from payment_integrations_app.controllers.dpo import DpoIntegration


class Command(BaseCommand):
    help = """
    - Checks for the token information with DPO to establish the status of the payments
    """

    def handle(self, *args, **options):
        print('\n=== Verifying DPO tokens ===\n')
        pending_transactions = DinifyTransaction.objects.filter(
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Pending,
            aggregator=Aggregator_DPO
        )

        for txs in pending_transactions:
            dpo_token = txs.aggregator_misc_details['transaction_token']
            DpoIntegration(
                amount=None,
                currency=None,
                msisdn=None,
                transaction_reference=str(txs.id),
                timestamp=None,
                dpo_transaction_token=dpo_token
            ).verify_token()
