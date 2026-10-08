"""
The first-time menu decision serializes on the restaurant row (Codex P1 on
PR #363). PostgreSQL only.

THE FINDING, stated exactly
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    "When two members submit concurrently, both requests read the restaurant as
    `pending` before entering the transaction, both pass the check, and the later
    save overwrites `first_time_menu_submitted_by`; both callers nevertheless
    receive success. The member whose attribution was overwritten can
    subsequently approve the menu despite having successfully submitted it,
    defeating the separation-of-duties rule this field implements."

It was valid. The restaurant was read in autocommit, before the transaction, and
nothing in the transaction locked it until the decision's own UPDATE. The same
unlocked read let a submission and an approval race, which the change that added
the column had recorded as reported and not fixed: an approval could land between
a submission's check and its write and be overwritten back to `submit` by it, and
a submission could be reported successful for a menu that was being approved.

THE FIX is the finding's own. The decision re-reads the restaurant under a row
lock inside its transaction, before it checks `pending` or the submitter, so a
second decision waits and then reads what the first one committed.
`first_time_batch_approval._lock_for_decision` says why the lock is
FOR NO KEY UPDATE, and ``TheLockDoesNotStallOrdersTests`` pins that choice.

THE HARNESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``TransactionTestCase``, two workers on two real connections, both driving the
real endpoint with a real customer JWT.

The first decision is parked immediately BEFORE it writes the restaurant row.
Both the old code and the new reach that point after deciding, so the same test
shows the race on the old code and the wait on the new one. The second worker is
started only once the first is parked, and the first is released only once the
second has been SEEN waiting on a lock, or has finished. Nothing waits on
something that is waiting on it.

The wait is observed from a third, read-only connection
(``pg_stat_activity.wait_event_type = 'Lock'``). It is never inferred from a
thread failing to finish, which any bug can also produce, and no ``lock_timeout``
is set on a worker that is meant to wait: ``orders_app/tests_kitchen_concurrency.py``
records how that made a test fail the worker it was meant to prove correct. Every
wait has a timeout, so a deadlock fails loudly instead of hanging CI.
"""
import threading
import time
from unittest import mock

from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, PaymentStatus_Pending, RESTAURANT_MANAGER,
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers.services.order_pricing import PRICING_VERSION_CORRECTED
from orders_app.models import Order, OrderItem
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.customer_access import issue_customer_tokens
from users_app.models import User

CONTROLLER = 'restaurants_app.controllers.first_time_batch_approval'
REVIEW_URL = '/api/v1/restaurant-setup/manager-actions/first-time-menu-review/'

# Independent oracles, deliberately not imported from the controller.
SUBMITTED = 'The restaurant menu has been submitted.'
APPROVED = 'The restaurant menu has been approved.'
ALREADY_SUBMITTED = 'Sorry, the restaurant menu has already been submitted.'
SUBMITTED_IT_YOURSELF = 'Sorry, you cannot approve a menu that you submitted.'

WAIT = 20
JOIN_TIMEOUT = 30
LOCK_TIMEOUT_MS = 1500

# The SQL Django emits for `restaurant.save(update_fields=...)`: the decision's
# write. Both the old code and the new reach it once per decision, after the
# decision has been made.
RESTAURANT_WRITE = 'UPDATE "restaurants"'


