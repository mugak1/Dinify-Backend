import logging
import uuid
from datetime import date
from misc_app.controllers.con_class_utils import ConMiscUtils
from dinify_backend.mongo_db import MONGO_DB

logger = logging.getLogger(__name__)


def archive_record(record_data: str, archive_collection: str):
    archival_time = ConMiscUtils.append_time_details(
        data={},
        just_return=True
    )
    record_data['archival_time'] = archival_time
    record_data['time_created'] = ConMiscUtils.break_down_time(
        record_data['time_created']
    )
    for key, value in record_data.items():
        if isinstance(value, uuid.UUID):
            record_data[key] = str(value)
        elif isinstance(value, date):
            record_data[key] = str(value)

    # save the object in mongodb
    try:
        MONGO_DB[archive_collection].find_one_and_update(
            filter={"id": record_data['id']},
            update={"$set": record_data},
            upsert=True
        )
    except Exception as e:
        logger.error("Failed to archive record %s to %s: %s", record_data.get('id'), archive_collection, e)
