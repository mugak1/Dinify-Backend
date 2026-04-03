import logging
from bson import ObjectId
from dinify_backend.mongo_db import MONGO_DB

logger = logging.getLogger(__name__)


def establish_customer_ages() -> dict:
    # read from archive orders where the key, customer_age_at_order is not present
    try:
        orders = MONGO_DB['archive_orders'].find({
            "customer_age_at_order": {"$exists": False},
        })
    except Exception as e:
        logger.error("Failed to query archive_orders for customer ages: %s", e)
        return {}

    for order in orders:
        try:
            customer_age_at_order = None
            days_since_last_customer_order = None
            days_since_last_customer_order_at_restaurant = None

            customer_id = order.get('customer')
            if customer_id is not None:
                customer = MONGO_DB['archive_users'].find_one({"id": customer_id})
                if customer is None:
                    continue
                customer_reg_time = None

                customer_reg_time = customer.get('time_created')
                if customer_reg_time is None:
                    customer_reg_time = customer.get('date_joined')
                customer_reg_time = customer_reg_time['timestamp']
                order_start_time = order.get('time_created')['timestamp']

                customer_age_at_order = (order_start_time - customer_reg_time).days

                last_customer_order = MONGO_DB['archive_orders'].find_one(
                    {
                        "customer": customer_id,
                        "time_created.timestamp": {
                            "$lt": order_start_time
                        }
                    },
                    sort=[("time_created.timestamp", -1)]
                )

                last_customer_order_at_restaurant = MONGO_DB['archive_orders'].find_one(
                    {
                        "customer": customer_id,
                        "restaurant": order.get('restaurant'),
                        "time_created.timestamp": {
                            "$lt": order_start_time
                        }
                    },
                    sort=[("time_created.timestamp", -1)]
                )

                if last_customer_order:
                    last_order_time = last_customer_order.get('time_created')['timestamp']
                    days_since_last_customer_order = (order_start_time - last_order_time).days

                if last_customer_order_at_restaurant:
                    last_order_time_at_restaurant = last_customer_order_at_restaurant.get('time_created')['timestamp']
                    days_since_last_customer_order_at_restaurant = (order_start_time - last_order_time_at_restaurant).days

                # update the order with the calculated values
                MONGO_DB['archive_orders'].update_one(
                    {"_id": ObjectId(order['_id'])},
                    {
                        "$set": {
                            "anl_customer_age_at_order": customer_age_at_order,
                            "anl_days_since_last_customer_order": days_since_last_customer_order,
                            "anl_days_since_last_customer_order_at_restaurant": days_since_last_customer_order_at_restaurant
                        }
                    }
                )
        except Exception as e:
            logger.error("Failed to process customer age for order %s: %s", order.get('_id'), e)
            continue
