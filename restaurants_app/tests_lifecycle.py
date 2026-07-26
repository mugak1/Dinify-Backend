"""
Restaurant lifecycle (PR-5) — the vocabulary, the policy, the transition service.

Four concerns, in order:

* the constrained field and the fail-closed data migration's mapping logic;
* the per-state behaviour policy, cell by cell — including the two deliberate
  WIDENINGS (`onboarding` grants staff access and serves the diner menu, where
  `pending` denied both);
* the transition service: every allowed pair succeeds, every disallowed pair is
  refused AND audited, reasons are required, the audit row shares the transition's
  transaction, and concurrent transitions serialize;
* that `status` is unwritable by any generic route.

The endpoint (authentication + elevation) is covered in
``platform_admin_app.tests_lifecycle_endpoint``; the cross-tenant / delegated cases
live with the closure suite, per its own charter.
"""
import importlib
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from dinify_backend.configss.edit_information import EDIT_INFORMATION
from dinify_backend.configss.messages import ERR_RESTAURANT_UNAVAILABLE
from dinify_backend.configss.string_definitions import (
    MODULE_KITCHEN,
    MODULE_MENU,
    MODULE_SUPPORT,
    MODULE_TABLES,
    RESTAURANT_LIFECYCLE_STATES,
    RESTAURANT_OWNER,
    RESTAURANT_STAFF,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from orders_app.controllers.con_orders import ConOrder
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_LIFECYCLE_TRANSITION,
    ADMIN_RESTAURANT_TRANSITION_DENIED,
)
from platform_admin_app.models import RESULT_DENIED, RESULT_SUCCESS, AdminAuditLog
from restaurants_app.controllers import lifecycle
from restaurants_app.controllers import lifecycle_policy as policy
from restaurants_app.controllers.menu_publication import (
    resolve_public_restaurant, restaurant_can_serve_menu,
)
from restaurants_app.models import MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table
from users_app.controllers.permissions_check import (
    can_user_access_module, get_module_restaurant_ids,
)
from users_app.models import User


def _user(phone, roles=None):
    return User.objects.create_user(
        first_name='Life', last_name='Cycle', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=roles or [],
    )


class LifecycleVocabularyTests(TestCase):
    """The field is constrained and defaults to the start of the lifecycle."""

    def test_four_states_exactly(self):
        self.assertEqual(
            list(RESTAURANT_LIFECYCLE_STATES),
            ['onboarding', 'live', 'suspended', 'offboarded'],
        )

    def test_field_has_choices_default_and_index(self):
        field = Restaurant._meta.get_field('status')
        self.assertEqual(
            [value for value, _label in field.choices],
            list(RESTAURANT_LIFECYCLE_STATES),
        )
        self.assertEqual(field.default, RestaurantStatus_Onboarding)
        self.assertTrue(field.db_index)

    def test_new_restaurant_starts_onboarding(self):
        restaurant = Restaurant.objects.create(
            name='Fresh', location='loc', owner=_user('256760000001'),
        )
        self.assertEqual(restaurant.status, RestaurantStatus_Onboarding)

    def test_full_clean_rejects_a_legacy_value(self):
        """`choices` is real validation, not decoration — and the error names `status`.

        Asserting the FIELD (not merely that something raised) matters here: a bare
        assertRaises would pass even if full_clean tripped over an unrelated field
        and never looked at the lifecycle at all.
        """
        restaurant = Restaurant(
            name='Legacy', location='loc', owner=_user('256760000002'),
            status='active',
        )
        with self.assertRaises(ValidationError) as ctx:
            restaurant.full_clean()
        self.assertIn('status', ctx.exception.error_dict)


