"""
A first-time menu approval and a menu-item or section-group edit no longer
deadlock (MENU-APPROVAL-EDIT-LOCK-00). PostgreSQL only.

THE DEFECT, as measured on `bfa393e`
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

An approval (`first_time_batch_approval`) locks the restaurant row, then updates
every section of the restaurant, then every section group, then every menu item,
in three bulk UPDATEs. Each UPDATE locks the rows it writes until the approval
commits.

A menu-item or section-group PUT or DELETE resolves its row through Secretary's
scoped queryset. That queryset filters on `section__restaurant_id`, so it joins
`menu_sections`, and Secretary's `select_for_update()` has no `of=`. One
statement therefore locks the record and then its section. An edit arriving
after the approval had updated the sections, and before it updated the groups or
the items, locked the record and waited for the section, while the approval
waited for the record. PostgreSQL detected the cycle after `deadlock_timeout` and
aborted one transaction. In every measured case it aborted the edit, which
answered 500 and was rolled back, and the approval succeeded.

Measured through the real endpoints: a menu-item PUT, a section-group PUT, a
menu-item DELETE and a section-group DELETE each answered 500 (`deadlock
detected`). A menu-section PUT waited and succeeded, because it locks the section
alone. The section-group case is the same mechanism as the menu-item one.

THE FIX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

`restaurant_setup._lock_parent_section`: a menu-item or section-group PUT or
DELETE locks the record's section FOR UPDATE right after the catalogue barrier
and before Secretary runs. It is the lock Secretary took anyway, on the same row,
in the same mode and through the same scoped queryset, taken earlier. The writes
now take their rows in the approval's order, parent before child:

* approval first: the edit waits for the section while holding no menu row, the
  approval finishes, and the edit then proceeds;
* edit first: the approval waits for the section in its section UPDATE, before it
  has locked any group or item, so nothing the edit locks next (its own row, the
  extras it assigns) can be held by the approval.

Narrowing Secretary's lock to the record (`of=('self',)`) was rejected. It
removes the section from the edit's locks, but an edit that assigns extras locks
them after its own row, and an approval that has already updated those extras
then waits for the edit's row: the same cycle on a different pair of rows
(`OppositeArrivalOrderTests.test_CONTROL_an_extras_edit_in_flight`).

THE HARNESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

`tests_menu_approval_concurrency._DecisionRace`'s restaurant and plumbing: real
endpoints, real customer JWTs, each worker on its own connection, which it closes.
`interleave(first, second, park)` holds `first` at a chosen statement (just
before it, or just after it has executed), starts `second`, and releases `first`
once `second` has been SEEN waiting on a lock from a third, read-only connection,
or has finished. It never sleeps to decide anything, and every wait is bounded,
so a deadlock fails loudly.

The tests do not require the old waiting pattern. After the fix an edit inside the
window waits for the approval, but the assertions are on the answers and on the
committed rows, not on whether a wait happened. Where a wait IS asserted it is the
premise of the scenario, labelled as such.

The approval reaches sections in the order they were created, but groups and
items in an order PostgreSQL derives from a hash of their ids, which changes from
run to run. No test assumes an item order. The one whose outcome under the
rejected design depends on it measures the order first
(`_EditRace.approval_item_order`).

Every client is built with `raise_request_exception=False`. A deadlock is an
exception in the view, and the test client reports request exceptions through a
global signal, so with two clients in two threads each would re-raise the other's.

WHAT THIS PROVES, AND WHAT IT DOES NOT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

`EditInsideTheApprovalWindowTests` and `RefusalsInsideTheApprovalWindowTests` are
the regressions: each edit starts while the approval is parked inside the window,
and each answered 500 on `bfa393e`. `OppositeArrivalOrderTests` starts the edit
first. `ControlTests` holds what must not change. `TheSectionIsLockedFirstTests`
pins the mechanism through the endpoints, and `LockParentSectionTests` pins the
helper itself.

It does NOT prove that every menu operation is deadlock-free with an approval.
The section and item reorder endpoints take no barrier and lock rows in the order
the caller sends; they can still deadlock with an approval, by a different
mechanism. A menu-item create that assigns extras, and an item reorder, can still
deadlock with an item PUT that assigns extras. The lock sets of all of those are
unchanged by this fix. An edit waiting for an approval holds the catalogue
barrier while it waits, so an order at that restaurant waits for both; before
the fix it waited the same way until the deadlock was detected.
"""
import threading
import time
import uuid
from collections import namedtuple
from decimal import Decimal

