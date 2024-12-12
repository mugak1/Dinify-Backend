from datetime import datetime
from django.core.management.base import BaseCommand
from dinify_backend.mongo_db import MONGO_DB, COL_YO_RESPONSES, COL_DPO_RESPONSES
from payment_integrations_app.controllers.yo_integrations import YoIntegration
from payment_integrations_app.controllers.dpo import DpoIntegration
from django.core.management import CommandError


RESPONSE_COLLECTIONS = {
    'yo': COL_YO_RESPONSES,
    'dpo': COL_DPO_RESPONSES
}


class Command(BaseCommand):
    help = """
    - Processes aggregator responses
    """

    def add_arguments(self, parser):
        # Adding a positional argument
        parser.add_argument('aggregator', type=str, help='the aggregator whose responses to process')

    def handle(self, *args, **options):
        aggregator = options['aggregator']

        if aggregator not in RESPONSE_COLLECTIONS.keys():
            raise CommandError(f"aggregator must be one of {RESPONSE_COLLECTIONS.keys()}")

        collection = RESPONSE_COLLECTIONS.get(aggregator)
        if collection is None:
            raise CommandError(f"no collection found for aggregator {aggregator}")

        print(f"\nProcessing {aggregator} responses at {datetime.now()}...")

        pending_responses = MONGO_DB[collection].find(
            {'dinify_processed': {'$exists': False}},
            {'_id': 1}
        ).batch_size(1000)

        for x in pending_responses:
            if aggregator == 'yo':
                YoIntegration().process_yo_response(response_id=x['_id'])
            elif aggregator == 'dpo':
                DpoIntegration().process_response(response_id=x['_id'])
            else:
                continue