class LifecyclePolicyTests(TestCase):
    """The per-state matrix, cell by cell, from the policy's own predicates."""

    def test_staff_portal_row(self):
        self.assertTrue(policy.grants_portal_access(RestaurantStatus_Onboarding))
        self.assertTrue(policy.grants_portal_access(RestaurantStatus_Live))
        self.assertFalse(policy.grants_portal_access(RestaurantStatus_Suspended))
        self.assertFalse(policy.grants_portal_access(RestaurantStatus_Offboarded))

    def test_order_create_row(self):
        self.assertTrue(policy.allows_order_creation(RestaurantStatus_Onboarding))
        self.assertTrue(policy.allows_order_creation(RestaurantStatus_Live))
        self.assertFalse(policy.allows_order_creation(RestaurantStatus_Suspended))
        self.assertFalse(policy.allows_order_creation(RestaurantStatus_Offboarded))

    def test_kitchen_row(self):
        self.assertTrue(policy.allows_kitchen(RestaurantStatus_Onboarding))
        self.assertTrue(policy.allows_kitchen(RestaurantStatus_Live))
        self.assertFalse(policy.allows_kitchen(RestaurantStatus_Suspended))
        self.assertFalse(policy.allows_kitchen(RestaurantStatus_Offboarded))

    def test_kitchen_and_portal_rows_cannot_drift(self):
        """
        Kitchen authorisation runs through the portal-access resolver, so the two
        rows must agree. If someone edits one cell without the other, this fails
        rather than leaving a board reachable at a suspended restaurant.
        """
        for state in RESTAURANT_LIFECYCLE_STATES:
            self.assertEqual(
                policy.allows_kitchen(state),
                policy.grants_portal_access(state),
                state,
            )

    def test_diner_menu_row(self):
        self.assertEqual(
            policy.diner_menu_visibility(RestaurantStatus_Onboarding),
            policy.DINER_MENU_ALLOWED,
        )
        self.assertEqual(
            policy.diner_menu_visibility(RestaurantStatus_Live),
            policy.DINER_MENU_ALLOWED,
        )
        self.assertEqual(
            policy.diner_menu_visibility(RestaurantStatus_Suspended),
            policy.DINER_MENU_UNAVAILABLE,
        )
        self.assertEqual(
            policy.diner_menu_visibility(RestaurantStatus_Offboarded),
            policy.DINER_MENU_GONE,
        )

    def test_support_row_allowed_in_every_state(self):
        for state in RESTAURANT_LIFECYCLE_STATES:
            self.assertTrue(policy.allows_support(state), state)

    def test_delegated_access_row(self):
        for state in (RestaurantStatus_Onboarding, RestaurantStatus_Live,
                      RestaurantStatus_Suspended):
            self.assertIsNone(policy.delegated_scope_ceiling(state), state)
            self.assertEqual(policy.effective_delegated_scope('support', state), 'support')
        self.assertEqual(
            policy.delegated_scope_ceiling(RestaurantStatus_Offboarded), 'view',
        )
        self.assertEqual(
            policy.effective_delegated_scope('support', RestaurantStatus_Offboarded),
            'view',
        )

    def test_effective_scope_never_upgrades(self):
        for state in RESTAURANT_LIFECYCLE_STATES:
            self.assertIn(
                policy.effective_delegated_scope('view', state), ('view',), state,
            )

    def test_unknown_state_denies_everything(self):
        """Fail-closed: a legacy string or a typo grants nothing and shows no menu."""
        for value in ('active', 'pending', 'blocked', 'rejected', 'inactive', '', None):
            self.assertFalse(policy.grants_portal_access(value), value)
            self.assertFalse(policy.allows_order_creation(value), value)
            self.assertFalse(policy.allows_kitchen(value), value)
            self.assertEqual(
                policy.diner_menu_visibility(value), policy.DINER_MENU_GONE, value,
            )
            self.assertEqual(policy.delegated_scope_ceiling(value), 'view', value)

    def test_derived_state_sets_match_the_matrix(self):
        self.assertEqual(
            policy.PORTAL_ACCESS_STATES,
            frozenset({RestaurantStatus_Onboarding, RestaurantStatus_Live}),
        )
        self.assertEqual(
            policy.ORDERING_STATES,
            frozenset({RestaurantStatus_Onboarding, RestaurantStatus_Live}),
        )
        self.assertEqual(sorted(policy.PORTAL_ACCESS_STATES), policy.portal_access_states())


