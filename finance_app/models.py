from django.db import models
from django.core.exceptions import ValidationError
from users_app.models import BaseModel
from restaurants_app.models import Restaurant
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    PaymentMode_Cash, PaymentMode_MobileMoney, PaymentMode_Card,
    TransactionType_OrderPayment, TransactionType_OrderRefund, TransactionType_OrderCharge, TransactionType_Disbursement, TransactionType_Subscription,  # noqa
    TransactionStatus_Success, TransactionStatus_Failed, TransactionStatus_Pending, TransactionStatus_Initiated,  # noqa
    TransactionPlatform_Web,
    ProcessingStatus_Pending,
    PaymentForm_Full
)

PAYMENT_MODES = [PaymentMode_Cash, PaymentMode_MobileMoney, PaymentMode_Card]  # noqa
TRANSACTION_TYPES = [TransactionType_OrderPayment, TransactionType_OrderRefund, TransactionType_OrderCharge, TransactionType_Disbursement, TransactionType_Subscription]  # noqa
TRANSACTION_STATUSES = [TransactionStatus_Success, TransactionStatus_Failed, TransactionStatus_Pending, TransactionStatus_Initiated]  # noqa
TRANSACTION_PLATFORMS = [TransactionPlatform_Web]


# Retained as no-ops solely because finance_app/migrations/0001_initial.py references them by path; no model uses them anymore.
def validate_account_type(value):
    return None


def validate_payment_mode(value):
    if value not in PAYMENT_MODES:
        raise ValidationError(f"{value} is not a valid payment mode.")


# Retained as no-ops solely because finance_app/migrations/0001_initial.py references them by path; no model uses them anymore.
def validate_account_status(value):
    return None


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
class DinifyTransaction(BaseModel):
    """
    the transactions on the platform
    """
    # for direct subscriptions
    restaurant = models.ForeignKey(Restaurant, on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)

    transaction_type = models.CharField(validators=[validate_transaction_type], max_length=255, db_index=True)  # noqa
    transaction_status = models.CharField(validators=[validate_transaction_status], max_length=255, db_index=True, default=TransactionStatus_Initiated)  # noqa
    transaction_platform = models.CharField(validators=[validate_transaction_platform], max_length=255, db_index=True)  # noqa
    processing_status = models.CharField(default=ProcessingStatus_Pending, max_length=255, db_index=True)  # noqa

    transaction_amount = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    transaction_collected_amount = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)
    msisdn = models.CharField(max_length=255, null=True, blank=True)
    payment_form = models.CharField(max_length=20, default=PaymentForm_Full)
    amount_in = models.DecimalField(default=0.0, max_digits=50, decimal_places=2)

    # aggregator details
    aggregator = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    aggregator_reference = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    payment_mode = models.CharField(validators=[validate_payment_mode], max_length=255, null=True, blank=True)  # noqa
    aggregator_status = models.CharField(max_length=255, null=True, blank=True)
    aggregator_misc_details = models.JSONField(default=dict)

    # for manual payments
    manual_payment = models.BooleanField(default=False)
    manual_payment_details = models.JSONField(null=True)

    class Meta:
        db_table = 'transactions'