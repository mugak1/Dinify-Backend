"""
Step 2, PR BE-D — the floor's Confirm and Decline commands on a HELD round.

NOTHING WRITES A HELD ROUND YET (BE-F holds at acceptance, later), so every held
round here is FABRICATED: a real order with real lines, an ``OrderAcceptance``, and
a visit, bill and placement written directly with ``released_at`` NULL — exactly
what BE-F's writer will produce.

What is pinned (two-connection races are in ``tests_round_release_concurrency``):

* Confirm writes ``released_at``, ``released_by`` and the RE-STAMPED
  ``business_date`` (03:50 accepted, 04:10 confirmed: the new business day), writes
  nothing on the order, and the round reaches the kitchen feed aged from release;
* every Confirm refusal leaves the round held and writes nothing; a staff round is
  confirmed through a pause; an item that merely left its schedule or was
  unpublished is still confirmed;
* Decline writes the cancellation, the server's reason and a revision bump, frees
  the table, and the round never reaches either kitchen feed;
* every idempotency and crossing case of decision 5;
* the ``tables`` module, re-checked under the lock; 404 for another restaurant;
* neither switch is consulted; the query counts.
"""
import ast
import inspect as pyinspect
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from dinify_backend.configss.string_definitions import (
    CANCELLATION_REASONS,
    RESTAURANT_KITCHEN,
    RESTAURANT_OWNER,
    RESTAURANT_STAFF,
    CancellationReason_CustomerChangedMind,
    CancellationReason_DeclinedByStaff,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Pending,
    OrderStatus_Served,
    PaymentStatus_Pending,
    RestaurantStatus_Live,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.services import order_eligibility, round_release
from orders_app.controllers.services.order_pricing import PRICING_VERSION_CORRECTED
from orders_app.controllers.services.round_placement import (
    business_date_for,
    visit_state,
)
from orders_app.models import (
    PAYMENT_TIMING_NOT_CHOSEN,
    Bill,
    Order,
    OrderAcceptance,
    OrderItem,
    OrderPlacement,
    Visit,
)
from restaurants_app.models import (
    DiningArea,
    MenuItem,
    MenuSection,
    Restaurant,
    RestaurantEmployee,
    Table,
)
from users_app.models import User

EAT = ZoneInfo(settings.TIME_ZONE)

FLOOR_URL = '/api/v1/floor/'
ACTIVE_URL = '/api/v1/kitchen/orders/active/'
COMPLETED_URL = '/api/v1/kitchen/orders/completed/'


def table_url(pk):
    return f'/api/v1/floor/tables/{pk}/'


def confirm_url(pk):
    return f'/api/v1/floor/rounds/{pk}/confirm/'


def decline_url(pk):
    return f'/api/v1/floor/rounds/{pk}/decline/'


ONION_OPTIONS = {'hasModifiers': True, 'groups': [{
    'id': 'g-onion', 'name': 'Onion', 'minSelections': 0, 'maxSelections': 1,
    'choices': [{'id': 'c-none', 'name': 'No onion', 'additionalCost': 0}],
}]}


class FloorFixture:
    """A restaurant, its staff, three tables and a small menu, built per test.

    Shared by the TestCase suites here and the TransactionTestCase races. The
    numbers in the phone numbers are per-instance so the two can coexist.
    """

    PHONE_BASE = 256700510000

    def build_floor(self):
        self._phone = self.PHONE_BASE
        self.owner = self._user()
        self.restaurant = Restaurant.objects.create(
            name='Floor R', location='Kampala', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True, country='UG')
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        # `staff` holds `tables` only; `kitchen` holds `kitchen` only (the coded
        # defaults in restaurants_app/configs/role_defaults.py).
        self.staff = self._member(self.restaurant, [RESTAURANT_STAFF])
        self.kitchen = self._member(self.restaurant, [RESTAURANT_KITCHEN])

        self.area = DiningArea.objects.create(restaurant=self.restaurant, name='Main')
        self.table1 = self._table(1)
        self.table2 = self._table(2)
        self.table3 = self._table(3)

        self.section = MenuSection.objects.create(
            restaurant=self.restaurant, name='Mains', approved=True, enabled=True,
            available=True)
        self.avocado = MenuItem.objects.create(
            section=self.section, name='Avocado', primary_price=2000, is_extra=True,
            available=True, in_stock=True, approved=True, enabled=True)
        self.dish = MenuItem.objects.create(
            section=self.section, name='Rolex', primary_price=8000,
            available=True, in_stock=True, approved=True, enabled=True,
            has_extras=True, extras_applicable=[str(self.avocado.pk)],
            extras_min_selections=0, extras_max_selections=0)

        self.other_owner = self._user()
        self.other = Restaurant.objects.create(
            name='Other R', location='Entebbe', owner=self.other_owner,
            status=RestaurantStatus_Live, accepting_orders=True, country='UG')
        RestaurantEmployee.objects.create(
            user=self.other_owner, restaurant=self.other, roles=[RESTAURANT_OWNER])
        self.other_table = Table.objects.create(restaurant=self.other, number=1)

    # --- builders ---------------------------------------------------------

    def _user(self):
        self._phone += 1
        phone = str(self._phone)
        return User.objects.create_user(
            first_name='F', last_name='L', email=f'{phone}@floor.test',
            phone_number=phone, username=phone, country='Uganda',
            password='pw', roles=[])

    def _member(self, restaurant, roles):
        user = self._user()
        RestaurantEmployee.objects.create(user=user, restaurant=restaurant, roles=roles)
        return user

    def _table(self, number, **over):
        defaults = dict(restaurant=self.restaurant, dining_area=self.area,
                        number=number, qr_mode='order_only')
        defaults.update(over)
        return Table.objects.create(**defaults)

    def make_round(self, table=None, *, held=True, placed=True, accepted=True,
                   created_by=None, accepted_at=None, actual_cost='16000.00',
                   with_extra=False, modified=False, quantity=2, **order_over):
        """One round: an accepted order with real lines, optionally placed.

        ``held=True`` fabricates what BE-F will write (a placement with no release);
        ``held=False`` is what Step 1's writer writes today (released at acceptance);
        ``placed=False`` is an order from before Step 1 (or with the writer off).
        """
        table = table or self.table1
        accepted_at = accepted_at or (timezone.now() - timedelta(minutes=2))
        fields = dict(
            restaurant=table.restaurant, table=table,
            total_cost=actual_cost, discounted_cost=actual_cost, savings=0,
            actual_cost=actual_cost,
            order_status=OrderStatus_Pending, payment_status=PaymentStatus_Pending,
            fulfilment_status='new', order_date=timezone.localdate(),
            pricing_version=PRICING_VERSION_CORRECTED, created_by=created_by)
        fields.update(order_over)
        order = Order.objects.create(**fields)
        main = OrderItem.objects.create(
            order=order, item=self.dish, quantity=quantity, available=True,
            unit_price=8000, discounted_price=8000, unit_cost_of_options=0,
            total_cost=8000 * quantity, discounted_cost=8000 * quantity, savings=0,
            cost_of_options=0, actual_cost=8000 * quantity,
            item_name_snapshot='Rolex',
            selected_modifiers={'g-onion': ['c-none']} if modified else {},
            modifiers_snapshot=['Onion: No onion'] if modified else [])
        if with_extra:
            OrderItem.objects.create(
                order=order, item=self.avocado, parent_item=main, quantity=1,
                available=True, unit_price=2000, discounted_price=2000,
                unit_cost_of_options=0, total_cost=2000, discounted_cost=2000,
                savings=0, cost_of_options=0, actual_cost=2000,
                item_name_snapshot='Avocado')
        if accepted:
            OrderAcceptance.objects.create(
                order=order, accepted_at=accepted_at, quote_ref='ref')
        if placed:
            visit = Visit.objects.create(
                restaurant_id=order.restaurant_id, table_id=order.table_id,
                opened_at=accepted_at, opened_by=created_by,
                payment_timing=PAYMENT_TIMING_NOT_CHOSEN)
            bill = Bill.objects.create(visit=visit)
            OrderPlacement.objects.create(
                order=order, visit=visit, bill=bill,
                payment_timing=PAYMENT_TIMING_NOT_CHOSEN,
                released_at=None if held else accepted_at,
                business_date=business_date_for(accepted_at, time(4, 0)),
                round_number=1)
        return order

    # --- requests -----------------------------------------------------------

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def confirm(self, order, user=None, restaurant=None):
        return self.client_for(user or self.staff).post(
            confirm_url(order.pk),
            {'restaurant': str(restaurant or self.restaurant.pk)}, format='json')

    def decline(self, order, user=None, restaurant=None, **extra):
        body = {'restaurant': str(restaurant or self.restaurant.pk)}
        body.update(extra)
        return self.client_for(user or self.staff).post(
            decline_url(order.pk), body, format='json')

    # --- what a refusal must leave untouched --------------------------------

    def snapshot(self, order):
        """Every column either command can write, to prove a refusal wrote nothing."""
        row = Order.objects.values(
            'order_status', 'fulfilment_status', 'cancelled_at', 'cancelled_by',
            'cancellation_reason', 'fulfilment_revision', 'time_last_updated',
            'priority', 'served_at',
        ).get(pk=order.pk)
        placement = OrderPlacement.objects.filter(order=order).values(
            'released_at', 'released_by', 'business_date').first()
        return row, placement


class FloorTestBase(FloorFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.build_floor()


# --- Confirm: the write --------------------------------------------------------------

class ConfirmReleasesAHeldRoundTests(FloorTestBase):

    def test_confirm_writes_the_release_and_nothing_on_the_order(self):
        order = self.make_round()
        before_row, _ = self.snapshot(order)

        response = self.confirm(order)

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['data']['changed'])
        self.assertEqual(body['data']['round']['state'], 'released')
        self.assertEqual(body['data']['round']['order_id'], str(order.pk))
        self.assertIsNone(body['data']['round']['held_since'])

        placement = OrderPlacement.objects.get(order=order)
        self.assertIsNotNone(placement.released_at)
        self.assertEqual(placement.released_by_id, self.staff.pk)
        self.assertEqual(
            placement.business_date,
            business_date_for(placement.released_at, time(4, 0)))
        self.assertEqual(
            datetime.fromisoformat(body['data']['round']['released_at']),
            placement.released_at.replace(
                microsecond=placement.released_at.microsecond // 1000 * 1000))
        # Releasing is not a kitchen command: the order row is not written at all.
        after_row, _ = self.snapshot(order)
        self.assertEqual(after_row, before_row)

    def test_the_round_reaches_the_kitchen_feed_aged_from_release(self):
        accepted = timezone.now() - timedelta(minutes=20)
        order = self.make_round(accepted_at=accepted)
        feed = self.client_for(self.kitchen).get(
            ACTIVE_URL, {'restaurant': str(self.restaurant.pk)})
        self.assertNotIn(str(order.pk), [t['id'] for t in feed.json()['data']])

        self.assertEqual(self.confirm(order).status_code, 200)

        feed = self.client_for(self.kitchen).get(
            ACTIVE_URL, {'restaurant': str(self.restaurant.pk)})
        tickets = {t['id']: t for t in feed.json()['data']}
        self.assertIn(str(order.pk), tickets)
        released_at = OrderPlacement.objects.get(order=order).released_at
        ticket_release = datetime.fromisoformat(
            tickets[str(order.pk)]['released_at'].replace('Z', '+00:00'))
        self.assertEqual(ticket_release, released_at)
        # Aged from the release, not the acceptance twenty minutes earlier.
        self.assertGreater(ticket_release, accepted + timedelta(minutes=19))

    def test_a_round_accepted_at_0350_and_confirmed_at_0410_gets_the_new_business_day(self):
        accepted = datetime(2026, 11, 3, 3, 50, tzinfo=EAT)
        confirmed = datetime(2026, 11, 3, 4, 10, tzinfo=EAT)
        order = self.make_round(accepted_at=accepted)
        placement = OrderPlacement.objects.get(order=order)
        # Provisionally the acceptance's business day: before the 04:00 cut-off.
        self.assertEqual(str(placement.business_date), '2026-11-02')

        with mock.patch('django.utils.timezone.now', return_value=confirmed):
            response = self.confirm(order)

        self.assertEqual(response.status_code, 200, response.content)
        placement.refresh_from_db()
        self.assertEqual(placement.released_at, confirmed)
        self.assertEqual(str(placement.business_date), '2026-11-03')

    def test_a_restaurants_own_cut_off_is_used(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            business_day_cutoff=time(5, 0))
        order = self.make_round(accepted_at=datetime(2026, 11, 3, 3, 50, tzinfo=EAT))
        with mock.patch('django.utils.timezone.now',
                        return_value=datetime(2026, 11, 3, 4, 10, tzinfo=EAT)):
            self.assertEqual(self.confirm(order).status_code, 200)
        self.assertEqual(
            str(OrderPlacement.objects.get(order=order).business_date), '2026-11-02')


# --- Confirm: every refusal leaves the round held -------------------------------------

class ConfirmRefusalTests(FloorTestBase):

    def assertRefusedHeld(self, response, order, reason):
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body['reason'], reason)
        self.assertEqual(body['state'], 'held')
        self.assertEqual(body['data']['round']['state'], 'held')
        self.assertEqual(body['status'], 409)
        placement = OrderPlacement.objects.get(order=order)
        self.assertIsNone(placement.released_at)
        self.assertIsNone(placement.released_by_id)
        return body

    def _refused_with_no_write(self, order, reason):
        before = self.snapshot(order)
        body = self.assertRefusedHeld(self.confirm(order), order, reason)
        self.assertEqual(self.snapshot(order), before)
        return body

    def test_a_diner_round_at_a_restaurant_not_trading(self):
        """``admit`` refuses a diner round while the restaurant is still onboarding
        (the launch boundary), and the staff portal still grants ``tables`` there."""
        order = self.make_round()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        self._refused_with_no_write(order, 'restaurant_not_trading')

    def test_a_suspended_restaurant_is_a_404_before_admission(self):
        """THE FINDING. Portal access is lifecycle-filtered, so at a suspended
        restaurant no member of staff holds ``tables``: the module gate answers the
        one 404 before ``admit`` is reached. Nothing is written."""
        order = self.make_round()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Suspended)
        before = self.snapshot(order)
        for user in (self.staff, self.owner):
            with self.subTest(user=user.username):
                response = self.confirm(order, user=user)
                self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(self.snapshot(order), before)

    def test_paused_ordering_leaves_a_diner_round_held(self):
        order = self.make_round()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(accepting_orders=False)
        self._refused_with_no_write(order, 'ordering_paused')

    def test_a_menu_only_table_leaves_a_diner_round_held(self):
        order = self.make_round()
        Table.objects.filter(pk=self.table1.pk).update(qr_mode='menu_only')
        self._refused_with_no_write(order, 'table_menu_only')

    def test_a_table_out_of_service_leaves_a_diner_round_held(self):
        order = self.make_round()
        Table.objects.filter(pk=self.table1.pk).update(
            status='out_of_service', is_active=False)
        self._refused_with_no_write(order, 'table_out_of_service')

    def test_a_sold_out_item_cannot_be_made(self):
        order = self.make_round()
        MenuItem.objects.filter(pk=self.dish.pk).update(in_stock=False)
        body = self._refused_with_no_write(order, 'cannot_be_made')
        self.assertEqual(body['item_name'], 'Rolex')
        self.assertEqual(body['cannot_be_made'],
                         {'reason': 'item_sold_out', 'item_name': 'Rolex'})

    def test_a_deleted_item_cannot_be_made(self):
        order = self.make_round()
        MenuItem.objects.filter(pk=self.dish.pk).update(deleted=True)
        body = self._refused_with_no_write(order, 'cannot_be_made')
        self.assertEqual(body['item_name'], 'Rolex')
        self.assertEqual(body['cannot_be_made']['reason'], 'item_deleted')

    def test_changed_modifiers_cannot_be_made(self):
        MenuItem.objects.filter(pk=self.dish.pk).update(options=ONION_OPTIONS)
        order = self.make_round(modified=True)
        relabelled = {'hasModifiers': True, 'groups': [{
            **ONION_OPTIONS['groups'][0],
            'choices': [{'id': 'c-none', 'name': 'Extra onion', 'additionalCost': 0}],
        }]}
        MenuItem.objects.filter(pk=self.dish.pk).update(options=relabelled)
        body = self._refused_with_no_write(order, 'cannot_be_made')
        self.assertEqual(body['cannot_be_made']['reason'],
                         'modifier_selection_meaning_changed')

    def test_an_unchanged_modified_round_is_confirmed(self):
        """Control for the modifier case: the fixture itself is makeable."""
        MenuItem.objects.filter(pk=self.dish.pk).update(options=ONION_OPTIONS)
        order = self.make_round(modified=True)
        self.assertEqual(self.confirm(order).status_code, 200)

    def test_a_sold_out_extra_cannot_be_made_and_is_named(self):
        order = self.make_round(with_extra=True)
        MenuItem.objects.filter(pk=self.avocado.pk).update(in_stock=False)
        body = self._refused_with_no_write(order, 'cannot_be_made')
        self.assertEqual(body['item_name'], 'Avocado')

    def test_a_refusal_is_never_written_as_a_quote_closure(self):
        from orders_app.models import OrderQuoteClosure
        order = self.make_round()
        MenuItem.objects.filter(pk=self.dish.pk).update(in_stock=False)
        self.confirm(order)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())


