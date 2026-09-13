"""
D05/K4 — the command BOUNDARY is the thing that validates, not the parser.

`kitchen_transition.execute` is documented as the authoritative boundary that
"self-guards a direct caller", and for the ACTION it does. For everything else it
does not: `KitchenCommand` is a bare frozen dataclass, so the revision's type and
range, the priority boolean and the cancellation vocabulary are enforced ONLY by
the three `parse_*` functions. Anything that builds a command another way — a
future internal caller, a management command, a retry helper — reaches `execute`
with none of it applied.

Three defects, each reproduced below on unmodified `4d1ca15`:

  * a command built directly may carry ANY `cancellation_reason`, which is then
    written to the row;
  * `_assert_revision` compares with `!=`, so `False` satisfies a precondition of
    0 and `1.0` satisfies one of 1 — a token whose whole purpose is to be exact,
    satisfied by Python's numeric tower;
  * `_apply_priority` has no active-only rule, so a SERVED ticket accepts a
    priority command. That is not merely meaningless: it BUMPS THE REVISION, so
    it invalidates the precondition a legitimate recall is holding, and the
    ten-minute window can expire while the operator reloads.

Labels follow `tests_kitchen_transition.py`: REGRESSION (reproduced, then
closed), CONTROL (already correct, must not change), NEW POLICY (introduced by
approval, no prior behaviour).
"""
from datetime import timedelta

from django.utils import timezone

from orders_app.models import Order
from orders_app.controllers.services import kitchen_transition as kt
from orders_app.tests_kitchen import KitchenTestBase
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    RESTAURANT_KITCHEN,
    CancellationReason_CustomerChangedMind,
    OrderStatus_Pending,
    OrderStatus_Served,
    RestaurantStatus_Live,
)


class DirectCommandConstructionTests(KitchenTestBase):
    """REGRESSION. Building a `KitchenCommand` proved only that Python accepted
    the keyword arguments. Every field rule lived in the parsers, so a caller
    that did not go through one carried none of them into the boundary."""

    def setUp(self):
        super().setUp()
        self.order = self._make_order(order_status=OrderStatus_Pending,
                                      fulfilment_status='new')

    def test_an_arbitrary_cancellation_reason_is_refused_at_construction(self):
        with self.assertRaises(kt.KitchenRefusal) as caught:
            kt.KitchenCommand(
                action=kt.ACTION_CANCEL, if_revision=0,
                cancellation_reason='whatever-the-caller-felt-like',
            )
        self.assertEqual(caught.exception.reason, 'cancellation_reason_invalid')
        self.assertEqual(caught.exception.status, 400)

    def test_an_arbitrary_cancellation_reason_never_reaches_the_row(self):
        """The consequence, stated as an outcome rather than as a type error."""
        try:
            command = kt.KitchenCommand(
                action=kt.ACTION_CANCEL, if_revision=0,
                cancellation_reason='whatever-the-caller-felt-like',
            )
        except kt.KitchenRefusal:
            command = None
        if command is not None:          # the defect: construction succeeded
            with self.assertRaises(kt.KitchenRefusal):
                kt.execute(self.order.pk, self.kitchen_user, command)

        self.order.refresh_from_db()
        self.assertIsNone(self.order.cancellation_reason)
        self.assertEqual(self.order.order_status, OrderStatus_Pending)
        self.assertEqual(self.order.fulfilment_revision, 0)

    def test_a_missing_cancellation_reason_is_refused(self):
        with self.assertRaises(kt.KitchenRefusal):
            kt.KitchenCommand(action=kt.ACTION_CANCEL, if_revision=0)

    def test_a_non_boolean_priority_is_refused_at_construction(self):
        for value in ('yes', 1, 0, None, [], {}):
            with self.assertRaises(kt.KitchenRefusal, msg=repr(value)):
                kt.KitchenCommand(
                    action=kt.ACTION_SET_PRIORITY, if_revision=0, priority=value,
                )

    def test_a_cross_action_field_is_unrepresentable(self):
        """A fulfilment command carrying cancellation or priority data is not a
        command this contract defines, so it cannot be built at all."""
        with self.assertRaises(kt.KitchenRefusal):
            kt.KitchenCommand(
                action=kt.ACTION_ADVANCE, if_revision=0,
                cancellation_reason=CancellationReason_CustomerChangedMind,
            )
        with self.assertRaises(kt.KitchenRefusal):
            kt.KitchenCommand(
                action=kt.ACTION_ADVANCE, if_revision=0, priority=True,
            )
        with self.assertRaises(kt.KitchenRefusal):
            kt.KitchenCommand(
                action=kt.ACTION_CANCEL, if_revision=0,
                cancellation_reason=CancellationReason_CustomerChangedMind,
                priority=True,
            )

    def test_an_unknown_action_is_refused_at_construction(self):
        for action in ('invalid', '', None, 5, kt.ACTION_ADVANCE.upper()):
            with self.assertRaises(kt.KitchenRefusal, msg=repr(action)):
                kt.KitchenCommand(action=action, if_revision=0)

    def test_a_malformed_revision_is_refused_at_construction(self):
        for value in (True, False, 1.0, '1', None, -1, kt.MAX_REVISION + 1):
            with self.assertRaises(kt.KitchenRefusal, msg=repr(value)):
                kt.KitchenCommand(action=kt.ACTION_ADVANCE, if_revision=value)

    def test_a_well_formed_command_still_builds(self):
        """CONTROL. The rules refuse malformed commands, not ordinary ones."""
        kt.KitchenCommand(action=kt.ACTION_ADVANCE, if_revision=0)
        kt.KitchenCommand(action=kt.ACTION_SERVE, if_revision=kt.MAX_REVISION - 1)
        kt.KitchenCommand(action=kt.ACTION_SET_PRIORITY, if_revision=0,
                          priority=False)
        kt.KitchenCommand(
            action=kt.ACTION_CANCEL, if_revision=0,
            cancellation_reason=CancellationReason_CustomerChangedMind)