from django.db import OperationalError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from restaurants_app.controllers.menu_relationships import (
    CROSS_RESTAURANT_MOVE_MESSAGE, EXTRA_STILL_REFERENCED_MESSAGE,
    INVALID_EXTRAS_MESSAGE, SECTION_GROUP_MISMATCH_MESSAGE,
)
from restaurants_app.endpoints.restaurant_setup import (
    build_scoped_instance_queryset,
)
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, SectionGroup,
)
from restaurants_app.tests_menu_approval_concurrency import (
    APPROVED, JOIN_TIMEOUT, REVIEW_URL, SUBMITTED, WAIT, _DecisionRace, _person,
)
from users_app.customer_access import issue_customer_tokens

SETUP = '/api/v1/restaurant-setup/'
ITEMS = SETUP + 'menuitems/'
GROUPS = SETUP + 'sectiongroups/'
SECTIONS = SETUP + 'menusections/'

# The approval's three bulk UPDATEs, in its order. Parked before one of them, the
# approval holds every row the earlier ones wrote.
BEFORE_SECTION_UPDATE = 'UPDATE "menu_sections"'
BEFORE_GROUP_UPDATE = 'UPDATE "section_groups"'
BEFORE_ITEM_UPDATE = 'UPDATE "menu_items"'

# Independent oracles, deliberately not imported from the endpoint.
PERMISSION_DENIED = 'You do not have permission to perform this action.'
EXTRA_DELETE_BLOCKED = (
    'This extra is still offered on 1 menu item(s). '
    'Remove it from those items before deleting it.'
)

Interleaving = namedtuple(
    'Interleaving', 'first second waited waiting_on observed')


class Park:
    """Where to hold a request: just BEFORE the first statement that matches, or
    just AFTER it has executed."""

    def __init__(self, label, matches, after=False):
        self.label = label
        self.matches = matches
        self.after = after

    def __str__(self):
        return self.label

    def wrapper(self, inside, release):
        def wrap(execute, sql, params, many, context):
            if inside.is_set() or not self.matches(sql):
                return execute(sql, params, many, context)
            if self.after:
                result = execute(sql, params, many, context)
            inside.set()
            if not release.wait(timeout=WAIT):
                raise AssertionError(f'the request parked {self} was never released')
            return result if self.after else execute(sql, params, many, context)
        return wrap


def before(prefix):
    return Park(f'before {prefix}', lambda sql: sql.startswith(prefix))


def after_first_row_lock():
    """After the request's first `... FOR UPDATE` statement has executed. The
    approval's own row lock is `FOR NO KEY UPDATE`, which this does not match."""
    return Park('after its first row lock', lambda sql: ' FOR UPDATE' in sql,
                after=True)


