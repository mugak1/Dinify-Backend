"""
D05 — the kitchen command contract, through the REAL routes.

Every test here drives an actual HTTP endpoint with an actual authorised
principal. Three kinds of test live in this file and they are labelled, because
conflating them is how a suite stops meaning anything:

  REGRESSION  — a defect reproduced on unmodified `fd190dd` and closed here. The
                Stage A run is cited in the docstring.
  CONTROL     — behaviour that was already correct and must not change.
  NEW POLICY  — a rule D05 introduces by approval, which had no prior behaviour.

The authoritative service is exercised BOTH through routing and directly, because
a rule that holds only at the adapter is a rule one future caller can walk past.
"""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from orders_app.models import Order, OrderAcceptance
from orders_app.controllers.services import kitchen_transition as kt
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.tests_kitchen import (
    KitchenTestBase, _fulfilment_url, _priority_url, _cancel_url, COMPLETED_URL,
)
from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind,
    OrderStatus_Cancelled,
    OrderStatus_Initiated,
    OrderStatus_Paid,
    OrderStatus_Pending,
    OrderStatus_Served,
)


class DraftBoundaryTests(KitchenTestBase):
    """REGRESSION. On `fd190dd` a draft walked new->preparing->ready->served over
    HTTP at 200/200/200, ending with `order_status='served'` — a SALE — and no
    acceptance evidence; the diner could then never place their own order
    (`400 This order cannot be submitted.`). Ordinary staff could also cancel one
    and set its priority. The board hid drafts, but hiding was never guarding."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def _draft(self):
        return self._make_order(order_status=OrderStatus_Initiated,
                                fulfilment_status='new')

    def test_no_fulfilment_action_may_touch_a_draft(self):
        for action in ('advance', 'serve', 'correct', 'recall'):
            order = self._draft()
            response = self._command(order, action)
            self.assertEqual(response.status_code, 409, msg=action)
            self.assertEqual(response.json()['reason'], 'order_is_draft', msg=action)
            order.refresh_from_db()
            self.assertEqual(order.fulfilment_status, 'new')
            self.assertEqual(order.order_status, OrderStatus_Initiated)
            self.assertIsNone(order.served_at)
            self.assertEqual(order.fulfilment_revision, 0)

    def test_cancel_may_not_touch_a_draft(self):
        order = self._draft()
        response = self._cancel_cmd(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'order_is_draft')
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        self.assertIsNone(order.cancelled_at)
        self.assertIsNone(order.cancellation_reason)

    def test_priority_may_not_touch_a_draft(self):
        order = self._draft()
        response = self._set_priority(order, True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'order_is_draft')
        order.refresh_from_db()
        self.assertFalse(order.priority)

    def test_the_service_refuses_a_draft_directly_too(self):
        """The rule lives in the boundary, not in the adapter — a future caller
        that skips the endpoint cannot walk past it."""
        order = self._draft()
        with self.assertRaises(kt.KitchenRefusal) as caught:
            kt.execute(
                order.pk, self.kitchen_user,
                kt.KitchenCommand(action=kt.ACTION_ADVANCE, if_revision=0),
            )
        self.assertEqual(caught.exception.reason, 'order_is_draft')
        self.assertEqual(caught.exception.status, 409)

    def test_the_diner_can_still_place_a_draft_the_kitchen_refused(self):
        """The knock-on the defect caused, now absent: the draft is untouched,
        so the diner's own submission still works."""
        order = self._draft()
        self._command(order, 'advance')
        self._cancel_cmd(order)
        order.refresh_from_db()

        from orders_app.controllers.manage_order import update_order_status
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref(order))
        self.assertEqual(result['status'], 200, result)
        self.assertTrue(OrderAcceptance.objects.filter(order=order).exists())

    def test_accepted_orders_are_the_control(self):
        """CONTROL: the boundary refuses drafts, not ordinary work."""
        order = self._make_order(fulfilment_status='new',
                                 order_status=OrderStatus_Pending)
        self.assertEqual(self._command(order, 'advance').status_code, 200)


