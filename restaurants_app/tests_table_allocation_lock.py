"""
Table-number allocation must actually serialise (TABLES-LOCK-00).

``create_tables_in_section`` reads the restaurant's current table count and
numbers the new tables ``count+1 .. count+N``. That read was written as

    Table.objects.select_for_update().filter(restaurant=restaurant).count()

which LOOKS like a locked read and is not one. ``QuerySet.count()`` routes
through ``Query.get_aggregation()``, which sets ``outer_query.select_for_update
= False`` before compiling (django/db/models/sql/query.py:625 in Django 5.2), so
``FOR UPDATE`` is never emitted. PostgreSQL rejects ``SELECT COUNT(*) ... FOR
UPDATE`` outright — it never received one. The statement ran as a plain unlocked
count, so two concurrent creations both read the same value and both tried to
write tables with the same numbers. ``Table.Meta.unique_together`` —
``['number', 'str_number', 'restaurant']`` — caught the collision, so the
consequence was never duplicated data: it was an uncaught ``IntegrityError``
propagating out as a 500 for whichever creation lost the race.

The fix locks the PARENT restaurant row inside the transaction instead. Locking
the counted rows would not have helped: a row lock on existing tables does not
block a concurrent INSERT, which is precisely the race.

WHAT IS NOT TESTED HERE, and why. An obvious-looking test — hold the restaurant
row in the test's own transaction and assert the allocator blocks — does NOT
discriminate: ``bulk_create`` takes ``FOR KEY SHARE`` on the restaurant row for
the FK, and that already conflicts with a held ``FOR UPDATE``, so the allocator
stalls on the pre-fix code too. It was written, found to pass against the bug,
and removed. The two guards that DO fail on the pre-fix code are
``test_allocation_locks_the_restaurant_row`` (no locking read was issued at all)
and ``test_concurrent_allocations_never_duplicate_a_table_number``
(``IntegrityError``).

``TransactionTestCase`` (not ``TestCase``) because the assertions depend on real
commits being visible to a second connection. The concurrency case is skipped
where row locking does not exist.
"""
import threading

from django.db import connection, transaction
from django.test import TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from restaurants_app.controllers.tables import create_tables_in_section
from restaurants_app.models import (
    DiningArea, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


class TableAllocationBase(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Tbl', last_name='Owner', email='tbl_owner@test.com',
            phone_number='256700000901', username='256700000901',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Allocation R', location='loc-alloc', owner=self.owner,
            status=RestaurantStatus_Live,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant,
        )

    def allocate(self, no_tables):
        return create_tables_in_section(
            restaurant_id=str(self.restaurant.id),
            no_tables=no_tables,
            user=self.owner,
            consideration='count',
            dining_area=self.area,
        )

    def numbers(self):
        return sorted(
            Table.objects.filter(restaurant=self.restaurant)
            .values_list('number', flat=True)
        )


class CountCannotCarryForUpdateTests(TableAllocationBase):
    """The Stage-1 finding, as an executable fact rather than a claim."""

    def test_select_for_update_is_silently_dropped_by_count(self):
        # This is the whole reason the old code was broken. If a future Django
        # starts emitting FOR UPDATE here (or raising), this test fails and the
        # comment in tables.py needs revisiting.
        with transaction.atomic():
            with CaptureQueriesContext(connection) as captured:
                Table.objects.select_for_update().filter(
                    restaurant=self.restaurant,
                ).count()
        sql = ' '.join(q['sql'] for q in captured.captured_queries).upper()
        self.assertIn('COUNT(', sql)
        self.assertNotIn('FOR UPDATE', sql)

    def test_allocation_no_longer_asks_the_count_to_lock(self):
        # The production path must not contain a lock claim that does nothing.
        with CaptureQueriesContext(connection) as captured:
            self.allocate(2)
        counts = [
            q['sql'] for q in captured.captured_queries
            if 'COUNT(' in q['sql'].upper()
        ]
        self.assertTrue(counts, 'the allocator no longer counts tables')
        for sql in counts:
            self.assertNotIn('FOR UPDATE', sql.upper())

    def test_allocation_locks_the_restaurant_row(self):
        # ...and the lock it does take is a real one, on the parent row.
        with CaptureQueriesContext(connection) as captured:
            self.allocate(2)
        locking = [
            q['sql'] for q in captured.captured_queries
            if 'FOR UPDATE' in q['sql'].upper()
        ]
        self.assertEqual(
            len(locking), 1,
            f'expected exactly one locking read, got: {locking}',
        )
        self.assertIn('RESTAURANT', locking[0].upper())


class TableNumberingTests(TableAllocationBase):
    """The behaviour the lock protects is unchanged."""

    def test_allocation_numbers_sequentially_from_the_current_count(self):
        result = self.allocate(3)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['data']['no_tables'], 3)
        self.assertEqual(self.numbers(), [1, 2, 3])

    def test_second_allocation_continues_the_sequence(self):
        self.allocate(3)
        self.allocate(2)
        self.assertEqual(self.numbers(), [1, 2, 3, 4, 5])


@tag('concurrency')
class TableAllocationLockTests(TableAllocationBase):
    """The part only concurrency can demonstrate."""

    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('row locking requires PostgreSQL')

    def test_concurrent_allocations_never_duplicate_a_table_number(self):
        """
        Two creations racing for the same restaurant must both succeed.

        On the pre-fix code both read the same count and both tried to write
        numbers 1-3; ``unique_together`` on (number, str_number, restaurant) then
        rejected the loser with an ``IntegrityError`` that nothing on the path
        catches — an owner adding a dining area got a 500 because someone else was
        adding one at the same moment. With the parent row locked, the second
        creation reads the first's committed count and continues the sequence.
        """
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                self.allocate(3)
            except Exception as exc:                # pragma: no cover - defensive
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        for index, thread in enumerate(threads):
            self.assertFalse(thread.is_alive(), f'worker {index} hung')

        self.assertEqual(errors, [])
        self.assertEqual(self.numbers(), [1, 2, 3, 4, 5, 6])