class _EditRace(_DecisionRace):
    """The `_DecisionRace` restaurant, its menu submitted by manager A and not yet
    approved, plus: a group holding the item, an empty group, a section of
    extras, an item that offers one of them, a plain item, a section created
    last, and another restaurant's menu.

    The approval's section UPDATE reaches this restaurant's sections in the
    order they were created (an index scan on `restaurant_id` over a fresh
    table), so `Late` is the last section it reaches. Its group and item UPDATEs
    reach rows in an order PostgreSQL derives from a hash of their ids, which
    changes from run to run. The one test that depends on that order measures it
    first (`approval_item_order`)."""

    def setUp(self):
        super().setUp()
        self.group = SectionGroup.objects.create(
            name='Grill', section=self.section,
            approved=False, enabled=False, available=True,
        )
        MenuItem.objects.filter(pk=self.item.pk).update(section_group=self.group)
        self.empty_group = SectionGroup.objects.create(
            name='Specials', section=self.section,
            approved=False, enabled=False, available=True,
        )
        self.sides = self._section('Sides', self.restaurant, self.owner)
        self.extra_1 = self._menu_item('Avocado', self.sides, is_extra=True)
        self.extra_2 = self._menu_item('Chapati', self.sides, is_extra=True)
        self.burger = self._menu_item(
            'Burger', self.section, has_extras=True,
            extras_applicable=[str(self.extra_1.id)], extras_max_selections=1,
        )
        self.item_b = self._menu_item('Chai', self.section)
        self.late = self._section('Late', self.restaurant, self.owner)
        self.item_late = self._menu_item('Pilau', self.late)

        self.other_owner = _person('256700009204', 'Other', 'Owner')
        self.other = Restaurant.objects.create(
            name='Other Kitchen', location='Gulu', owner=self.other_owner,
            status=RestaurantStatus_Live,
            first_time_menu_approval=False,
            first_time_menu_approval_decision='pending',
        )
        RestaurantEmployee.objects.create(
            user=self.other_owner, restaurant=self.other,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.foreign_section = self._section(
            'Foreign Mains', self.other, self.other_owner)
        self.foreign_group = SectionGroup.objects.create(
            name='Foreign Grill', section=self.foreign_section,
            approved=False, enabled=False, available=True,
        )
        self.foreign_item = self._menu_item('Foreign Rolex', self.foreign_section)
        self.foreign_extra = self._menu_item(
            'Foreign Avocado', self.foreign_section, is_extra=True)

        self.assertAnswer(self.review(self.manager_a, 'submit')(), 200, SUBMITTED)

    @staticmethod
    def _section(name, restaurant, creator):
        return MenuSection.objects.create(
            name=name, restaurant=restaurant, created_by=creator,
            approved=False, enabled=False, available=True,
        )

    @staticmethod
    def _menu_item(name, section, **fields):
        values = dict(
            primary_price=Decimal('5000'), approved=False, enabled=False,
            available=True, in_stock=True,
        )
        values.update(fields)
        return MenuItem.objects.create(name=name, section=section, **values)

    # --- the requests -------------------------------------------------------

    def call(self, method, path, body, person=None):
        """A callable making one request through the real endpoint, as the owner
        unless told otherwise. The token is minted here, in the main thread."""
        token = str(issue_customer_tokens(person or self.owner).access_token)

        def request():
            return getattr(Client(raise_request_exception=False), method)(
                path, data=body, content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )
        return request

    def approve(self):
        """Manager B approves the menu manager A submitted."""
        return self.call('post', REVIEW_URL, {
            'restaurant': str(self.restaurant.id), 'decision': 'approve',
        }, person=self.manager_b)

    @staticmethod
    def recording(request, prefix, seen):
        """``request``, appending to ``seen`` each statement it runs that starts
        with ``prefix``, with its parameters."""
        def run():
            def record(execute, sql, params, many, context):
                if sql.startswith(prefix):
                    seen.append((sql, params))
                return execute(sql, params, many, context)
            with connection.execute_wrapper(record):
                return request()
        return run

    # --- measuring the approval ---------------------------------------------

    def approval_item_order(self):
        """The order in which the approval's item UPDATE reaches this
        restaurant's items: that statement, run with `RETURNING` in a transaction
        that is rolled back. Returns the item ids in that order and the statement
        with its parameters, so a test can check the approval runs the same one."""
        statements, order = [], []

        def returning(execute, sql, params, many, context):
            if not sql.startswith(BEFORE_ITEM_UPDATE):
                return execute(sql, params, many, context)
            statements.append((sql, params))
            result = execute(
                f'{sql} RETURNING "menu_items"."id"', params, many, context)
            order.extend(row[0] for row in context['cursor'].fetchall())
            return result

        with transaction.atomic(), connection.execute_wrapper(returning):
            # The approval's own statement (`first_time_batch_approval`).
            MenuItem.objects.filter(section__restaurant=self.restaurant).update(
                approved=True, enabled=True)
            transaction.set_rollback(True)
        self.assertEqual(len(statements), 1, statements)
        self.assertEqual(len(order), 6, 'the approval reaches six items')
        return order, statements[0]

    # --- interleaving -------------------------------------------------------

    def interleave(self, first, second, park, during=None):
        """Hold ``first`` at ``park``, run ``second`` against it, and release
        ``first`` once ``second`` is seen waiting on a lock or has finished.
        ``during`` runs while both are in flight, before the release."""
        inside, release, second_done = (
            threading.Event(), threading.Event(), threading.Event())
        results, errors, threads = {}, [], []
        wrap = park.wrapper(inside, release)

        def parked_first():
            with connection.execute_wrapper(wrap):
                return first()

        waited, waiting_on, observed = False, frozenset(), None
        try:
            threads.append(self._spawn(parked_first, results, 'first', errors))
            if not inside.wait(timeout=WAIT):
                self.fail(f'the first request was never parked {park}: {errors}')
            if second is not None:
                threads.append(self._spawn(
                    second, results, 'second', errors, done=second_done))
                waited, waiting_on = self._watch(second_done)
            if during is not None:
                observed = during()
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=JOIN_TIMEOUT)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertEqual(alive, [], f'workers hung: {alive} (deadlock?)')
        self.assertEqual(errors, [])
        return Interleaving(
            results.get('first'), results.get('second'), waited, waiting_on,
            observed)

    def park_alone(self, request, park, during):
        run = self.interleave(request, None, park, during=during)
        return run.first, run.observed

    # --- the observer -------------------------------------------------------

    def _observer(self):
        import psycopg
        settings = connection.settings_dict
        parts = [f"dbname={settings['NAME']}", f"user={settings['USER']}"]
        for key in ('HOST', 'PORT', 'PASSWORD'):
            if settings.get(key):
                parts.append(f'{key.lower()}={settings[key]}')
        return psycopg.connect(' '.join(parts), autocommit=True)

    def _watch(self, done):
        """``(waited, waiting_on)``: whether another backend in this database was
        seen WAITING on a lock before ``done`` was set, and on what: the table of
        the row it waited for, or ``advisory``. A row-level wait holds the
        tuple's lock while it waits on the holder's transaction, so the table is
        read from that tuple lock."""
        name = connection.settings_dict['NAME']
        deadline = time.monotonic() + WAIT
        with self._observer() as observer:
            while time.monotonic() < deadline:
                with observer.cursor() as cursor:
                    cursor.execute(
                        "SELECT l.locktype, l.relation::regclass::text, l.granted "
                        "FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid "
                        "WHERE a.datname = %s AND a.wait_event_type = 'Lock' "
                        "AND a.pid <> pg_backend_pid()", [name])
                    rows = cursor.fetchall()
                if rows:
                    return True, frozenset(
                        relation if locktype == 'tuple' else locktype
                        for locktype, relation, granted in rows
                        if locktype == 'tuple'
                        or (locktype == 'advisory' and not granted))
                if done.is_set():
                    return False, frozenset()
                done.wait(0.02)
        return False, frozenset()

    def _locked(self, table, pk):
        """True if another transaction holds a lock on that row that conflicts
        with FOR UPDATE. The row must exist, so a missing row cannot read as
        unlocked."""
        import psycopg
        from psycopg import sql
        with self._observer() as observer, observer.cursor() as cursor:
            try:
                cursor.execute(
                    sql.SQL('SELECT 1 FROM {} WHERE id = %s FOR UPDATE NOWAIT')
                    .format(sql.Identifier(table)), [pk])
            except psycopg.errors.LockNotAvailable:
                return True
            found = cursor.fetchall()
        self.assertEqual(len(found), 1, f'there is no {table} row {pk}')
        return False

    # --- assertions ---------------------------------------------------------

    def assertAnswered(self, response, status, message=None, what='the request'):
        self.assertIsNotNone(response, f'{what} produced no response')
        self.assertEqual(
            response.status_code, status,
            f'{what} answered {response.status_code}: {response.content[:400]!r}',
        )
        if message is not None:
            self.assertIn(
                message, response.json().get('message') or '',
                f'{what}: {response.content[:400]!r}')

    def assertApproved(self, response):
        """The approval answered, the decision was recorded with its submitter
        intact, every menu row of this restaurant is approved and enabled, and
        the other restaurant's menu is untouched."""
        self.assertAnswered(response, 200, what='the approval')
        self.assertEqual(response.json().get('message'), APPROVED)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'approve')
        self.assertTrue(self.restaurant.first_time_menu_approval)
        self.assertEqual(
            self.restaurant.first_time_menu_submitted_by_id, self.manager_a.pk,
            'the approval changed who is recorded as the submitter')
        rows = [
            *MenuSection.objects.filter(restaurant=self.restaurant),
            *SectionGroup.objects.filter(section__restaurant=self.restaurant),
            *MenuItem.objects.filter(section__restaurant=self.restaurant),
        ]
        # 3 sections, 2 groups, 6 items.
        self.assertEqual(len(rows), 11)
        self.assertEqual(
            [str(row) for row in rows if not (row.approved and row.enabled)], [])
        foreign = [
            *MenuSection.objects.filter(restaurant=self.other),
            *SectionGroup.objects.filter(section__restaurant=self.other),
            *MenuItem.objects.filter(section__restaurant=self.other),
        ]
        self.assertEqual(len(foreign), 4)
        self.assertEqual(
            [str(row) for row in foreign if row.approved or row.enabled], [])
        self.other.refresh_from_db()
        self.assertEqual(self.other.first_time_menu_approval_decision, 'pending')

    def edit_inside_the_window(self, edit, park=before(BEFORE_ITEM_UPDATE)):
        """The approval parked inside the window, the edit started against it."""
        return self.interleave(self.approve(), edit, park)


