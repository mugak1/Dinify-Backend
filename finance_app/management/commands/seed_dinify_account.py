
from django.core.management.base import BaseCommand
from finance_app.models import DinifyAccount
from dinify_backend.configss.string_definitions import AccountType_DinifyRevenue


def seed_dinify_account():
    try:
        DinifyAccount.objects.get(account_type=AccountType_DinifyRevenue)
        print('Dinify Revenue Account already exists')
    except DinifyAccount.DoesNotExist:
        DinifyAccount.objects.create(account_type=AccountType_DinifyRevenue)
        print('Dinify Revenue Account created')


class Command(BaseCommand):
    help = """
    - Checks for the token information with DPO to establish the status of the payments
    """

    def handle(self, *args, **options):
        seed_dinify_account()
