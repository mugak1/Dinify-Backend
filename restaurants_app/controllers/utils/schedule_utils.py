from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings


# ISO weekday number (Monday=1..Sunday=7) -> string code matching the frontend.
ISO_WEEKDAY_TO_CODE = {
    1: 'mon', 2: 'tue', 3: 'wed', 4: 'thu',
    5: 'fri', 6: 'sat', 7: 'sun',
}


def _to_minutes(hhmm: str) -> Optional[int]:
    """Parse 'HH:MM' to minutes-since-midnight; return None on malformed input."""
    try:
        hh, mm = hhmm.split(':')
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def _is_time_in_window(current_min: int, start_min: int, end_min: int) -> bool:
    """True if current_min falls inside [start, end), with overnight support.

    Mirrors frontend logic in src/app/restaurant-mgt/menu/utils/schedule-utils.ts.
    """
    if start_min <= end_min:
        return start_min <= current_min < end_min
    # Overnight window (e.g. 22:00-02:00): valid if past start OR before end.
    return current_min >= start_min or current_min < end_min


def is_section_currently_active(section, now: Optional[datetime] = None) -> bool:
    """Whether a MenuSection should be visible right now.

    Returns True if:
    - availability == 'always', OR
    - availability == 'scheduled' AND schedules is empty (safe fallback), OR
    - availability == 'scheduled' AND now falls within at least one slot.

    Day codes are strings matching the frontend's ScheduleDay domain
    ('mon'..'sun'). Time windows handle overnight spans (e.g. 22:00-02:00).
    Evaluation uses settings.TIME_ZONE.

    THE SUPPLIED INSTANT IS CONVERTED, NOT TRUSTED (D06 completion, G2-A). A
    schedule is a statement about the restaurant's own wall clock — "we serve
    lunch from 12:00" — so the day and hour must be read in `settings.TIME_ZONE`
    whatever zone the caller's instant is expressed in. This used to be the
    CALLER's job and only one caller knew it: `handle_show_menu` passes
    `timezone.localtime()`, while D06's acceptance and retire-for-review
    boundaries pass `timezone.now()` (UTC). At UTC+3 that read every window three
    hours out at the moment a draft becomes food — refusing an order at 12:30
    local because 09:30 UTC is before noon, and accepting one at 15:30 local
    because 12:30 UTC is not yet three.

    Converting HERE rather than at each call site is deliberate: it is one
    boundary that cannot be forgotten by the next caller, and it is a no-op for
    the read path, which already hands over a local instant. It does NOT sample a
    second clock — the caller's protected decision instant is preserved exactly,
    only re-expressed — so a decision still describes the single moment its
    locks were held at.

    A NAIVE value is read as already-local, which is what it meant before; only
    test callers supply one, and raising would turn a tolerated input into a 500.

    `now` is optional: production callers on the schedule-display path omit it.
    """
    if section.availability != 'scheduled':
        return True

    schedules = section.schedules
    if not schedules or not isinstance(schedules, list):
        return True  # Scheduled mode with no slots: keep section visible.

    tz = ZoneInfo(settings.TIME_ZONE)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is not None:
        now = now.astimezone(tz)

    current_code = ISO_WEEKDAY_TO_CODE.get(now.isoweekday())
    current_min = now.hour * 60 + now.minute

    for slot in schedules:
        if not isinstance(slot, dict):
            continue
        days = slot.get('days')
        if not isinstance(days, list) or current_code not in days:
            continue
        start_min = _to_minutes(slot.get('startTime', ''))
        end_min = _to_minutes(slot.get('endTime', ''))
        if start_min is None or end_min is None:
            continue
        if _is_time_in_window(current_min, start_min, end_min):
            return True

    return False
