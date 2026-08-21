"""
``manage.py mark_restaurant_test`` — the audited operator path for ``is_test``.

The flag decides whether a tenant's orders count as commerce, so the tests below are
organised around the three ways getting it wrong would hurt, rather than around the
command's arguments:

  WRONG TENANT — targeting is UUID-only and exact. A name-matched or `.first()`-ed
  target fails silently: the restaurant keeps working, it just stops being revenue.
  Covered by the malformed / unknown / soft-deleted cases.

  UNATTRIBUTABLE — a classification nobody stands behind. The actor is validated on
  all three counts before anything is written, the reason has to be a real sentence,
  and the write and its audit row share a transaction, so a failed audit takes the
  classification down with it.

  REWRITTEN HISTORY — orders placed under the previous classification keep it. That
  is asserted explicitly (`HistoricalOrdersUntouchedTests`), because the failure mode
  is a restated revenue figure rather than an error anybody would see.

THE TENANT WALL IS NOT RE-ASSERTED HERE. That ``Restaurant.is_test`` has no
customer-plane write surface — absent from both ``EDIT_INFORMATION['restaurants']``
and ``SerializerPutRestaurant`` — is already pinned by
``restaurants_app.tests_write_surface_tenancy.RestaurantIsTestPlatformOwnedTests``,
which is the ratchet that owns that guarantee. Copying its two assertions here would
add a second place to update without adding a second guarantee; this command adds an
OPERATOR path, not an API one, and does not weaken that wall.
"""
import uuid
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RestaurantStatus_Live,
)
from orders_app.models import Order
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED,
)
from platform_admin_app.management.commands.mark_restaurant_test import (
    Command,
    _parse_test_flag,
)
from platform_admin_app.models import RESULT_SUCCESS, AdminAuditLog
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.controllers.lifecycle import MIN_REASON_LENGTH
from restaurants_app.models import DiningArea, Restaurant, Table
from users_app.models import User

# Distinct phone range from the other admin suites (…04…) so the unique phone_number
# constraint cannot collide when suites run in one process.
_PHONE = iter(f'2567041000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'
REASON = 'Internal demo tenant, not a paying customer.'

COMMAND = 'mark_restaurant_test'


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True, is_active=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=[],
        account_type=account_type, is_active=is_active,
    )


def _make_staff(username='class-admin', email='class-admin@t.com', is_active=True):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False, is_active=is_active,
    )


def _make_restaurant(name='Baba Fixture', *, is_test=False, deleted=False):
    return Restaurant.objects.create(
        name=name, location=f'{name} Road', status=RestaurantStatus_Live,
        is_test=is_test, deleted=deleted,
        owner=_make_user(f'owner-{name}@t.com'.replace(' ', '-')),
    )


class _CommandFixture(AuditAssertionsMixin, TestCase):
    """One platform-staff actor and one restaurant, plus a terse invoker."""

    def setUp(self):
        super().setUp()
        self.actor = _make_staff()
        self.restaurant = _make_restaurant()

    def run_command(self, *, test, restaurant=None, actor=None, reason=REASON):
        """Invoke the command and return everything it printed."""
        out = StringIO()
        call_command(
            COMMAND,
            restaurant=str(
                self.restaurant.id if restaurant is None else restaurant
            ),
            test=test,
            actor=self.actor.username if actor is None else actor,
            reason=reason,
            stdout=out,
        )
        return out.getvalue()

    def flag(self):
        return Restaurant.objects.values_list('is_test', flat=True).get(
            pk=self.restaurant.pk,
        )

    def classification_entries(self):
        return AdminAuditLog.objects.filter(
            action=ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED,
        )


# --- A + B: the classification changes, in both directions ---------------------------

