"""
Step 2, PR BE-A — the table actions write only what they decide, under a lock.

Seat, clear, transfer and the floor-plan save each read their table(s) unlocked
and then called a bare ``table.save()``: a FULL-ROW write of every column from an
instance loaded before the decision. So anything committed in between was
silently reverted, and the column that matters most is ``qr_version`` —
regenerating a table's QR bumps it, and that bump is what revokes every diner
credential and session for the table. ``_update_status`` was fixed the same way
in D06 (``tests_admission_writers``); this module covers the other four.

Two further decisions (3 November 2026) are pinned here:

* CLEAR and TRANSFER refuse **409 ``table_has_open_visit``** when the table has an
  order that occupies it, by THE one occupancy rule
  (``orders_app.controllers.occupancy.ongoing_order_q``). A transfer moves NO
  orders, so the rule binds the SOURCE (its order would be left on a table the
  floor calls empty) and the DESTINATION (a second party seated onto an open
  bill). A served or cancelled order, or a draft, does not occupy and does not
  block. SEAT is unchanged apart from its narrowed write.
* The FLOOR-PLAN save locks every row in ONE statement, in PRIMARY-KEY order, and
  writes geometry only.

WHERE A LOCK IS THE POINT the test is a ``TransactionTestCase`` on real
PostgreSQL with a second connection (Django gives each thread its own), and a
lock is proved POSITIVELY — a ``FOR UPDATE NOWAIT`` probe from another
connection raises — never by watching something fail to finish. The QR
regeneration in the "survives" tests is COMMITTED on another connection at the
real seam between the action's unlocked read and its transaction (the module
gate), so it is a committed write the old full save reverted, not a value
planted inside the action's own transaction.

The floor-plan lock ORDER is pinned two ways: STRUCTURALLY (exactly one locking
statement, ``ORDER BY "tables"."id" ASC``) and by OBSERVATION (with the higher
key held elsewhere and the client sending the rows highest-first, the save is
seen holding the LOWER key while it waits). No deadlock is provoked: a test that
needs two transactions to deadlock is timing-dependent, and the observation
proves the same property deterministically.
"""
import threading
import time
from unittest.mock import patch