class CancelledOrderIsTerminalTests(KitchenTestBase):
    """REGRESSION. On `fd190dd` the cancelled guard existed only on the `served`
    branch, so a cancelled order was still walked to `served` at 200/200/200 —
    acquiring a `served_at` stamp — and `ready->preparing` worked on one too."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.manager_user)

    def _cancelled(self, fulfilment='ready'):
        return self._make_order(
            fulfilment_status=fulfilment, order_status=OrderStatus_Cancelled,
            cancelled_at=timezone.now(), cancelled_by=self.owner_user,
            cancellation_reason=CancellationReason_CustomerChangedMind)

    def test_no_command_may_progress_a_cancelled_order(self):
        for action in ('advance', 'serve', 'correct', 'recall'):
            order = self._cancelled()
            response = self._command(order, action)
            self.assertEqual(response.status_code, 409, msg=action)
            self.assertEqual(response.json()['reason'], 'order_cancelled', msg=action)
            order.refresh_from_db()
            self.assertEqual(order.fulfilment_status, 'ready')
            self.assertIsNone(order.served_at)
            self.assertEqual(order.fulfilment_revision, 0)

    def test_a_cancelled_order_with_an_acceptance_receipt_is_still_terminal(self):
        """The receipt records that the diner's submission LANDED; it says
        nothing about the order still being live. Reordering is a new purchase."""
        order = self._cancelled()
        OrderAcceptance.objects.create(
            order=order, accepted_at=timezone.now(), quote_ref='x' * 64)
        response = self._command(order, 'serve')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'order_cancelled')

    def test_priority_may_not_touch_a_cancelled_order(self):
        order = self._cancelled()
        self.assertEqual(self._set_priority(order, True).status_code, 409)


class IncoherentStateTests(KitchenTestBase):
    """NEW POLICY (D5). Contradictory rows exist — the pre-D05 races could leave
    a served order carrying cancellation provenance, or an active one carrying a
    served stamp. They are refused for manual review and LEFT EXACTLY AS THEY
    ARE: never repaired, normalised or backfilled on the way past."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.manager_user)

    def _assert_refused_untouched(self, order, reason='order_state_incoherent'):
        before = Order.objects.get(pk=order.pk)
        response = self._command(order, 'advance')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], reason)
        after = Order.objects.get(pk=order.pk)
        for field in ('order_status', 'fulfilment_status', 'served_at',
                      'cancelled_at', 'cancellation_reason',
                      'fulfilment_revision', 'priority'):
            self.assertEqual(getattr(after, field), getattr(before, field),
                             msg=f'{field} was modified by a refusal')

    def test_served_order_status_with_cancellation_provenance_is_refused(self):
        # Exactly the shape the C1 cancel-vs-serve race produced.
        self._assert_refused_untouched(self._make_order(
            order_status=OrderStatus_Served, fulfilment_status='served',
            served_at=timezone.now(), cancelled_at=timezone.now(),
            cancellation_reason=CancellationReason_CustomerChangedMind))

    def test_an_active_ticket_retaining_a_served_stamp_is_refused(self):
        self._assert_refused_untouched(self._make_order(
            order_status=OrderStatus_Pending, fulfilment_status='preparing',
            served_at=timezone.now()))

    def test_a_served_order_with_no_served_stamp_is_refused(self):
        self._assert_refused_untouched(self._make_order(
            order_status=OrderStatus_Served, fulfilment_status='served',
            served_at=None))

    def test_a_served_order_status_on_an_active_fulfilment_is_refused(self):
        self._assert_refused_untouched(self._make_order(
            order_status=OrderStatus_Served, fulfilment_status='ready',
            served_at=None))

    def test_an_unknown_order_status_is_refused(self):
        self._assert_refused_untouched(self._make_order(
            order_status='banana', fulfilment_status='new'))

    def test_a_closed_order_is_refused_as_terminal_not_incoherent(self):
        """A paid order is not contradictory — it is finished. Saying
        'incoherent' would send an operator hunting for a data fault."""
        self._assert_refused_untouched(
            self._make_order(order_status=OrderStatus_Paid,
                             fulfilment_status='served',
                             served_at=timezone.now()),
            reason='order_terminal')


