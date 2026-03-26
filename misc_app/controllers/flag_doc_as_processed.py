import logging
from bson import ObjectId
from dinify_backend.mongo_db import MONGO_DB
from misc_app.controllers.describe_time import describe_time

logger = logging.getLogger(__name__)


def flag_doc_as_processed(collection_name, doc_id: str):
    processing_time = describe_time()
    try:
        MONGO_DB[collection_name].update_one(
            {'_id': ObjectId(doc_id)},
            {
                '$set': {
                    'dinify_processed': True,
                    'dinify_processed_time': processing_time
                }
            }
        )
    except Exception as e:
        logger.error("Failed to flag doc %s as processed in %s: %s", doc_id, collection_name, e)
