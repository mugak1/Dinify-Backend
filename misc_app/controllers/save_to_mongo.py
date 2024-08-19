from typing import Union
from dinify_backend.mongo_db import MONGO_DB
from misc_app.controllers.save_action_log import make_detailed_time


def save_to_mongodb(
    collection: str,
    data: dict,
    return_id=False
) -> Union[bool, str]:
    """
    Save data to mongodb
    """
    try:
        creation_time = make_detailed_time()
        data['creation_timestamp'] = creation_time
        record = MONGO_DB[collection].insert_one(data)
        if return_id:
            return str(record.inserted_id)
        return True
    except Exception as e:
        print(f"\n...Failed to save to mongodb: {e}...\n{data}\n.......")
        return False