class LegacyCompatibilityTests(KitchenTestBase):
    """NEW POLICY (D5). An order predating `OrderAcceptance` has no receipt, and
    requiring one would strand real historical service. "Not a draft" is far too
    broad a classifier, so the COHERENT combinations are enumerated and remain
    fully operable — under the same revision, permission, recall-age and
    occupancy rules as anything else.

    This is a bounded operational decision under historical uncertainty. It does
    NOT assert those rows were accepted, and it changes nothing about D04's
    `evidence_unavailable`."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.manager_user)

    def test_a_coherent_active_legacy_order_is_fully_operable(self):
        for order_status in (OrderStatus_Pending, 'preparing'):
            order = self._make_order(order_status=order_status,
                                     fulfilment_status='new')
            self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())
            self.assertEqual(self._command(order, 'advance').status_code, 200,
                             msg=order_status)
            self.assertEqual(self._command(order, 'advance').status_code, 200)
            self.assertEqual(self._command(order, 'serve').status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.fulfilment_status, 'served')

    def test_a_coherent_served_legacy_order_is_recallable(self):
        order = self._make_order(
            order_status=OrderStatus_Served, fulfilment_status='served',
            served_at=timezone.now() - timedelta(minutes=2))
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())
        self.assertEqual(self._command(order, 'recall').status_code, 200)

    def test_a_legacy_order_is_cancellable_under_the_ordinary_rules(self):
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        self.assertEqual(self._cancel_cmd(order).status_code, 200)

    def test_no_receipt_is_invented_for_a_legacy_order(self):
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        self._command(order, 'advance')
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())


class RevisionPreconditionTests(KitchenTestBase):
    """REGRESSION + NEW POLICY. On `fd190dd` a delayed
    `{'fulfilment_status': 'preparing'}` arriving after another device reached
    `ready` was executed as a RECALL (200), and after a serve/recall/serve cycle
    a delayed recall reopened a LATER completion — a case no source-state check
    can see, because the source state is `served` again."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def test_a_delayed_advance_is_never_reinterpreted_as_a_correction(self):
        order = self._make_order(fulfilment_status='new')
        stale = self._rev(order)                       # device A's view
        self.assertEqual(self._command(order, 'advance').status_code, 200)
        self.assertEqual(self._command(order, 'advance').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')

        # A's delayed request lands. The ACTION alone already prevents it being
        # read as a correction; the revision refuses it outright.
        response = self._command(order, 'advance', if_revision=stale)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'kitchen_precondition_stale')
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'ready')

    def test_a_delayed_recall_cannot_reopen_a_later_completion(self):
        """THE CYCLE CASE. The source state matches again, so only the revision
        can tell these two moments apart."""
        order = self._make_order(fulfilment_status='ready')
        self.assertEqual(self._command(order, 'serve').status_code, 200)
        stale = self._rev(order)                       # the FIRST completion

        self.assertEqual(self._command(order, 'recall').status_code, 200)
        self.assertEqual(self._command(order, 'serve').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')
        second_completion = order.served_at

        response = self._command(order, 'recall', if_revision=stale)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'kitchen_precondition_stale')
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'served')
        self.assertEqual(order.served_at, second_completion)

    def test_a_stale_revision_refuses_even_when_the_state_matches_the_target(self):
        """The rule is compare-and-set, not "is the world already how you want
        it". A stale command never performs another effect."""
        order = self._make_order(fulfilment_status='new')
        stale = self._rev(order)
        self._command(order, 'advance')                # -> preparing (rev 1)
        self._command(order, 'correct', if_revision=None)  # no: correct needs ready
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'preparing')

        # Replay A's original advance: the target state is ALREADY `preparing`.
        response = self._command(order, 'advance', if_revision=stale)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'kitchen_precondition_stale')
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'preparing')
        self.assertEqual(order.fulfilment_revision, 1)

    def test_an_exact_duplicate_advance_is_refused(self):
        order = self._make_order(fulfilment_status='new')
        stale = self._rev(order)
        self.assertEqual(
            self._command(order, 'advance', if_revision=stale).status_code, 200)
        self.assertEqual(
            self._command(order, 'advance', if_revision=stale).status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'preparing')
        self.assertEqual(order.fulfilment_revision, 1)

    def test_the_revision_counts_every_applied_command_not_only_fulfilment(self):
        order = self._make_order(fulfilment_status='new', priority=False)
        self.assertEqual(self._rev(order), 0)
        self._command(order, 'advance')
        self.assertEqual(self._rev(order), 1)
        self._set_priority(order, True)                       # a real change
        self.assertEqual(self._rev(order), 2)
        self._set_priority(order, True)                       # no change
        self.assertEqual(self._rev(order), 2)
        # Cancelling a ticket already in preparation needs the manage-level
        # gate — that rule is unchanged by D05, so the fixture respects it.
        self.client.force_authenticate(user=self.manager_user)
        self.assertEqual(self._cancel_cmd(order).status_code, 200)
        self.assertEqual(self._rev(order), 3)

    def test_a_refusal_never_increments_the_revision(self):
        order = self._make_order(fulfilment_status='new')
        for call in (
            lambda: self._command(order, 'serve'),
            lambda: self._command(order, 'recall'),
            lambda: self._command(order, 'advance', if_revision=99),
            lambda: self._set_priority(order, True, if_revision=99),
        ):
            call()
            self.assertEqual(self._rev(order), 0)

    def test_the_revision_never_resets_across_a_serve_recall_cycle(self):
        order = self._make_order(fulfilment_status='ready')
        self._command(order, 'serve')
        self._command(order, 'recall')
        self._command(order, 'serve')
        self.assertEqual(self._rev(order), 3)

    def test_the_revision_is_not_writable_through_any_request(self):
        order = self._make_order(fulfilment_status='new')
        self._command(order, 'advance', fulfilment_revision=4096)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_revision, 1)

    def test_a_revision_at_the_ceiling_refuses_rather_than_overflowing(self):
        order = self._make_order(fulfilment_status='new')
        Order.objects.filter(pk=order.pk).update(
            fulfilment_revision=kt.MAX_REVISION)
        response = self._command(order, 'advance', if_revision=kt.MAX_REVISION)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'revision_limit_reached')
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_revision, kt.MAX_REVISION)