def _person(phone, first, last):
    return User.objects.create_user(
        first_name=first, last_name=last, email=f'{phone}@approval-race.test',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class _DecisionRace(TransactionTestCase):
    """A restaurant whose menu waits to be submitted, its owner and two managers.
    The section was created by the OWNER, so the separate created-it-yourself
    check cannot be what refuses anybody here."""

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('row-lock semantics are only meaningful on PostgreSQL')
        self.owner = _person('256700009201', 'Race', 'Owner')
        self.manager_a = _person('256700009202', 'Manager', 'Alpha')
        self.manager_b = _person('256700009203', 'Manager', 'Bravo')
        self.restaurant = Restaurant.objects.create(
            name='Approval Race Kitchen', location='Kampala', owner=self.owner,
            status=RestaurantStatus_Live,
            first_time_menu_approval=False,
            first_time_menu_approval_decision='pending',
        )
        for person, role in ((self.owner, RESTAURANT_OWNER),
                             (self.manager_a, RESTAURANT_MANAGER),
                             (self.manager_b, RESTAURANT_MANAGER)):
            RestaurantEmployee.objects.create(
                user=person, restaurant=self.restaurant, roles=[role], active=True,
            )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, created_by=self.owner,
            approved=False, enabled=False, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price='10000',
            approved=False, enabled=False, available=True, in_stock=True,
        )

    # --- the request ------------------------------------------------------

    def review(self, person, decision):
        """A callable that posts one decision through the real endpoint. The
        token is minted here, in the main thread, so a worker does nothing but
        the request."""
        token = str(issue_customer_tokens(person).access_token)
        body = {'restaurant': str(self.restaurant.id), 'decision': decision}

        def call():
            return Client().post(
                REVIEW_URL, data=body, content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
        return call

    # --- worker plumbing --------------------------------------------------

    def _spawn(self, fn, results, key, errors, done=None):
        def run():
            try:
                results[key] = fn()
            except BaseException as exc:            # surfaced, never swallowed
                errors.append(f'{key}: {exc!r}')
            finally:
                connections.close_all()
                if done is not None:
                    done.set()
        thread = threading.Thread(target=run, name=key)
        thread.start()
        return thread

    def _join(self, threads, errors):
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertEqual(alive, [], f'workers hung: {alive} (deadlock?)')
        self.assertEqual(errors, [])

    def _observe_waiter(self, done):
        """True once some other backend in this database is WAITING on a lock;
        False as soon as the observed worker has finished without one.

        A row-level wait blocks on the holder's transaction id, so the question
        is ``pg_stat_activity.wait_event_type``, not a join through
        ``pg_locks.relation`` (which is NULL for that wait)."""
        import psycopg
        settings = connection.settings_dict
        parts = [f"dbname={settings['NAME']}", f"user={settings['USER']}"]
        for key in ('HOST', 'PORT', 'PASSWORD'):
            if settings.get(key):
                parts.append(f'{key.lower()}={settings[key]}')
        deadline = time.monotonic() + WAIT
        with psycopg.connect(' '.join(parts), autocommit=True) as observer:
            while time.monotonic() < deadline:
                with observer.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = %s AND wait_event_type = 'Lock' "
                        "AND pid <> pg_backend_pid()",
                        [settings['NAME']])
                    if cursor.fetchone()[0] > 0:
                        return True
                if done.is_set():
                    return False
                done.wait(0.02)
        return False

    def race(self, first, second):
        """Park ``first`` immediately before it writes the restaurant row, run
        ``second`` against it, and release ``first`` once ``second`` is seen
        waiting on a lock or has finished.

        Returns ``(first_result, second_result, second_waited)``."""
        inside, release, second_done = (
            threading.Event(), threading.Event(), threading.Event())
        results, errors = {}, []

        def wrapper(execute, sql, params, many, context):
            if not inside.is_set() and sql.startswith(RESTAURANT_WRITE):
                inside.set()
                if not release.wait(timeout=WAIT):
                    raise AssertionError('the parked decision was never released')
            return execute(sql, params, many, context)

        def parked_first():
            with connection.execute_wrapper(wrapper):
                return first()

        t1 = self._spawn(parked_first, results, 'first', errors)
        if not inside.wait(timeout=WAIT):
            release.set()
            self._join([t1], errors)
            self.fail('the first decision never reached its write')
        t2 = self._spawn(second, results, 'second', errors, done=second_done)
        waited = self._observe_waiter(second_done)
        release.set()
        self._join([t1, t2], errors)
        return results['first'], results['second'], waited

    # --- assertions -------------------------------------------------------

    def assertAnswer(self, response, status, message):
        self.assertEqual(
            (response.status_code, response.json().get('message')),
            (status, message), response.content)

    def assertMenuApproved(self):
        self.restaurant.refresh_from_db()
        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'approve')
        self.assertTrue(self.restaurant.first_time_menu_approval)
        self.assertTrue(self.section.approved and self.section.enabled)
        self.assertTrue(self.item.approved and self.item.enabled)


