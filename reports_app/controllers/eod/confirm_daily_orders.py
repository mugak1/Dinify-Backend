import logging
from django.db import transaction
from django.db.models import Q
from datetime import date, timedelta, datetime
from restaurants_app.models import Restaurant
from orders_app.models import Order, OrderItem
from finance_app.models import DinifyAccount, DinifyTransaction
from finance_app.serializers import SerializerPutDinifyTransaction, SerializerPutAccount

from orders_app.serializers import SerializerPutOrder, SerializerPutOrderItem
from misc_app.controllers.save_to_mongo import save_to_mongodb
from misc_app.controllers.utils.archive_record import archive_record
import concurrent.futures


def snapshot_daily_orders(restaurant_id: str, eod_date: date) -> dict:
    start_time = datetime.now()
    with transaction.atomic():
        restaurant = Restaurant.objects.select_for_update().get(id=restaurant_id)
        restaurant_orders = Order.objects.select_for_update().filter(
            restaurant=restaurant,
            eod_record_date=None,
        )

        restaurant_order_items = OrderItem.objects.select_for_update().filter(
            order__in=[order.id for order in restaurant_orders],
        )

        # not used to avoid nullable outer joins error
        # restaurant_transactions = DinifyTransaction.objects.select_for_update().filter(
        #     (Q(restaurant=restaurant) | Q(order__restaurant=restaurant)),
        #     eod_record_date=None,
        # )

        res_order_transactions = DinifyTransaction.objects.select_for_update().filter(
            order__in=[order.id for order in restaurant_orders],
            eod_record_date=None,
        )
        res_transactions = DinifyTransaction.objects.select_for_update().filter(
            restaurant=restaurant,
            eod_record_date=None,
        )

        restaurant_transactions = res_order_transactions | res_transactions

        restaurant_accounts = DinifyAccount.objects.select_for_update().filter(
            restaurant=restaurant,
            eod_record_date=None,
        )

        #  bulk update the restaurant records with the eod date
        for x in restaurant_transactions:
            x.eod_record_date = eod_date

        # save each order to the archive i.e. mongo
        for order in restaurant_orders:
            data = SerializerPutOrder(order).data
            data['eod_record_date'] = str(eod_date)
            archive_record(
                record_data=data,
                archive_collection='archive_orders'
            )
        # save each order item to the archive
        for order_item in restaurant_order_items:
            data = SerializerPutOrderItem(order_item).data
            data['eod_record_date'] = str(eod_date)
            archive_record(
                record_data=data,
                archive_collection='archive_order_items'
            )
        # save each transaction to the archive
        for tx in restaurant_transactions:
            data = SerializerPutDinifyTransaction(tx).data
            data['eod_record_date'] = str(eod_date)
            archive_record(
                record_data=data,
                archive_collection='archive_transactions'
            )

        # save each account to the archive.
        for account in restaurant_accounts:
            data = SerializerPutAccount(account).data
            archive_record(
                record_data=data,
                archive_collection='archive_accounts'
            )

        restaurant_orders.update(eod_record_date=eod_date)
        restaurant_order_items.update(eod_record_date=eod_date)
        DinifyTransaction.objects.bulk_update(
            restaurant_transactions,
            fields=['eod_record_date']
        )

        # set new system date for the restaurant to allow new orders
        restaurant.system_date = eod_date + timedelta(days=1)
        restaurant.eod_restaurant_status = 4
        restaurant.save()

        end_time = datetime.now()
        result = {
            'date': str(eod_date),
            'stage': 'snapshot_daily_orders',
            'restaurant_id': restaurant_id,
            'restaurant_name': restaurant.name,
            'count_orders': restaurant_orders.count(),
            'count_order_items': restaurant_order_items.count(),
            'count_transactions': restaurant_transactions.count(),
            'count_accounts': restaurant_accounts.count(),
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': (end_time - start_time).total_seconds(),
        }

        save_to_mongodb(
            data=result,
            collection='eod_logs'
        )


def initiate_restaurant_eod(eod_date: date):
    all_restaurants = Restaurant.objects.all()

    with concurrent.futures.ThreadPoolExecutor(thread_name_prefix='DinifyEOD') as executor:
        futures = [
            executor.submit(
                snapshot_daily_orders,
                restaurant_id=str(restaurant.id),
                eod_date=eod_date
            )
            for restaurant in all_restaurants
        ]
        # Wait for all threads to complete
        for future in concurrent.futures.as_completed(futures):
            try:
                # result = future.result()
                # Optionally log result
                # logging.warning(f"\nResult: {result}\n")
                pass
            except Exception as exc:
                logging.error(f"Thread generated an exception: {exc}")
