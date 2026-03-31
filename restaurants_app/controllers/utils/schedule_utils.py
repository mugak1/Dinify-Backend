from datetime import datetime
import pytz


def is_section_currently_active(section) -> bool:
    """
    Evaluate whether a MenuSection should be visible right now
    based on its availability setting and schedule slots.

    Returns True if:
    - availability == 'always', OR
    - availability == 'scheduled' AND current time falls within at least one schedule slot

    Uses Africa/Kampala timezone for evaluation.
    """
    if section.availability != 'scheduled':
        return True

    schedules = section.schedules
    if not schedules or not isinstance(schedules, list):
        return True  # No schedules defined = always available (safe fallback)

    tz = pytz.timezone('Africa/Kampala')
    now = datetime.now(tz)
    current_day = now.isoweekday()  # 1=Monday, 7=Sunday
    current_time = now.strftime('%H:%M')

    for slot in schedules:
        days = slot.get('days', [])
        start_time = slot.get('startTime', '00:00')
        end_time = slot.get('endTime', '23:59')

        if current_day in days and start_time <= current_time <= end_time:
            return True

    return False