class EditInsideTheApprovalWindowTests(_EditRace):
    """REGRESSIONS. The approval is parked after its section UPDATE (and, for the
    item cases, after its group UPDATE); the edit starts against it. On
    `bfa393e` every one of these edits answered 500. Both must now complete, and
    the committed rows must say what each request asked for."""

    def test_REGRESSION_an_item_edit_inside_the_window_completes(self):
        """The reported pair: a menu-item PUT landing after the approval has
        updated the sections and before it updates the items."""
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item.id), 'description': 'edited during approval'}))

        self.assertAnswered(run.second, 200, what='the item edit')
        self.assertApproved(run.first)
        self.item.refresh_from_db()
        self.assertEqual(self.item.description, 'edited during approval')

    def test_REGRESSION_a_group_edit_inside_the_window_completes(self):
        """The section-group analogue, measured: same mechanism, same 500."""
        run = self.edit_inside_the_window(
            self.call('put', GROUPS, {
                'id': str(self.group.id), 'description': 'edited during approval'}),
            park=before(BEFORE_GROUP_UPDATE))

        self.assertAnswered(run.second, 200, what='the group edit')
        self.assertApproved(run.first)
        self.group.refresh_from_db()
        self.assertEqual(self.group.description, 'edited during approval')

    def test_REGRESSION_an_item_delete_inside_the_window_completes(self):
        run = self.edit_inside_the_window(self.call('delete', ITEMS, {
            'id': str(self.item_b.id), 'deletion_reason': 'off the menu'}))

        self.assertAnswered(run.second, 200, what='the item delete')
        self.assertApproved(run.first)
        self.item_b.refresh_from_db()
        self.assertTrue(self.item_b.deleted)

    def test_REGRESSION_a_group_delete_inside_the_window_completes(self):
        run = self.edit_inside_the_window(
            self.call('delete', GROUPS, {
                'id': str(self.empty_group.id), 'deletion_reason': 'no longer used'}),
            park=before(BEFORE_GROUP_UPDATE))

        self.assertAnswered(run.second, 200, what='the group delete')
        self.assertApproved(run.first)
        self.empty_group.refresh_from_db()
        self.assertTrue(self.empty_group.deleted)

    def test_REGRESSION_an_extras_edit_inside_the_window_completes(self):
        """An item PUT that assigns extras: after its own row it locks the
        extras, which the approval is about to update."""
        extras = [str(self.extra_1.id), str(self.extra_2.id)]
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item_b.id), 'has_extras': True,
            'extras_applicable': extras,
            'extras_min_selections': 1, 'extras_max_selections': 2}))

        self.assertAnswered(run.second, 200, what='the extras edit')
        self.assertApproved(run.first)
        self.item_b.refresh_from_db()
        self.assertTrue(self.item_b.has_extras)
        self.assertEqual(self.item_b.extras_applicable, extras)
        self.assertEqual(
            (self.item_b.extras_min_selections, self.item_b.extras_max_selections),
            (1, 2))

    def test_REGRESSION_moving_an_item_to_another_section_inside_the_window(self):
        """Same-tenant movement. The edit locks the section the item is in; the
        section it moves to is checked by the foreign key at commit, a lock that
        does not conflict with the approval's UPDATE of it."""
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item_b.id), 'section': str(self.sides.id)}))

        self.assertAnswered(run.second, 200, what='the move')
        self.assertApproved(run.first)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.section_id, self.sides.id)