class ConfirmAllowsTests(FloorTestBase):
    """What Confirm deliberately does NOT re-check."""

    def test_a_staff_round_is_confirmed_through_a_pause(self):
        order = self.make_round(created_by=self.staff)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(accepting_orders=False)
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['round']['source'], 'staff')

    def test_a_staff_round_is_confirmed_at_a_menu_only_table(self):
        order = self.make_round(created_by=self.staff)
        Table.objects.filter(pk=self.table1.pk).update(qr_mode='menu_only')
        self.assertEqual(self.confirm(order).status_code, 200)

    def test_a_staff_round_at_a_table_out_of_service_is_still_refused(self):
        """Table LIVENESS binds every provenance at acceptance (D06), so it does
        here too: a staff round passes the pause and the QR policy, not this."""
        order = self.make_round(created_by=self.staff)
        Table.objects.filter(pk=self.table1.pk).update(status='out_of_service')
        response = self.confirm(order)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['reason'], 'table_out_of_service')

    def test_an_item_that_left_its_section_schedule_is_still_confirmed(self):
        order = self.make_round()
        MenuSection.objects.filter(pk=self.section.pk).update(
            availability='scheduled',
            schedules=[{'days': [], 'startTime': '00:00', 'endTime': '00:01'}])
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_unpublished_item_is_still_confirmed(self):
        order = self.make_round()
        MenuItem.objects.filter(pk=self.dish.pk).update(approved=False, enabled=False)
        MenuSection.objects.filter(pk=self.section.pk).update(available=False)
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_expired_quote_is_not_re_checked(self):
        order = self.make_round()
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() - timedelta(days=3))
        self.assertEqual(self.confirm(order).status_code, 200)


