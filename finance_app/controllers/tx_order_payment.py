import logging
from typing import Optional
from decimal import Decimal
from finance_app.models import DinifyAccount, DinifyTransaction
from users_app.models import User
from orders_app.models import Order
from misc_app.controllers.clean_amount import clean_amount
from dinify_backend.configss.string_definitions import (
    AccountType_Restaurant,
    TransactionType_OrderPayment,
    TransactionPlatform_Web,
    PaymentForm_Split, PaymentForm_Full,
    TransactionStatus_Initiated,
    ProcessingStatus_Pending,
    ProcessingStatus_Confirmed,
    PaymentMode_MobileMoney
)
from users_app.controllers.otp_manager import OtpManager

logger = logging.getLogger(__name__)


class OrderPaymentTransaction:
    def __init__(self):
        pass

    def initiate(
        self,
        order: Order,
        payment_mode: str,
        transaction_platform=TransactionPlatform_Web,
        payment_form=PaymentForm_Full,
        msisdn: Optional[str] = None,
        amount: Optional[int] = None,
        user: Optional[User] = None,
        manual_payment: Optional[bool] = False,
        manual_payment_details: Optional[dict] = None,
        otp: Optional[str] = None
    ) -> dict:
        try:
            account = DinifyAccount.objects.get(restaurant=order.restaurant)
        except DinifyAccount.DoesNotExist:
            account = DinifyAccount.objects.create(
                account_type=AccountType_Restaurant,
                restaurant=order.restaurant
            )

        transaction_amount = clean_amount(Decimal(str(order.actual_cost))) if payment_form is PaymentForm_Full else clean_amount(Decimal(str(amount))) # noqa
        if transaction_amount is None:
            return {
                'status': 400,
                'message': 'Invalid transaction amount'
            }

        if payment_form == PaymentForm_Split:
            logger.debug("inside split payment form, amount: %s", amount)
            if amount is None:
                return {
                    'status': 400,
                    'message': 'Specify an amount for the split payment.'
                }

        if payment_form == PaymentForm_Split:
            if transaction_amount >= clean_amount(Decimal(str(order.actual_cost))):
                return {
                    'status': 400,
                    'message': 'The split payment amount should be less than the order amount.'
                }

        logger.debug("%s - %s - %s - %s", order.pk, amount, transaction_amount, payment_form)
        # return {
        #     'status': 400,
        #     'message': 'Blocking all payments for now.',
        #     'data': {
        #         'order': str(order.pk),
        #         'amount': amount,
        #         'transaction_amount': transaction_amount,
        #         'payment_form': payment_form
        #     }
        # }
        if amount is None:
            return {
                'status': 400,
                'message': 'Invalid amount'
            }

        # determine of the verify otp
        check_otp = False

        if not manual_payment and payment_mode == PaymentMode_MobileMoney:
            try:
                User.objects.get(username=msisdn)
            except Exception as error:
                logger.error("Error checking for msisdn when initiating payment: %s", error)
                check_otp = True

        if manual_payment:
            check_otp = True

        # print(manual_payment, not manual_payment, payment_mode, payment_mode is PaymentMode_MobileMoney)

        # check_otp = True
        if check_otp:
            if otp is None:
                return {
                    'status': 400,
                    'message': 'Please provide the OTP.'
                }

            otp_verification = OtpManager().verify_otp(
                user_id=str(user.id) if user is not None else None,
                otp=otp,
                msisdn=msisdn
            )
            if not otp_verification['data']['valid']:
                return {
                    'status': 400,
                    'message': 'Invalid OTP.'
                }

        # determine the amount to collect based on the aggregator charges
        amount_collectable = transaction_amount

        processing_status = ProcessingStatus_Pending
        if manual_payment:
            processing_status = ProcessingStatus_Confirmed

        # make a transaction record for the payment
        order_payment = DinifyTransaction.objects.create(
            account=account,
            order=order,
            restaurant=order.restaurant,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Initiated,
            transaction_platform=transaction_platform,
            processing_status=processing_status,
            transaction_amount=transaction_amount,
            transaction_collected_amount=amount_collectable,
            msisdn=msisdn,
            payment_mode=payment_mode,
            payment_form=payment_form,
            created_by=user,
            manual_payment=manual_payment,
            manual_payment_details=manual_payment_details
        )

        # [8b] provider collection call goes here

        message = 'The order payment has been initiated.'
        return {
            'status': 200,
            'message': message,
            'data': {
                "transaction_id": str(order_payment.id)
            }
        }
