from decimal import Decimal
from finance_app.models import DinifyAccount, DinifyTransaction
from users_app.models import User
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    TransactionType_OrderPayment,
    TransactionStatus_Success,
    PaymentStatus_Paid,
    OrderItemStatus_Served,
    AccountType_User,
    TransactionType_Tip,
    ProcessingStatus_Done
)
from misc_app.controllers.clean_amount import clean_amount
from dinify_backend.configss.messages import OK_ORDER_PAYMENT_PROCESSED
from finance_app.controllers.update_wallet_balance import update_wallet_balance


def process_order_payment(
    transaction_record: DinifyTransaction,
    transaction_status: str
) -> bool:
    """
    Process the status of the order payment
    """
    order = Order.objects.select_for_update().get(id=transaction_record.order.id)
    if transaction_status == TransactionStatus_Success:
        # TODO check if the cumulative amount paid is equal to the order amount
        total_paid = order.total_paid
        total_paid += transaction_record.transaction_amount
        balance_payable = clean_amount(Decimal(order.total_cost)) - total_paid
        order.total_paid = total_paid
        order.balance_payable = balance_payable

        if order.balance_payable <= clean_amount(Decimal(1.00)):
            order.payment_status = PaymentStatus_Paid
            if order.order_status == OrderItemStatus_Served:
                order.order_status = "Paid"
        order.save()
        return True

    # TODO check if a tip is included and add it to the waiter's account.
    if transaction_record.tip_amount > Decimal(0.00):
        collect_tip(
            waiter=order.waiter,
            amount=transaction_record.tip_amount,
            order_payment=transaction_record
        )


def collect_tip(
    waiter: User,
    amount: Decimal,
    order_payment: DinifyTransaction
) -> bool:
    # check if the waiter already has a wallet
    # if not create a wallet for the waiter
    # add the tip to the waiter's wallet
    waiter_account = None
    try:
        waiter_account = DinifyAccount.objects.select_for_update().get(
            user=waiter,
        )
    except DinifyAccount.DoesNotExist:
        waiter_account = DinifyAccount.objects.create(
            user=waiter,
            account_type=AccountType_User
        )

    if waiter_account is None:
        raise Exception('No waiter accunt found')

    balance_update = update_wallet_balance(
        id=str(waiter_account.id),
        mode=order_payment.payment_mode,
        credit=amount
    )

    # record tip transaction
    tip_transaction = DinifyTransaction.objects.create(
        account=waiter_account,
        order=order_payment.order,
        transaction_type=TransactionType_Tip,
        transaction_status=TransactionStatus_Success,
        transaction_platform=order_payment.transaction_platform,

        transaction_amount=amount,
        tip_amount=amount,
        transaction_collected_amount=amount,
        msisdn=order_payment.msisdn,
        payment_form=order_payment.payment_form,
        source_order_payment=order_payment,

        aggregator='Dinify',
        payment_mode=order_payment.payment_mode,
        account_balances=balance_update,

        processed=ProcessingStatus_Done
    )