from django.db import (
    OperationalError, close_old_connections, connection, transaction,
)
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.models import Order
from restaurants_app.endpoints import table_actions
from restaurants_app.models import (
    DiningArea, Reservation, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


BASE = '/api/v1/restaurant-setup/table-actions/'
GATE = 'restaurants_app.endpoints.table_actions.can_user_access_module'
WAIT = 10  # seconds; every wait is bounded so a deadlock fails loudly


class _Fixture:
    """Shared fixture for both test-case flavours."""

    def make_world(self):
        self.owner = User.objects.create_user(
            first_name='Floor', last_name='Owner', email='floor@test.com',
            phone_number='256700000881', username='256700000881',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Floor R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant)
        self.table = self.make_table(1)
        self.other = self.make_table(2)
        self.token = str(RefreshToken.for_user(self.owner).access_token)
        self._n = 0

    def make_table(self, number, **kw):
        kw.setdefault('qr_mode', 'order_only')
        return Table.objects.create(
            number=number, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, has_qr=True, **kw,
        )

    def make_order(self, table, *, order_status='pending',
                   fulfilment_status='new'):
        self._n += 1
        return Order.objects.create(
            restaurant=self.restaurant, table=table,
            order_number=81000 + self._n,
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status=order_status, fulfilment_status=fulfilment_status,
        )

    def post(self, action, body, on_gate=None):
        """POST through the real client with a real bearer token (the endpoint
        decodes the JWT itself). ``on_gate`` runs at the module gate: after the
        action's unlocked read and before its transaction."""
        def _gate(*args, **kwargs):
            if on_gate is not None:
                on_gate()
            return True

        with patch(GATE, side_effect=_gate):
            return self.client.post(
                f'{BASE}{action}/', data=body, content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {self.token}',
            )

    @staticmethod
    def body(response):
        return response.json()


def _run_in_thread(fn):
    """Run ``fn`` on its own database connection, to completion."""
    errors = []

    def _target():
        try:
            fn()
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            close_old_connections()
            connection.close()

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(WAIT)
    if thread.is_alive():
        raise AssertionError('the competing write never finished')
    if errors:
        raise errors[0]


# ----------------------------------------------------------------------------
# 1. A QR regeneration COMMITTED between the read and the write survives.
# ----------------------------------------------------------------------------
@skipUnlessDBFeature('has_select_for_update')
class QrRegenerationSurvivesEachActionTests(_Fixture, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.make_world()

    def regenerate(self, *tables):
        """Commit a QR regeneration on ANOTHER connection, as regenerate-qr does."""
        from django.db.models import F

        def _commit():
            Table.objects.filter(pk__in=[t.pk for t in tables]).update(
                qr_version=F('qr_version') + 1, qr_mode='menu_only')
        return lambda: _run_in_thread(_commit)

    def assert_regenerated(self, *tables):
        for table in tables:
            table.refresh_from_db()
            self.assertEqual(
                table.qr_version, 2,
                'a table action wrote the old QR generation back, un-revoking '
                'every diner credential the regeneration had revoked',
            )
            self.assertEqual(table.qr_mode, 'menu_only')

    def test_seat(self):
        response = self.post('seat', {'table_id': str(self.table.id)},
                             on_gate=self.regenerate(self.table))
        self.assertEqual(response.status_code, 200, response.content)
        self.assert_regenerated(self.table)
        self.assertEqual(self.table.status, 'seated')

    def test_clear(self):
        response = self.post('clear', {'table_id': str(self.table.id)},
                             on_gate=self.regenerate(self.table))
        self.assertEqual(response.status_code, 200, response.content)
        self.assert_regenerated(self.table)
        self.assertEqual(self.table.status, 'dirty')

    def test_transfer(self):
        response = self.post(
            'transfer',
            {'source_table_id': str(self.table.id),
             'destination_table_id': str(self.other.id)},
            on_gate=self.regenerate(self.table, self.other),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assert_regenerated(self.table, self.other)
        self.assertEqual(self.table.status, 'dirty')
        self.assertEqual(self.other.status, 'seated')

    def test_floor_plan(self):
        """The floor plan read each row INSIDE its loop, after the gate, so the
        gate seam cannot separate its read from its write. The seam here is the
        write itself: just before the first row is saved, a regeneration is
        started on another connection. Unlocked (the old code) it commits at
        once and the full save then writes the old generation back; locked, it
        waits for the save to commit and applies afterwards."""
        from django.db.models import F

        committed = threading.Event()
        regen = []

        def _regenerate():
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('application_name', 'regen', false)")
                Table.objects.filter(pk__in=[self.table.pk, self.other.pk]).update(
                    qr_version=F('qr_version') + 1, qr_mode='menu_only')
                committed.set()
            finally:
                close_old_connections()
                connection.close()

        original = Table.save

        def _save(self_, *args, **kwargs):
            if not regen:
                thread = threading.Thread(target=_regenerate)
                regen.append(thread)
                thread.start()
                self._wait_committed_or_waiting(committed, 'regen')
            return original(self_, *args, **kwargs)

        with patch.object(Table, 'save', _save):
            response = self.post(
                'update-floor-plan',
                {'restaurant': str(self.restaurant.id), 'tables': [
                    {'id': str(self.table.id), 'floor_x': 11.5},
                    {'id': str(self.other.id), 'floor_y': 22.5},
                ]},
            )
        regen[0].join(WAIT)
        self.assertTrue(committed.is_set(), 'the regeneration never committed')
        self.assertEqual(response.status_code, 200, response.content)
        self.assert_regenerated(self.table, self.other)
        self.assertEqual(self.table.floor_x, 11.5)
        self.assertEqual(self.other.floor_y, 22.5)

    def _wait_committed_or_waiting(self, committed, name):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if committed.is_set():
                return
            with connection.cursor() as cursor:
                # This poll runs INSIDE the action's transaction, and
                # pg_stat_activity is snapshotted per transaction: clear it, or
                # every poll reads the first answer.
                cursor.execute('SELECT pg_stat_clear_snapshot()')
                cursor.execute(
                    'SELECT wait_event_type FROM pg_stat_activity '
                    'WHERE application_name = %s', [name])
                row = cursor.fetchone()
            if row and row[0] == 'Lock':
                return
            time.sleep(0.02)
        self.fail(f'{name} neither committed nor waited on a lock')


# ----------------------------------------------------------------------------
# 2. Each action changes only its own fields.
# ----------------------------------------------------------------------------
class EachActionWritesOnlyItsOwnFieldsTests(_Fixture, TestCase):
    """The structural statement: a writer that writes only what it decided
    cannot revert anything, whether or not anybody thought of the column."""

    def setUp(self):
        super().setUp()
        self.make_world()

    def capture(self, model, action, body):
        captured = []
        original = model.save

        def _capture(self_, *args, **kwargs):
            captured.append(kwargs.get('update_fields'))
            return original(self_, *args, **kwargs)

        with patch.object(model, 'save', _capture):
            response = self.post(action, body)
        self.assertEqual(response.status_code, 200, response.content)
        return [set(fields) if fields is not None else None
                for fields in captured]

    def test_seat_writes_status_only(self):
        self.assertEqual(
            self.capture(Table, 'seat', {'table_id': str(self.table.id)}),
            [{'status'}])

    def test_seat_writes_only_the_reservation_fields_it_decides(self):
        from datetime import timedelta
        from django.utils import timezone
        reservation = Reservation.objects.create(
            restaurant=self.restaurant, guest_name='Guest',
            date_time=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(
            self.capture(Reservation, 'seat', {
                'table_id': str(self.table.id),
                'reservation_id': str(reservation.id)}),
            [{'status', 'seated_at', 'table'}])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, 'seated')
        self.assertEqual(reservation.table_id, self.table.id)

    def test_clear_writes_status_only(self):
        self.assertEqual(
            self.capture(Table, 'clear', {'table_id': str(self.table.id)}),
            [{'status'}])

    def test_transfer_writes_status_only_on_both(self):
        self.assertEqual(
            self.capture(Table, 'transfer', {
                'source_table_id': str(self.table.id),
                'destination_table_id': str(self.other.id)}),
            [{'status'}, {'status'}])

    def test_floor_plan_writes_only_the_geometry_each_entry_sent(self):
        self.assertEqual(
            self.capture(Table, 'update-floor-plan', {
                'restaurant': str(self.restaurant.id), 'tables': [
                    {'id': str(self.table.id), 'floor_x': 1, 'floor_width': 3},
                    {'id': str(self.other.id), 'floor_x': 1, 'floor_y': 2,
                     'floor_width': 3, 'floor_height': 4,
                     'status': 'out_of_service', 'qr_version': 99},
                ]}),
            [{'floor_x', 'floor_width'},
             {'floor_x', 'floor_y', 'floor_width', 'floor_height'}])
        self.other.refresh_from_db()
        self.assertEqual(self.other.status, 'available')
        self.assertEqual(self.other.qr_version, 1)

    def test_floor_plan_semantics_are_unchanged(self):
        """Negative control: skipped entries are still skipped and counted as
        before, and a foreign table is never touched."""
        foreign_owner = User.objects.create_user(
            first_name='F', last_name='O', email='f@test.com',
            phone_number='256700000882', username='256700000882',
            country='Uganda', password='password', roles=[],
        )
        foreign_r = Restaurant.objects.create(
            name='Foreign', location='l', owner=foreign_owner,
            status=RestaurantStatus_Live)
        foreign = Table.objects.create(number=1, restaurant=foreign_r)
        response = self.post('update-floor-plan', {
            'restaurant': str(self.restaurant.id), 'tables': [
                {'floor_x': 5},                                  # no id
                {'id': str(foreign.id), 'floor_x': 5},           # foreign
                {'id': str(self.table.id).upper(), 'floor_x': 7},  # spelling
                {'id': str(self.other.id)},                      # nothing sent
            ]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.body(response)['data'], {'updated_count': 2})
        foreign.refresh_from_db()
        self.table.refresh_from_db()
        self.assertEqual(foreign.floor_x, 50.0)
        self.assertEqual(self.table.floor_x, 7.0)


# ----------------------------------------------------------------------------
# 3. Clear and transfer refuse a table with an open visit.
# ----------------------------------------------------------------------------
class OpenVisitRefusalTests(_Fixture, TestCase):

    def setUp(self):
        super().setUp()
        self.make_world()
        Table.objects.filter(pk=self.table.pk).update(status='seated')

    def assert_refused(self, response):
        self.assertEqual(response.status_code, 409, response.content)
        body = self.body(response)
        self.assertEqual(body['reason'], 'table_has_open_visit')
        self.assertEqual(body['status'], 409)

    def clear(self):
        return self.post('clear', {'table_id': str(self.table.id)})

    def transfer(self):
        return self.post('transfer', {
            'source_table_id': str(self.table.id),
            'destination_table_id': str(self.other.id)})

    # --- clear ----------------------------------------------------------
    def test_clear_refuses_a_table_with_an_ongoing_order(self):
        self.make_order(self.table)
        self.assert_refused(self.clear())
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, 'seated', 'a refusal writes nothing')

    def test_clear_refuses_every_ongoing_fulfilment_state(self):
        for fulfilment in ('new', 'preparing', 'ready'):
            Order.objects.all().delete()
            self.make_order(self.table, fulfilment_status=fulfilment)
            self.assert_refused(self.clear())

    def test_clear_accepts_a_served_order(self):
        self.make_order(self.table, order_status='served',
                        fulfilment_status='served')
        self.assertEqual(self.clear().status_code, 200)

    def test_clear_accepts_a_cancelled_order(self):
        self.make_order(self.table, order_status='cancelled')
        self.assertEqual(self.clear().status_code, 200)

    def test_clear_accepts_a_draft_and_a_soft_deleted_order(self):
        self.make_order(self.table, order_status='initiated')
        Order.objects.filter(pk=self.make_order(self.table).pk).update(
            deleted=True)
        self.assertEqual(self.clear().status_code, 200)

    def test_clear_is_not_blocked_by_another_tables_order(self):
        self.make_order(self.other)
        self.assertEqual(self.clear().status_code, 200)

    # --- transfer -------------------------------------------------------
    def test_transfer_refuses_a_source_with_an_ongoing_order(self):
        self.make_order(self.table)
        self.assert_refused(self.transfer())
        self.table.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.table.status, 'seated')
        self.assertEqual(self.other.status, 'available')

    def test_transfer_refuses_a_destination_with_an_ongoing_order(self):
        self.make_order(self.other)
        self.assert_refused(self.transfer())
        self.table.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.table.status, 'seated')
        self.assertEqual(self.other.status, 'available')

    def test_a_refused_transfer_moves_no_reservation(self):
        from datetime import timedelta
        from django.utils import timezone
        reservation = Reservation.objects.create(
            restaurant=self.restaurant, guest_name='Guest', table=self.table,
            status='seated', date_time=timezone.now() + timedelta(hours=1),
        )
        self.make_order(self.other)
        self.assert_refused(self.transfer())
        reservation.refresh_from_db()
        self.assertEqual(reservation.table_id, self.table.id)

    def test_transfer_accepts_served_and_cancelled_orders_at_both_ends(self):
        self.make_order(self.table, order_status='served',
                        fulfilment_status='served')
        self.make_order(self.other, order_status='cancelled')
        response = self.transfer()
        self.assertEqual(response.status_code, 200, response.content)
        self.table.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.table.status, 'dirty')
        self.assertEqual(self.other.status, 'seated')

    def test_seat_is_unchanged_and_does_not_consult_occupancy(self):
        self.make_order(self.table)
        self.assertEqual(
            self.post('seat', {'table_id': str(self.table.id)}).status_code,
            200)


