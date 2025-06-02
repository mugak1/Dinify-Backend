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
from reports_app.controllers.eod.reports.daily_reports import generate_restaurant_daily_report


def generate_daily_reports(eod_date):
    all_restaurants = Restaurant.objects.all()
    # with concurrent.futures.ThreadPoolExecutor(thread_name_prefix='DinifyEodDailyReports') as executor:
    #     futures = [
    #         executor.submit(
    #             generate_restaurant_daily_report,
    #             restaurant_id=str(restaurant.id),
    #             eod_date=eod_date
    #         )
    #         for restaurant in all_restaurants
    #     ]
    #     for future in concurrent.futures.as_completed(futures):
    #         try:
    #             future.result()
    #             # logging.info(f"Successfully generated reports for {restaurant.name} on {eod_date}.")
    #             pass
    #         except Exception as e:
    #             logging.error(f"EOD Report Error: {e}")

    for restaurant in all_restaurants:
        generate_restaurant_daily_report(
            restaurant_id=str(restaurant.id),
            eod_date=eod_date
        )