class RefusalsInsideTheApprovalWindowTests(_EditRace):
    """The relationship rules still refuse what they refused, inside the window.
    The first five are REGRESSIONS (each reached Secretary's lock and answered
    500 on `bfa393e`), the last two are CONTROLS (refused before any menu row is
    locked, on both). Each refusal must leave the row as it was."""

    def test_REGRESSION_a_move_that_strands_the_items_group_is_refused(self):
        """Rolex is in the Grill group of Mains. Moving it to Sides while leaving
        the group out of the request keeps the group, which is in another
        section."""
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item.id), 'section': str(self.sides.id)}))

        self.assertAnswered(
            run.second, 400, SECTION_GROUP_MISMATCH_MESSAGE, what='the move')
        self.assertApproved(run.first)
        self.item.refresh_from_db()
        self.assertEqual(
            (self.item.section_id, self.item.section_group_id),
            (self.section.id, self.group.id))

    def test_REGRESSION_a_move_to_another_restaurants_section_is_refused(self):
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item_b.id), 'section': str(self.foreign_section.id)}))

        self.assertAnswered(
            run.second, 400, CROSS_RESTAURANT_MOVE_MESSAGE, what='the move')
        self.assertApproved(run.first)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.section_id, self.section.id)

    def test_REGRESSION_another_restaurants_extra_is_refused(self):
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item_b.id), 'has_extras': True,
            'extras_applicable': [str(self.foreign_extra.id)],
            'extras_max_selections': 1}))

        self.assertAnswered(
            run.second, 400, INVALID_EXTRAS_MESSAGE, what='the extras edit')
        self.assertApproved(run.first)
        self.item_b.refresh_from_db()
        self.assertEqual(
            (self.item_b.has_extras, self.item_b.extras_applicable), (False, []))

    def test_REGRESSION_another_restaurants_group_is_refused(self):
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.item_b.id), 'section_group': str(self.foreign_group.id)}))

        self.assertAnswered(
            run.second, 400, SECTION_GROUP_MISMATCH_MESSAGE, what='the group edit')
        self.assertApproved(run.first)
        self.item_b.refresh_from_db()
        self.assertIsNone(self.item_b.section_group_id)

    def test_REGRESSION_demoting_an_extra_that_is_offered_is_refused(self):
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.extra_1.id), 'is_extra': False}))

        self.assertAnswered(
            run.second, 400, EXTRA_STILL_REFERENCED_MESSAGE, what='the demotion')
        self.assertApproved(run.first)
        self.extra_1.refresh_from_db()
        self.assertTrue(self.extra_1.is_extra)

    def test_CONTROL_deleting_an_extra_that_is_offered_is_refused(self):
        """The 409 comes from `deletion_blockers()`, which runs before Secretary
        locks anything, so on `bfa393e` it never reached the deadlock. After
        the fix it may wait for the section first; either way the answer is
        the same."""
        run = self.edit_inside_the_window(self.call('delete', ITEMS, {
            'id': str(self.extra_1.id), 'deletion_reason': 'out of avocados'}))

        self.assertAnswered(run.second, 409, what='the delete')
        self.assertEqual(run.second.json().get('message'), EXTRA_DELETE_BLOCKED)
        self.assertApproved(run.first)
        self.extra_1.refresh_from_db()
        self.assertFalse(self.extra_1.deleted)

    def test_CONTROL_another_restaurants_item_is_refused_before_any_lock(self):
        """The permission gate refuses a foreign target outside the transaction,
        so the edit never waits and locks nothing."""
        description = self.foreign_item.description
        run = self.edit_inside_the_window(self.call('put', ITEMS, {
            'id': str(self.foreign_item.id), 'description': 'not yours'}))

        self.assertAnswered(run.second, 403, PERMISSION_DENIED, what='the edit')
        self.assertFalse(run.waited, 'a refused edit waited for the approval')
        self.assertApproved(run.first)
        self.foreign_item.refresh_from_db()
        self.assertEqual(self.foreign_item.description, description)


