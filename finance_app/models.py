from django.db import models
from django.core.exceptions import ValidationError
from users_app.models import BaseModel, User
from restaurants_app.models import Restaurant
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    AccountType_Restaurant, AccountType_DinifyRevenue, AccountType_User,
    PaymentMode_Cash, PaymentMode_MobileMoney, PaymentMode_Card, PaymentMode_Ova,
    AccountStatus_Active, AccountStatus_Inactive, AccountStatus_Blocked,
    TransactionType_OrderPayment, TransactionType_OrderRefund, TransactionType_OrderCharge, TransactionType_Disbursement, TransactionType_Subscription,  # noqa
    TransactionStatus_Success, TransactionStatus_Failed, TransactionStatus_Pending, TransactionStatus_Initiated,  # noqa
    TransactionPlatform_Web, 
    ProcessingStatus_Pending,
    PaymentForm_Full
)

ACCOUNT_TYPES = [AccountType_Restaurant, AccountType_DinifyRevenue, AccountType_User]
PAYMENT_MODES = [PaymentMode_Cash, PaymentMode_MobileMoney, PaymentMode_Card, PaymentMode_Ova]
ACCOUNT_STATUSES = [AccountStatus_Active, AccountStatus_Inactive, AccountStatus_Blocked]
TRANSACTION_TYPES = [TransactionType_OrderPayment, TransactionType_OrderRefund, TransactionType_OrderCharge, TransactionType_Disbursement, TransactionType_Subscription]  # noqa
TRANSACTION_STATUSES = [TransactionStatus_Success, TransactionStatus_Failed, TransactionStatus_Pending, TransactionStatus_Initiated]  # noqa
TRANSACTION_PLATFORMS = [TransactionPlatform_Web]


def validate_account_type(value):
    if value not in ACCOUNT_TYPES:
        raise ValidationError(f"{value} is not a valid account type.")


def validate_payment_mode(value):
    if value not in PAYMENT_MODES:
        raise ValidationError(f"{value} is not a valid payment mode.")


def validate_account_status(value):
    if value not in ACCOUNT_STATUSES:
        raise ValidationError(f"{value} is not a valid account status.")


def validate_transaction_type(value):
    if value not in TRANSACTION_TYPES:
        raise ValidationError(f"{value} is not a valid transaction type.")


def validate_transaction_status(value):
    if value not in TRANSACTION_STATUSES:
        raise ValidationError(f"{value} is not a valid transaction status.")


def validate_transaction_platform(value):
    if value not in TRANSACTION_PLATFORMS:
        raise ValidationError(f"{value} is not a valid transaction platform.")


# Create your models here.
class DinifyAccount(BaseModel):
    """
    the accounts held at Dinify
    """
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        null=True
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True
    )  # to facilitate waiter tips

    account_currency = models.CharField(default="UGX", max_length=10, db_index=True)
    account_type = models.CharField(validators=[validate_account_type], max_length=255, db_index=True)  # noqa
    account_status = models.CharField(validators=[validate_account_status], default=AccountStatus_Active, max_length=20)  # noqa

    # account amounts
    # mobile money
    momo_actual_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    momo_available_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    momo_cumulative_in = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    momo_cumulative_out = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)

    momo_cumulative_in_charges = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    momo_cumulative_out_charges = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    momo_cumulative_refunds = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    momo_cumulative_disbursements = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)  # noqa

    # card i.e. bank
    card_actual_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    card_available_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    card_cumulative_in = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    card_cumulative_out = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)

    card_cumulative_in_charges = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    card_cumulative_out_charges = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    card_cumulative_refunds = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    card_cumulative_disbursements = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)  # noqa

    # cash collections
    cash_actual_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    cash_available_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    cash_cumulative_in = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    cash_cumulative_out = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)

    cash_cumulative_in_charges = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    cash_cumulative_out_charges = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    cash_cumulative_refunds = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)
    cash_cumulative_disbursements = models.DecimalField(default=0.0, max_digits=100, decimal_places=2)  # noqa

    class Meta:
        """
        the metadata for the DinifyAccount model
        """
        db_table = 'accounts'


class DinifyTransaction(BaseModel):
    """
    the transactions on the platform
    """
    account = models.ForeignKey(DinifyAccount, on_delete=models.CASCADE)
    # for direct subscriptions
    restaurant = models.ForeignKey(Restaurant, on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)

    transaction_type = models.CharField(validators=[validate_transaction_type], max_length=255, db_index=True)  # noqa
    transaction_status = models.CharField(validators=[validate_transaction_status], max_length=255, db_index=True, default=TransactionStatus_Initiated)  # noqa
    transaction_platform = models.CharField(validators=[validate_transaction_platform], max_length=255, db_index=True)  # noqa
    processing_status = models.CharField(default=ProcessingStatus_Pending, max_length=255, db_index=True)  # noqa

    transaction_amount = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    tip_amount = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    transaction_collected_amount = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    msisdn = models.CharField(max_length=255, null=True, blank=True)
    payment_form = models.CharField(max_length=20, default=PaymentForm_Full)
    amount_in = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    amount_out = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)

    # for orders and revenue collection
    parent_transaction = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        related_name='ref_parent_transaction'
    )

    # aggregator details
    aggregator = models.CharField(max_length=255, null=True, blank=True)
    aggregator_reference = models.CharField(max_length=255, null=True, blank=True)
    payment_mode = models.CharField(validators=[validate_payment_mode], max_length=255, null=True, blank=True)  # noqa
    aggregator_status = models.CharField(max_length=255, null=True, blank=True)
    aggregator_misc_details = models.JSONField(default=dict)

    # the account balances/amounts will be tracked using a json
    account_balances = models.JSONField(default=dict)

    # for manual payments
    manual_payment = models.BooleanField(default=False)
    manual_payment_details = models.JSONField(null=True)
    gross_amount_paid = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    customer_balance = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)

    # to track the processing of the transaction
    processed = models.CharField(max_length=25, default=ProcessingStatus_Pending)  # i.e. accounts updated, revenue collected, etc.  # noqa

    # for restaurants where Dinify has surcharge
    revenue_collected = models.BooleanField(default=False, db_index=True)  # i.e. revenue collected from the transaction  # noqa

    class Meta:
        db_table = 'transactions'
