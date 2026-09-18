"""
D06 completion, G2-A — THE PROTECTED DECISION READS THE SCHEDULE IN THE
RESTAURANT'S OWN ZONE.

WHAT WAS WRONG. `is_section_currently_active` reads `.isoweekday()`, `.hour` and
`.minute` off the instant it is HANDED, converting only when it samples the clock
itself. The diner menu read knew that and passed `timezone.localtime()`; D06's
two new protected decisions — acceptance and retire-for-review — pass
`timezone.now()`, which is UTC. Africa/Nairobi is UTC+3, so every scheduled
section was evaluated three hours out at the one moment that turns a draft into
food.

BOTH DIRECTIONS ARE REAL, and each is a different kind of harm:

  local 12:30, UTC 09:30, window 12:00-15:00   the section IS serving
                                               -> was REFUSED for review
  local 15:30, UTC 12:30, window 12:00-15:00   the section has CLOSED
                                               -> was ACCEPTED and sent to the
                                                  kitchen

The first is a diner turned away from a restaurant that is open. The second is
the one that reaches a kitchen: an order accepted against a section that had
stopped serving half an hour earlier.

WHAT THE FIX IS, AND WHAT IT IS NOT. The conversion happens ONCE, inside the
helper, so every caller present and future reads the schedule in the configured
zone. No caller samples a second clock — the protected decision instant is
converted, not re-read — and expiry stays an absolute-time comparison, because a
deadline is a duration from an anchor and has no opinion about local hours.
Overnight windows, the day-code mapping and the "scheduled with no slots stays
visible" fallback are untouched.

THESE ARE ENDPOINT-LEVEL REGRESSIONS, not a copy of the helper. Each drives the
real acceptance or retirement path with a real draft and asserts the outcome the
diner would get.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import OrderStatus_Pending
from orders_app.controllers.manage_order import retire_quote_for_review
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.purchase_integrity import (
    REASON_PURCHASE_NEEDS_REVIEW,
)
from orders_app.models import Order, OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin
from restaurants_app.controllers.diner_capability import capability_from_table
from restaurants_app.controllers.utils.schedule_utils import (
    is_section_currently_active,
)

LOCAL = ZoneInfo(settings.TIME_ZONE)
UTC = ZoneInfo('UTC')

#: A Monday, chosen so the weekday code is fixed and readable. Every instant
#: below is built from it explicitly rather than derived from the wall clock, so
#: these tests mean the same thing whenever they run.
MONDAY_LOCAL = datetime(2026, 9, 21, tzinfo=LOCAL)


def _local(hour, minute=0, day=MONDAY_LOCAL):
    """An aware instant at a given LOCAL wall-clock time."""
    return day.replace(hour=hour, minute=minute)


def _as_production_clock(instant):
    """The SAME moment, expressed the way `timezone.now()` expresses it.

    THIS IS LOAD-BEARING, not tidiness. Under `USE_TZ` Django's `now()` returns a
    UTC-expressed aware datetime, and the defect these tests exist for is that the
    helper read `.hour` and `.isoweekday()` off whatever expression it was handed.
    A fixture that pinned the clock to a LOCAL-expressed instant would therefore
    pass against the unfixed code — the wall-clock digits would already be right —
    and would prove nothing about production. Converting here keeps the moment
    identical and the expression faithful.
    """
    return instant.astimezone(UTC)


class ScheduleZoneFixture(QuoteFixtureMixin, TestCase):
    """The shared fixture, plus a section that only serves at lunchtime."""

    WINDOW = {'days': ['mon'], 'startTime': '12:00', 'endTime': '15:00'}

    def _schedule_lunch(self):
        """Put the section on a 12:00-15:00 LOCAL schedule.

        Applied AFTER the draft exists, which is both the realistic operator
        edit (a schedule added mid-service) and what isolates the question: the
        draft was created against an always-available section, so the only thing
        under test is how acceptance reads the schedule.
        """
        self.section.availability = 'scheduled'
        self.section.schedules = [dict(self.WINDOW)]
        self.section.save(update_fields=['availability', 'schedules'])

    def _capability(self):
        return capability_from_table(self.table)

    def _extra_table(self, number):
        from restaurants_app.models import Table
        return Table.objects.create(
            number=number, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )

    def _capability_for(self, order):
        """The capability for the order's OWN table.

        Acceptance re-verifies the presented capability against the table it
        locked, so a fixture that reused one table's capability everywhere would
        be refused by the authorization gate and never reach the schedule rule
        this file is about.
        """
        from restaurants_app.models import Table
        return capability_from_table(Table.objects.get(pk=order.table_id))

    def _anchor_to(self, order, instant):
        """Put the draft's lifetime anchor just before `instant`.

        The probes below sit on a fixed calendar Monday so the weekday code is
        readable, which is days away from the moment the fixture actually runs —
        and the quote lifetime is thirty minutes from `Order.time_created`. Left
        alone, every probe would be refused `quote_expired` BEFORE the schedule
        rule is ever consulted, and this file would pass or fail on the wrong
        dimension. `time_created` is `auto_now_add`, so it is moved with a
        queryset UPDATE rather than a save.
        """
        Order.objects.filter(pk=order.pk).update(
            time_created=instant - timedelta(minutes=1))

    def _submit_at(self, order, instant):
        """Drive the real acceptance path with the decision clock pinned."""
        from orders_app.controllers import manage_order
        self._anchor_to(order, instant)
        with patch.object(manage_order.timezone, 'now',
                          return_value=_as_production_clock(instant)):
            return manage_order.update_order_status(
                order, OrderStatus_Pending, None,
                quote_ref=quote_ref(order),
                capability=self._capability_for(order),
            )

    def _retire_at(self, order, instant):
        from orders_app.controllers import manage_order
        self._anchor_to(order, instant)
        with patch.object(manage_order.timezone, 'now',
                          return_value=_as_production_clock(instant)):
            return retire_quote_for_review(
                order, quote_ref(order), capability=self._capability_for(order),
            )


class TheHelperConvertsWhatItIsHandedTests(TestCase):
    """The conversion boundary itself, stated once.

    This is the isolated probe the review ran, promoted into the suite so the
    boundary cannot move without something failing. The endpoint regressions
    below are what prove the production paths actually reach it.
    """

    class _Section:
        availability = 'scheduled'
        schedules = [{'days': ['mon'], 'startTime': '12:00', 'endTime': '15:00'}]

    def test_the_same_instant_in_utc_and_local_agree(self):
        inside = _local(12, 30)                      # 12:30 EAT == 09:30 UTC
        outside = _local(15, 30)                     # 15:30 EAT == 12:30 UTC
        section = self._Section()

        for instant, expected in ((inside, True), (outside, False)):
            for supplied in (instant, instant.astimezone(ZoneInfo('UTC'))):
                with self.subTest(instant=instant, tz=supplied.tzinfo):
                    self.assertIs(
                        is_section_currently_active(section, now=supplied),
                        expected,
                        'the same moment must read the same whichever zone it '
                        'is expressed in',
                    )

    def test_a_naive_instant_is_still_read_as_local(self):
        """The pre-existing contract for a naive value is unchanged.

        Nothing in production supplies one, but the helper accepted it before
        and a conversion that raised would turn a tolerated input into a 500.
        """
        section = self._Section()
        self.assertIs(
            is_section_currently_active(
                section, now=datetime(2026, 9, 21, 12, 30)),
            True,
        )

    def test_overnight_windows_are_unchanged(self):
        section = self._Section()
        section.schedules = [
            {'days': ['mon'], 'startTime': '22:00', 'endTime': '02:00'},
        ]
        # 23:00 local Monday is inside; 03:00 local Monday is not.
        self.assertIs(
            is_section_currently_active(section, now=_local(23, 0)), True)
        self.assertIs(
            is_section_currently_active(section, now=_local(3, 0)), False)


class AcceptanceReadsTheScheduleLocallyTests(ScheduleZoneFixture):
    """Dimension 7a — the acceptance path, both directions."""

    def test_an_open_section_is_not_refused_for_being_open(self):
        """local 12:30 / UTC 09:30 — inside the window. Pre-fix: REFUSED."""
        order = self._draft_order()
        self._schedule_lunch()

        result = self._submit_at(order, _local(12, 30))

        self.assertEqual(result.get('status'), 200, result)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'a section that is serving must not retire the diner\'s quote',
        )

    def test_a_closed_section_does_not_reach_the_kitchen(self):
        """local 15:30 / UTC 12:30 — the window shut at 15:00. Pre-fix: ACCEPTED."""
        order = self._draft_order()
        self._schedule_lunch()

        result = self._submit_at(order, _local(15, 30))

        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(result.get('reason'), REASON_PURCHASE_NEEDS_REVIEW)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_the_boundary_instant_is_outside_the_window(self):
        """15:00 local is the END, and an end is exclusive — unchanged semantics."""
        order = self._draft_order()
        self._schedule_lunch()

        result = self._submit_at(order, _local(15, 0))

        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(result.get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

    def test_the_first_instant_of_the_window_is_inside(self):
        order = self._draft_order()
        self._schedule_lunch()

        result = self._submit_at(order, _local(12, 0))

        self.assertEqual(result.get('status'), 200, result)


class TheLocalDayIsTheDayTests(ScheduleZoneFixture):
    """A weekday crossing: the two zones disagree about which day it is."""

    def test_late_local_sunday_is_not_read_as_monday(self):
        """23:30 local SUNDAY is 20:30 UTC Sunday — both Sunday, so a Monday-only
        window must refuse. The interesting half is the other one."""
        sunday = MONDAY_LOCAL - timedelta(days=1)
        order = self._draft_order()
        self._schedule_lunch()

        result = self._submit_at(order, _local(23, 30, day=sunday))

        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(result.get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

    def test_early_local_monday_is_read_as_monday(self):
        """01:30 local MONDAY is 22:30 UTC SUNDAY.

        Pre-fix the UTC reading said Sunday, so an overnight Monday window was
        refused on the very day it covers. This is the day-boundary case the
        hour-only tests cannot reach.
        """
        # The draft is created FIRST, as everywhere else here: creation itself
        # consults the schedule, so applying an overnight window before the
        # draft exists refuses the order at the wrong boundary and this test
        # would never reach the one it is about.
        order = self._draft_order()
        self.section.availability = 'scheduled'
        self.section.schedules = [
            {'days': ['mon'], 'startTime': '00:00', 'endTime': '04:00'},
        ]
        self.section.save(update_fields=['availability', 'schedules'])

        result = self._submit_at(order, _local(1, 30))

        self.assertEqual(result.get('status'), 200, result)


class RetirementReadsTheSameClockTests(ScheduleZoneFixture):
    """The retire-for-review path shares the rule, so it shares the fix.

    A retirement that read the schedule three hours out would CLOSE a perfectly
    good quote permanently — the one refusal in this domain that cannot be
    undone — so this direction matters more here than on acceptance.
    """

    def test_an_open_section_is_not_retired(self):
        order = self._draft_order()
        self._schedule_lunch()

        result = self._retire_at(order, _local(12, 30))

        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result.get('outcome'), 'quote_still_valid', result)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_a_closed_section_retires_the_quote(self):
        order = self._draft_order()
        self._schedule_lunch()

        result = self._retire_at(order, _local(15, 30))

        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result.get('outcome'), 'quote_closed', result)
        self.assertTrue(OrderQuoteClosure.objects.filter(order=order).exists())


class TheMenuAndTheQuoteAgreeTests(ScheduleZoneFixture):
    """The control that ties the two surfaces together.

    A diner who can SEE a section on the menu must be able to have an order
    against it accepted at the same instant, and vice versa. Before the fix these
    two answered from clocks three hours apart.
    """

    def _menu_says_active(self, instant):
        from restaurants_app.controllers.menu_publication import (
            section_operationally_visible,
        )
        # The read path's own conversion, unchanged.
        return section_operationally_visible(
            self.section, instant.astimezone(LOCAL))

    def test_both_surfaces_agree_at_every_probe(self):
        probes = ((11, 30), (12, 0), (12, 30), (14, 59), (15, 0), (15, 30))
        # Every draft is created BEFORE the schedule exists — one per probe, each
        # at its own table, so no two compete for occupancy. The schedule is then
        # applied once, which is the real operator edit and what isolates the
        # question to how the ACCEPTANCE boundary reads it.
        tables = [self.table, self.table_b] + [
            self._extra_table(n) for n in range(3, 3 + len(probes) - 2)
        ]
        orders = [self._draft_order(table=table) for table in tables]
        self._schedule_lunch()

        for (hour, minute), order in zip(probes, orders):
            instant = _local(hour, minute)
            with self.subTest(local=f'{hour:02d}:{minute:02d}'):
                accepted = self._submit_at(order, instant).get('status') == 200
                self.assertIs(
                    accepted, self._menu_says_active(instant),
                    'the menu and the acceptance boundary must not disagree '
                    'about whether the section is serving',
                )