class RecallWindowTests(KitchenTestBase):
    """NEW POLICY (D1). A server-enforced 10-minute window from the CURRENT
    completion. On `fd190dd` there was no server age rule at all: a ticket served
    30 days earlier was invisible in the Completed feed and still recalled at
    200. Feed retention (24h) is VISIBILITY and is deliberately a different
    duration from permission."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def _served(self, ago):
        return self._make_order(
            fulfilment_status='served', order_status=OrderStatus_Served,
            served_at=timezone.now() - ago)

    def test_just_inside_the_window_is_allowed(self):
        order = self._served(kt.RECALL_WINDOW - timedelta(seconds=5))
        self.assertEqual(self._command(order, 'recall').status_code, 200)

    def test_just_outside_the_window_is_refused(self):
        order = self._served(kt.RECALL_WINDOW + timedelta(seconds=5))
        response = self._command(order, 'recall')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'recall_window_expired')

    def test_at_the_boundary_is_allowed(self):
        """`0 <= age <= RECALL_WINDOW` — the bound is inclusive. A hair is
        subtracted because the server reads its own clock after the lock, which
        is always a moment later than the fixture's."""
        order = self._served(kt.RECALL_WINDOW - timedelta(milliseconds=200))
        self.assertEqual(self._command(order, 'recall').status_code, 200)

    def test_a_future_completion_stamp_is_not_an_unlimited_window(self):
        order = self._make_order(
            fulfilment_status='served', order_status=OrderStatus_Served,
            served_at=timezone.now() + timedelta(hours=1))
        response = self._command(order, 'recall')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'recall_window_expired')

    def test_re_serving_starts_a_fresh_window(self):
        order = self._served(kt.RECALL_WINDOW - timedelta(seconds=5))
        self.assertEqual(self._command(order, 'recall').status_code, 200)
        self.assertEqual(self._command(order, 'serve').status_code, 200)
        # The new completion is now, so recall is available again.
        self.assertEqual(self._command(order, 'recall').status_code, 200)

    def test_feed_visibility_is_not_recall_permission(self):
        """The two durations are deliberately different: an operator can still
        SEE a ticket served an hour ago and cannot recall it."""
        order = self._served(timedelta(hours=1))
        feed = self.client.get(COMPLETED_URL, {'restaurant': str(self.restaurant.id)})
        self.assertIn(str(order.id), [t['id'] for t in feed.json()['data']])
        self.assertEqual(self._command(order, 'recall').status_code, 409)

    def test_there_is_no_manager_override_for_an_expired_recall(self):
        order = self._served(timedelta(hours=1))
        for user in (self.manager_user, self.owner_user):
            self.client.force_authenticate(user=user)
            self.assertEqual(self._command(order, 'recall').status_code, 409,
                             msg=user.username)

    def test_correction_carries_no_time_rule(self):
        """CONTROL: `ready -> preparing` completed nothing, so there is no
        completion for it to be within a window of."""
        order = self._make_order(fulfilment_status='ready')
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() - timedelta(days=3))
        self.assertEqual(self._command(order, 'correct').status_code, 200)


class RecallOccupancyTests(KitchenTestBase):
    """REGRESSION. On `fd190dd` recall never consulted occupancy, so serving an
    order (freeing the table), letting a new order claim it, then recalling the
    first left TWO ongoing orders on one table — deterministically, without any
    race at all."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def _occupants(self, table):
        return list(
            Order.objects.filter(table=table, deleted=False)
            .exclude(order_status=OrderStatus_Initiated)
            .exclude(order_status=OrderStatus_Cancelled)
            .exclude(fulfilment_status='served')
            .values_list('id', flat=True))

    def test_recall_is_refused_when_the_table_was_taken(self):
        served = self._make_order(
            table=self.table1, fulfilment_status='served',
            order_status=OrderStatus_Served, served_at=timezone.now())
        newcomer = self._make_order(table=self.table1,
                                    order_status=OrderStatus_Pending)

        response = self._command(served, 'recall')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'table_occupied')

        occupants = self._occupants(self.table1)
        self.assertEqual(occupants, [newcomer.id])
        served.refresh_from_db()
        self.assertEqual(served.fulfilment_status, 'served')
        self.assertEqual(served.fulfilment_revision, 0)

    def test_the_refusal_does_not_name_the_occupying_order(self):
        """A conflict explains itself with a reason code; it does not hand over
        another order's identity."""
        served = self._make_order(
            table=self.table1, fulfilment_status='served',
            order_status=OrderStatus_Served, served_at=timezone.now())
        newcomer = self._make_order(table=self.table1,
                                    order_status=OrderStatus_Pending)
        body = self._command(served, 'recall').json()
        self.assertNotIn(str(newcomer.id), str(body))
        self.assertEqual(body['data']['id'], str(served.id))

    def test_recall_onto_a_free_table_is_the_control(self):
        served = self._make_order(
            table=self.table2, fulfilment_status='served',
            order_status=OrderStatus_Served, served_at=timezone.now())
        self.assertEqual(self._command(served, 'recall').status_code, 200)
        self.assertEqual(self._occupants(self.table2), [served.id])

    def test_a_cancelled_occupant_does_not_block_a_recall(self):
        """CONTROL: the definition of "ongoing" is not weakened, and a cancelled
        order was never an occupant."""
        served = self._make_order(
            table=self.table3, fulfilment_status='served',
            order_status=OrderStatus_Served, served_at=timezone.now())
        self._make_order(table=self.table3, order_status=OrderStatus_Cancelled,
                         cancelled_at=timezone.now())
        self.assertEqual(self._command(served, 'recall').status_code, 200)