class ConcurrentSubmissionTests(_DecisionRace):

    def test_REGRESSION_two_members_submitting_at_once_record_one_submission(self):
        """The finding, end to end. Manager A's submission is parked inside its
        decision; manager B submits. B must wait for A, then be told the menu has
        already been submitted, and the record must name A, who then cannot
        approve it. On the old code both were told their menu was submitted, the
        record named only the later writer, and the other could approve."""
        first, second, waited = self.race(
            self.review(self.manager_a, 'submit'),
            self.review(self.manager_b, 'submit'),
        )

        self.assertAnswer(first, 200, SUBMITTED)
        self.assertEqual(
            (second.status_code, second.json().get('message')),
            (400, ALREADY_SUBMITTED),
            'two concurrent submissions were both reported as successful',
        )
        self.assertTrue(
            waited, 'the second submission never waited for the first')
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'submit')
        self.assertEqual(
            self.restaurant.first_time_menu_submitted_by_id, self.manager_a.pk,
            'the record must name the submission that succeeded',
        )

        # The consequence the finding names: the member who submitted cannot
        # approve, and the other member, who did not, can.
        self.assertAnswer(
            self.review(self.manager_a, 'approve')(), 400, SUBMITTED_IT_YOURSELF)
        approved = self.review(self.manager_b, 'approve')()
        self.assertAnswer(approved, 200, APPROVED)
        self.assertMenuApproved()


class SubmissionAndApprovalTests(_DecisionRace):
    """The race the change that added the column reported and did not fix. The
    same lock closes it, both ways round."""

    def test_REGRESSION_an_approval_waits_for_a_submission_in_flight(self):
        """A submission is parked inside its decision; another member approves.
        The approval must wait, then see the committed submission, and approve
        it. On the old code it approved from `pending` and the parked
        submission then wrote `submit` over it: an approved menu left reading
        as awaiting approval."""
        first, second, waited = self.race(
            self.review(self.manager_a, 'submit'),
            self.review(self.manager_b, 'approve'),
        )

        self.assertAnswer(first, 200, SUBMITTED)
        self.assertAnswer(second, 200, APPROVED)
        self.assertMenuApproved()
        self.assertEqual(
            self.restaurant.first_time_menu_submitted_by_id, self.manager_a.pk)
        self.assertTrue(waited, 'the approval never waited for the submission')

    def test_REGRESSION_the_submitter_cannot_approve_alongside_their_own_submission(self):
        """The same member submits and, before that commits, approves (two
        tabs, or a quick second tap). The approval must wait, see the submission
        it raced, and be refused like any other approval by the submitter. On
        the old code it approved from `pending`, before the submission was
        written, so the submitter approved their own menu."""
        first, second, waited = self.race(
            self.review(self.manager_a, 'submit'),
            self.review(self.manager_a, 'approve'),
        )

        self.assertAnswer(first, 200, SUBMITTED)
        self.assertAnswer(second, 400, SUBMITTED_IT_YOURSELF)
        self.assertTrue(waited, 'the approval never waited for the submission')
        self.restaurant.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'submit')
        self.assertFalse(self.restaurant.first_time_menu_approval)
        self.assertFalse(self.item.approved)
        self.assertEqual(
            self.restaurant.first_time_menu_submitted_by_id, self.manager_a.pk)

    def test_REGRESSION_a_submission_waits_for_an_approval_in_flight(self):
        """An approval is parked inside its decision, AFTER it has approved
        every menu row and before it writes the restaurant. A submission must
        wait for it and then be refused, because the menu is no longer pending.
        On the old code the submission was reported successful and recorded a
        submitter for a menu that was being approved."""
        first, second, waited = self.race(
            self.review(self.manager_a, 'approve'),
            self.review(self.manager_b, 'submit'),
        )

        self.assertAnswer(first, 200, APPROVED)
        self.assertEqual(
            (second.status_code, second.json().get('message')),
            (400, ALREADY_SUBMITTED),
            'a submission was reported successful while the menu was being approved',
        )
        self.assertMenuApproved()
        self.assertIsNone(self.restaurant.first_time_menu_submitted_by_id)
        self.assertTrue(waited, 'the submission never waited for the approval')


