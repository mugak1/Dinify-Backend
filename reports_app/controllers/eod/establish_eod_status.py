import logging
from datetime import date, datetime
from dinify_backend.mongo_db import MONGO_DB
from bson import ObjectId
from misc_app.controllers.save_to_mongo import save_to_mongodb

logger = logging.getLogger(__name__)


def establish_eod_status(eod_date: date):
    """
    Organises records by their status.
    """
    start_time = datetime.now()
    eod_date = str(eod_date)
    # in mongodb, find records where the eod_record_date is equal to the eod_date
    try:
        eod_orders = MONGO_DB['archive_orders'].find(
            {'eod_record_date': eod_date}
        )
        eod_order_items = MONGO_DB['archive_order_items'].find(
            {'eod_record_date': eod_date}
        )
        eod_transactions = MONGO_DB['archive_transactions'].find(
            {'eod_record_date': eod_date}
        )
    except Exception as e:
        logger.error("Failed to query MongoDB for EOD status (date %s): %s", eod_date, e)
        return

    # print the count of the records
    eod_orders = list(eod_orders) if eod_orders is not None else []
    eod_order_items = list(eod_order_items) if eod_order_items is not None else []
    eod_transactions = list(eod_transactions) if eod_transactions is not None else []

    for order in eod_orders:
        statuses_at_eod = {
            'order_status': order['order_status'],
            'payment_status': order['payment_status']
        }
        # update the document to have the statuses established
        try:
            MONGO_DB['archive_orders'].find_one_and_update(
                filter={
                    '_id': ObjectId(order['_id'])
                },
                update={
                    '$set': {
                        'statuses_at_eod': statuses_at_eod,
                    }
                }
            )
        except Exception as e:
            logger.error("Failed to update EOD status for order %s: %s", order.get('_id'), e)
            continue

    for order_item in eod_order_items:
        statuses_at_eod = {
            'status': order_item['status'],
        }
        # attempt to simply get the document
        # update the document to have the statuses established
        try:
            MONGO_DB['archive_order_items'].find_one_and_update(
                filter={
                    '_id': ObjectId(order_item['_id'])
                },
                update={
                    '$set': {
                        'statuses_at_eod': statuses_at_eod,
                    }
                }
            )
        except Exception as e:
            logger.error("Failed to update EOD status for order_item %s: %s", order_item.get('_id'), e)
            continue

    for transaction in eod_transactions:
        statuses_at_eod = {
            'transaction_status': transaction['transaction_status'],
            'processing_status': transaction['processing_status'],
            'aggregator_status': transaction['aggregator_status'],
        }
        # update the document to have the statuses established
        try:
            MONGO_DB['archive_transactions'].find_one_and_update(
                filter={
                    '_id': ObjectId(transaction['_id'])
                },
                update={
                    '$set': {
                        'statuses_at_eod': statuses_at_eod,
                    }
                }
            )
        except Exception as e:
            logger.error("Failed to update EOD status for transaction %s: %s", transaction.get('_id'), e)
            continue

    end_time = datetime.now()
    result = {
        'date': eod_date,
        'stage': 'establish_eod_status',
        'count_orders': len(eod_orders),
        'count_order_items': len(eod_order_items),
        'count_transactions': len(eod_transactions),
        'start_time': start_time,
        'end_time': end_time,
        'duration_seconds': (end_time - start_time).total_seconds()
    }

    save_to_mongodb(
        data=result,
        collection='eod_logs'
    )