class PriorityContractTests(KitchenTestBase):
    """REGRESSION (D3). On `fd190dd` `bool(request.data.get('priority'))` made
    `'no'` and `'false'` True and `{'a': 1}` True, and an OMITTED value toggled —
    so a retried request undid itself."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def test_non_boolean_values_are_refused_never_coerced(self):
        order = self._make_order(priority=False)
        for value in ('no', 'false', 'true', 1, 0, [], ['x'], {'a': 1}, None, 1.0):
            response = self._set_priority(order, value)
            self.assertEqual(response.status_code, 400, msg=repr(value))
            self.assertEqual(response.json()['reason'], 'priority_invalid')
            order.refresh_from_db()
            self.assertFalse(order.priority, msg=repr(value))
            self.assertEqual(order.fulfilment_revision, 0)

    def test_an_omitted_value_is_refused_not_toggled(self):
        order = self._make_order(priority=False)
        response = self.client.put(
            _priority_url(order.pk), {'if_revision': 0}, format='json')
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertFalse(order.priority)

    def test_setting_the_same_value_is_a_no_write_unchanged_result(self):
        """It reports CURRENT state and claims nothing about history — so it
        moves neither the revision nor `time_last_updated`, which would be a
        trace someone could mistake for a receipt."""
        order = self._make_order(priority=True)
        before = Order.objects.get(pk=order.pk)
        response = self._set_priority(order, True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['outcome'], 'unchanged')
        after = Order.objects.get(pk=order.pk)
        self.assertEqual(after.fulfilment_revision, before.fulfilment_revision)
        self.assertEqual(after.time_last_updated, before.time_last_updated)

    def test_the_same_value_with_a_stale_revision_is_a_conflict(self):
        """Equality with the CURRENT revision is a no-write success; equality
        with a STALE one is a conflict, because the caller is acting on a view of
        the ticket that has moved."""
        order = self._make_order(priority=False)
        self._set_priority(order, True)                 # rev 1
        response = self._set_priority(order, True, if_revision=0)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['reason'], 'kitchen_precondition_stale')

    def test_a_real_change_reports_applied_and_bumps_the_revision(self):
        order = self._make_order(priority=False)
        response = self._set_priority(order, True)
        self.assertEqual(response.json()['outcome'], 'applied')
        self.assertEqual(response.json()['data']['priority'], True)
        self.assertEqual(response.json()['data']['fulfilment_revision'], 1)


class RequestContractTests(KitchenTestBase):
    """REGRESSION + NEW POLICY. A non-mapping body used to reach `.get()` and
    raise AttributeError -> 500."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def test_a_non_mapping_body_is_a_controlled_400(self):
        order = self._make_order()
        for url in (_fulfilment_url(order.pk), _priority_url(order.pk),
                    _cancel_url(order.pk)):
            response = self.client.put(url, [1, 2], format='json')
            self.assertEqual(response.status_code, 400, msg=url)
            self.assertEqual(response.json()['reason'], 'kitchen_request_invalid')

    def test_the_action_is_required_and_the_old_target_form_is_not_accepted(self):
        order = self._make_order(fulfilment_status='new')
        response = self.client.put(
            _fulfilment_url(order.pk),
            {'fulfilment_status': 'preparing'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], 'kitchen_action_required')
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'new')

    def test_an_unknown_or_non_string_action_is_refused(self):
        order = self._make_order()
        for action in ('banana', 'cancel', 'set_priority', 3, None, [], {}):
            response = self.client.put(
                _fulfilment_url(order.pk),
                {'action': action, 'if_revision': 0}, format='json')
            self.assertEqual(response.status_code, 400, msg=repr(action))

    def test_the_precondition_is_required_on_every_command(self):
        order = self._make_order(fulfilment_status='new')
        for url, body in (
            (_fulfilment_url(order.pk), {'action': 'advance'}),
            (_priority_url(order.pk), {'priority': True}),
            (_cancel_url(order.pk),
             {'cancellation_reason': CancellationReason_CustomerChangedMind}),
        ):
            response = self.client.put(url, body, format='json')
            self.assertEqual(response.status_code, 400, msg=url)
            self.assertEqual(response.json()['reason'],
                             'kitchen_precondition_required')
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_revision, 0)

    def test_a_malformed_precondition_is_refused_by_type(self):
        order = self._make_order(fulfilment_status='new')
        for value in (True, False, '0', '1', 1.0, 2.5, None, [], {}, -1,
                      kt.MAX_REVISION + 1):
            response = self.client.put(
                _fulfilment_url(order.pk),
                {'action': 'advance', 'if_revision': value}, format='json')
            self.assertEqual(response.status_code, 400, msg=repr(value))
            self.assertEqual(response.json()['reason'],
                             'kitchen_precondition_invalid', msg=repr(value))
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, 'new')

    def test_zero_is_a_valid_precondition(self):
        """The adoption baseline every pre-D05 row carries."""
        order = self._make_order(fulfilment_status='new')
        self.assertEqual(
            self.client.put(_fulfilment_url(order.pk),
                            {'action': 'advance', 'if_revision': 0},
                            format='json').status_code, 200)

    def test_an_unknown_or_malformed_order_is_one_non_disclosing_404(self):
        for pk in (uuid.uuid4(), 'not-a-uuid'):
            response = self.client.put(
                _fulfilment_url(pk),
                {'action': 'advance', 'if_revision': 0}, format='json')
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('data', response.json())

    def test_a_foreign_order_is_the_same_404_shaped_answer(self):
        """CONTROL for non-disclosure: an order at a restaurant the caller has
        no relationship with must not become readable through a conflict body."""
        other_owner = self._make_member('256900019001', None)
        from restaurants_app.models import Restaurant, Table, DiningArea
        other = Restaurant.objects.create(
            name='Other', location='Elsewhere', owner=other_owner, country='UG')
        area = DiningArea.objects.create(restaurant=other, name='M')
        table = Table.objects.create(restaurant=other, dining_area=area, number=9)
        foreign = Order.objects.create(
            restaurant=other, table=table, total_cost=0, discounted_cost=0,
            savings=0, actual_cost=0, order_status=OrderStatus_Pending,
            order_date=timezone.localdate())
        response = self.client.put(
            _fulfilment_url(foreign.pk),
            {'action': 'advance', 'if_revision': 0}, format='json')
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn('data', response.json())