class ClassificationWriteTests(_CommandFixture):
    def test_false_to_true_sets_the_flag_and_audits_once(self):
        self.assertFalse(self.flag())

        self.run_command(test='true')

        self.assertTrue(self.flag())
        entry = self.assertAudited(
            ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.before_state, {'is_test': False})
        self.assertEqual(entry.after_state, {'is_test': True})

    def test_true_to_false_clears_the_flag_and_audits_the_reverse(self):
        """
        Correcting a mistaken classification is a first-class operation.

        If clearing the flag were not available through the same audited path, the
        correction would happen in a shell — the exact thing this command exists to
        replace, and precisely when a record matters most.
        """
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)

        self.run_command(test='false')

        self.assertFalse(self.flag())
        entry = self.assertAudited(
            ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.before_state, {'is_test': True})
        self.assertEqual(entry.after_state, {'is_test': False})

    def test_a_round_trip_leaves_two_entries_and_the_original_value(self):
        self.run_command(test='true')
        self.run_command(test='false')

        self.assertFalse(self.flag())
        self.assertEqual(self.classification_entries().count(), 2)

    def test_it_targets_only_the_named_restaurant(self):
        """No bulk mode, and no chance of one: siblings are untouched."""
        other = _make_restaurant('Sibling Ltd')

        self.run_command(test='true')

        other.refresh_from_db()
        self.assertFalse(other.is_test)
        self.assertEqual(
            self.classification_entries().filter(restaurant_id=other.id).count(), 0,
        )

    def test_the_write_touches_exactly_one_column(self):
        """
        The narrowness is the point, so it is asserted structurally rather than by
        listing the fields somebody remembered to worry about.

        ``time_last_updated`` is ``auto_now`` bookkeeping that every partial save in
        this repo names (see ``lifecycle.transition_restaurant``); ``is_test`` is the
        only field with meaning that moves. Status, owner, the soft-delete flag, the
        subscription and payment columns and everything else must compare equal.
        """
        before = Restaurant.objects.filter(pk=self.restaurant.pk).values().get()

        self.run_command(test='true')

        after = Restaurant.objects.filter(pk=self.restaurant.pk).values().get()
        differing = {key for key in before if before[key] != after[key]}
        self.assertEqual(differing, {'is_test', 'time_last_updated'})


# --- C: idempotency ------------------------------------------------------------------

class IdempotencyTests(_CommandFixture):
    """
    Re-running the same classification is a no-op, not a second decision.

    The audit log records decisions that CHANGED platform state. A runbook pasted
    twice must not read, months later, as two separate reclassifications.
    """

    def test_same_value_run_writes_nothing_and_audits_nothing(self):
        self.run_command(test='true')
        self.assertEqual(self.classification_entries().count(), 1)
        stamp = Restaurant.objects.values_list('time_last_updated', flat=True).get(
            pk=self.restaurant.pk,
        )

        self.run_command(test='true')

        self.assertTrue(self.flag())
        self.assertEqual(self.classification_entries().count(), 1)
        self.assertEqual(
            Restaurant.objects.values_list('time_last_updated', flat=True).get(
                pk=self.restaurant.pk,
            ),
            stamp,
            'a no-op must not even re-stamp the row',
        )

    def test_clearing_an_already_clear_flag_is_also_a_no_op(self):
        self.assertFalse(self.flag())

        self.run_command(test='false')

        self.assertFalse(self.flag())
        self.assertEqual(self.classification_entries().count(), 0)

    def test_a_no_op_says_so(self):
        printed = self.run_command(test='false')
        self.assertIn('already has is_test=false; no changes made', printed)
        self.assertIn('no-op', printed)

    def test_a_real_change_reports_the_transition(self):
        printed = self.run_command(test='true')
        self.assertIn('false -> true', printed)
        self.assertIn(str(self.restaurant.id), printed)
        self.assertIn(self.actor.username, printed)
        self.assertIn('changed', printed)

    def test_the_output_names_no_owner_or_diner_data(self):
        """Enough to confirm the target, and nothing about the people behind it."""
        printed = self.run_command(test='true')
        owner = self.restaurant.owner
        for leak in (owner.email, owner.username, owner.phone_number, str(owner.id)):
            self.assertNotIn(leak, printed)


