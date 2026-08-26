"""
Every production ``RestaurantEmployee`` writer takes the parent-Restaurant barrier
(MEMBERSHIP-LOCK-00).

WHY A STRUCTURAL TEST AND NOT ONLY THE CONCURRENCY PROOFS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The concurrency suite (``platform_admin_app/tests_owner_membership_concurrency.py``)
proves the writers that exist today serialize. It cannot say anything about the
writer somebody adds next year, and a membership writer is an easy thing to add
innocently — it looks like an ordinary model save.

So this file states the INVENTORY as an executable fact: the set of production
modules capable of writing a membership is exactly the set below, and each one either
takes the barrier or carries a recorded reason why it does not. Adding a new
create / update / reactivate / delete path makes this fail until its lock discipline
has been looked at deliberately.

WHAT IT DOES NOT DO. It is not a repository-wide ORM framework and does not try to
prove correct lock ORDERING — no static check can. It answers one question: is a new
membership writer visible to review? Test and fixture writes are out of scope
entirely (the whole suite creates memberships freely; a ratchet that fired on those
would be noise, and noise gets muted).
"""
import ast
import pathlib
import uuid

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from dinify_backend.configss.string_definitions import RESTAURANT_OWNER
from restaurants_app.controllers.employee_membership_lock import (
    lock_restaurant_for_membership_mutation,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

BARRIER = 'lock_restaurant_for_membership_mutation'

MODEL = 'RestaurantEmployee'
WRITE_SERIALIZER = 'SerializerPutRestaurantEmployee'

# Manager methods that write. `filter(...).delete()` and `.update()` are included
# because they bypass `save()` entirely, which is exactly how a membership change
# slips past a reviewer looking for assignments.
MUTATING_MANAGER_CALLS = frozenset({
    'create', 'get_or_create', 'update_or_create', 'update',
    'bulk_create', 'bulk_update', 'delete',
})

# Field assignments that change what `assert_owner_consistency` counts.
PREDICATE_FIELDS = frozenset({'roles', 'active', 'deleted'})

# ── THE INVENTORY ────────────────────────────────────────────────────────────────
# Every production module that can write a membership, and its lock discipline.
# `True`  -> must reference the barrier by name.
# `str`   -> exempt, for the recorded reason.
SANCTIONED_WRITERS = {
    'restaurants_app/endpoints/restaurant_setup.py': True,
    'restaurants_app/controllers/create_employee.py': True,
    'restaurants_app/controllers/employees/create_employee.py': True,
    'platform_admin_app/onboarding_creation.py': (
        'Creates the owner membership for a restaurant INSERTed moments earlier in '
        'the same uncommitted transaction. No concurrent writer can see that parent '
        'row, let alone hold a membership at it, so there is nothing to serialize '
        'against — the tenant does not exist outside this transaction yet. Locking '
        'a row this transaction just created would be pure ceremony, and it would '
        'put a second Restaurant acquisition into a service whose lock order '
        '(User -> INSERT Restaurant -> ...) is deliberately documented.'
    ),
}

# Modules that merely NAME the model or the serializer — resolvers, policy tables,
# permission grids, the model and serializer definitions themselves. Listed so the
# scan can be blunt: anything not here and not sanctioned is a new writer.
KNOWN_NON_WRITERS = frozenset({
    'restaurants_app/models.py',
    'restaurants_app/serializers.py',
    'restaurants_app/controllers/con_cla_employees.py',
    'restaurants_app/controllers/employee_membership_lock.py',
    'restaurants_app/controllers/tenant_scope.py',
    'users_app/controllers/permissions_check.py',
    'misc_app/controllers/notifications/determine_recipients.py',
    'platform_admin_app/audit_actions.py',
    'platform_admin_app/models.py',
    'platform_admin_app/onboarding.py',
    'platform_admin_app/services.py',
    'dinify_backend/tenancy/all_fields_policy.py',
    'dinify_backend/tenancy/ambient_authority.py',
    'dinify_backend/tenancy/write_surface_policy.py',
})


def _production_modules():
    for path in REPO_ROOT.rglob('*.py'):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (
            'migrations/' in rel
            or '/tests' in rel
            or rel.startswith('tests')
            or pathlib.Path(rel).name.startswith('test')
            or 'tests' in pathlib.Path(rel).name
            or rel.startswith('scripts/')
            or rel.startswith('.')
        ):
            continue
        yield rel, path


def _attr_root(node):
    """The leftmost Name of an attribute chain, e.g. ``A`` in ``A.b.c()``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _writes_memberships(tree, source):
    """
    Does this module contain a production membership WRITE?

    Three signals, all read off the AST so docstrings and comments are invisible
    (``write_surface_policy`` names the serializer in a string literal, and that must
    not fire):

      1. ``RestaurantEmployee.objects.<mutating>(...)`` or ``RestaurantEmployee(...)``;
      2. a reference to the sole write serializer over the model;
      3. assignment to ``roles`` / ``active`` / ``deleted`` in a module that imports
         the model — the direct-instance shape the reactivation path uses.
    """
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    imports_model = MODEL in names

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == MODEL:
                return True                                        # signal 1
            if (
                isinstance(func, ast.Attribute)
                and func.attr in MUTATING_MANAGER_CALLS
                and _attr_root(func) == MODEL
            ):
                return True                                        # signal 1
        if isinstance(node, ast.Name) and node.id == WRITE_SERIALIZER:
            return True                                            # signal 2
        if imports_model and isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in PREDICATE_FIELDS
                ):
                    return True                                    # signal 3
    return False


class MembershipWriterInventoryTests(TestCase):
    """The ratchet."""

    def _scan(self):
        found = {}
        for rel, path in _production_modules():
            source = path.read_text(encoding='utf-8')
            if MODEL not in source and WRITE_SERIALIZER not in source:
                continue
            if _writes_memberships(ast.parse(source), source):
                found[rel] = source
        return found

    def test_the_set_of_production_membership_writers_is_the_sanctioned_set(self):
        found = self._scan()
        unexpected = sorted(set(found) - set(SANCTIONED_WRITERS))
        self.assertEqual(
            unexpected, [],
            'NEW PRODUCTION MEMBERSHIP WRITER(S). A path that can create, update, '
            'reactivate or soft-delete a RestaurantEmployee changes what '
            'assert_owner_consistency reads, so it must take the parent-Restaurant '
            'barrier (restaurants_app.controllers.employee_membership_lock) before '
            'it writes — or be added to SANCTIONED_WRITERS with the reason it does '
            f'not need to: {unexpected}',
        )

    def test_every_sanctioned_writer_still_writes(self):
        # The other direction: an entry that no longer writes anything is stale and
        # would silently stop protecting whatever replaced it.
        found = self._scan()
        missing = sorted(set(SANCTIONED_WRITERS) - set(found))
        self.assertEqual(
            missing, [],
            f'SANCTIONED_WRITERS names modules that no longer write a membership; '
            f'remove them so the inventory keeps meaning something: {missing}',
        )

    def test_every_non_exempt_writer_takes_the_barrier(self):
        found = self._scan()
        for rel, discipline in SANCTIONED_WRITERS.items():
            with self.subTest(module=rel):
                if discipline is not True:
                    self.assertIsInstance(
                        discipline, str,
                        'an exemption must record its reason as a string',
                    )
                    self.assertGreater(len(discipline), 80, 'state the reason fully')
                    continue
                tree = ast.parse(found[rel])
                self.assertIn(
                    BARRIER,
                    {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)},
                    f'{rel} writes memberships but never calls {BARRIER}',
                )

    def test_known_non_writers_are_not_secretly_writers(self):
        # Keeps the two lists honest: a module can be in exactly one of them.
        overlap = sorted(set(KNOWN_NON_WRITERS) & set(SANCTIONED_WRITERS))
        self.assertEqual(overlap, [])
        found = self._scan()
        self.assertEqual(sorted(set(found) & KNOWN_NON_WRITERS), [])


class BarrierPrimitiveTests(TransactionTestCase):
    """
    The helper's own contract. Small, but each of these is a way the barrier could be
    present in the diff and absent at runtime.
    """

    reset_sequences = False

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Bar', last_name='Rier', email='barrier@test.com',
            phone_number='256700000771', username='256700000771',
            country='UG', password='x', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Barrier Primitive', location='loc-barrier', owner=self.owner,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )

    def test_it_refuses_to_run_outside_a_transaction(self):
        # A select_for_update in autocommit is released by the statement that took
        # it: the caller would hold nothing and be told nothing, and every test above
        # would still pass. This is the one failure mode that has to be loud.
        with self.assertRaises(RuntimeError) as caught:
            lock_restaurant_for_membership_mutation(self.restaurant.id)
        self.assertIn('inside a transaction', str(caught.exception))

    def test_it_returns_the_locked_restaurant(self):
        with transaction.atomic():
            locked = lock_restaurant_for_membership_mutation(self.restaurant.id)
        self.assertEqual(locked.pk, self.restaurant.pk)

    def test_it_accepts_a_string_id(self):
        with transaction.atomic():
            locked = lock_restaurant_for_membership_mutation(str(self.restaurant.id))
        self.assertEqual(locked.pk, self.restaurant.pk)

    def test_an_unresolvable_target_is_none_and_not_an_exception(self):
        # The callers already have an authorization gate and a not-found posture;
        # a new refusal here would change what those endpoints answer.
        for value in (None, '', 'not-a-uuid', 42, uuid.uuid4()):
            with self.subTest(value=value), transaction.atomic():
                self.assertIsNone(lock_restaurant_for_membership_mutation(value))

    def test_a_soft_deleted_restaurant_is_still_returned(self):
        # Deliberately NOT refused: this is a serialization primitive, not a policy
        # one. Refusing here would invent a 404 the employee endpoints never had.
        self.restaurant.deleted = True
        self.restaurant.save(update_fields=['deleted'])
        with transaction.atomic():
            locked = lock_restaurant_for_membership_mutation(self.restaurant.id)
        self.assertEqual(locked.pk, self.restaurant.pk)

    def test_it_locks_the_restaurants_row_and_nothing_else(self):
        # `of=('self',)` is what keeps this from locking joined rows. PR-E is the
        # documented cost of getting that wrong.
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        if connection.vendor != 'postgresql':
            self.skipTest('FOR UPDATE OF is PostgreSQL-specific.')
        with CaptureQueriesContext(connection) as captured, transaction.atomic():
            lock_restaurant_for_membership_mutation(self.restaurant.id)
        locking = [q['sql'] for q in captured.captured_queries if 'FOR UPDATE' in q['sql']]
        self.assertEqual(len(locking), 1, captured.captured_queries)
        self.assertIn('FOR UPDATE OF', locking[0])
        self.assertIn('"restaurants"', locking[0])
        self.assertNotIn('JOIN', locking[0].upper())

    def test_it_never_mutates_a_membership(self):
        before = list(
            RestaurantEmployee.objects.filter(restaurant=self.restaurant)
            .values_list('id', 'roles', 'active', 'deleted')
        )
        with transaction.atomic():
            lock_restaurant_for_membership_mutation(self.restaurant.id)
        after = list(
            RestaurantEmployee.objects.filter(restaurant=self.restaurant)
            .values_list('id', 'roles', 'active', 'deleted')
        )
        self.assertEqual(before, after)
