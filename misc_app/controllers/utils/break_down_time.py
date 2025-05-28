from datetime import datetime
from dateutil import parser
day_names = [
    'Mon', 'Tue', 'Wed',
    'Thu', 'Fri', 'Sat',
    'Sun'
]


def append_time_details(data: dict, just_return=None) -> dict:
    """
    - Adds the date, year, month, day, timestamp to the ime provided
    """

    right_now = datetime.now()
    time_detail = {}
    if just_return:
        time_detail['date'] = right_now.day
        time_detail['month'] = right_now.month
        time_detail['year'] = right_now.year
        day = right_now.weekday()
        time_detail['day'] = day_names[day]
        time_detail['hour'] = right_now.hour
        time_detail['minute'] = right_now.minute
        time_detail['timestamp'] = right_now
        return time_detail

    data['date'] = right_now.day
    data['month'] = right_now.month
    data['year'] = right_now.year
    day = right_now.weekday()
    data['day'] = day_names[day]
    data['hour'] = right_now.hour
    data['minute'] = right_now.minute
    data['timestamp'] = right_now
    return data



def break_down_time(datetime_string: str):
    """
    Breaks down the datetime string into its components
    """
    try:
        datetime_object = datetime.strptime(datetime_string, '%Y-%m-%dT%H:%M:%S.%f%z')
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