class OppositeArrivalOrderTests(_EditRace):
    """The edit first, holding its first row lock, and the approval arriving
    against it. These passed on `bfa393e` as well: there the approval also waited
    for the section before locking any group or item. They are here because the
    fix changes when the edit takes its rows, and they must still both complete.
    That the approval waited is the PREMISE of each: it is what makes the two
    overlap."""

    def test_CONTROL_an_item_edit_in_flight(self):
        run = self.interleave(
            self.call('put', ITEMS, {
                'id': str(self.item.id), 'description': 'edited before approval'}),
            self.approve(), after_first_row_lock())

        self.assertTrue(run.waited, 'PREMISE: the approval did not overlap the edit')
        self.assertAnswered(run.first, 200, what='the item edit')
        self.assertApproved(run.second)
        self.item.refresh_from_db()
        self.assertEqual(self.item.description, 'edited before approval')

    def test_CONTROL_an_extras_edit_in_flight(self):
        """The shape a record-only lock (`of=('self',)`) leaves open. With that
        lock the edit holds only the item it is editing, so the approval updates
        the sections and the groups without waiting, then updates items until it
        reaches the edited one, holding every item it has passed. The edit next
        locks the extras it assigns, and an extra the approval has passed is a
        deadlock.

        The approval reaches items in an order PostgreSQL derives from a hash of
        their ids, which changes from run to run, so the order is measured first.
        The edited item is the one the approval reaches LAST, and it is given
        every other extra of the restaurant, each of which the approval reaches
        before it. The relationship rules let any item offer extras, an extra
        included, so no outcome of the measurement is excluded, and under a
        record-only lock this deadlocks on every run. With the section locked
        first, the approval waits in its section UPDATE and holds none of the
        extras."""
        order, statement = self.approval_item_order()
        extra_pks = set(MenuItem.objects.filter(
            section__restaurant=self.restaurant, is_extra=True,
        ).values_list('pk', flat=True))
        parent = MenuItem.objects.get(pk=order[-1])
        extras = [pk for pk in order[:-1] if pk in extra_pks]
        self.assertTrue(extras, 'PREMISE: the approval reaches no extra before the edit')
        names = dict(MenuItem.objects.filter(pk__in=extras).values_list('pk', 'name'))
        assigned = [str(pk) for pk in extras]
        ran = []

        run = self.interleave(
            self.call('put', ITEMS, {
                'id': str(parent.id), 'has_extras': True,
                'extras_applicable': assigned,
                'extras_max_selections': len(assigned)}),
            self.recording(self.approve(), BEFORE_ITEM_UPDATE, ran),
            after_first_row_lock(),
            during=lambda: {
                names[pk]: self._locked('menu_items', pk) for pk in extras})

        self.assertTrue(run.waited, 'PREMISE: the approval did not overlap the edit')
        self.assertEqual(
            run.observed, {names[pk]: False for pk in extras},
            'the approval locked an extra the edit was about to lock')
        self.assertAnswered(run.first, 200, what='the extras edit')
        self.assertApproved(run.second)
        self.assertEqual(
            ran, [statement],
            'PREMISE: the approval ran a different item UPDATE from the one measured')
        parent.refresh_from_db()
        self.assertTrue(parent.has_extras)
        self.assertEqual(parent.extras_applicable, assigned)

    def test_CONTROL_moving_an_item_into_a_section_the_approval_holds(self):
        """Pilau moves from Late, the last section, to Mains. The approval
        updates Mains and Sides and waits for Late, so it holds Mains, the move's
        destination, while the edit commits. The foreign-key check on Mains at
        that commit is FOR KEY SHARE, which the approval's UPDATE does not
        block; if it did, the two would deadlock here."""
        run = self.interleave(
            self.call('put', ITEMS, {
                'id': str(self.item_late.id), 'section': str(self.section.id)}),
            self.approve(), after_first_row_lock(),
            during=lambda: self._locked('menu_sections', self.section.pk))

        self.assertTrue(run.waited, 'PREMISE: the approval did not overlap the edit')
        self.assertTrue(
            run.observed, 'PREMISE: the approval did not hold the destination section')
        self.assertAnswered(run.first, 200, what='the move')
        self.assertApproved(run.second)
        self.item_late.refresh_from_db()
        self.assertEqual(self.item_late.section_id, self.section.id)

    def test_CONTROL_an_item_delete_in_flight(self):
        run = self.interleave(
            self.call('delete', ITEMS, {
                'id': str(self.item_b.id), 'deletion_reason': 'off the menu'}),
            self.approve(), after_first_row_lock())

        self.assertTrue(run.waited, 'PREMISE: the approval did not overlap the delete')
        self.assertAnswered(run.first, 200, what='the item delete')
        self.assertApproved(run.second)
        self.item_b.refresh_from_db()
        self.assertTrue(self.item_b.deleted)