# --- D: attribution ------------------------------------------------------------------

class AuditAttributionTests(_CommandFixture):
    def test_the_entry_names_the_actor_restaurant_and_reason(self):
        self.run_command(test='true', reason=f'   {REASON}   ')

        entry = self.assertAudited(ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED)
        self.assertEqual(entry.actor_id, self.actor.id)
        self.assertEqual(entry.actor_label, self.actor.username)
        self.assertEqual(entry.reason, REASON, 'the reason is stored trimmed')
        self.assertEqual(entry.resource_type, 'Restaurant')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.result, RESULT_SUCCESS)

    def test_the_state_blobs_carry_the_classification_and_nothing_else(self):
        """
        The UUID is the identity; the blobs are one boolean each.

        An audit row is read by people and kept forever, so it carries no owner name,
        email, phone or credential — nothing that would make the log a second copy of
        the tenant's PII.
        """
        self.run_command(test='true')

        entry = self.assertAudited(ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED)
        self.assertEqual(set(entry.before_state), {'is_test'})
        self.assertEqual(set(entry.after_state), {'is_test'})

        blob = f'{entry.before_state}{entry.after_state}{entry.reason}'
        owner = self.restaurant.owner
        for leak in (owner.email, owner.username, str(owner.id)):
            self.assertNotIn(leak, blob)

    def test_the_command_is_not_an_admin_session_action(self):
        """No session is involved: this is the shell, not an authenticated request."""
        self.run_command(test='true')

        entry = self.assertAudited(ADMIN_RESTAURANT_TEST_CLASSIFICATION_CHANGED)
        self.assertIsNone(entry.session_id)
        self.assertEqual(entry.request_id, '')
        self.assertIsNone(entry.source_ip)


# --- E: the actor must be a real, active platform-staff human ------------------------

class ActorValidationTests(_CommandFixture):
    def _assert_refused(self, actor):
        with self.assertRaises(CommandError):
            self.run_command(test='true', actor=actor)
        self.assertFalse(self.flag())
        self.assertEqual(self.classification_entries().count(), 0)

    def test_unknown_actor_is_refused(self):
        self._assert_refused('no-such-operator')

    def test_restaurant_user_actor_is_refused(self):
        outsider = _make_user('outsider@t.com', ACCOUNT_TYPE_RESTAURANT_USER,
                              username='class-outsider')
        self._assert_refused(outsider.username)

    def test_inactive_platform_staff_actor_is_refused(self):
        retired = _make_staff(username='class-retired', email='class-retired@t.com',
                              is_active=False)
        self._assert_refused(retired.username)

    def test_blank_actor_is_refused(self):
        self._assert_refused('   ')

    def test_the_restaurant_owner_cannot_be_the_actor(self):
        """
        The most tempting wrong actor: the tenant's own owner.

        Classification is a Dinify decision about a customer, so the customer can
        never be recorded as having made it.
        """
        self._assert_refused(self.restaurant.owner.username)


# --- F: the target must be a live restaurant, named by UUID --------------------------

