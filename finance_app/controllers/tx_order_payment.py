from typing import Optional
from decimal import Decimal
from django.db import transaction
from finance_app.models import DinifyAccount, DinifyTransaction
from users_app.models import User
from orders_app.models import Order
from misc_app.controllers.clean_amount import clean_amount
from dinify_backend.configss.string_definitions import (
    AccountType_Restaurant,
    TransactionType_OrderPayment,
    TransactionPlatform_Web,
    PaymentForm_Split, PaymentForm_Full,
    TransactionStatus_Success, TransactionStatus_Initiated,
    PaymentStatus_Paid,
    OrderStatus_Paid,
    OrderStatus_Served,
    OrderItemStatus_Served,
    AccountType_User,
    TransactionType_Tip,
    ProcessingStatus_Done, ProcessingStatus_Pending,
    ProcessingStatus_Confirmed,
    ProcessingStatus_Failed,
    PaymentMode_Cash, PaymentMode_Card, PaymentMode_MobileMoney
)
from finance_app.controllers.tx_tip import TipTransaction
from users_app.controllers.otp_manager import OtpManager
from payment_integrations_app.controllers.yo_integrations import YoIntegration
from payment_integrations_app.controllers.dpo import DpoIntegration
from dinify_backend.configss.messages import (
    OK_ORDER_PAYMENT_INITIATED,
    ERR_ORDER_PAYMENT_INITIATION
)
from finance_app.controllers.update_wallet_balance import update_wallet_balance


class OrderPaymentTransaction:
    def __init__(self):
        pass

    def initiate(
        self,
        order: Order,
        tip_amount: int,
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

        transaction_amount = clean_amount(Decimal(order.actual_cost)) if payment_form is PaymentForm_Full else clean_amount(Decimal(amount)) # noqa
        if transaction_amount is None:
            return {
                'status': 400,
                'message': 'Invalid transaction amount'
            }
        tip_amount = clean_amount(Decimal(tip_amount))

        if payment_form == PaymentForm_Split:
            print("inside split payment form", amount)
            if amount is None:
                return {
                    'status': 400,
                    'message': 'Specify an amount for the split payment.'
                }

        if payment_form == PaymentForm_Split:
            if transaction_amount >= clean_amount(Decimal(order.actual_cost)):
                return {
                    'status': 400,
                    'message': 'The split payment amount should be less than the order amount.'
                }

        print(f"{str(order.pk)} - {amount} - {transaction_amount} - {payment_form}")
        return {
            'status': 400,
            'message': 'Blocking all payments for now.',
            'data': {
                'order': str(order.pk),
                'amount': amount,
                'transaction_amount': transaction_amount,
                'payment_form': payment_form
            }
        }
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
                print(f"Error checking for msisdn when initiating payment: {error}")
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
        amount_collectable = transaction_amount + tip_amount
        if payment_mode is PaymentMode_MobileMoney:
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
            tip_amount=tip_amount,
            transaction_collected_amount=amount_collectable,
            msisdn=msisdn,
            payment_mode=payment_mode,
            payment_form=payment_form,
            created_by=user,
            manual_payment=manual_payment,
            manual_payment_details=manual_payment_details
        )

        if payment_mode == PaymentMode_MobileMoney and not manual_payment:
            collection = YoIntegration().momo_collect(
                transaction_amount=int(amount_collectable),
                msisdn=msisdn,
                transaction_id=str(order_payment.id)
            )
            if collection:
                return {
                    'status': 200,
                    'message': OK_ORDER_PAYMENT_INITIATED,
                    'data': {
                        "transaction_id": str(order_payment.id)
                    }
                }
            else:
                return {
                    'status': 400,
                    'message': ERR_ORDER_PAYMENT_INITIATION,
                    'data': {
                        "transaction_id": str(order_payment.id)
                    }
                }

        if payment_mode == PaymentMode_Card and not manual_payment:
            dpo_token = DpoIntegration().create_token(
                amount=int(amount_collectable),
                currency=account.account_currency,
                transaction_reference=str(order_payment.id),
                timestamp=str(order_payment.time_created),
            )

            if dpo_token is not None:
                return {
                    'status': 200,
                    'message': 'The payment has been initiated successfully.',
                    'data': {
                        "transaction_id": str(order_payment.id),
                        "dpo_token": dpo_token,
                        "redirect_url": dpo_token
                    }
                }
            else:
                return {
                    'status': 400,
                    'message': 'Sorry, an error occurred while initiating the payment. Please try again.',  # noqa
                    'data': {
                        "transaction_id": str(order_payment.id)
                    }
                }

        message = 'The order payment has been initiated.'
        return {
            'status': 200,
            'message': message,
            'data': {
                "transaction_id": str(order_payment.id)
            }
        }

    def process(self, transaction_id: str):
        with transaction.atomic():
            txs_record = DinifyTransaction.objects.select_for_update().get(id=transaction_id)
            order = Order.objects.select_for_update().get(id=txs_record.order.id)

            if txs_record.processing_status == ProcessingStatus_Confirmed:
                txs_record.transaction_status = TransactionStatus_Success
                txs_record.processing_status = ProcessingStatus_Done

                # TODO update account balances
                balance_update = update_wallet_balance(
                    id=str(txs_record.account.id),
                    mode=txs_record.payment_mode,
                    credit=txs_record.transaction_amount
                )
                txs_record.account_balances = balance_update
                txs_record.save()

                # TODO check if the cumulative amount paid is equal to the order amount
                total_paid = order.total_paid
                total_paid += txs_record.transaction_amount
                balance_payable = clean_amount(Decimal(order.total_cost)) - total_paid
                order.total_paid = total_paid
                order.balance_payable = balance_payable

                if order.balance_payable <= clean_amount(Decimal(1.00)):
                    order.payment_status = PaymentStatus_Paid
                    if order.order_status == OrderStatus_Served:
                        order.order_status = OrderStatus_Paid
                order.save()

                if txs_record.tip_amount > Decimal(0.00):
                    return TipTransaction().initiate(
                        waiter=order.waiter,
                        order_payment=txs_record,
                        amount=txs_record.tip_amount
                    )
                return True

            elif txs_record.processing_status == ProcessingStatus_Failed:
                txs_record.transaction_status = ProcessingStatus_Failed
                txs_record.processing_status = ProcessingStatus_Done
                # TODO update account balances
                balance_update = update_wallet_balance(
                    id=str(txs_record.account.id),
                    mode=txs_record.payment_mode,
                    credit=Decimal(0.00)
                )
                txs_record.account_balances = balance_update
                txs_record.save()
                return False