class PerStateBehaviourTests(TestCase):
    """The policy as the READERS actually experience it, one state at a time."""

    def setUp(self):
        self.owner = _user('256761000001')
        self.staff = _user('256761000002')
        self.restaurant = Restaurant.objects.create(
            name='Behaviour R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        self.section = MenuSection.objects.create(
            restaurant=self.restaurant, name='Mains', approved=True, enabled=True,
            available=True, availability='always',
        )
        self.item = MenuItem.objects.create(
            section=self.section, name='Dish', primary_price=Decimal('1000.00'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, enabled=True, is_active=True,
        )

    def _set(self, state):
        self.restaurant.status = state
        self.restaurant.save(update_fields=['status'])

    def _order(self):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.id),
            table_id=str(self.table.id),
            items=[{'item': str(self.item.id), 'quantity': 1}],
        )

    # --- staff portal / kitchen -------------------------------------------
    def test_onboarding_grants_staff_portal_and_kitchen(self):
        """THE WIDENING: `pending` denied the portal; `onboarding` grants it."""
        self._set(RestaurantStatus_Onboarding)
        self.assertTrue(
            can_user_access_module(self.owner, str(self.restaurant.id), MODULE_MENU))
        self.assertTrue(
            can_user_access_module(self.owner, str(self.restaurant.id), MODULE_KITCHEN))
        self.assertTrue(
            can_user_access_module(self.staff, str(self.restaurant.id), MODULE_TABLES))
        self.assertEqual(
            get_module_restaurant_ids(self.staff, MODULE_TABLES),
            {str(self.restaurant.id)},
        )

    def test_suspended_denies_staff_portal_and_kitchen(self):
        self._set(RestaurantStatus_Suspended)
        self.assertFalse(
            can_user_access_module(self.owner, str(self.restaurant.id), MODULE_MENU))
        self.assertFalse(
            can_user_access_module(self.owner, str(self.restaurant.id), MODULE_KITCHEN))
        self.assertEqual(get_module_restaurant_ids(self.staff, MODULE_TABLES), set())

    def test_offboarded_denies_staff_portal_and_kitchen(self):
        self._set(RestaurantStatus_Offboarded)
        self.assertFalse(
            can_user_access_module(self.owner, str(self.restaurant.id), MODULE_MENU))
        self.assertFalse(
            can_user_access_module(self.owner, str(self.restaurant.id), MODULE_KITCHEN))

    def test_support_reachable_in_every_state(self):
        for state in RESTAURANT_LIFECYCLE_STATES:
            self._set(state)
            self.assertTrue(
                can_user_access_module(
                    self.staff, str(self.restaurant.id), MODULE_SUPPORT,
                ),
                state,
            )

    # --- diner menu --------------------------------------------------------
    def test_diner_menu_per_state(self):
        self._set(RestaurantStatus_Onboarding)
        self.assertTrue(restaurant_can_serve_menu(self.restaurant))
        self._set(RestaurantStatus_Live)
        self.assertTrue(restaurant_can_serve_menu(self.restaurant))
        self._set(RestaurantStatus_Suspended)
        self.assertFalse(restaurant_can_serve_menu(self.restaurant))
        self._set(RestaurantStatus_Offboarded)
        self.assertFalse(restaurant_can_serve_menu(self.restaurant))

    def test_resolve_public_restaurant_per_state(self):
        self._set(RestaurantStatus_Onboarding)
        resolved, error = resolve_public_restaurant(str(self.restaurant.id))
        self.assertIsNone(error)
        self.assertEqual(resolved.id, self.restaurant.id)

        self._set(RestaurantStatus_Suspended)
        resolved, error = resolve_public_restaurant(str(self.restaurant.id))
        self.assertIsNone(resolved)
        self.assertEqual(error['status'], 503)
        self.assertEqual(error['message'], ERR_RESTAURANT_UNAVAILABLE)

        self._set(RestaurantStatus_Offboarded)
        resolved, error = resolve_public_restaurant(str(self.restaurant.id))
        self.assertIsNone(resolved)
        self.assertEqual(error['status'], 404)

    def test_soft_deleted_live_restaurant_is_still_404(self):
        """`deleted` is orthogonal — the soft-delete still wins over a live state."""
        self.restaurant.deleted = True
        self.restaurant.save(update_fields=['deleted'])
        resolved, error = resolve_public_restaurant(str(self.restaurant.id))
        self.assertIsNone(resolved)
        self.assertEqual(error['status'], 404)

    # --- order creation ----------------------------------------------------
    def test_order_create_allowed_while_onboarding(self):
        self._set(RestaurantStatus_Onboarding)
        self.assertEqual(self._order().get('status'), 200)

    def test_order_create_blocked_when_suspended(self):
        self._set(RestaurantStatus_Suspended)
        self.assertEqual(self._order().get('status'), 400)

    def test_order_create_blocked_when_offboarded(self):
        self._set(RestaurantStatus_Offboarded)
        self.assertEqual(self._order().get('status'), 400)


