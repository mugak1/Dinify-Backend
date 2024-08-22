from decimal import Decimal
from typing import Optional
from finance_app.models import Wallet
from misc_app.controllers.clean_amount import clean_amount
from dinify_backend.configss.string_definitions import (
    PaymentMode_Card,
    PaymentMode_Cash,
    PaymentMode_MobileMoney
)


def update_wallet_balance(
    id: str,
    mode: str,
    credit: Optional[Decimal] = Decimal(0.0),
    debit: Optional[Decimal] = Decimal(0.0),
    credit_charge: Optional[Decimal] = Decimal(0.0),
    debit_charge: Optional[Decimal] = Decimal(0.0),
    predebit: Optional[Decimal] = Decimal(0.0),
) -> dict:
    """
    - Should be called in an atomic transaction
    - Updates the wallet balance and cumulative amounts for the wallet.
    """
    wallet = Wallet.objects.select_for_update().get(id=id)

    old_balances = {
        'momo_actual_balance': wallet.momo_actual_balance,
        'momo_available_balance': wallet.momo_available_balance,
        'momo_cumulative_in': wallet.momo_cumulative_in,
        'momo_cumulative_out': wallet.momo_cumulative_out,
        'momo_cumulative_in_charges': wallet.momo_cumulative_in_charges,
        'momo_cumulative_out_charges': wallet.momo_cumulative_out_charges,
        'momo_cumulative_refunds': wallet.momo_cumulative_refunds,
        'momo_cumulative_disbursements': wallet.momo_cumulative_disbursements,

        'card_actual_balance': wallet.card_actual_balance,
        'card_available_balance': wallet.card_available_balance,
        'card_cumulative_in': wallet.card_cumulative_in,
        'card_cumulative_out': wallet.card_cumulative_out,
        'card_cumulative_in_charges': wallet.card_cumulative_in_charges,
        'card_cumulative_out_charges': wallet.card_cumulative_out_charges,
        'card_cumulative_refunds': wallet.card_cumulative_refunds,
        'card_cumulative_disbursements': wallet.card_cumulative_disbursements,

        'cash_actual_balance': wallet.cash_actual_balance,
        'cash_available_balance': wallet.cash_available_balance,
        'cash_cumulative_in': wallet.cash_cumulative_in,
        'cash_cumulative_out': wallet.cash_cumulative_out,
        'cash_cumulative_in_charges': wallet.cash_cumulative_in_charges,
        'cash_cumulative_out_charges': wallet.cash_cumulative_out_charges,
        'cash_cumulative_refunds': wallet.cash_cumulative_refunds,
        'cash_cumulative_disbursements': wallet.cash_cumulative_disbursements
    }

    credit_amount = clean_amount(credit)
    debit_amount = clean_amount(debit)

    if mode == PaymentMode_MobileMoney:
        wallet.momo_actual_balance += credit_amount - debit_amount - debit_charge
        wallet.momo_available_balance += credit_amount - debit_amount - debit_charge - predebit
        wallet.momo_cumulative_in += credit_amount
        wallet.momo_cumulative_out += debit_amount
        wallet.momo_cumulative_in_charges += credit_charge
        wallet.momo_cumulative_out_charges += debit_charge
    elif mode == PaymentMode_Card:
        wallet.card_actual_balance += credit_amount - debit_amount - debit_charge
        wallet.card_available_balance += credit_amount - debit_amount - debit_charge - predebit
        wallet.card_cumulative_in += credit_amount
        wallet.card_cumulative_out += debit_amount
        wallet.card_cumulative_in_charges += credit_charge
        wallet.card_cumulative_out_charges += debit_charge
    elif mode == PaymentMode_Cash:
        wallet.cash_actual_balance += credit_amount - debit_amount - debit_charge
        wallet.cash_available_balance += credit_amount - debit_amount - debit_charge - predebit
        wallet.cash_cumulative_in += credit_amount
        wallet.cash_cumulative_out += debit_amount
        wallet.cash_cumulative_in_charges += credit_charge
        wallet.cash_cumulative_out_charges += debit_charge

    wallet.save()

    new_balances = {
        'actual_balance': wallet.actual_balance,
        'cum_credit': wallet.cum_credit,
        'cum_debit': wallet.cum_debit
    }

    # convert the values to strings
    formatted_old_balances = {key: str(value) for key, value in old_balances.items()}
    formatted_new_balances = {key: str(value) for key, value in new_balances.items()}

    return {
        'before': old_balances,
        'after': new_balances,
        'formatted_values': {
            'before': formatted_old_balances,
            'after': formatted_new_balances
        }
    }
