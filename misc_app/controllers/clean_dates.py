from datetime import datetime, date
from typing import Union


def clean_dates(
    date_from: Union[str, date],
    date_to: Union[str, date]
) -> dict:
    try:
        date_from = datetime.strptime(date_from, '%Y-%m-%d').date() if type(date_from) == str else date_from # noqa
        date_to = datetime.strptime(date_to, '%Y-%m-%d').date() if type(date_to) == str else date_to # noqa
    except ValueError:
        return {
            'status': 400,
            'message': 'Invalid dates provided'
        }
    # Ensure date_to is not before date_from
    if date_to < date_from:
        return {
            'status': 400,
            'message': 'The date to should not be earlier than the date from'
        }
    return {
        'status': 200,
        'date_from': date_from,
        'date_to': date_to
    }
