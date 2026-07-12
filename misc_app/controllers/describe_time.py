import datetime
from dateutil import parser
from django.utils import timezone

day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def describe_time() -> dict:
    # EAT wall clock as a naive value — corrects the date/hour fields while
    # preserving the dict shape (utc_offset/timezone stay None). A naive
    # datetime.now() on the UTC server would otherwise stamp UTC values.
    now = timezone.localtime(timezone.now()).replace(tzinfo=None)
    time_dict = {
        'date': now.day,
        'month': now.month,
        'month_name': now.strftime('%B'),
        'year': now.year,
        'hour': now.hour,
        'minute': now.minute,
        'second': now.second,
        'epoch_timestamp': now.timestamp(),
        'iso_format': now.isoformat(),
        'utc_offset': now.utcoffset(),
        'timezone': now.tzname(),
    }
    return time_dict


def describe_date_value(date_value: datetime.date) -> dict:
    time_dict = {
        'date': date_value.day,
        'month': date_value.month,
        'month_name': date_value.strftime('%B'),
        'year': date_value.year,
        'iso_format': date_value.isoformat(),
    }
    return time_dict


def break_down_time(datetime_string: str):
    """
    Breaks down the datetime string into its components
    """
    try:
        datetime_object = datetime.datetime.strptime(datetime_string, '%Y-%m-%dT%H:%M:%S.%f%z')
    except:
        datetime_object = parser.isoparse(datetime_string)

    time_detail = {}
    time_detail['date'] = datetime_object.day
    time_detail['month'] = datetime_object.month
    time_detail['year'] = datetime_object.year
    day = datetime_object.weekday()
    time_detail['day'] = day_names[day]
    time_detail['hour'] = datetime_object.hour
    time_detail['minute'] = datetime_object.minute
    time_detail['timestamp'] = datetime_object
    return time_detail