class ControlTests(_EditRace):
    """What the fix must not change."""

    def test_CONTROL_an_edit_before_the_approval_reaches_the_menu_does_not_wait(self):
        """Non-overlap. The approval holds only the restaurant row until its
        section UPDATE, and the edit locks no restaurant row, so the edit runs
        straight through."""
        run = self.interleave(
            self.approve(),
            self.call('put', ITEMS, {
                'id': str(self.item.id), 'description': 'edited alongside'}),
            before(BEFORE_SECTION_UPDATE))

        self.assertFalse(
            run.waited,
            f'the edit waited ({sorted(run.waiting_on)}) although the approval '
            'held no menu row')
        self.assertAnswered(run.second, 200, what='the item edit')
        self.assertApproved(run.first)
        self.item.refresh_from_db()
        self.assertEqual(self.item.description, 'edited alongside')

    def test_CONTROL_a_section_edit_inside_the_window_waits_and_completes(self):
        """A section PUT locks the section alone, so it never took part in the
        cycle: it waited and completed on `bfa393e` too, and nothing here adds
        a lock to it."""
        run = self.edit_inside_the_window(self.call('put', SECTIONS, {
            'id': str(self.section.id), 'description': 'edited during approval'}))

        self.assertAnswered(run.second, 200, what='the section edit')
        self.assertApproved(run.first)
        self.section.refresh_from_db()
        self.assertEqual(self.section.description, 'edited during approval')

    def test_CONTROL_assigning_an_extra_and_demoting_it_still_serialize(self):
        """Relationship serialization. Two writes at one restaurant are serialized
        by the catalogue barrier: the demotion waits for the assignment, then
        sees that the extra is now offered and is refused. The extra stays an
        extra and stays assigned."""
        extras = [str(self.extra_1.id), str(self.extra_2.id)]
        run = self.interleave(
            self.call('put', ITEMS, {
                'id': str(self.burger.id), 'extras_applicable': extras,
                'extras_max_selections': 2}),
            self.call('put', ITEMS, {'id': str(self.extra_2.id), 'is_extra': False}),
            after_first_row_lock())

        self.assertTrue(run.waited, 'PREMISE: the demotion did not overlap')
        self.assertIn('advisory', run.waiting_on)
        self.assertAnswered(run.first, 200, what='the assignment')
        self.assertAnswered(
            run.second, 400, EXTRA_STILL_REFERENCED_MESSAGE, what='the demotion')
        self.burger.refresh_from_db()
        self.extra_2.refresh_from_db()
        self.assertEqual(self.burger.extras_applicable, extras)
        self.assertTrue(self.extra_2.is_extra)