class TransitionMatrixTests(TestCase):
    """Every cell of the matrix, allowed and refused."""

    def setUp(self):
        self.actor = _user('256762000001', roles=[])  # the actor is an identity for the audit row, not an authority
        self.restaurant = Restaurant.objects.create(
            name='Matrix R', location='loc', owner=_user('256762000002'),
            status=RestaurantStatus_Onboarding,
        )

    def _at(self, state):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(status=state)
        self.restaurant.refresh_from_db()
        return self.restaurant

    def _go(self, to_state, reason='A perfectly good reason'):
        return lifecycle.transition_restaurant(
            restaurant=self.restaurant, to_state=to_state,
            reason=reason, actor=self.actor,
        )

    def test_every_allowed_transition_succeeds(self):
        for from_state, to_state in sorted(lifecycle.ALLOWED_TRANSITIONS):
            with self.subTest(f'{from_state}->{to_state}'):
                self._at(from_state)
                updated = self._go(to_state)
                self.assertEqual(updated.status, to_state)
                self.restaurant.refresh_from_db()
                self.assertEqual(self.restaurant.status, to_state)

    def test_every_disallowed_transition_is_refused_and_audited(self):
        for from_state in RESTAURANT_LIFECYCLE_STATES:
            for to_state in RESTAURANT_LIFECYCLE_STATES:
                if lifecycle.is_allowed(from_state, to_state):
                    continue
                with self.subTest(f'{from_state}->{to_state}'):
                    self._at(from_state)
                    # AdminAuditLog is append-only (no delete), so each iteration
                    # measures the DELTA rather than clearing the table.
                    before = AdminAuditLog.objects.filter(
                        action=ADMIN_RESTAURANT_TRANSITION_DENIED,
                        result=RESULT_DENIED,
                    ).count()
                    with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
                        self._go(to_state)
                    self.assertEqual(ctx.exception.code, 'transition_not_allowed')
                    self.restaurant.refresh_from_db()
                    self.assertEqual(self.restaurant.status, from_state)
                    after = AdminAuditLog.objects.filter(
                        action=ADMIN_RESTAURANT_TRANSITION_DENIED,
                        result=RESULT_DENIED,
                    ).count()
                    self.assertEqual(after, before + 1)

    def test_offboarded_to_live_is_refused(self):
        """Restoration is re-onboarding, not a transition."""
        self._at(RestaurantStatus_Offboarded)
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            self._go(RestaurantStatus_Live)
        self.assertEqual(ctx.exception.code, 'transition_not_allowed')

    def test_self_transition_is_refused(self):
        self._at(RestaurantStatus_Live)
        with self.assertRaises(lifecycle.LifecycleTransitionError):
            self._go(RestaurantStatus_Live)

    def test_allowed_targets_helper(self):
        self.assertEqual(
            lifecycle.allowed_targets(RestaurantStatus_Onboarding),
            ['live', 'offboarded'],
        )
        self.assertEqual(
            lifecycle.allowed_targets(RestaurantStatus_Offboarded), [],
        )