class CurrentStateProjectionTests(KitchenTestBase):
    """NEW POLICY (D7). One projection shape from every command, so a client has
    one thing to reconcile against."""

    EXPECTED_KEYS = {
        'id', 'fulfilment_revision', 'order_status', 'fulfilment_status',
        'priority', 'served_at', 'cancelled_at', 'cancellation_reason',
    }

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def test_success_and_conflict_share_one_shape(self):
        order = self._make_order(fulfilment_status='new')
        applied = self._command(order, 'advance').json()
        self.assertEqual(set(applied['data']), self.EXPECTED_KEYS)

        conflict = self._command(order, 'advance', if_revision=0).json()
        self.assertEqual(set(conflict['data']), self.EXPECTED_KEYS)
        self.assertEqual(conflict['reason'], 'kitchen_precondition_stale')

    def test_the_projection_describes_the_state_that_was_written(self):
        order = self._make_order(fulfilment_status='ready')
        data = self._command(order, 'serve').json()['data']
        order.refresh_from_db()
        self.assertEqual(data['fulfilment_status'], 'served')
        self.assertEqual(data['order_status'], OrderStatus_Served)
        self.assertEqual(data['fulfilment_revision'], order.fulfilment_revision)
        self.assertEqual(data['served_at'], order.served_at.isoformat())

    def test_the_feeds_publish_the_fields_a_client_commands_with(self):
        order = self._make_order(fulfilment_status='new')
        self._command(order, 'advance')
        from orders_app.tests_kitchen import ACTIVE_URL
        body = self.client.get(
            ACTIVE_URL, {'restaurant': str(self.restaurant.id)}).json()
        self.assertEqual(body['kitchen_protocol'], kt.KITCHEN_PROTOCOL)
        row = next(t for t in body['data'] if t['id'] == str(order.id))
        self.assertEqual(row['fulfilment_revision'], 1)
        self.assertEqual(row['order_status'], OrderStatus_Pending)

    def test_the_completed_feed_publishes_them_too(self):
        order = self._make_order(fulfilment_status='ready')
        self._command(order, 'serve')
        body = self.client.get(
            COMPLETED_URL, {'restaurant': str(self.restaurant.id)}).json()
        self.assertEqual(body['kitchen_protocol'], kt.KITCHEN_PROTOCOL)
        row = next(t for t in body['data'] if t['id'] == str(order.id))
        self.assertEqual(row['order_status'], OrderStatus_Served)
        self.assertIn('fulfilment_revision', row)