class RestaurantResolutionTests(_CommandFixture):
    def test_malformed_uuid_is_a_clean_error(self):
        with self.assertRaises(CommandError) as caught:
            self.run_command(test='true', restaurant='baba-house')
        self.assertIn('UUID', str(caught.exception))
        self.assertEqual(self.classification_entries().count(), 0)

    def test_a_name_is_never_a_valid_identifier(self):
        """
        The whole targeting contract in one test.

        A name that matches an existing restaurant exactly is still refused — there
        is no lookup by name to fall back to, so no fuzzy match can ever pick the
        wrong tenant.
        """
        with self.assertRaises(CommandError):
            self.run_command(test='true', restaurant=self.restaurant.name)
        self.assertFalse(self.flag())

    def test_unknown_uuid_is_a_clean_error(self):
        with self.assertRaises(CommandError):
            self.run_command(test='true', restaurant=uuid.uuid4())
        self.assertEqual(self.classification_entries().count(), 0)

    def test_soft_deleted_restaurant_is_refused_and_not_revealed(self):
        gone = _make_restaurant('Closed Ltd', deleted=True)

        with self.assertRaises(CommandError) as caught:
            self.run_command(test='true', restaurant=gone.id)

        gone.refresh_from_db()
        self.assertFalse(gone.is_test)
        self.assertEqual(self.classification_entries().count(), 0)
        # Same sentence a missing row gets: the command never confirms a soft-deleted
        # tenant exists.
        self.assertIn('No restaurant with id', str(caught.exception))
        self.assertNotIn(gone.name, str(caught.exception))


# --- G: the reason must be a real sentence -------------------------------------------

class ReasonValidationTests(_CommandFixture):
    def _assert_refused(self, reason):
        with self.assertRaises(CommandError):
            self.run_command(test='true', reason=reason)
        self.assertFalse(self.flag())
        self.assertEqual(self.classification_entries().count(), 0)

    def test_blank_reason_is_refused(self):
        self._assert_refused('')

    def test_whitespace_only_reason_is_refused(self):
        self._assert_refused('        ')

    def test_reason_shorter_than_the_minimum_is_refused(self):
        self._assert_refused('x' * (MIN_REASON_LENGTH - 1))

    def test_reason_padded_to_length_with_whitespace_is_refused(self):
        """Trimming happens BEFORE the length check, or the bar is trivial to clear."""
        self._assert_refused('test' + ' ' * 40)

    def test_reason_at_exactly_the_minimum_is_accepted(self):
        self.run_command(test='true', reason='x' * MIN_REASON_LENGTH)
        self.assertTrue(self.flag())


# --- H: --test is a closed vocabulary ------------------------------------------------

class TestFlagVocabularyTests(_CommandFixture):
    def _assert_refused(self, value):
        with self.assertRaises(CommandError):
            self.run_command(test=value)
        self.assertFalse(self.flag())
        self.assertEqual(self.classification_entries().count(), 0)

    def test_truthy_strings_are_not_coerced(self):
        """
        '1', 'yes', 'y' and 'on' each require a GUESS about what the operator meant,
        and the guess decides whether a tenant's trading counts as revenue.
        """
        for value in ('1', '0', 'yes', 'no', 'y', 'n', 'on', 'off', 't', 'f'):
            with self.subTest(value=value):
                self._assert_refused(value)

    def test_empty_and_nonsense_values_are_refused(self):
        for value in ('', '   ', 'maybe', 'null', 'None'):
            with self.subTest(value=value):
                self._assert_refused(value)

    def test_case_is_folded(self):
        """
        ``TRUE`` is the same word, and must mean the same thing however it arrives.

        This is not cosmetic. ``call_command`` pushes a keyword through the parser
        for validation but then hands ``handle()`` the caller's RAW value, so a
        command that trusted argparse's ``type`` conversion would accept ``'TRUE'``
        and then classify the tenant ``false`` — the flag inverted, silently, with a
        success message and an audit row saying so.
        """
        self.run_command(test='TRUE')
        self.assertTrue(self.flag())

    def test_folding_does_not_widen_the_vocabulary(self):
        """``TRUEISH`` folds to a word that is still not in the vocabulary."""
        self._assert_refused('TRUEISH')
        self._assert_refused('False!')

    def test_the_shell_parser_itself_rejects_an_invalid_value(self):
        """
        ``choices`` is on the argument so ``--help`` prints the vocabulary and the
        CLI fails before ``handle()`` ever runs.
        """
        parser = Command().create_parser('manage.py', COMMAND)
        with self.assertRaises(CommandError):
            parser.parse_args([
                '--restaurant', str(self.restaurant.id), '--test', 'yes',
                '--actor', self.actor.username, '--reason', REASON,
            ])

    def test_the_two_invocation_modes_agree(self):
        """
        The shell path and the in-process path resolve every accepted spelling
        identically — the property the raw-keyword overwrite above would break.
        """
        parser = Command().create_parser('manage.py', COMMAND)
        for spelling, expected in (
            ('true', True), ('TRUE', True), ('  True  ', True),
            ('false', False), ('FALSE', False),
        ):
            with self.subTest(spelling=spelling):
                from_shell = parser.parse_args([
                    '--restaurant', str(self.restaurant.id), '--test', spelling,
                    '--actor', self.actor.username, '--reason', REASON,
                ]).test
                self.assertEqual(_parse_test_flag(from_shell), expected)
                self.assertEqual(_parse_test_flag(spelling), expected)