class TransitionValidationTests(TestCase):
    """Reason and target validation, and the audit entry each refusal leaves."""

    def setUp(self):
        self.actor = _user('256763000001', roles=[])  # the actor is an identity for the audit row, not an authority
        self.restaurant = Restaurant.objects.create(
            name='Validate R', location='loc', owner=_user('256763000002'),
            status=RestaurantStatus_Live,
        )

    def _go(self, **kwargs):
        payload = dict(
            restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
            reason='A perfectly good reason', actor=self.actor,
        )
        payload.update(kwargs)
        return lifecycle.transition_restaurant(**payload)

    def test_missing_reason_refused(self):
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            self._go(reason=None)
        self.assertEqual(ctx.exception.code, 'reason_required')
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    def test_blank_reason_refused(self):
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            self._go(reason='      ')
        self.assertEqual(ctx.exception.code, 'reason_required')

    def test_short_reason_refused(self):
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            self._go(reason='too short')          # 9 characters
        self.assertEqual(ctx.exception.code, 'reason_too_short')

    def test_minimum_length_reason_accepted(self):
        updated = self._go(reason='x' * lifecycle.MIN_REASON_LENGTH)
        self.assertEqual(updated.status, RestaurantStatus_Suspended)

    def test_unknown_target_state_refused(self):
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            self._go(to_state='archived')
        self.assertEqual(ctx.exception.code, 'unknown_state')

    def test_legacy_state_name_refused(self):
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            self._go(to_state='active')
        self.assertEqual(ctx.exception.code, 'unknown_state')

    def test_refusal_is_audited_with_its_error_code(self):
        with self.assertRaises(lifecycle.LifecycleTransitionError):
            self._go(reason='')
        entry = AdminAuditLog.objects.get(
            action=ADMIN_RESTAURANT_TRANSITION_DENIED)
        self.assertEqual(entry.action, ADMIN_RESTAURANT_TRANSITION_DENIED)
        self.assertEqual(entry.result, RESULT_DENIED)
        self.assertEqual(entry.error_code, 'reason_required')
        self.assertEqual(entry.resource_type, 'Restaurant')
        self.assertEqual(entry.resource_id, str(self.restaurant.id))


