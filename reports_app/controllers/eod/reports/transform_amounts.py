import logging
from decimal import Decimal

from bson import ObjectId
from dinify_backend.mongo_db import MONGO_DB

logger = logging.getLogger(__name__)


def transform_transaction_amounts():
    try:
        transactions = MONGO_DB['archive_transactions'].find({
            '$or': [
                {"transformed_amounts": {"$exists": False}},
                {"transformed_amounts": {"$eq": False}},
            ]
        })
    except Exception as e:
        logger.error("Failed to query archive_transactions for transform: %s", e)
        return

    amount_columns = [
        'amount_in', 'amount_out', 'transaction_amount',
        'tip_amount', 'transaction_collected_amount', 'gross_amount_paid',
        'customer_balance'
    ]

    for tx in transactions:
        amount_values = {}
        for column in amount_columns:
            amount_values[column] = Decimal(str(tx[column])).quantize(Decimal('0.01'))
        amount_values['transformed_amounts'] = True
        account_balances = tx.get('account_balances')
        if account_balances is not None:
            if account_balances.get('after') is not None:
                for key, value in tx['account_balances']['after'].items():
                    amount_values[f'account_balances.after.{key}'] = Decimal(str(value)).quantize(Decimal('0.01'))
                for key, value in tx['account_balances']['before'].items():
                    amount_values[f'account_balances.before.{key}'] = Decimal(str(value)).quantize(Decimal('0.01'))

        amount_values['transformed_amounts'] = True
        try:
            MONGO_DB['archive_transactions'].update_one(
                {"_id": ObjectId(tx['_id'])},
                {"$set": amount_values}
            )
        except Exception as e:
            logger.error("Failed to update transaction %s amounts: %s", tx.get('_id'), e)
            continue


def transform_account_amounts():
    try:
        accounts = MONGO_DB['archive_accounts'].find({
            '$or': [
                {"transformed_amounts": {"$exists": False}},
                {"transformed_amounts": {"$eq": False}},
            ]
        })
    except Exception as e:
        logger.error("Failed to query archive_accounts for transform: %s", e)
        return

    ignore_fields = ['archived']

    for account in accounts:
        amount_values = {}
        for key, value in account.items():
            try:
                if key in ignore_fields:
                    continue
                amount_values[key] = Decimal(str(value)).quantize(Decimal('0.01'))
            except Exception as error:
                logger.error("Error converting %s in account %s: %s", key, account['_id'], error)
        amount_values['transformed_amounts'] = True
        try:
            MONGO_DB['archive_accounts'].update_one(
                {"_id": ObjectId(account['_id'])},
                {"$set": amount_values}
            )
        except Exception as e:
            logger.error("Failed to update account %s amounts: %s", account.get('_id'), e)
            continue


def transform_order_amounts():
    """
    converts values from strings to decimal values
    """
    try:
        orders = MONGO_DB['archive_orders'].find({
            '$or': [
                {"transformed_amounts": {"$exists": False}},
                {"transformed_amounts": {"$eq": False}},
            ]
        })
    except Exception as e:
        logger.error("Failed to query archive_orders for transform: %s", e)
        return

    amount_columns = [
        'actual_cost', 'balance_payable', 'discounted_cost',
        'savings', 'total_cost', 'total_paid'
    ]

    for order in orders:
        amount_values = {}
        for column in amount_columns:
            if column in order:
                amount_values[column] = Decimal(str(order[column])).quantize(Decimal('0.01'))
        amount_values['transformed_amounts'] = True
        try:
            MONGO_DB['archive_orders'].update_one(
                {"_id": ObjectId(order['_id'])},
                {"$set": amount_values}
            )
        except Exception as e:
            logger.error("Failed to update order %s amounts: %s", order.get('_id'), e)
            continue


def transform_amounts():
    """
    converts values from strings to decimal values
    """
    transform_transaction_amounts()
    transform_account_amounts()
    transform_order_amounts()
