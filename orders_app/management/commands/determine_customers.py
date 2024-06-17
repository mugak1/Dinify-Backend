from typing import Optional
from typing import Union
import random
from django.db import transaction
from django.core.management.base import BaseCommand
from orders_app.models import Order
from finance_app.models import DinifyTransaction
from users_app.models import User
from misc_app.controllers.clean_msisdn import internationalise_msisdn
from dinify_backend.configss.string_definitions import TransactionType_OrderPayment


class Command(BaseCommand):
    help = """
    - Determines the customers who have made purchases.
    - To be run every  minute
    """

    def match_customer(
        self,
        restaurant_country: str,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None
    ) -> Union[User, None]:
        user_phone = None
        user_email = None

        if customer_phone is None and customer_email is None:
            return None

        if customer_phone is not None:
            msisdn = internationalise_msisdn(
                country=restaurant_country,
                msisdn=customer_phone
            )
            try:
                user = User.objects.get(phone_number=msisdn)
                return user
            except User.DoesNotExist:
                user_phone = msisdn
            except Exception as error:
                print(f"Error matching customer based on phone: {error}")

        if customer_email is not None:
            try:
                user = User.objects.get(email=customer_email.strip())
                return user
            except User.DoesNotExist:
                user_email = customer_email.strip()
            except Exception as error:
                print(f"Error matching customer based on email: {error}")

        # create the user
        username = user_phone if user_phone is not None else user_email
        user = User.objects.create(
            phone_number=user_phone,
            email=user_email,
            username=username,
            country=restaurant_country
        )
        user.set_password(random.randint(100000, 999999))
        user.save()
        return user

    def handle(self, *args, **options):
        with transaction.atomic():
            # get the paid orders where the customer is null
            # TODO only consider paid orders
            orders = Order.objects.filter(
                customer=None,
                customer_match_attempted=False
            )
            for order in orders:
                customer_phone = None
                customer_email = None
                # if the customer phone and customer email are both null,
                # check if the order payments has any records to map to
                if order.customer_phone is None and order.customer_email is None:
                    order_payment = DinifyTransaction.objects.values(
                        'msisdn'
                    ).filter(
                        transaction_type=TransactionType_OrderPayment,
                        order=order,
                        msisdn__isnull=False
                    ).first()
                    if order_payment is not None:
                        customer_phone = order_payment['msisdn']

                customer = self.match_customer(
                    restaurant_country=order.restaurant.country,
                    customer_phone=customer_phone,
                    customer_email=customer_email
                )
                if customer is not None:
                    order.customer = customer
                    # order.customer_match_attempted = True
                    # order.save()
            print(orders.count())
