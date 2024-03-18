from decimal import Decimal


def clean_amount(amount: Decimal) -> Decimal:
    """
    - Quantize the amount to 2 decimal places.
    """
    return amount.quantize(Decimal("0.01"))