# --- Decline ---------------------------------------------------------------------------

class DeclineTests(FloorTestBase):

    def test_decline_writes_the_cancellation_and_bumps_the_revision(self):
        order = self.make_round()
        response = self.decline(order)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()['data']
        self.assertTrue(body['changed'])
        self.assertEqual(body['round']['state'], 'declined')

        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.order_status, OrderStatus_Cancelled)
        self.assertEqual(row.cancellation_reason, CancellationReason_DeclinedByStaff)
        self.assertEqual(row.cancelled_by_id, self.staff.pk)
        self.assertIsNotNone(row.cancelled_at)
        self.assertEqual(row.fulfilment_revision, 1)
        # The placement is not touched: a declined round was never released.
        placement = OrderPlacement.objects.get(order=order)
        self.assertIsNone(placement.released_at)
        self.assertIsNone(placement.released_by_id)

    def test_decline_frees_the_table_and_ends_the_visit(self):
        order = self.make_round()
        self.assertTrue(ConOrder.any_present_ongoing_order(self.table1)['present'])
        self.decline(order)
        self.assertFalse(ConOrder.any_present_ongoing_order(self.table1)['present'])
        visit = OrderPlacement.objects.get(order=order).visit
        self.assertEqual(visit_state(visit), {'state': 'ended', 'payment': 'unknown'})

    def test_a_declined_round_is_on_neither_kitchen_feed(self):
        order = self.make_round()
        self.decline(order)
        client = self.client_for(self.kitchen)
        for url in (ACTIVE_URL, COMPLETED_URL):
            with self.subTest(url=url):
                feed = client.get(url, {'restaurant': str(self.restaurant.pk)})
                self.assertEqual(feed.status_code, 200)
                self.assertNotIn(str(order.pk), [t['id'] for t in feed.json()['data']])

    def test_decline_takes_no_reason_from_the_client(self):
        order = self.make_round()
        self.decline(order, cancellation_reason=CancellationReason_CustomerChangedMind)
        self.assertEqual(Order.objects.get(pk=order.pk).cancellation_reason,
                         CancellationReason_DeclinedByStaff)

    def test_the_kitchen_cannot_send_the_decline_reason(self):
        order = self.make_round(held=False)
        response = self.client_for(self.owner).put(
            f'/api/v1/kitchen/orders/{order.pk}/cancel/',
            {'cancellation_reason': CancellationReason_DeclinedByStaff,
             'if_revision': 0}, format='json')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()['reason'], 'cancellation_reason_invalid')
        self.assertEqual(Order.objects.get(pk=order.pk).order_status, OrderStatus_Pending)

    def test_a_kitchen_command_formed_against_the_round_is_now_stale(self):
        order = self.make_round()
        self.decline(order)
        response = self.client_for(self.owner).put(
            f'/api/v1/kitchen/orders/{order.pk}/fulfilment-status/',
            {'action': 'advance', 'if_revision': 0}, format='json')
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(Order.objects.get(pk=order.pk).fulfilment_status, 'new')

    def test_decline_ignores_a_pause_and_a_table_out_of_service(self):
        """Declining admits no new work, so neither operational rule applies."""
        order = self.make_round()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(accepting_orders=False)
        Table.objects.filter(pk=self.table1.pk).update(status='out_of_service')
        self.assertEqual(self.decline(order).status_code, 200)