class TransitionAuditTests(TestCase):
    """The audit entry a successful transition writes, and its atomicity."""

    def setUp(self):
        self.actor = _user('256764000001', roles=[])  # the actor is an identity for the audit row, not an authority
        self.restaurant = Restaurant.objects.create(
            name='Audit R', location='loc', owner=_user('256764000002'),
            status=RestaurantStatus_Live,
        )

    def test_success_writes_one_entry_with_before_and_after(self):
        lifecycle.transition_restaurant(
            restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
            reason='Non-payment, third notice sent', actor=self.actor,
        )
        entry = AdminAuditLog.objects.get(
            action=ADMIN_RESTAURANT_LIFECYCLE_TRANSITION)
        self.assertEqual(entry.result, RESULT_SUCCESS)
        self.assertEqual(entry.actor_id, self.actor.id)
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))
        self.assertEqual(entry.before_state, {'status': RestaurantStatus_Live})
        self.assertEqual(entry.after_state, {'status': RestaurantStatus_Suspended})
        self.assertEqual(entry.reason, 'Non-payment, third notice sent')

    def test_audit_failure_rolls_back_the_transition(self):
        """
        NO AUDIT, NO ACTION. The entry shares the transition's transaction, so a
        failed audit write must leave the restaurant exactly where it was.
        """
        with patch(
            'platform_admin_app.audit.record',
            side_effect=RuntimeError('audit down'),
        ):
            with self.assertRaises(RuntimeError):
                lifecycle.transition_restaurant(
                    restaurant=self.restaurant,
                    to_state=RestaurantStatus_Suspended,
                    reason='Non-payment, third notice sent',
                    actor=self.actor,
                )
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action=ADMIN_RESTAURANT_LIFECYCLE_TRANSITION).exists()
        )

    def test_go_live_notifies_the_owner(self):
        """
        The notification that used to fire from Secretary.make_notification now
        fires here — going live is exactly when the owner wants to hear from us.
        """
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        self.restaurant.refresh_from_db()
        with patch(
            'misc_app.controllers.notifications.notification.Notification'
        ) as MockNotification:
            MockNotification.return_value.create_notification.return_value = None
            lifecycle.transition_restaurant(
                restaurant=self.restaurant, to_state=RestaurantStatus_Live,
                reason='Readiness confirmed, going live', actor=self.actor,
            )
        MockNotification.assert_called_once()
        msg_data = MockNotification.call_args.kwargs['msg_data']
        self.assertEqual(msg_data['msg_type'], 'restaurant-activated')
        self.assertEqual(msg_data['restaurant_id'], str(self.restaurant.id))

    def test_go_live_notification_survives_a_null_first_name(self):
        """The null-safe greeting contract, carried over from the old call site."""
        owner = self.restaurant.owner
        owner.first_name = None
        owner.save(update_fields=['first_name'])
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        self.restaurant.refresh_from_db()
        with patch(
            'misc_app.controllers.notifications.notification.Notification'
        ) as MockNotification:
            MockNotification.return_value.create_notification.return_value = None
            lifecycle.transition_restaurant(
                restaurant=self.restaurant, to_state=RestaurantStatus_Live,
                reason='Readiness confirmed, going live', actor=self.actor,
            )
        self.assertEqual(
            MockNotification.call_args.kwargs['msg_data']['first_name'], 'there',
        )

    def test_notification_failure_does_not_undo_the_transition(self):
        """Best-effort: a notification backend problem must not roll back go-live."""
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Onboarding)
        self.restaurant.refresh_from_db()
        with patch(
            'misc_app.controllers.notifications.notification.Notification',
            side_effect=RuntimeError('mongo down'),
        ):
            updated = lifecycle.transition_restaurant(
                restaurant=self.restaurant, to_state=RestaurantStatus_Live,
                reason='Readiness confirmed, going live', actor=self.actor,
            )
        self.assertEqual(updated.status, RestaurantStatus_Live)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    def test_non_go_live_transition_sends_no_notification(self):
        with patch(
            'misc_app.controllers.notifications.notification.Notification'
        ) as MockNotification:
            lifecycle.transition_restaurant(
                restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
                reason='Non-payment, third notice sent', actor=self.actor,
            )
        MockNotification.assert_not_called()


class TransitionSeamTests(TestCase):
    """The two Phase-1 seams: they exist, they are called, and they can refuse."""

    def setUp(self):
        self.actor = _user('256765000001', roles=[])  # the actor is an identity for the audit row, not an authority
        self.restaurant = Restaurant.objects.create(
            name='Seam R', location='loc', owner=_user('256765000002'),
            status=RestaurantStatus_Onboarding,
        )

    def test_readiness_seam_defaults_to_ready(self):
        result = lifecycle.check_go_live_readiness(self.restaurant)
        self.assertTrue(result.ready)
        self.assertEqual(result.blockers, [])

    def test_receivables_seam_defaults_to_none_outstanding(self):
        self.assertFalse(lifecycle.has_outstanding_receivables(self.restaurant))

    def test_go_live_consults_the_readiness_seam(self):
        with patch.object(
            lifecycle, 'check_go_live_readiness',
            return_value=lifecycle.ReadinessResult(False, ['No published menu item']),
        ) as mock_readiness:
            with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
                lifecycle.transition_restaurant(
                    restaurant=self.restaurant, to_state=RestaurantStatus_Live,
                    reason='Trying to go live early', actor=self.actor,
                )
        mock_readiness.assert_called_once()
        self.assertEqual(ctx.exception.code, 'not_ready_for_go_live')
        self.assertIn('No published menu item', ctx.exception.errors['blockers'])
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Onboarding)

    def test_readiness_seam_does_not_gate_other_transitions(self):
        with patch.object(
            lifecycle, 'check_go_live_readiness',
            return_value=lifecycle.ReadinessResult(False, ['blocked']),
        ):
            updated = lifecycle.transition_restaurant(
                restaurant=self.restaurant, to_state=RestaurantStatus_Offboarded,
                reason='Never opened; contract cancelled', actor=self.actor,
            )
        self.assertEqual(updated.status, RestaurantStatus_Offboarded)

    def test_offboarding_consults_the_receivables_seam(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Live)
        self.restaurant.refresh_from_db()
        with patch.object(
            lifecycle, 'has_outstanding_receivables', return_value=True,
        ) as mock_receivables:
            with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
                lifecycle.transition_restaurant(
                    restaurant=self.restaurant,
                    to_state=RestaurantStatus_Offboarded,
                    reason='Closing the account down', actor=self.actor,
                )
        mock_receivables.assert_called_once()
        self.assertEqual(ctx.exception.code, 'outstanding_receivables')
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.status, RestaurantStatus_Live)

    def test_receivables_seam_does_not_gate_suspension(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Live)
        self.restaurant.refresh_from_db()
        with patch.object(
            lifecycle, 'has_outstanding_receivables', return_value=True,
        ):
            updated = lifecycle.transition_restaurant(
                restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
                reason='Non-payment, third notice sent', actor=self.actor,
            )
        # Suspension is often FOR non-payment; gating it on receivables would make
        # the lever unusable exactly when it is needed.
        self.assertEqual(updated.status, RestaurantStatus_Suspended)