class RevisionIdentityTests(KitchenTestBase):
    """REGRESSION. `_assert_revision` compared with `!=` against a value nothing
    had type-checked once the parsers were bypassed, so Python's numeric tower
    satisfied the precondition: `False == 0` and `1.0 == 1` are both True.

    A precondition a coercion table can satisfy is not a precondition — the same
    reasoning `_parse_revision` already states about numeric strings."""

    def setUp(self):
        super().setUp()
        self.order = self._make_order(order_status=OrderStatus_Pending,
                                      fulfilment_status='new')

    def _execute(self, **kwargs):
        return kt.execute(self.order.pk, self.kitchen_user,
                          kt.KitchenCommand(**kwargs))

    def test_false_does_not_satisfy_a_precondition_of_zero(self):
        self.assertEqual(self.order.fulfilment_revision, 0)
        with self.assertRaises(kt.KitchenRefusal):
            self._execute(action=kt.ACTION_ADVANCE, if_revision=False)
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfilment_status, 'new')
        self.assertEqual(self.order.fulfilment_revision, 0)

    def test_a_float_does_not_satisfy_an_integer_precondition(self):
        kt.execute(self.order.pk, self.kitchen_user,
                   kt.KitchenCommand(action=kt.ACTION_ADVANCE, if_revision=0))
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfilment_revision, 1)

        with self.assertRaises(kt.KitchenRefusal):
            self._execute(action=kt.ACTION_ADVANCE, if_revision=1.0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfilment_status, 'preparing')
        self.assertEqual(self.order.fulfilment_revision, 1)

    def test_the_exact_integer_still_applies(self):
        """CONTROL. Strictness must not cost the ordinary case."""
        result = self._execute(action=kt.ACTION_ADVANCE, if_revision=0)
        self.assertEqual(result['outcome'], 'applied')
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfilment_status, 'preparing')
        self.assertEqual(self.order.fulfilment_revision, 1)


