
from django.core.management.base import BaseCommand
from dinify_backend.mongo_db import MONGO_DB, COL_YO_RESPONSES
from payment_integrations_app.controllers.yo_integrations import YoIntegration


class Command(BaseCommand):
    help = """
    - Processes yo responses
    """

    def handle(self, *args, **options):
        pending_responses = MONGO_DB[COL_YO_RESPONSES].find(
            {'dinify_processed': {'$exists': False}},
            {'_id': 1}
        ).batch_size(1000)

        for x in pending_responses:
            YoIntegration().process_yo_response(response_id=x['_id'])