class DinerContractIsUntouchedTests(KitchenTestBase):
    """D04/D02 PRESERVATION. A kitchen command must not move anything the diner
    agreed to. The STORED acceptance reference is what a replay compares against,
    and no kitchen command writes it."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def _accepted(self):
        order = self._make_order(order_status=OrderStatus_Initiated)
        from orders_app.controllers.manage_order import update_order_status
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref(order))
        self.assertEqual(result['status'], 200, result)
        order.refresh_from_db()
        return order, OrderAcceptance.objects.get(order=order)

    def test_the_stored_reference_and_moment_survive_every_command(self):
        order, evidence = self._accepted()
        before = (evidence.quote_ref, evidence.accepted_at)

        self._command(order, 'advance')
        self._command(order, 'advance')
        self._command(order, 'correct')
        self._command(order, 'advance')
        self._command(order, 'serve')
        self._command(order, 'recall')
        self._command(order, 'serve')

        evidence.refresh_from_db()
        self.assertEqual((evidence.quote_ref, evidence.accepted_at), before)
        self.assertEqual(OrderAcceptance.objects.filter(order=order).count(), 1)

    def test_the_d04_replay_still_returns_the_original_evidence(self):
        """A retry compares the supplied reference against the STORED
        `OrderAcceptance.quote_ref` — not against a digest recomputed from
        today's rows — so kitchen progress cannot break it."""
        order, evidence = self._accepted()
        original_ref = evidence.quote_ref

        self._command(order, 'advance')
        self._command(order, 'advance')
        self._command(order, 'serve')
        order.refresh_from_db()

        from orders_app.controllers.manage_order import update_order_status
        replay = update_order_status(
            order, OrderStatus_Pending, None, original_ref)
        self.assertEqual(replay['status'], 200)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(replay['checkout']['acceptance']['quote_ref'],
                         original_ref)

    def test_a_cancelled_but_accepted_order_still_replays_as_accepted(self):
        order, evidence = self._accepted()
        self.client.force_authenticate(user=self.manager_user)
        self.assertEqual(self._cancel_cmd(order).status_code, 200)
        order.refresh_from_db()

        from orders_app.controllers.manage_order import update_order_status
        replay = update_order_status(
            order, OrderStatus_Pending, None, evidence.quote_ref)
        self.assertEqual(replay['status'], 200)
        self.assertTrue(replay['idempotent'])

    def test_no_kitchen_command_writes_order_item_status_or_reprices(self):
        order, _ = self._accepted()
        from orders_app.models import OrderItem
        before = list(OrderItem.objects.filter(order=order).values(
            'status', 'quantity', 'unit_price', 'actual_cost',
            'item_name_snapshot', 'selected_modifiers'))
        money_before = (order.total_cost, order.discounted_cost,
                        order.savings, order.actual_cost,
                        order.pricing_version, order.payment_status)

        self._command(order, 'advance')
        self._command(order, 'advance')
        self._command(order, 'serve')

        after = list(OrderItem.objects.filter(order=order).values(
            'status', 'quantity', 'unit_price', 'actual_cost',
            'item_name_snapshot', 'selected_modifiers'))
        order.refresh_from_db()
        self.assertEqual(after, before)
        self.assertEqual(
            (order.total_cost, order.discounted_cost, order.savings,
             order.actual_cost, order.pricing_version, order.payment_status),
            money_before)

    def test_the_revision_is_not_in_the_quote_fingerprint(self):
        """The reference is what the DINER agreed to pay. A kitchen command
        moving it would change the answer to a question it was never asked."""
        order, _ = self._accepted()
        before = quote_ref(order)
        self._command(order, 'advance')
        order.refresh_from_db()
        self.assertEqual(quote_ref(order), before)