# --- idempotency and crossings (decision 5) --------------------------------------------

class IdempotencyAndCrossingTests(FloorTestBase):

    def test_confirm_twice_is_changed_false_and_writes_nothing_more(self):
        order = self.make_round()
        self.assertTrue(self.confirm(order).json()['data']['changed'])
        first = self.snapshot(order)
        again = self.confirm(order, user=self.owner)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertFalse(again.json()['data']['changed'])
        self.assertEqual(again.json()['data']['round']['state'], 'released')
        self.assertEqual(self.snapshot(order), first)
        # released_by still names who released it, not who resent.
        self.assertEqual(OrderPlacement.objects.get(order=order).released_by_id,
                         self.staff.pk)

    def test_confirm_on_a_round_released_at_acceptance_is_changed_false(self):
        order = self.make_round(held=False)
        before = self.snapshot(order)
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['data']['changed'])
        self.assertEqual(self.snapshot(order), before)

    def test_confirm_on_an_unplaced_order_is_changed_false(self):
        order = self.make_round(placed=False)
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.content)
        round_ = response.json()['data']['round']
        self.assertFalse(response.json()['data']['changed'])
        self.assertEqual((round_['state'], round_['round_number'], round_['released_at']),
                         ('released', 1, None))

    def test_confirm_on_a_declined_round_is_round_declined(self):
        order = self.make_round()
        self.decline(order)
        before = self.snapshot(order)
        response = self.confirm(order)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['reason'], 'round_declined')
        self.assertEqual(response.json()['state'], 'declined')
        self.assertEqual(self.snapshot(order), before)

    def test_decline_twice_is_changed_false_and_writes_nothing_more(self):
        order = self.make_round()
        self.decline(order)
        first = self.snapshot(order)
        again = self.decline(order, user=self.owner)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertFalse(again.json()['data']['changed'])
        self.assertEqual(self.snapshot(order), first)
        row = Order.objects.get(pk=order.pk)
        self.assertEqual((row.fulfilment_revision, row.cancelled_by_id), (1, self.staff.pk))

    def test_decline_on_a_released_round_is_round_released(self):
        for label, order in (('confirmed', self.make_round(table=self.table1)),
                             ('released at acceptance',
                              self.make_round(table=self.table2, held=False)),
                             ('unplaced', self.make_round(table=self.table3,
                                                         placed=False))):
            with self.subTest(label):
                if label == 'confirmed':
                    self.confirm(order)
                before = self.snapshot(order)
                response = self.decline(order)
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()['reason'], 'round_released')
                self.assertEqual(response.json()['state'], 'released')
                self.assertEqual(self.snapshot(order), before)

    def test_anything_else_not_held_is_round_not_held(self):
        cases = {
            'served': dict(order_status=OrderStatus_Served, fulfilment_status='served',
                           served_at=timezone.now()),
            'cancelled': dict(order_status=OrderStatus_Cancelled,
                              cancellation_reason=CancellationReason_CustomerChangedMind,
                              cancelled_at=timezone.now()),
            'draft': dict(order_status=OrderStatus_Initiated, placed=False,
                          accepted=False),
        }
        tables = iter((self.table1, self.table2, self.table3))
        for state, over in cases.items():
            # Served and kitchen-cancelled rounds were released; a draft is unplaced.
            order = self.make_round(table=next(tables), held=False, **over)
            for command in (self.confirm, self.decline):
                with self.subTest(state=state, command=command.__name__):
                    before = self.snapshot(order)
                    response = command(order)
                    self.assertEqual(response.status_code, 409, response.content)
                    self.assertEqual(response.json()['reason'], 'round_not_held')
                    self.assertEqual(response.json()['state'], state)
                    self.assertEqual(self.snapshot(order), before)

    def test_the_409_envelope(self):
        order = self.make_round(held=False)
        body = self.decline(order).json()
        self.assertEqual(set(body), {'status', 'message', 'reason', 'state', 'data'})
        self.assertEqual(set(body['data']), {'round'})


