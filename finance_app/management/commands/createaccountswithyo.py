
from django.core.management.base import BaseCommand
from finance_app.models import BankAccountRecord
from payment_integrations_app.controllers.yo_integrations import YoIntegration


class Command(BaseCommand):
    help = """
    - create verified accounts with Yo
    """

    def handle(self, *args, **options):
        null_yo_references = BankAccountRecord.objects.filter(yo_reference__isnull=True)
        print(f"Found {len(null_yo_references)} accounts without Yo references")
        # return
        for account in null_yo_references:
            YoIntegration().bank_create_verified_account(
                arg_account_id=str(account.id),
                arg_account_name=account.account_name,
                arg_account_number=account.account_number,
                arg_bank_name=account.bank_name,
                arg_address_line1=account.address_line1,
                arg_address_line2=account.address_line2,
                arg_city=account.city,
                arg_country=account.country
            )
            print(f'Created Yo account for {account.account_name}')