class TransitionConcurrencyTests(TestCase):
    """The from-state comes from the LOCKED row, not the caller's instance."""

    def setUp(self):
        self.actor = _user('256766000001', roles=[])  # the actor is an identity for the audit row, not an authority
        self.restaurant = Restaurant.objects.create(
            name='Race R', location='loc', owner=_user('256766000002'),
            status=RestaurantStatus_Live,
        )

    def test_stale_in_memory_instance_cannot_replay_a_transition(self):
        stale = Restaurant.objects.get(pk=self.restaurant.pk)   # still says `live`
        lifecycle.transition_restaurant(
            restaurant=self.restaurant, to_state=RestaurantStatus_Suspended,
            reason='Non-payment, third notice sent', actor=self.actor,
        )
        # The second caller still believes the restaurant is live. The service reads
        # the row under select_for_update, sees `suspended`, and refuses the
        # live->suspended pair as a self-transition rather than writing twice.
        with self.assertRaises(lifecycle.LifecycleTransitionError) as ctx:
            lifecycle.transition_restaurant(
                restaurant=stale, to_state=RestaurantStatus_Suspended,
                reason='Non-payment, third notice sent', actor=self.actor,
            )
        self.assertEqual(ctx.exception.code, 'transition_not_allowed')


class StatusWriteSurfaceTests(TestCase):
    """`status` is unreachable through every generic write route."""

    def test_absent_from_edit_information_restaurants(self):
        keys = {entry['key'] for entry in EDIT_INFORMATION.get('restaurants')}
        self.assertNotIn('status', keys)

    def test_absent_from_edit_information_table(self):
        """The Table.status entry went too — table-actions/update-status/ owns it."""
        keys = {entry['key'] for entry in EDIT_INFORMATION.get('table')}
        self.assertNotIn('status', keys)

    def test_no_edit_information_section_exposes_status(self):
        for section, entries in EDIT_INFORMATION.items():
            keys = {entry['key'] for entry in entries}
            self.assertNotIn('status', keys, section)

    def test_serializer_marks_status_read_only(self):
        from restaurants_app.serializers import SerializerPutRestaurant

        self.assertIn('status', SerializerPutRestaurant.Meta.read_only_fields)
        self.assertTrue(SerializerPutRestaurant().fields['status'].read_only)


