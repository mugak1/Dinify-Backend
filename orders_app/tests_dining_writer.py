"""
Dining model, Step 1 — the WRITER (PR 2): every order accepted from now on gets exactly
one visit, one bill and one placement, written in the transaction that accepts it.

What these tests pin, in the order the module reads:

* WHO GETS A ROUND: a diner, a staff, a test-restaurant and a pre-go-live rehearsal
  acceptance each get exactly one visit, bill and placement. The visit's restaurant and
  table are the ORDER's; ``opened_by`` is the order's ``created_by``, never the
  submitter; released time and ``opened_at`` are the acceptance instant.
* WHO DOES NOT: a replay (same quote, and a 409 different quote), refusals, and a draft
  that is never submitted write nothing.
* THE BUSINESS DATE, as a pure function and through a real submit.
* THE PAYMENT TIMING copied at acceptance, including both "nobody chose" cases.
* THE SWITCH: off writes nothing and costs nothing (14 queries); on costs 18.
* A FAULT at any of the three inserts rolls the whole acceptance back.
* ``counted_visits_q`` follows the one test-order rule.
* VISIT STATE through the real kitchen endpoints, with zero writes to the new tables.
* NOTHING A READER SEES CHANGES: the diner order read, the kitchen feeds, the
  dashboards, the reports and the order responses.

The two-connection race is in ``tests_dining_writer_concurrency``; the occupancy
equivalence matrix in ``tests_dining_occupancy``.
"""
import json
import re
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from commercial_app.models import (
    PAYMENT_TIMING_PAY_AFTER, PAYMENT_TIMING_PAY_FIRST,
    RestaurantServiceConfiguration,
)
from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind, OrderStatus_Initiated,
    OrderStatus_Pending, RESTAURANT_OWNER, RESTAURANT_WAITER,
    RestaurantStatus_Live, RestaurantStatus_Onboarding, RestaurantStatus_Suspended,
)
from orders_app.checks import CHECK_ID, check_dining_visit_writes
from orders_app.controllers.manage_order import update_order_status
from orders_app.controllers.services import round_placement
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.round_placement import (
    PAYMENT_UNKNOWN, VISIT_ENDED, VISIT_OPEN, business_date_for,
    resolve_payment_timing, visit_state,
)
from orders_app.controllers.test_orders import counted_visits_q
from orders_app.models import (
    PAYMENT_TIMING_NOT_CHOSEN, Bill, Order, OrderAcceptance, OrderPlacement, Visit,
)
from restaurants_app.controllers.diner_capability import (
    issue_qr_credential, issue_table_session,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

NAIROBI = ZoneInfo('Africa/Nairobi')
FOUR = time(4, 0)
NEW_TABLES = ('"visits"', '"bills"', '"order_placements"')


def _user(phone):
    return User.objects.create_user(
        first_name='Dining', last_name='Writer', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda', password='password',
        roles=[],
    )


def _eat(y, m, d, hh, mm, ss=0, us=0):
    """An aware instant at the given Africa/Nairobi wall time."""
    return datetime(y, m, d, hh, mm, ss, us, tzinfo=NAIROBI)


class DiningFixture(TestCase):
    """A live restaurant with an owner, a waiter, four orderable tables and one dish."""

    restaurant_status = RestaurantStatus_Live
    restaurant_is_test = False
    phone_seed = '25670099'

    def setUp(self):
        super().setUp()
        self.owner = _user(f'{self.phone_seed}0001')
        self.waiter = _user(f'{self.phone_seed}0002')
        self.restaurant = self._restaurant('Dining Writer', self.owner)
        RestaurantEmployee.objects.create(
            user=self.waiter, restaurant=self.restaurant,
            roles=[RESTAURANT_WAITER], active=True,
        )
        self.tables = [self._table(self.restaurant, n) for n in range(1, 5)]
        self.item = self._item(self.restaurant)

    def _restaurant(self, name, owner, status=None, is_test=None):
        restaurant = Restaurant.objects.create(
            name=name, location='Kampala', owner=owner,
            status=status or self.restaurant_status,
            is_test=self.restaurant_is_test if is_test is None else is_test,
        )
        RestaurantEmployee.objects.create(
            user=owner, restaurant=restaurant, roles=[RESTAURANT_OWNER], active=True,
        )
        return restaurant

    def _table(self, restaurant, number):
        area, _ = DiningArea.objects.get_or_create(name='Main', restaurant=restaurant)
        return Table.objects.create(
            number=number, restaurant=restaurant, dining_area=area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )

    def _item(self, restaurant):
        section = MenuSection.objects.create(
            name='Mains', restaurant=restaurant,
            approved=True, enabled=True, available=True,
        )
        return MenuItem.objects.create(
            name='Matooke', section=section, primary_price=Decimal('12000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )

    def draft(self, table=None, created_by=None, restaurant=None, item=None):
        result = _create_order(
            restaurant=restaurant or self.restaurant,
            table=table or self.tables[0],
            items=[{'item': str((item or self.item).pk), 'quantity': 1}],
            created_by=created_by,
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def submit(self, order, user=None, ref=None):
        return update_order_status(
            order, OrderStatus_Pending, user,
            quote_ref=quote_ref(order) if ref is None else ref,
        )

    def accept(self, order, user=None):
        result = self.submit(order, user=user)
        self.assertEqual(result.get('status'), 200, result)
        return result

    def counts(self):
        return (Visit.objects.count(), Bill.objects.count(),
                OrderPlacement.objects.count())


# --- who gets a round -------------------------------------------------------------

class EveryAcceptedOrderGetsOneRoundTests(DiningFixture):
    def assert_one_round(self, order, opened_by):
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        placement = OrderPlacement.objects.select_related('visit', 'bill').get(
            order=order)
        visit, bill = placement.visit, placement.bill
        accepted_at = OrderAcceptance.objects.get(order=order).accepted_at
        self.assertEqual(Visit.objects.filter(placements__order=order).count(), 1)
        self.assertEqual(Bill.objects.filter(visit=visit).count(), 1)
        self.assertEqual(bill.visit_id, visit.pk)
        # Tenant isolation: the visit is the ORDER's restaurant and table.
        self.assertEqual(visit.restaurant_id, order.restaurant_id)
        self.assertEqual(visit.table_id, order.table_id)
        self.assertEqual(visit.opened_by_id, opened_by)
        # One instant: the acceptance's.
        self.assertEqual(visit.opened_at, accepted_at)
        self.assertEqual(placement.released_at, accepted_at)
        cutoff = Restaurant.objects.get(pk=order.restaurant_id).business_day_cutoff
        self.assertEqual(placement.business_date,
                         business_date_for(accepted_at, cutoff))
        self.assertEqual(placement.round_number, 1)
        self.assertEqual(visit.payment_timing, PAYMENT_TIMING_NOT_CHOSEN)
        self.assertEqual(placement.payment_timing, PAYMENT_TIMING_NOT_CHOSEN)
        return placement

    def test_a_diner_acceptance(self):
        order = self.draft()
        self.accept(order)
        self.assert_one_round(order, opened_by=None)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_a_staff_acceptance_records_the_staff_member_who_placed_it(self):
        order = self.draft(created_by=self.waiter)
        # Submitted by somebody ELSE: the visit still names the waiter.
        self.accept(order, user=self.owner)
        self.assert_one_round(order, opened_by=self.waiter.pk)

    def test_a_diner_draft_submitted_by_staff_stays_a_diner_visit(self):
        order = self.draft()
        self.accept(order, user=self.owner)
        self.assert_one_round(order, opened_by=None)

    def test_a_test_restaurant_acceptance(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)
        order = self.draft()
        self.accept(order)
        order.refresh_from_db()
        self.assertTrue(order.is_test)
        self.assert_one_round(order, opened_by=None)

    def test_a_pre_go_live_rehearsal(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        self.restaurant.refresh_from_db()
        order = self.draft(created_by=self.owner)
        self.accept(order, user=self.owner)
        order.refresh_from_db()
        self.assertTrue(order.is_test)
        self.assert_one_round(order, opened_by=self.owner.pk)

    def test_the_visit_is_copied_from_the_locked_row_not_the_callers_instance(self):
        """Tenant isolation rests on this: the restaurant and table come from the
        order RE-READ under the acceptance's locks. A caller holding a stale or
        tampered instance cannot steer the visit onto another table or tenant."""
        order = self.draft(table=self.tables[0])
        ref = quote_ref(order)
        other_owner = _user('256700990103')
        other = self._restaurant('Elsewhere', other_owner)
        foreign_table = self._table(other, 1)
        order.table_id = self.tables[1].pk          # stale, same tenant
        self.assertEqual(self.submit(order, ref=ref)['status'], 200)
        visit = OrderPlacement.objects.get(order_id=order.pk).visit
        self.assertEqual(visit.table_id, self.tables[0].pk)
        self.assertEqual(visit.restaurant_id, self.restaurant.pk)

        second = self.draft(table=self.tables[2])
        ref = quote_ref(second)
        second.restaurant_id = other.pk             # foreign tenant
        second.table_id = foreign_table.pk
        self.submit(second, ref=ref)
        self.assertFalse(Visit.objects.filter(restaurant=other).exists())
        self.assertFalse(Visit.objects.filter(table=foreign_table).exists())
        for placement in OrderPlacement.objects.select_related('visit', 'order'):
            self.assertEqual(placement.visit.restaurant_id,
                             placement.order.restaurant_id)
            self.assertEqual(placement.visit.table_id, placement.order.table_id)

    def test_each_order_is_its_own_visit_until_step_3(self):
        first = self.draft(table=self.tables[0])
        second = self.draft(table=self.tables[1])
        self.accept(first)
        self.accept(second)
        self.assertEqual(self.counts(), (2, 2, 2))
        self.assertNotEqual(first.placement.visit_id, second.placement.visit_id)


class NothingIsWrittenWithoutAFirstAcceptanceTests(DiningFixture):
    def test_a_draft_that_is_never_submitted(self):
        self.draft()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_a_replay_with_the_same_quote_writes_nothing_more(self):
        order = self.draft()
        ref = quote_ref(order)
        self.accept(order)
        replay = self.submit(order, ref=ref)
        self.assertEqual(replay['status'], 200, replay)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_a_replay_with_a_different_quote_writes_nothing_more(self):
        order = self.draft()
        self.accept(order)
        conflict = self.submit(order, ref='v1:' + '0' * 64)
        self.assertEqual(conflict['status'], 409, conflict)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_a_stale_quote_is_refused_and_writes_nothing(self):
        order = self.draft()
        refused = self.submit(order, ref='v1:' + '0' * 64)
        self.assertEqual(refused['status'], 400, refused)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_a_missing_quote_is_refused_and_writes_nothing(self):
        order = self.draft()
        refused = update_order_status(order, OrderStatus_Pending, None, quote_ref=None)
        self.assertEqual(refused['status'], 400, refused)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_a_suspended_restaurant_is_refused_and_writes_nothing(self):
        order = self.draft()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Suspended)
        refused = self.submit(order)
        self.assertEqual(refused['status'], 400, refused)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_an_occupied_table_is_refused_and_writes_nothing_more(self):
        first = self.draft()
        second = self.draft()
        self.accept(first)
        refused = self.submit(second)
        self.assertEqual(refused['status'], 400, refused)
        self.assertEqual(self.counts(), (1, 1, 1))


# --- the business date ------------------------------------------------------------

class BusinessDateRuleTests(TestCase):
    """The pure rule. Africa/Nairobi is UTC+3 with no daylight saving."""

    def test_before_the_cutoff_is_the_previous_day(self):
        self.assertEqual(business_date_for(_eat(2026, 11, 2, 1, 30), FOUR),
                         date(2026, 11, 1))

    def test_after_the_cutoff_is_the_same_day(self):
        self.assertEqual(business_date_for(_eat(2026, 11, 2, 4, 30), FOUR),
                         date(2026, 11, 2))

    def test_the_last_microsecond_before_the_cutoff_is_the_previous_day(self):
        self.assertEqual(
            business_date_for(_eat(2026, 11, 2, 3, 59, 59, 999999), FOUR),
            date(2026, 11, 1))

    def test_the_cutoff_itself_starts_the_new_day(self):
        self.assertEqual(business_date_for(_eat(2026, 11, 2, 4, 0), FOUR),
                         date(2026, 11, 2))

    def test_a_midnight_cutoff_always_gives_the_local_calendar_day(self):
        midnight = time(0, 0)
        for hh, mm in ((0, 0), (0, 1), (3, 59), (12, 0), (23, 59)):
            with self.subTest(at=f'{hh:02}:{mm:02}'):
                self.assertEqual(
                    business_date_for(_eat(2026, 11, 2, hh, mm), midnight),
                    date(2026, 11, 2))

    def test_the_rule_reads_the_local_day_not_the_utc_day(self):
        # 22:30 UTC on the 1st is 01:30 EAT on the 2nd: before the cut-off.
        utc = datetime(2026, 11, 1, 22, 30, tzinfo=ZoneInfo('UTC'))
        self.assertEqual(business_date_for(utc, FOUR), date(2026, 11, 1))
        # 01:30 UTC on the 2nd is 04:30 EAT: after it.
        utc = datetime(2026, 11, 2, 1, 30, tzinfo=ZoneInfo('UTC'))
        self.assertEqual(business_date_for(utc, FOUR), date(2026, 11, 2))

    def test_an_active_non_default_time_zone_does_not_change_the_result(self):
        cases = (
            (_eat(2026, 11, 2, 1, 30), date(2026, 11, 1)),
            (_eat(2026, 11, 2, 4, 30), date(2026, 11, 2)),
            (_eat(2026, 11, 2, 3, 59, 59, 999999), date(2026, 11, 1)),
            (_eat(2026, 11, 2, 4, 0), date(2026, 11, 2)),
        )
        for zone in ('America/Los_Angeles', 'Pacific/Auckland', 'UTC'):
            with timezone.override(ZoneInfo(zone)):
                for at, expected in cases:
                    with self.subTest(zone=zone, at=at.isoformat()):
                        self.assertEqual(business_date_for(at, FOUR), expected)

    def test_a_naive_instant_is_refused(self):
        with self.assertRaises(ValueError):
            business_date_for(datetime(2026, 11, 2, 4, 30), FOUR)


class BusinessDateThroughSubmitTests(DiningFixture):
    """The same rule, reached through a real acceptance at a pinned instant."""

    def accept_at(self, at, table_index=0):
        order = self.draft(table=self.tables[table_index])
        # Keep the quote inside its 30-minute lifetime at the pinned instant.
        Order.objects.filter(pk=order.pk).update(time_created=at - timedelta(minutes=1))
        order.refresh_from_db()
        with mock.patch('django.utils.timezone.now', return_value=at):
            self.accept(order)
        placement = OrderPlacement.objects.get(order=order)
        self.assertEqual(placement.released_at, at)
        return placement

    def test_the_boundaries(self):
        cases = (
            (_eat(2026, 11, 2, 1, 30), date(2026, 11, 1)),
            (_eat(2026, 11, 2, 4, 30), date(2026, 11, 2)),
            (_eat(2026, 11, 2, 3, 59, 59, 999999), date(2026, 11, 1)),
            (_eat(2026, 11, 2, 4, 0), date(2026, 11, 2)),
        )
        for index, (at, expected) in enumerate(cases):
            with self.subTest(at=at.isoformat()):
                self.assertEqual(self.accept_at(at, index).business_date, expected)

    def test_a_midnight_cutoff_gives_the_calendar_day(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            business_day_cutoff=time(0, 0))
        self.assertEqual(
            self.accept_at(_eat(2026, 11, 2, 1, 30)).business_date, date(2026, 11, 2))

    def test_an_active_non_default_time_zone_does_not_change_it(self):
        with timezone.override(ZoneInfo('America/Los_Angeles')):
            placement = self.accept_at(_eat(2026, 11, 2, 1, 30))
        self.assertEqual(placement.business_date, date(2026, 11, 1))


# --- the payment timing -----------------------------------------------------------

class PaymentTimingTests(DiningFixture):
    def visit_for(self, table_index=0):
        order = self.draft(table=self.tables[table_index])
        self.accept(order)
        return OrderPlacement.objects.select_related('visit').get(order=order)

    def configure(self, value):
        configuration, _ = RestaurantServiceConfiguration.objects.get_or_create(
            restaurant=self.restaurant)
        configuration.payment_timing = value
        configuration.payment_timing_set_at = timezone.now() if value else None
        configuration.payment_timing_set_by = self.owner if value else None
        configuration.save()

    def test_no_configuration_row_is_not_chosen(self):
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        placement = self.visit_for()
        self.assertEqual(placement.visit.payment_timing, PAYMENT_TIMING_NOT_CHOSEN)
        self.assertEqual(placement.payment_timing, PAYMENT_TIMING_NOT_CHOSEN)

    def test_an_unset_axis_is_not_chosen(self):
        self.configure(None)
        self.assertTrue(RestaurantServiceConfiguration.objects.exists())
        placement = self.visit_for()
        self.assertEqual(placement.visit.payment_timing, PAYMENT_TIMING_NOT_CHOSEN)

    def test_pay_first_and_pay_after_are_copied(self):
        for index, value in enumerate((PAYMENT_TIMING_PAY_FIRST,
                                       PAYMENT_TIMING_PAY_AFTER)):
            with self.subTest(value=value):
                self.configure(value)
                placement = self.visit_for(table_index=index)
                self.assertEqual(placement.visit.payment_timing, value)
                self.assertEqual(placement.payment_timing, value)

    def test_a_later_configuration_change_leaves_an_existing_visit_alone(self):
        self.configure(PAYMENT_TIMING_PAY_FIRST)
        placement = self.visit_for()
        self.configure(PAYMENT_TIMING_PAY_AFTER)
        placement.refresh_from_db()
        placement.visit.refresh_from_db()
        self.assertEqual(placement.visit.payment_timing, PAYMENT_TIMING_PAY_FIRST)
        self.assertEqual(placement.payment_timing, PAYMENT_TIMING_PAY_FIRST)

    def test_the_resolver(self):
        self.assertEqual(resolve_payment_timing(None), PAYMENT_TIMING_NOT_CHOSEN)
        self.assertEqual(resolve_payment_timing(PAYMENT_TIMING_PAY_FIRST),
                         PAYMENT_TIMING_PAY_FIRST)
        self.assertEqual(resolve_payment_timing(PAYMENT_TIMING_PAY_AFTER),
                         PAYMENT_TIMING_PAY_AFTER)
        with self.assertRaises(ValueError):
            resolve_payment_timing('cash')


# --- the switch -------------------------------------------------------------------

class SwitchTests(DiningFixture):
    @override_settings(DINING_VISIT_WRITES_ENABLED=False)
    def test_off_accepts_the_order_and_writes_nothing(self):
        order = self.draft()
        self.accept(order)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.assertTrue(OrderAcceptance.objects.filter(order=order).exists())
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_on_is_the_default(self):
        self.assertTrue(settings.DINING_VISIT_WRITES_ENABLED)
        order = self.draft()
        self.accept(order)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_switching_back_on_resumes_for_the_next_acceptance(self):
        with override_settings(DINING_VISIT_WRITES_ENABLED=False):
            self.accept(self.draft(table=self.tables[0]))
        self.accept(self.draft(table=self.tables[1]))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_the_system_check_warns_only_while_off(self):
        self.assertEqual(check_dining_visit_writes(True), [])
        messages = check_dining_visit_writes(False)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, CHECK_ID)
        self.assertEqual(messages[0].level, 30)  # WARNING, never an Error

    def test_the_registered_check_reads_the_live_setting(self):
        from django.core.checks import run_checks
        ids = [m.id for m in run_checks()]
        self.assertNotIn(CHECK_ID, ids)
        with override_settings(DINING_VISIT_WRITES_ENABLED=False):
            ids = [m.id for m in run_checks()]
        self.assertIn(CHECK_ID, ids)


# --- a fault rolls the acceptance back --------------------------------------------

class FaultRollsTheAcceptanceBackTests(DiningFixture):
    def assert_rolled_back(self, model):
        order = self.draft()
        boom = RuntimeError(f'injected {model.__name__} fault')
        with mock.patch.object(model.objects, 'create', side_effect=boom):
            refused = self.submit(order)
        self.assertNotEqual(refused.get('status'), 200, refused)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())
        self.assertEqual(self.counts(), (0, 0, 0))
        # And the same draft is acceptable once the fault is gone.
        self.accept(order)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_a_fault_at_the_visit_insert(self):
        self.assert_rolled_back(Visit)

    def test_a_fault_at_the_bill_insert(self):
        self.assert_rolled_back(Bill)

    def test_a_fault_at_the_placement_insert(self):
        self.assert_rolled_back(OrderPlacement)


# --- the test-order rule ----------------------------------------------------------

class CountedVisitsTests(DiningFixture):
    def test_practice_orders_are_left_out_and_a_test_restaurant_is_kept(self):
        real = self.draft(table=self.tables[0])
        self.accept(real)

        # A practice order: a rehearsal at a restaurant that is not a test one.
        rehearsing_owner = _user('256700990101')
        rehearsing = self._restaurant('Rehearsing', rehearsing_owner,
                                      status=RestaurantStatus_Onboarding)
        practice = self.draft(table=self._table(rehearsing, 1),
                              created_by=rehearsing_owner, restaurant=rehearsing,
                              item=self._item(rehearsing))
        self.accept(practice, user=rehearsing_owner)

        # A test restaurant's order: flagged, and counted.
        test_owner = _user('256700990102')
        tester = self._restaurant('Tester', test_owner, is_test=True)
        tested = self.draft(table=self._table(tester, 1), restaurant=tester,
                            item=self._item(tester))
        self.accept(tested)

        practice.refresh_from_db()
        tested.refresh_from_db()
        self.assertTrue(practice.is_test)
        self.assertTrue(tested.is_test)

        counted = set(Visit.objects.filter(counted_visits_q())
                      .values_list('pk', flat=True))
        self.assertEqual(counted, {real.placement.visit_id,
                                   tested.placement.visit_id})
        self.assertNotIn(practice.placement.visit_id, counted)


# --- visit state through the real kitchen ------------------------------------------

def _writes_to_new_tables(captured):
    return [q['sql'] for q in captured.captured_queries
            if q['sql'].split(' ', 1)[0] in ('INSERT', 'UPDATE', 'DELETE')
            and any(t in q['sql'] for t in NEW_TABLES)]


class VisitStateThroughTheKitchenTests(DiningFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def command(self, order, action):
        revision = Order.objects.values_list(
            'fulfilment_revision', flat=True).get(pk=order.pk)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.put(
                f'/api/v1/kitchen/orders/{order.pk}/fulfilment-status/',
                {'action': action, 'if_revision': revision}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_writes_to_new_tables(captured), [])
        return response

    def cancel(self, order):
        revision = Order.objects.values_list(
            'fulfilment_revision', flat=True).get(pk=order.pk)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.put(
                f'/api/v1/kitchen/orders/{order.pk}/cancel/',
                {'cancellation_reason': CancellationReason_CustomerChangedMind,
                 'if_revision': revision}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_writes_to_new_tables(captured), [])

    def state(self, order):
        return visit_state(OrderPlacement.objects.get(order=order).visit)

    def test_open_ended_on_serve_and_open_again_on_recall(self):
        order = self.draft()
        self.accept(order)
        before = self.counts()
        self.assertEqual(self.state(order),
                         {'state': VISIT_OPEN, 'payment': PAYMENT_UNKNOWN})
        self.command(order, 'advance')            # new -> preparing
        self.assertEqual(self.state(order)['state'], VISIT_OPEN)
        self.command(order, 'advance')            # preparing -> ready
        self.command(order, 'serve')
        self.assertEqual(self.state(order),
                         {'state': VISIT_ENDED, 'payment': PAYMENT_UNKNOWN})
        self.command(order, 'recall')
        self.assertEqual(self.state(order)['state'], VISIT_OPEN)
        self.command(order, 'serve')
        self.assertEqual(self.state(order)['state'], VISIT_ENDED)
        self.assertEqual(self.counts(), before)

    def test_ended_on_cancel(self):
        order = self.draft()
        self.accept(order)
        before = self.counts()
        self.cancel(order)
        self.assertEqual(self.state(order),
                         {'state': VISIT_ENDED, 'payment': PAYMENT_UNKNOWN})
        self.assertEqual(self.counts(), before)

    def test_soft_deleting_the_order_ends_the_visit_without_a_write(self):
        order = self.draft()
        self.accept(order)
        Order.objects.filter(pk=order.pk).update(deleted=True)
        self.assertEqual(self.state(order)['state'], VISIT_ENDED)


# --- nothing a reader sees changes ------------------------------------------------

_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?')
_QUOTE = re.compile(r'(?:v1:)?[0-9a-f]{64}')


def _normalised(payload):
    """Identifiers, instants and quote references replaced by placeholders, so two
    responses about two different orders can be compared for SHAPE and content."""
    text = json.dumps(payload, sort_keys=True, default=str)
    text = _QUOTE.sub('<quote>', text)
    text = _UUID.sub('<uuid>', text)
    return _STAMP.sub('<instant>', text)


class ReadersSeeNoDifferenceTests(DiningFixture):
    """The Step 1 rows are invisible to every reader. Proved two ways: every read is
    byte-identical with and without the rows, and the submit and initiate responses
    are identical (up to identifiers and instants) with the switch on and off."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = RefreshToken.for_user(self.owner).access_token

    def auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

    def reads(self, order):
        rid = self.restaurant.pk
        today = timezone.localdate().isoformat()
        week_ago = (timezone.localdate() - timedelta(days=7)).isoformat()
        window = f'from={week_ago}&to={today}'
        urls = {
            'diner_order_read': None,
            'table_scan': None,
            'kitchen_active': '/api/v1/kitchen/orders/active/'
                              f'?restaurant={rid}',
            'kitchen_completed': '/api/v1/kitchen/orders/completed/'
                                 f'?restaurant={rid}',
            'dashboard': f'/api/v1/reports/restaurant/dashboard/?restaurant={rid}',
            'dashboard_v2': '/api/v1/reports/restaurant/dashboard-v2/'
                            f'?restaurant={rid}&{window}&bucket=day',
            'sales_listing': '/api/v1/reports/restaurant/sales-listing/'
                             f'?restaurant={rid}&{window}',
            'sales_trends': '/api/v1/reports/restaurant/sales-trends/'
                            f'?restaurant={rid}&{window}&period=day',
            'menu_summary': '/api/v1/reports/restaurant/menu-summary/'
                            f'?restaurant={rid}&{window}',
            'diners_summary': '/api/v1/reports/restaurant/diners-summary/'
                              f'?restaurant={rid}&{window}',
            'transactions_summary': '/api/v1/reports/restaurant/'
                                    f'transactions-summary/?restaurant={rid}&{window}',
        }
        bodies = {}
        for name, url in urls.items():
            if name == 'diner_order_read':
                response = self.client.get(
                    '/api/v1/orders/journey/order-details/',
                    {'order': str(order.pk)},
                    HTTP_X_DINER_SESSION=issue_table_session(order.table))
            elif name == 'table_scan':
                table = Table.objects.get(pk=order.table_id)
                response = self.client.get(
                    '/api/v1/orders/journey/table-scan/',
                    HTTP_X_DINER_CREDENTIAL=issue_qr_credential(
                        table.restaurant_id, table.pk, table.qr_version))
            else:
                response = self.client.get(url, **self.auth())
            self.assertIn(response.status_code, (200, 400), (name, response.content))
            bodies[name] = (response.status_code, response.content)
        return bodies

    #: Reads that are ALLOWED one difference, and which key carries it. Step 2's
    #: kitchen release filter (BE-C) adds `released_at` to every kitchen ticket:
    #: the placement's release instant with the rows, null without them (an order
    #: with no placement). That is the ONE place the rows are now visible, by
    #: design; everything else on the ticket, and every other read, stays
    #: byte-identical. (The completed feed is empty here, so it stays identical.)
    #:
    #: Step 2's diner round state (BE-E) adds `checkout.current.round` to the diner
    #: order read and `current_order.held` to the table scan. Both reads stay in the
    #: IDENTICAL set here, deliberately: a round released at acceptance and an order
    #: with no placement are both `released` / `held: false`, so with and without
    #: the rows the two reads are still byte-identical. The one difference they can
    #: show — a HELD round — is pinned key by key in
    #: `test_a_held_round_shows_in_the_diner_reads_by_exactly_one_key` below.
    DIFFERS_ONLY_IN = {'kitchen_active': 'released_at'}

    def test_every_read_is_identical_with_and_without_the_rows(self):
        order = self.draft()
        self.accept(order)
        self.assertEqual(self.counts(), (1, 1, 1))
        released_at = OrderPlacement.objects.get(order=order).released_at
        with_rows = self.reads(order)
        OrderPlacement.objects.all().delete()
        Bill.objects.all().delete()
        Visit.objects.all().delete()
        without_rows = self.reads(order)
        for name in with_rows:
            with self.subTest(read=name):
                if name not in self.DIFFERS_ONLY_IN:
                    self.assertEqual(with_rows[name], without_rows[name])
                    continue
                key = self.DIFFERS_ONLY_IN[name]
                self.assertEqual(with_rows[name][0], without_rows[name][0])
                body_with = json.loads(with_rows[name][1])
                body_without = json.loads(without_rows[name][1])
                [ticket_with] = body_with['data']
                [ticket_without] = body_without['data']
                self.assertEqual(
                    datetime.fromisoformat(
                        ticket_with.pop(key).replace('Z', '+00:00')),
                    released_at)
                self.assertIsNone(ticket_without.pop(key))
                self.assertEqual(body_with, body_without)
        # And the reads did run: the order is on the active board.
        self.assertIn(str(order.pk).encode(), with_rows['kitchen_active'][1])

    def test_a_held_round_shows_in_the_diner_reads_by_exactly_one_key(self):
        """Step 2, BE-E. A HELD round against the same order with no placement:

        - the diner order read differs by exactly `checkout.current.round` (held,
          held since the acceptance, not stale; released, null, false without);
        - the table scan differs by exactly `current_order.held` (true; false).

        Nothing else in either response moves. (The kitchen reads are not compared
        here: a held round is OFF the board by BE-C's release filter, so they
        differ by design and are pinned in `tests_kitchen_release_filter`.)"""
        order = self.draft()
        self.accept(order)
        OrderPlacement.objects.filter(order=order).update(released_at=None)
        accepted_at = OrderAcceptance.objects.get(order=order).accepted_at
        held = self.reads(order)
        OrderPlacement.objects.all().delete()
        Bill.objects.all().delete()
        Visit.objects.all().delete()
        bare = self.reads(order)

        read_held = json.loads(held['diner_order_read'][1])
        read_bare = json.loads(bare['diner_order_read'][1])
        self.assertEqual(held['diner_order_read'][0], 200)
        self.assertEqual(
            read_held['data']['checkout']['current'].pop('round'),
            {'state': 'held', 'held_since': accepted_at.isoformat(),
             'stale': False})
        self.assertEqual(
            read_bare['data']['checkout']['current'].pop('round'),
            {'state': 'released', 'held_since': None, 'stale': False})
        self.assertEqual(read_held, read_bare)

        scan_held = json.loads(held['table_scan'][1])
        scan_bare = json.loads(bare['table_scan'][1])
        self.assertEqual(held['table_scan'][0], 200)
        self.assertIs(scan_held['data']['current_order'].pop('held'), True)
        self.assertIs(scan_bare['data']['current_order'].pop('held'), False)
        self.assertEqual(scan_held, scan_bare)
        self.assertEqual(scan_held['data']['current_order'],
                         {'ongoing': True, 'order_id': str(order.pk)})

    def test_the_order_responses_are_identical_with_the_switch_on_and_off(self):
        on_draft = _create_order(
            restaurant=self.restaurant, table=self.tables[0],
            items=[{'item': str(self.item.pk), 'quantity': 1}])
        on_submit = self.submit(on_draft['order'])
        with override_settings(DINING_VISIT_WRITES_ENABLED=False):
            off_draft = _create_order(
                restaurant=self.restaurant, table=self.tables[1],
                items=[{'item': str(self.item.pk), 'quantity': 1}])
            off_submit = self.submit(off_draft['order'])
        self.assertEqual(on_submit['status'], 200)
        self.assertEqual(off_submit['status'], 200)
        self.maxDiff = None
        self.assertEqual(_normalised(on_submit), _normalised(off_submit))
        self.assertEqual(
            _normalised({k: v for k, v in on_draft.items() if k != 'order'}),
            _normalised({k: v for k, v in off_draft.items() if k != 'order'}))
        self.assertEqual(self.counts(), (1, 1, 1))


# --- the reference allowlist (see tests_dining_schema) ------------------------------

class WriterIsTheOnlyProducerTests(TestCase):
    def test_round_placement_is_the_module_that_creates_the_rows(self):
        import inspect
        source = inspect.getsource(round_placement)
        for name in ('Visit.objects.create', 'Bill.objects.create',
                     'OrderPlacement.objects.create'):
            self.assertIn(name, source)