class WhatACommandCostsTests(KitchenTestBase):
    """PINNED QUERY BUDGETS, so the cost of the D05 boundary cannot drift
    unnoticed — and so the breakdown below stays honest.

    THE READS ARE UNCHANGED, which is the number that matters most: the active
    feed is POLLED every three seconds, and it still costs exactly what it did
    before (4 for a four-ticket board, 3 for an empty Completed feed), with no
    per-ticket growth.

    A MUTATION WENT FROM 4 QUERIES TO 10, and every one of the six is something
    the approved design requires:

      +1 / +1  SAVEPOINT and RELEASE SAVEPOINT — the decision and the write now
               share one transaction, which is the whole fix.
      +1       the Table row lock, acquired before the Order lock to match
               acceptance's existing `advisory -> Table -> Order` ordering.
      +1       the Order row lock and re-read: the caller's instance is a
               LOCATOR, and every fact is resolved after the blocking wait.
      +2       the authoritative permission re-check. The resolver holds no
               request-level cache — deliberately, since a cached answer would
               be a decision about a moment that has passed — so re-asking costs
               its two reads.

    The pre-lock permission gate is counted in the "before" figure too: it is the
    same check the old endpoint made, kept so an unauthorised caller cannot make
    a row lock wait. It is not free (2 of the 10), and it is worth it: without
    it any authenticated principal could force lock acquisition on any order row
    by UUID.

    Numbers are ENDPOINT cost: `_rev()` in the test helpers is a harness read and
    is excluded by issuing each request directly.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)
        self.rid = str(self.restaurant.id)

    def _put(self, url, body):
        return self.client.put(url, body, format='json')

    def test_the_active_feed_is_unchanged_and_flat(self):
        from orders_app.tests_kitchen import ACTIVE_URL
        for table in (self.table1, self.table2):
            self._make_order(table=table)
        with self.assertNumQueries(4):
            self._put and self.client.get(ACTIVE_URL, {'restaurant': self.rid})

        # Twice the tickets, the same cost — no per-ticket read crept in with
        # the two new projected fields.
        for table in (self.table3, self.table4):
            self._make_order(table=table)
        with self.assertNumQueries(4):
            self.client.get(ACTIVE_URL, {'restaurant': self.rid})

    def test_the_completed_feed_is_unchanged(self):
        with self.assertNumQueries(3):
            self.client.get(COMPLETED_URL, {'restaurant': self.rid})

    def test_a_fulfilment_command_costs_ten(self):
        order = self._make_order(fulfilment_status='new')
        with self.assertNumQueries(10):
            self._put(_fulfilment_url(order.pk),
                      {'action': 'advance', 'if_revision': 0})

    def test_serving_costs_the_same_as_advancing(self):
        order = self._make_order(fulfilment_status='ready')
        with self.assertNumQueries(10):
            self._put(_fulfilment_url(order.pk),
                      {'action': 'serve', 'if_revision': 0})

    def test_a_recall_costs_one_more_for_the_occupancy_check(self):
        order = self._make_order(
            fulfilment_status='served', order_status=OrderStatus_Served,
            served_at=timezone.now())
        with self.assertNumQueries(11):
            self._put(_fulfilment_url(order.pk),
                      {'action': 'recall', 'if_revision': 0})

    def test_a_no_change_priority_costs_one_less(self):
        """It writes nothing, so it does not pay for the UPDATE."""
        order = self._make_order(priority=True)
        with self.assertNumQueries(9):
            self._put(_priority_url(order.pk),
                      {'priority': True, 'if_revision': 0})

    def test_a_free_void_does_not_pay_for_the_manage_gate(self):
        """The escalation is consulted only once preparation has started, so
        cancelling a `new` ticket costs exactly what any other command does."""
        order = self._make_order(fulfilment_status='new')
        with self.assertNumQueries(10):
            self._put(_cancel_url(order.pk),
                      {'cancellation_reason': CancellationReason_CustomerChangedMind,
                       'if_revision': 0})

    def test_cancelling_a_preparing_ticket_pays_for_the_escalation(self):
        order = self._make_order(fulfilment_status='preparing')
        self.client.force_authenticate(user=self.manager_user)
        with self.assertNumQueries(11):
            self._put(_cancel_url(order.pk),
                      {'cancellation_reason': CancellationReason_CustomerChangedMind,
                       'if_revision': 0})

    def test_a_refusal_costs_no_more_than_a_success(self):
        """A conflict must not be the expensive path — it is the one a busy
        board produces most. It skips the UPDATE and pays for the rollback
        instead, so it lands on the same figure."""
        order = self._make_order(fulfilment_status='new')
        with self.assertNumQueries(10):
            self._put(_fulfilment_url(order.pk),
                      {'action': 'advance', 'if_revision': 99})

    def test_a_malformed_request_touches_the_database_only_to_authorise(self):
        """Body validation happens before any lookup, so a bad request is
        cheap — and it cannot be used to probe for order ids."""
        order = self._make_order()
        with self.assertNumQueries(0):
            self._put(_fulfilment_url(order.pk), {'action': 'advance'})