class MigrationMappingTests(TestCase):
    """
    The 0056 data migration's mapping and its fail-closed refusals.

    Driven against the real migration module through the historical-model API the
    migration itself uses, so the mapping under test is the one that will run —
    not a re-statement of it.
    """

    migration = importlib.import_module(
        'restaurants_app.migrations.0056_restaurant_lifecycle_states'
        if False else
        'restaurants_app.migrations.%s' % '0056_restaurant_lifecycle_states'
    )

    class _Apps:
        """Minimal ``apps`` stand-in: the migration only calls get_model."""

        def get_model(self, app_label, model_name):
            return Restaurant

    def _run_forward(self):
        self.migration.forward(self._Apps(), None)

    def _run_backward(self):
        self.migration.backward(self._Apps(), None)

    def _restaurant(self, phone, status):
        restaurant = Restaurant.objects.create(
            name=f'M {phone}', location='loc', owner=_user(phone),
        )
        Restaurant.objects.filter(pk=restaurant.pk).update(status=status)
        return restaurant

    def test_forward_maps_the_three_known_values(self):
        active = self._restaurant('256767000001', 'active')
        pending = self._restaurant('256767000002', 'pending')
        blocked = self._restaurant('256767000003', 'blocked')
        self._run_forward()
        active.refresh_from_db()
        pending.refresh_from_db()
        blocked.refresh_from_db()
        self.assertEqual(active.status, RestaurantStatus_Live)
        self.assertEqual(pending.status, RestaurantStatus_Onboarding)
        self.assertEqual(blocked.status, RestaurantStatus_Suspended)

    def test_forward_is_idempotent(self):
        already = self._restaurant('256767000004', RestaurantStatus_Live)
        self._run_forward()
        self._run_forward()
        already.refresh_from_db()
        self.assertEqual(already.status, RestaurantStatus_Live)

    def test_forward_refuses_inactive_without_a_decision(self):
        self._restaurant('256767000005', 'inactive')
        with self.assertRaises(RuntimeError) as ctx:
            self._run_forward()
        self.assertIn('inactive', str(ctx.exception))

    def test_forward_refuses_rejected_without_a_decision(self):
        self._restaurant('256767000006', 'rejected')
        with self.assertRaises(RuntimeError) as ctx:
            self._run_forward()
        self.assertIn('rejected', str(ctx.exception))

    def test_forward_refuses_an_unrecognised_value(self):
        self._restaurant('256767000007', 'banana')
        with self.assertRaises(RuntimeError) as ctx:
            self._run_forward()
        self.assertIn('banana', str(ctx.exception))

    def test_forward_refusal_leaves_the_rows_untouched(self):
        """Fail closed AND fail clean — a refused run maps nothing."""
        good = self._restaurant('256767000008', 'active')
        self._restaurant('256767000009', 'inactive')
        with self.assertRaises(RuntimeError):
            self._run_forward()
        good.refresh_from_db()
        self.assertEqual(good.status, 'active')

    def test_backward_restores_the_prior_values(self):
        live = self._restaurant('256767000010', RestaurantStatus_Live)
        onboarding = self._restaurant('256767000011', RestaurantStatus_Onboarding)
        suspended = self._restaurant('256767000012', RestaurantStatus_Suspended)
        self._run_backward()
        live.refresh_from_db()
        onboarding.refresh_from_db()
        suspended.refresh_from_db()
        self.assertEqual(live.status, 'active')
        self.assertEqual(onboarding.status, 'pending')
        self.assertEqual(suspended.status, 'blocked')

    def test_round_trip_is_lossless_for_the_mapped_values(self):
        rows = {
            'active': self._restaurant('256767000013', 'active'),
            'pending': self._restaurant('256767000014', 'pending'),
            'blocked': self._restaurant('256767000015', 'blocked'),
        }
        self._run_forward()
        self._run_backward()
        for original, restaurant in rows.items():
            restaurant.refresh_from_db()
            self.assertEqual(restaurant.status, original)

    def test_backward_refuses_an_offboarded_row(self):
        """`offboarded` has no pre-migration equivalent, so reversal must stop."""
        self._restaurant('256767000016', RestaurantStatus_Offboarded)
        with self.assertRaises(RuntimeError) as ctx:
            self._run_backward()
        self.assertIn('offboarded', str(ctx.exception))