class TheSectionIsLockedFirstTests(_EditRace):
    """PIN the mechanism through the endpoints. Each write is held just after its
    first row lock, with no approval running, and the section must be locked
    while the record is not yet. On `bfa393e` the first row lock was Secretary's,
    which takes both at once."""

    def _pin(self, request, table, pk, section_pk):
        response, (section_locked, record_locked) = self.park_alone(
            request, after_first_row_lock(),
            during=lambda: (self._locked('menu_sections', section_pk),
                            self._locked(table, pk)))
        self.assertEqual(
            (section_locked, record_locked), (True, False),
            'the first row lock must take the section and not yet the record')
        return response

    def test_PIN_an_item_edit_locks_the_section_first(self):
        response = self._pin(
            self.call('put', ITEMS, {'id': str(self.item.id), 'description': 'pinned'}),
            'menu_items', self.item.pk, self.section.pk)
        self.assertAnswered(response, 200, what='the item edit')
        self.item.refresh_from_db()
        self.assertEqual(self.item.description, 'pinned')

    def test_PIN_an_item_delete_locks_the_section_first(self):
        response = self._pin(
            self.call('delete', ITEMS, {
                'id': str(self.item_b.id), 'deletion_reason': 'pinned'}),
            'menu_items', self.item_b.pk, self.section.pk)
        self.assertAnswered(response, 200, what='the item delete')
        self.item_b.refresh_from_db()
        self.assertTrue(self.item_b.deleted)

    def test_PIN_a_group_edit_locks_the_section_first(self):
        response = self._pin(
            self.call('put', GROUPS, {'id': str(self.group.id), 'description': 'pinned'}),
            'section_groups', self.group.pk, self.section.pk)
        self.assertAnswered(response, 200, what='the group edit')
        self.group.refresh_from_db()
        self.assertEqual(self.group.description, 'pinned')

    def test_PIN_a_group_delete_locks_the_section_first(self):
        response = self._pin(
            self.call('delete', GROUPS, {
                'id': str(self.empty_group.id), 'deletion_reason': 'pinned'}),
            'section_groups', self.empty_group.pk, self.section.pk)
        self.assertAnswered(response, 200, what='the group delete')
        self.empty_group.refresh_from_db()
        self.assertTrue(self.empty_group.deleted)


class _FailingQueryset:
    """Stands in for a scoped queryset whose database read fails."""

    def filter(self, **kwargs):
        return self

    def select_related(self, *fields):
        return self

    def select_for_update(self, **kwargs):
        return self

    def order_by(self, *fields):
        return self

    def __iter__(self):
        raise OperationalError('the database went away')


class _UntouchableQueryset:
    """Fails the test if the helper uses the queryset at all."""

    def __init__(self, test):
        self._test = test

    def __getattr__(self, name):
        self._test.fail(f'the queryset was used ({name}) for a record with no section')


class LockParentSectionTests(_EditRace):
    """PIN the helper. Looked up on the module when each test runs, so on
    `bfa393e`, where it does not exist, these error rather than fail."""

    def helper(self):
        from restaurants_app.endpoints import restaurant_setup
        return restaurant_setup._lock_parent_section

    def scoped(self, record, model):
        return build_scoped_instance_queryset(self.owner, record, model)

    def test_PIN_it_locks_the_section_of_the_callers_own_record(self):
        lock = self.helper()
        for record, model, table, row in (
                ('menuitems', MenuItem, 'menu_items', self.item),
                ('sectiongroups', SectionGroup, 'section_groups', self.group)):
            # Resolving the caller's scope reads the memberships; build it before
            # counting, so the count is the helper's alone.
            scoped = self.scoped(record, model)
            with self.subTest(record=record), transaction.atomic():
                with CaptureQueriesContext(connection) as queries:
                    lock(record, scoped, str(row.id))
                self.assertEqual(len(queries), 1, queries.captured_queries)
                self.assertTrue(self._locked('menu_sections', self.section.pk))
                self.assertFalse(self._locked(table, row.pk))

    def test_PIN_a_record_outside_the_callers_scope_locks_nothing(self):
        lock = self.helper()
        for record, model, table, row in (
                ('menuitems', MenuItem, 'menu_items', self.foreign_item),
                ('sectiongroups', SectionGroup, 'section_groups', self.foreign_group)):
            with self.subTest(record=record), transaction.atomic():
                lock(record, self.scoped(record, model), str(row.id))
                self.assertFalse(self._locked('menu_sections', self.foreign_section.pk))
                self.assertFalse(self._locked(table, row.pk))

    def test_PIN_a_malformed_id_locks_nothing_and_raises_nothing(self):
        lock = self.helper()
        for malformed in ('not-a-uuid', ['a list'], {'a': 'dict'}, None):
            with self.subTest(id=malformed), transaction.atomic():
                lock('menuitems', self.scoped('menuitems', MenuItem), malformed)
                # The transaction is still usable: nothing reached the database
                # that could have aborted it.
                self.assertTrue(MenuSection.objects.filter(pk=self.section.pk).exists())
                self.assertFalse(self._locked('menu_sections', self.section.pk))

    def test_PIN_a_database_error_is_not_swallowed(self):
        lock = self.helper()
        with self.assertRaises(OperationalError):
            lock('menuitems', _FailingQueryset(), str(uuid.uuid4()))

    def test_PIN_other_records_issue_no_query(self):
        lock = self.helper()
        for record in ('menusections', 'tables', 'diningareas', 'restaurants',
                       'employees'):
            with self.subTest(record=record):
                with CaptureQueriesContext(connection) as queries:
                    lock(record, _UntouchableQueryset(self), str(uuid.uuid4()))
                self.assertEqual(len(queries), 0, queries.captured_queries)
