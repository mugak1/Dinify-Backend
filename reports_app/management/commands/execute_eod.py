from datetime import datetime, timedelta, date
from django.utils import timezone
from django.core.management.base import BaseCommand
from misc_app.models import SysActivityConfig
from restaurants_app.models import Restaurant
from dinify_backend.configss.string_definitions import (
    SysConfig_EodStartTime,
    SysConfig_EodEndTime,
    SysConfig_EodLastDate,
    SysConfig_EodCurrentStatus,
    SysConfig_BusinessDate
)
from reports_app.controllers.eod.confirm_daily_orders import initiate_restaurant_eod
from reports_app.controllers.eod.establish_eod_status import establish_eod_status
from reports_app.controllers.eod.generate_daily_reports import generate_daily_reports


class Command(BaseCommand):
    help = """
    - Executes Dinify EOD
    """

    def handle(self, *args, **options):
        # update the last eod start time
        start_time = timezone.now()
        start_date = start_time.date()
        eod_date = start_date - timedelta(days=1)
        self.stdout.write(self.style.WARNING(f"Starting EOD Execution |  {eod_date}..."))

        SysActivityConfig.objects.update_or_create(
            config_name=SysConfig_EodStartTime,
            defaults={'config_datetime_value': start_time}
        )

        SysActivityConfig.objects.update_or_create(
            config_name=SysConfig_EodCurrentStatus,
            defaults={'config_integer_value': 1}
        )

        self.stdout.write(self.style.WARNING("Blocking new orders..."))

        #  for each restaurant, set the eod status to 1
        # 1. block incoming orders
        #  for each restaurant, take a snapshot of the values as at the moment
        # 2. Confirm daily orders
        # Restaurant.objects.all().update(eod_restaurant_status=1)

        # return
        SysActivityConfig.objects.update_or_create(
            config_name=SysConfig_EodCurrentStatus,
            defaults={'config_integer_value': 2}
        )
        # 3a. Set new system business date at restaurant level
        # initiate_restaurant_eod(eod_date)

        # 3b. Set new system business date at system level
        # system is typically open for orders at this stage
        SysActivityConfig.objects.update_or_create(
            config_name=SysConfig_BusinessDate,
            defaults={'config_date_value': start_date}
        )

        self.stdout.write(self.style.WARNING("Establishing EOD statuses..."))
        # 4. organising records by EOD status
        # this can be run outside of django
        # establish_eod_status(eod_date)

        # 5. reconcile payments and confirm accounts balances

        self.stdout.write(self.style.WARNING("Generating daily reports..."))
        # 6. generate daily reports
        generate_daily_reports(eod_date)

        # 7. generate periodical reports

        # 8. send out notifications

        # 9. archive records

        self.stdout.write(self.style.SUCCESS("Completed EOD!"))
