from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from finance_app.models import DinifyTransaction
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment, TransactionType_Subscription,
    Aggregator_DPO,
    Aggregator_Yo,
    TransactionStatus_Pending,
    TransactionStatus_Initiated,
    ProcessingStatus_Pending
)
from payment_integrations_app.controllers.dpo import DpoIntegration
from payment_integrations_app.controllers.yo_integrations import YoIntegration


class Command(BaseCommand):
    help = """
    - Check transactions to get their statuses with the respective aggregators
    """

    def add_arguments(self, parser):
        # Adding a positional argument
        parser.add_argument('aggregator', type=str, help='the aggregator whose responses to process')

    def handle(self, *args, **options):
        aggregator = options['aggregator']

        if aggregator not in ['yo', 'dpo']:
            raise CommandError("aggregator must be one of ['yo', 'dpo']")

        print(f"\nCheck transaction statuses with {aggregator} at {datetime.now()}...")

        filters = {
            'transaction_type__in': [TransactionType_OrderPayment, TransactionType_Subscription],
            'transaction_status__in': [TransactionStatus_Pending, TransactionStatus_Initiated],
            'processing_status__in': [ProcessingStatus_Pending]
        }

        if aggregator == 'dpo':
            filters['aggregator'] = Aggregator_DPO
        elif aggregator == 'yo':
            filters['aggregator'] = Aggregator_Yo

        pending_transactions = DinifyTransaction.objects.values(
            'id',
            'transaction_type',
            'aggregator',
            'aggregator_reference'
        ).filter(**filters).exclude(aggregator_reference__isnull=True)

        for txs in pending_transactions:
            if txs['transaction_type'] in [TransactionType_OrderPayment, TransactionType_Subscription]:  # noqa
                if aggregator == 'dpo':
                    DpoIntegration().verify_token(
                        transaction_reference=txs['id'],
                        dpo_token=txs['aggregator_reference']
                    )
                elif aggregator == 'yo':
                    YoIntegration().momo_check_transaction(yo_transaction_reference=txs['aggregator_reference'])  # noqa
                else:
                    continue
            else:
                continue