# --- permission and tenancy (decision 6) ----------------------------------------------

class PermissionTests(FloorTestBase):

    def test_the_tables_module_is_required(self):
        order = self.make_round()
        before = self.snapshot(order)
        for command in (self.confirm, self.decline):
            with self.subTest(command=command.__name__):
                response = command(order, user=self.kitchen)
                self.assertEqual(response.status_code, 404, response.content)
                self.assertEqual(response.json(), {'status': 404, 'message': 'Not found'})
        self.assertEqual(self.snapshot(order), before)

    def test_a_user_from_another_restaurant_gets_404(self):
        order = self.make_round()
        before = self.snapshot(order)
        for restaurant in (self.restaurant.pk, self.other.pk):
            for command in (self.confirm, self.decline):
                with self.subTest(restaurant=str(restaurant), command=command.__name__):
                    response = command(order, user=self.other_owner,
                                       restaurant=restaurant)
                    self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(self.snapshot(order), before)

    def test_an_order_outside_the_named_restaurant_is_404(self):
        """The body's restaurant may only MATCH the order's."""
        mine = self.make_round()
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.other, roles=[RESTAURANT_STAFF])
        response = self.confirm(mine, restaurant=self.other.pk)
        self.assertEqual(response.status_code, 404, response.content)
        self.assertIsNone(OrderPlacement.objects.get(order=mine).released_at)

    def test_unknown_malformed_and_missing(self):
        client = self.client_for(self.staff)
        order = self.make_round()
        rid = str(self.restaurant.pk)
        cases = [
            (confirm_url('00000000-0000-0000-0000-000000000000'), {'restaurant': rid}, 404),
            (confirm_url('not-a-uuid'), {'restaurant': rid}, 404),
            (confirm_url(order.pk), {'restaurant': 'not-a-uuid'}, 404),
            (confirm_url(order.pk), {'restaurant': 7}, 404),
            (confirm_url(order.pk), {}, 400),
            (confirm_url(order.pk), {'restaurant': '  '}, 400),
            (decline_url(order.pk), {}, 400),
        ]
        for url, body, status in cases:
            with self.subTest(url=url, body=body):
                response = client.post(url, body, format='json')
                self.assertEqual(response.status_code, status, response.content)
        response = client.post(confirm_url(order.pk), [rid], format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(OrderPlacement.objects.get(order=order).released_at)

    def test_unauthenticated_is_401(self):
        order = self.make_round()
        response = APIClient().post(confirm_url(order.pk),
                                    {'restaurant': str(self.restaurant.pk)},
                                    format='json')
        self.assertEqual(response.status_code, 401)

    def test_the_tables_module_is_re_checked_under_the_lock(self):
        """The pre-lock gate passes (patched, standing in for a revocation that
        commits while the command waits on its locks); the re-check under the lock
        sees the membership deactivated and refuses with the same 404."""
        order = self.make_round()
        held = self.make_round(table=self.table2)
        RestaurantEmployee.objects.filter(user=self.staff).update(active=False)
        with mock.patch.object(round_release, 'can_user_access_module',
                               return_value=True):
            for command, target in ((self.confirm, order), (self.decline, held)):
                with self.subTest(command=command.__name__):
                    before = self.snapshot(target)
                    response = command(target)
                    self.assertEqual(response.status_code, 404, response.content)
                    self.assertEqual(self.snapshot(target), before)

    def test_responses_are_no_store(self):
        order = self.make_round()
        for response in (self.confirm(order), self.decline(order),
                         self.confirm(order, user=self.kitchen)):
            self.assertIn('no-store', response['Cache-Control'])


class DelegatedSessionTests(FloorTestBase):
    """The delegated-session allow-list refuses all four floor routes."""

    def test_no_floor_route_is_on_the_allow_list(self):
        from platform_admin_app.configs.delegation_scopes import ALLOWED_ROUTES
        self.assertEqual([r for (r, _m) in ALLOWED_ROUTES if 'floor' in r], [])

    def test_a_delegated_session_is_refused_on_all_four_routes(self):
        from platform_admin_app import delegated_sessions, delegation
        from platform_admin_app.configs.delegation_scopes import SCOPE_VIEW
        from platform_admin_app.delegated_middleware import SESSION_HEADER
        from platform_admin_app.models import SCOPE_SUPPORT
        from dinify_backend.configss.string_definitions import (
            ACCOUNT_TYPE_PLATFORM_STAFF)

        admin = User.objects.create_user(
            first_name='P', last_name='S', email='floor-admin@t.com',
            username='floor-admin', phone_number=None, country='Uganda',
            password='correct-horse-battery', roles=[],
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        order = self.make_round()
        meta = 'HTTP_' + SESSION_HEADER.upper().replace('-', '_')
        rid = str(self.restaurant.pk)
        for scope in (SCOPE_VIEW, SCOPE_SUPPORT):
            raw_code, _grant = delegation.mint_grant(
                administrator=admin, admin_session=None, restaurant=self.restaurant,
                scope=scope, reason='Diagnosing a reported problem on the floor.')
            raw_token, _context = delegated_sessions.exchange_code(raw_code)
            headers = {meta: raw_token}
            requests = [
                ('get', FLOOR_URL, {'restaurant': rid}),
                ('get', table_url(self.table1.pk), {'restaurant': rid}),
                ('post', confirm_url(order.pk), {'restaurant': rid}),
                ('post', decline_url(order.pk), {'restaurant': rid}),
            ]
            for method, url, data in requests:
                with self.subTest(scope=scope, url=url):
                    if method == 'get':
                        response = self.client.get(url, data, **headers)
                    else:
                        response = self.client.post(
                            url, data, content_type='application/json', **headers)
                    self.assertEqual(response.status_code, 403, response.content)
        self.assertIsNone(OrderPlacement.objects.get(order=order).released_at)
        self.assertEqual(Order.objects.get(pk=order.pk).order_status, OrderStatus_Pending)


# --- the switches ----------------------------------------------------------------------

class NeitherSwitchIsConsultedTests(FloorTestBase):

    @override_settings(DINING_VISIT_WRITES_ENABLED=False)
    def test_confirm_and_decline_work_with_the_writer_switched_off(self):
        confirmed = self.make_round(table=self.table1)
        declined = self.make_round(table=self.table2)
        self.assertEqual(self.confirm(confirmed).status_code, 200)
        self.assertEqual(self.decline(declined).status_code, 200)
        self.assertIsNotNone(OrderPlacement.objects.get(order=confirmed).released_at)
        self.assertEqual(Order.objects.get(pk=declined.pk).cancellation_reason,
                         CancellationReason_DeclinedByStaff)

    def test_the_module_reads_no_switch(self):
        """No settings import, no ``writes_enabled``, no switch name outside a
        docstring: there is nothing BE-F's switch could be wired to either."""
        tree = ast.parse(pyinspect.getsource(round_release))
        docstrings = {
            id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        imported = {alias.asname or alias.name for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        modules = {node.module for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and id(n) not in docstrings}
        self.assertNotIn('django.conf', modules)
        self.assertNotIn('settings', imported | names)
        self.assertNotIn('writes_enabled', imported | names | attributes)
        self.assertFalse([s for s in strings if 'ENABLED' in s or 'HOLD' in s])


# --- the cost --------------------------------------------------------------------------

class QueryCountTests(FloorTestBase):
    """EXACT counts (TestCase, so each includes a SAVEPOINT pair). Measured:

    confirm (held -> released) ..... 15  pre-lock gate 2, locator 1, savepoint,
                                         advisory, admit read, Table, Order, gate 2,
                                         lines 1, catalogue 1, cut-off 1, UPDATE, release
    confirm (already released) ..... 11  stops after the authority re-check
    decline (held -> declined) ..... 10  pre-lock gate 2, locator 1, savepoint,
                                         Table, Order, gate 2, UPDATE, release
    """

    def _count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            response = fn()
        self.assertIn(response.status_code, (200, 409), response.content)
        return len(ctx.captured_queries)

    def test_confirm(self):
        order = self.make_round()
        self.assertEqual(self._count(lambda: self.confirm(order)), 15)

    def test_confirm_with_more_lines_costs_the_same(self):
        order = self.make_round(with_extra=True)
        self.assertEqual(self._count(lambda: self.confirm(order)), 15)

    def test_confirm_resent(self):
        order = self.make_round(held=False)
        self.assertEqual(self._count(lambda: self.confirm(order)), 11)

    def test_decline(self):
        order = self.make_round()
        self.assertEqual(self._count(lambda: self.decline(order)), 10)


# --- structure -------------------------------------------------------------------------

class StructureTests(TestCase):

    def test_the_eligibility_mapping_is_exhaustive(self):
        self.assertEqual(set(round_release.ELIGIBILITY_REASONS),
                         set(order_eligibility.TRANSIENT_REASONS))
        self.assertEqual(
            set(round_release.ELIGIBILITY_REASONS.values()),
            {'ordering_paused', 'table_menu_only', 'table_out_of_service',
             'restaurant_not_trading'})

    def test_the_decline_reason_is_outside_the_kitchen_vocabulary(self):
        self.assertNotIn(CancellationReason_DeclinedByStaff, CANCELLATION_REASONS)
        choices = dict(Order._meta.get_field('cancellation_reason').choices)
        self.assertIn(CancellationReason_DeclinedByStaff, choices)

    def test_confirm_takes_the_admission_lock_then_table_then_order(self):
        """THE LOCK ORDER, read off the statements Confirm actually issues."""
        if connection.vendor != 'postgresql':
            self.skipTest('the advisory lock is PostgreSQL-only')
        fixture = FloorFixture()
        fixture.build_floor()
        order = fixture.make_round()
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(fixture.confirm(order).status_code, 200)
        statements = [q['sql'] for q in ctx.captured_queries]

        def first(predicate):
            return next(i for i, sql in enumerate(statements) if predicate(sql))

        advisory = first(lambda s: 'pg_advisory_xact_lock_shared' in s)
        table = first(lambda s: 'FOR UPDATE' in s and s.lstrip().startswith(
            'SELECT') and 'FROM "tables"' in s)
        orders = first(lambda s: 'FOR UPDATE' in s and 'FROM "orders"' in s)
        update = first(lambda s: s.startswith('UPDATE "order_placements"'))
        self.assertLess(advisory, table)
        self.assertLess(table, orders)
        self.assertLess(orders, update)
        self.assertIn('FOR UPDATE OF "tables"', statements[table])
        self.assertIn('FOR UPDATE OF "orders"', statements[orders])

    def test_decline_takes_table_then_order_and_no_advisory_lock(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL statement shapes')
        fixture = FloorFixture()
        fixture.build_floor()
        order = fixture.make_round()
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(fixture.decline(order).status_code, 200)
        statements = [q['sql'] for q in ctx.captured_queries]
        self.assertFalse([s for s in statements if 'pg_advisory' in s])
        table = next(i for i, s in enumerate(statements)
                     if 'FOR UPDATE OF "tables"' in s)
        orders = next(i for i, s in enumerate(statements)
                      if 'FOR UPDATE OF "orders"' in s)
        self.assertLess(table, orders)

    def test_confirm_never_writes_a_quote_closure_or_the_order(self):
        tree = ast.parse(Path(round_release.__file__).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn('quote_closure', names | attributes)
        self.assertNotIn('OrderQuoteClosure', names)