class TheLockDoesNotStallOrdersTests(_DecisionRace):
    """CONTROL. The lock is FOR NO KEY UPDATE, the lock the decision's own UPDATE
    of the restaurant row already takes. Plain FOR UPDATE would also conflict
    with FOR KEY SHARE, which PostgreSQL takes on the restaurant row to check
    the foreign key of every row inserted with a reference to it, so an order
    would wait for the whole approval."""

    def setUp(self):
        super().setUp()
        self.area = DiningArea.objects.create(restaurant=self.restaurant, name='Main')
        self.table = Table.objects.create(
            restaurant=self.restaurant, dining_area=self.area, number=1)

    def _place_order(self):
        """An order and its line, in one transaction, with a lock timeout so a
        conflicting lock fails by name rather than by a thread not finishing.
        Django's foreign keys are DEFERRABLE INITIALLY DEFERRED, so the checks
        that read the restaurant and menu rows run at the commit."""
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
            order = Order.objects.create(
                restaurant=self.restaurant, table=self.table,
                total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
                order_status=OrderStatus_Pending,
                payment_status=PaymentStatus_Pending,
                fulfilment_status='new', order_date=timezone.localdate(),
                pricing_version=PRICING_VERSION_CORRECTED,
            )
            OrderItem.objects.create(
                order=order, item=self.item, quantity=1, available=True,
                unit_price=0, discounted_price=0, unit_cost_of_options=0,
                total_cost=0, discounted_cost=0, savings=0, cost_of_options=0,
                actual_cost=0, item_name_snapshot=self.item.name,
            )
        return order.pk

    def test_CONTROL_an_order_commits_while_an_approval_holds_the_row(self):
        first, order_id, waited = self.race(
            self.review(self.manager_a, 'approve'), self._place_order,
        )

        self.assertFalse(
            waited, 'an order waited for a menu approval to finish')
        self.assertTrue(Order.objects.filter(pk=order_id).exists())
        self.assertAnswer(first, 200, APPROVED)
        self.assertMenuApproved()


class WithoutTheLockTests(_DecisionRace):
    """NEGATIVE CONTROL. With the lock replaced by the unlocked read it
    replaced, this harness reproduces the finding exactly. It shows the
    regression tests above can see the defect, rather than passing for some
    other reason. ``create=True`` lets it run against the code before the fix,
    where it holds for the same reason."""

    def test_CONTROL_without_the_lock_the_overwrite_reproduces(self):
        def unlocked(restaurant_id):
            return Restaurant.objects.get(id=restaurant_id)

        with mock.patch(f'{CONTROLLER}._lock_for_decision', unlocked, create=True):
            first, second, waited = self.race(
                self.review(self.manager_a, 'submit'),
                self.review(self.manager_b, 'submit'),
            )

        self.assertAnswer(first, 200, SUBMITTED)
        self.assertAnswer(second, 200, SUBMITTED)
        self.assertFalse(waited)
        self.restaurant.refresh_from_db()
        # The parked submission wrote last, over manager B's.
        self.assertEqual(
            self.restaurant.first_time_menu_submitted_by_id, self.manager_a.pk)
        # Manager B was told the menu was submitted, and can now approve it.
        self.assertAnswer(self.review(self.manager_b, 'approve')(), 200, APPROVED)
