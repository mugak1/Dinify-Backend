from datetime import datetime


def clean_dates(date_from: str, date_to: str) -> dict:
    try:
        date_from = datetime.strptime(date_from, '%Y-%m-%d').date()
        date_to = datetime.strptime(date_to, '%Y-%m-%d').date()
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
