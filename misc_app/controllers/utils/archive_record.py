import uuid
from misc_app.controllers.con_class_utils import ConMiscUtils
from dinify_backend.mongo_db import MONGO_DB


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

    # save the object in mongodb
    MONGO_DB[archive_collection].find_one_and_update(
        filter={"id": record_data['id']},
        update={"$set": record_data},
        upsert=True
    )
