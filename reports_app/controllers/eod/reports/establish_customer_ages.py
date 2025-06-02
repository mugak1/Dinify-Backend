from bson import ObjectId
from dinify_backend.mongo_db import MONGO_DB


def establish_customer_ages() -> dict:
    # read from archive orders where the key, customer_age_at_order is not present
    orders = MONGO_DB['archive_orders'].find({
        "customer_age_at_order": {"$exists": False},
    })

    for order in orders:
        customer_age_at_order = None
        days_since_last_customer_order = None
        days_since_last_customer_order_at_restaurant = None

        customer_id = order.get('customer')
        if customer_id is not None:
            customer = MONGO_DB['archive_users'].find_one({"id": customer_id})
            customer_reg_time = customer.get('time_created')['timestamp']
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