# --- I: the write and its audit row are atomic ---------------------------------------

class AuditAtomicityTests(_CommandFixture):
    """
    No audit, no classification.

    This is the half of the contract in ``platform_admin_app.audit`` that applies to a
    privileged successful state change: an administrative action that cannot be
    attributed must not be allowed to stand.
    """

    def test_a_failed_audit_write_rolls_the_classification_back(self):
        with patch(
            'platform_admin_app.audit.record',
            side_effect=RuntimeError('audit backend down'),
        ):
            with self.assertRaises(RuntimeError):
                self.run_command(test='true')

        self.assertFalse(self.flag())
        self.assertEqual(self.classification_entries().count(), 0)

    def test_a_failed_audit_write_rolls_a_clear_back_too(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)

        with patch(
            'platform_admin_app.audit.record',
            side_effect=RuntimeError('audit backend down'),
        ):
            with self.assertRaises(RuntimeError):
                self.run_command(test='false')

        self.assertTrue(self.flag())


# --- J: classification governs the future, never the past ----------------------------

class HistoricalOrdersUntouchedTests(_CommandFixture):
    """
    Existing ``Order.is_test`` rows are deliberately left alone.

    ``Order.is_test`` is derived at ADMISSION time, from the tenant flag and the
    lifecycle status read together under the advisory lock (see
    ``orders_app.controllers.services.order_admission``). Reclassifying the tenant
    changes what future orders derive; it does not restate orders already placed.
    Retro-labelling them would silently move historical revenue, which is a separate
    decision needing its own reasoning — and its own migration.
    """

    def setUp(self):
        super().setUp()
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        self.table = Table.objects.create(
            restaurant=self.restaurant, number=1, dining_area=area,
        )
        self.real_order = self._order(is_test=False)
        self.rehearsal_order = self._order(is_test=True)

    def _order(self, *, is_test):
        zero = Decimal('0.00')
        return Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=zero, discounted_cost=zero, savings=zero, actual_cost=zero,
            is_test=is_test,
        )

    def _assert_orders_unchanged(self):
        self.real_order.refresh_from_db()
        self.rehearsal_order.refresh_from_db()
        self.assertFalse(self.real_order.is_test)
        self.assertTrue(self.rehearsal_order.is_test)

    def test_marking_the_tenant_test_does_not_relabel_existing_orders(self):
        self.run_command(test='true')

        self.assertTrue(self.flag())
        self._assert_orders_unchanged()

    def test_clearing_the_tenant_flag_does_not_relabel_existing_orders(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)

        self.run_command(test='false')

        self.assertFalse(self.flag())
        self._assert_orders_unchanged()

    def test_the_order_rows_are_not_written_at_all(self):
        """Not merely equal afterwards — untouched, so the stamps still match."""
        stamps = dict(
            Order.objects.filter(restaurant=self.restaurant)
            .values_list('id', 'time_last_updated')
        )

        self.run_command(test='true')

        self.assertEqual(
            dict(
                Order.objects.filter(restaurant=self.restaurant)
                .values_list('id', 'time_last_updated')
            ),
            stamps,
        )