class PriorityAppliesToWorkInProgressTests(KitchenTestBase):
    """REGRESSION. A SERVED ticket accepted a priority command and BUMPED THE
    REVISION for it. Priority is a statement about what the kitchen should cook
    next, so on a completed ticket it means nothing — and the revision bump is a
    real harm, because it invalidates the precondition a recall is holding while
    the ten-minute window runs down."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)
        self.order = self._make_order(
            order_status=OrderStatus_Served, fulfilment_status='served',
            served_at=timezone.now() - timedelta(minutes=1),
        )

    def test_priority_is_refused_on_a_served_ticket(self):
        response = self._set_priority(self.order, True)
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()['reason'], 'illegal_transition')
        self.order.refresh_from_db()
        self.assertFalse(self.order.priority)
        self.assertEqual(self.order.fulfilment_revision, 0)

    def test_a_refused_priority_leaves_a_recall_possible(self):
        """THE HARM, stated end to end: the operator loaded the ticket at
        revision 0 and the stray priority command must not spend it."""
        held_revision = self._rev(self.order)
        self._set_priority(self.order, True)

        recall = self._command(self.order, 'recall', if_revision=held_revision)
        self.assertEqual(recall.status_code, 200, recall.json())
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfilment_status, 'ready')

    def test_priority_still_applies_to_an_active_ticket(self):
        """CONTROL. The three states a kitchen is actually working."""
        for status in ('new', 'preparing', 'ready'):
            order = self._make_order(order_status=OrderStatus_Pending,
                                     fulfilment_status=status)
            response = self._set_priority(order, True)
            self.assertEqual(response.status_code, 200, msg=status)
            order.refresh_from_db()
            self.assertTrue(order.priority, msg=status)

    def test_the_service_refuses_it_directly_too(self):
        with self.assertRaises(kt.KitchenRefusal) as caught:
            kt.execute(
                self.order.pk, self.kitchen_user,
                kt.KitchenCommand(action=kt.ACTION_SET_PRIORITY,
                                  if_revision=0, priority=True),
            )
        self.assertEqual(caught.exception.reason, 'illegal_transition')
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfilment_revision, 0)


def _state_url(pk):
    return f'/api/v1/kitchen/orders/{pk}/state/'


class OrderStateReadTests(KitchenTestBase):
    """NEW POLICY. ``GET kitchen/orders/<pk>/state/`` did not exist, so nothing
    here is a reproduction — an absent route is not a red baseline.

    WHY IT EXISTS. A kitchen command whose reply is lost leaves the client unable
    to say whether the server acted, and the commands whose outcome matters most
    are the ones that REMOVE the order from both feeds. The two tests below that
    assert an order is in NEITHER feed are the justification, not decoration: if
    a feed could answer, this route should not exist."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.kitchen_user)

    def _feeds(self):
        active = self.client.get(
            f'/api/v1/kitchen/orders/active/?restaurant={self.restaurant.pk}')
        completed = self.client.get(
            f'/api/v1/kitchen/orders/completed/?restaurant={self.restaurant.pk}')
        ids = [row['id'] for row in active.json()['data']]
        ids += [row['id'] for row in completed.json()['data']]
        return ids

    def test_it_answers_for_an_active_ticket(self):
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='preparing')
        response = self.client.get(_state_url(order.pk))
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertEqual(body['kitchen_protocol'], kt.KITCHEN_PROTOCOL)
        self.assertEqual(body['data']['id'], str(order.pk))
        self.assertEqual(body['data']['fulfilment_status'], 'preparing')
        self.assertEqual(body['data']['fulfilment_revision'], 0)

    def test_it_answers_for_a_cancelled_order_which_is_in_NEITHER_feed(self):
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        self.assertEqual(self._cancel_cmd(order).status_code, 200)
        order.refresh_from_db()

        self.assertNotIn(str(order.pk), self._feeds())

        response = self.client.get(_state_url(order.pk))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()['data']['order_status'],
                         'cancelled')
        self.assertIsNotNone(response.json()['data']['cancelled_at'])

    def test_it_answers_for_a_served_order_past_the_completed_window(self):
        order = self._make_order(
            order_status=OrderStatus_Served, fulfilment_status='served',
            served_at=timezone.now() - timedelta(hours=48),
        )
        self.assertNotIn(str(order.pk), self._feeds())

        response = self.client.get(_state_url(order.pk))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()['data']['fulfilment_status'], 'served')

    def test_it_answers_for_a_draft(self):
        """ELIGIBILITY IS A QUESTION ABOUT COMMANDS, not about observation. A
        draft is refused by `execute` and still described here — otherwise the
        client could not tell a draft from an order it had never heard of."""
        from dinify_backend.configss.string_definitions import OrderStatus_Initiated
        order = self._make_order(order_status=OrderStatus_Initiated,
                                 fulfilment_status='new')
        response = self.client.get(_state_url(order.pk))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()['data']['order_status'], 'initiated')

    def test_the_projection_is_the_one_a_command_answers_with(self):
        """ONE shape to reconcile against, whichever way the client obtained it."""
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        command = self._command(order, 'advance')
        self.assertEqual(command.status_code, 200, command.json())

        observed = self.client.get(_state_url(order.pk))
        self.assertEqual(observed.json()['data'], command.json()['data'])

    def test_it_writes_nothing_and_takes_no_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='ready')
        before = Order.objects.values(
            'fulfilment_revision', 'order_status', 'fulfilment_status',
            'priority', 'served_at', 'cancelled_at', 'cancellation_reason',
            'time_last_updated',
        ).get(pk=order.pk)

        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(_state_url(order.pk)).status_code, 200)

        statements = [q['sql'].upper() for q in captured.captured_queries]
        self.assertFalse(
            [q for q in statements if 'FOR UPDATE' in q],
            'an observation must not queue behind live service',
        )
        self.assertFalse(
            [q for q in statements
             if q.lstrip().startswith(('UPDATE', 'INSERT', 'DELETE'))],
            'an observation writes nothing',
        )

        after = Order.objects.values(*before.keys()).get(pk=order.pk)
        self.assertEqual(before, after)

    def test_an_unknown_or_malformed_id_is_one_non_disclosing_404(self):
        import uuid
        for pk in (str(uuid.uuid4()), 'not-a-uuid', '', '12345'):
            response = self.client.get(_state_url(pk))
            self.assertEqual(response.status_code, 404, msg=repr(pk))

    def test_a_soft_deleted_order_is_a_404(self):
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        Order.objects.filter(pk=order.pk).update(deleted=True)
        self.assertEqual(self.client.get(_state_url(order.pk)).status_code, 404)

    def test_a_caller_outside_this_kitchen_cannot_tell_the_order_exists(self):
        """REGRESSION (Codex P2 on PR #320, valid — and this file's own first
        cut asserted the defect).

        The first version of this read MIRRORED `execute`, answering 403 when the
        module gate refused, and a test here pinned that. But 403-for-foreign
        beside 404-for-unknown is an EXISTENCE ORACLE over the whole orders
        table, reachable by any authenticated kitchen user with a free GET — and
        it contradicts two things already written down: the repository's rule
        that a tenant-scoped DETAIL READ answers 404 "so existence is not
        confirmed", and `OrderNotFound`'s own docstring, which says it covers an
        order "out of the caller's scope" precisely so this route cannot be used
        to learn that some UUID is real somewhere.

        A FOREIGN ID AND AN UNKNOWN ONE MUST BE INDISTINGUISHABLE — status AND
        body, because a differing `reason` key would leak exactly as loudly."""
        import uuid as _uuid
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')

        # Kitchen staff at a DIFFERENT restaurant: authorised somewhere, not here.
        other_restaurant = Restaurant.objects.create(
            name='Other Kitchen', location='Elsewhere',
            owner=self.admin_user, status=RestaurantStatus_Live,
        )
        stranger = self._make_member('256900000101', None)
        RestaurantEmployee.objects.create(
            user=stranger, restaurant=other_restaurant,
            roles=[RESTAURANT_KITCHEN],
        )
        self.client.force_authenticate(user=stranger)

        foreign = self.client.get(_state_url(order.pk))
        unknown = self.client.get(_state_url(str(_uuid.uuid4())))

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), unknown.json())

    def test_a_caller_with_no_membership_anywhere_is_answered_the_same_way(self):
        import uuid as _uuid
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        self.client.force_authenticate(user=self.outsider_user)

        foreign = self.client.get(_state_url(order.pk))
        unknown = self.client.get(_state_url(str(_uuid.uuid4())))

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), unknown.json())

    def test_the_kitchen_it_belongs_to_still_reads_it(self):
        """CONTROL. Non-disclosure must not become non-function."""
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        self.client.force_authenticate(user=self.kitchen_user)
        response = self.client.get(_state_url(order.pk))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()['data']['id'], str(order.pk))

    def test_an_anonymous_caller_is_refused(self):
        order = self._make_order(order_status=OrderStatus_Pending,
                                 fulfilment_status='new')
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(_state_url(order.pk)).status_code, (401, 403))