# ----------------------------------------------------------------------------
# 4. The lock is held while the decision is made, and in primary-key order.
# ----------------------------------------------------------------------------
@skipUnlessDBFeature('has_select_for_update')
class LocksAreRealTests(_Fixture, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.make_world()

    def probe_locked(self, table):
        """True iff another connection holds ``table``'s row lock right now."""
        result = []

        def _probe():
            try:
                with transaction.atomic():
                    list(Table.objects.select_for_update(nowait=True)
                         .filter(pk=table.pk))
                result.append(False)
            except OperationalError:
                result.append(True)
        _run_in_thread(_probe)
        return result[0]

    def run_parked(self, action, body, park_on):
        """Run the action on its own connection, parked inside
        ``_has_ongoing_order`` (i.e. after the lock, at the decision), and
        return the tables a probe found locked at that moment."""
        parked, release = threading.Event(), threading.Event()
        real = table_actions._has_ongoing_order
        responses = []

        def _parked(table_id):
            answer = real(table_id)
            parked.set()
            release.wait(WAIT)
            return answer

        def _request():
            try:
                with patch.object(table_actions, '_has_ongoing_order', _parked):
                    responses.append(self.post(action, body))
            finally:
                parked.set()
                close_old_connections()
                connection.close()

        thread = threading.Thread(target=_request)
        thread.start()
        try:
            self.assertTrue(parked.wait(WAIT), 'the action never reached its decision')
            locked = [self.probe_locked(t) for t in park_on]
        finally:
            release.set()
            thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0].status_code, 200, responses[0].content)
        return locked

    def test_clear_holds_the_table_lock_while_it_decides(self):
        """So no submit (which takes this row first) can make the table occupied
        between the occupancy check and the write."""
        self.assertEqual(
            self.run_parked('clear', {'table_id': str(self.table.id)},
                            [self.table]),
            [True])

    def test_transfer_holds_both_locks_while_it_decides(self):
        self.assertEqual(
            self.run_parked('transfer', {
                'source_table_id': str(self.table.id),
                'destination_table_id': str(self.other.id)},
                [self.table, self.other]),
            [True, True])

    def test_floor_plan_takes_one_locking_statement_in_primary_key_order(self):
        """STRUCTURAL: exactly one FOR UPDATE on `tables`, ordered by the key."""
        low, high = sorted([self.table, self.other], key=lambda t: t.pk)
        with CaptureQueriesContext(connection) as captured:
            response = self.post('update-floor-plan', {
                'restaurant': str(self.restaurant.id), 'tables': [
                    {'id': str(high.id), 'floor_x': 1},
                    {'id': str(low.id), 'floor_x': 2},
                ]})
        self.assertEqual(response.status_code, 200, response.content)
        locking = [q['sql'] for q in captured.captured_queries
                   if 'FOR UPDATE' in q['sql'].upper()]
        self.assertEqual(len(locking), 1, locking)
        self.assertIn('FROM "tables"', locking[0])
        self.assertIn('ORDER BY "tables"."id" ASC', locking[0])

    def test_floor_plan_is_seen_holding_the_lower_key_while_it_waits(self):
        """OBSERVED: hold the HIGHER key elsewhere and send the rows
        highest-first. A save that locks in key order takes the lower row, then
        waits on the higher one — so a probe finds the lower row locked. One that
        followed the client's order would wait on the higher row holding nothing.
        """
        low, high = sorted([self.table, self.other], key=lambda t: t.pk)
        held, release = threading.Event(), threading.Event()
        responses = []

        def _hold():
            try:
                with transaction.atomic():
                    list(Table.objects.select_for_update().filter(pk=high.pk))
                    held.set()
                    release.wait(WAIT)
            finally:
                close_old_connections()
                connection.close()

        def _save():
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('application_name', 'floorplan', false)")
                responses.append(self.post('update-floor-plan', {
                    'restaurant': str(self.restaurant.id), 'tables': [
                        {'id': str(high.id), 'floor_x': 1},
                        {'id': str(low.id), 'floor_x': 2},
                    ]}))
            finally:
                close_old_connections()
                connection.close()

        holder = threading.Thread(target=_hold)
        holder.start()
        saver = None
        try:
            self.assertTrue(held.wait(WAIT))
            saver = threading.Thread(target=_save)
            saver.start()
            self.wait_until_waiting('floorplan')
            self.assertTrue(
                self.probe_locked(low),
                'the floor-plan save waited on the higher key without holding '
                'the lower one: it did not lock in primary-key order')
        finally:
            release.set()
            holder.join(WAIT)
            if saver is not None:
                saver.join(WAIT)
        self.assertEqual(responses[0].status_code, 200, responses[0].content)

    def wait_until_waiting(self, name):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT wait_event_type FROM pg_stat_activity '
                    'WHERE application_name = %s', [name])
                row = cursor.fetchone()
            if row and row[0] == 'Lock':
                return
            time.sleep(0.02)
        self.fail(f'{name} never waited on a lock')
