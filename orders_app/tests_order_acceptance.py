"""
D04/C — the durable acceptance fact, and recovering an order without its id.

THE TWO FAILURES THIS CLOSES, both of them a checkout reporting FAILURE after
SUCCESS — the worst answer a checkout can give, because the diner believes
nothing was ordered while the kitchen is already cooking it:

  1. A RETRY AFTER A LOST RESPONSE was told `This order cannot be submitted.`
     The order had been accepted; the re-check that produced that answer looks
     at `order_status`, which by then says `pending` (or `preparing`, or
     `served`, or `cancelled`) — none of which is a statement about whether
     the diner's submission landed.

  2. A CLIENT THAT LOST THE RESPONSE ENTIRELY held no order id, because that
     is exactly what it lost. It could mint a fresh `client_order_id` and
     re-initiate — which D04/B answers correctly — but it had no way to LOOK
     UP what the key it already holds resolved to, so it could not tell an
     accepted order from an abandoned draft without placing another one.

WHAT IS NOT CLAIMED. Not exactly-once network delivery: a response can still
be lost, and the retry is what recovers it. Not payment idempotency — nothing
here touches money. And `accepted` is not a claim that the food arrived.
"""
import uuid
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from dinify_backend.configss.string_definitions import (
    OrderStatus_Cancelled, OrderStatus_Pending, OrderStatus_Preparing,
    OrderStatus_Served, RESTAURANT_OWNER, RestaurantStatus_Live,
    RestaurantStatus_Suspended,
)
from orders_app.controllers.manage_order import (
    REASON_ALREADY_ACCEPTED, REASON_QUOTE_REQUIRED, REASON_QUOTE_STALE,
    update_order_status,
)
from orders_app.controllers.services.checkout_protocol import (
    CHECKOUT_PROTOCOL, CHECKOUT_PROTOCOL_RECOVERABLE,
)
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderAcceptance
from orders_app.serializers import SerializerPublicOrderDetails
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.controllers.handle_diner_journey import (
    handle_show_order_details,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


class AcceptanceFixture(TestCase):
    """One restaurant, two orderable tables, one dish."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Acc', last_name='Owner', email='acc_owner@test.com',
            phone_number='256700000971', username='256700000971',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Acceptance R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant,
        )
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        self.table_b = Table.objects.create(
            number=2, restaurant=self.restaurant, dining_area=self.area,
            enabled=True, is_active=True, qr_mode='order_pay', has_qr=True,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price=Decimal('10000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )

    # -- shorthand ---------------------------------------------------------

    def _draft(self, table=None, key=None):
        result = _create_order(
            restaurant=self.restaurant, table=table or self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            client_order_id=key,
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def _submit(self, order, ref=None):
        return update_order_status(
            order, OrderStatus_Pending, None,
            quote_ref=ref if ref is not None else quote_ref(order),
        )

    def _read(self, table=None, **params):
        query = {k: v for k, v in params.items() if v is not None}
        request = APIRequestFactory().get(
            '/api/v1/orders/journey/', query,
            HTTP_X_DINER_SESSION=issue_table_session(table or self.table),
        )
        return handle_show_order_details(request)


# ---------------------------------------------------------------------------
# A. THE EVIDENCE
# ---------------------------------------------------------------------------

class AcceptanceEvidenceIsWrittenWithTheTransitionTests(AcceptanceFixture):

    def test_a_successful_submit_records_when_and_against_which_quote(self):
        order = self._draft()
        ref = quote_ref(order)
        before = timezone.now()

        result = self._submit(order, ref)

        self.assertEqual(result.get('status'), 200, result)
        self.assertFalse(result['idempotent'])
        evidence = OrderAcceptance.objects.get(order=order)
        self.assertEqual(evidence.quote_ref, ref)
        self.assertGreaterEqual(evidence.accepted_at, before)
        self.assertLessEqual(evidence.accepted_at, timezone.now())

    def test_a_draft_carries_no_evidence(self):
        order = self._draft()
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())

    def test_a_refused_submit_records_nothing(self):
        """Every acceptance invariant must leave the order exactly as it was —
        including, now, with no evidence that it was accepted."""
        order = self._draft()
        refused = self._submit(order, ref='not-the-saved-quote')
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(refused.get('reason'), REASON_QUOTE_STALE)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_the_evidence_and_the_transition_commit_together(self):
        """Neither half may exist without the other: an accepted order with no
        record reports a retry as a failure, and a record with no transition
        tells a client an order was placed the kitchen never saw."""
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.assertTrue(OrderAcceptance.objects.filter(order=order).exists())

        self.assertEqual(
            Order.objects.filter(order_status=OrderStatus_Pending).count(),
            OrderAcceptance.objects.count(),
        )


class TheEvidenceSurvivesOrdinaryOrderSavesTests(AcceptanceFixture):
    """WHY IT IS A ROW AND NOT TWO COLUMNS ON `Order`.

    Django's `save()` writes every field from the in-memory instance, so a
    caller holding an instance loaded BEFORE acceptance writes the
    pre-acceptance values back — silently, with no error, leaving an order
    that reads as never accepted. No production path does that today; the
    point is that a separate row makes it IMPOSSIBLE rather than
    currently-unreached.

    THESE TESTS FAIL AGAINST A TWO-COLUMN DESIGN, which is what makes the
    choice load-bearing rather than aesthetic.
    """

    def test_the_hazard_is_real_a_stale_save_does_clobber_a_column(self):
        """THE PROOF THAT THE TWO TESTS BELOW ARE NOT THEORETICAL.

        Demonstrated on an ordinary column so the mechanism is visible: a
        stale instance saved after the transition writes `order_status` back
        to `initiated`, silently, with no error. Anything stored ON `Order`
        is exposed to exactly that, which is why the acceptance evidence is
        not stored there.
        """
        order = self._draft()
        stale = Order.objects.get(pk=order.pk)
        self.assertEqual(self._submit(order).get('status'), 200)
        self.assertEqual(
            Order.objects.get(pk=order.pk).order_status, OrderStatus_Pending)

        stale.save()

        self.assertEqual(
            Order.objects.get(pk=order.pk).order_status,
            'initiated',
            'a stale save no longer clobbers — re-examine whether the '
            'evidence still needs its own row',
        )

    def test_a_deliberately_stale_instance_cannot_erase_it(self):
        order = self._draft()
        stale = Order.objects.get(pk=order.pk)      # loaded BEFORE acceptance

        self.assertEqual(self._submit(order).get('status'), 200)
        evidence = OrderAcceptance.objects.get(order=order)

        stale.save()                                # the clobber

        refreshed = OrderAcceptance.objects.get(order=order)
        self.assertEqual(refreshed.accepted_at, evidence.accepted_at)
        self.assertEqual(refreshed.quote_ref, evidence.quote_ref)

    def test_the_kitchen_advancing_the_order_does_not_move_it(self):
        """`order_status` is precisely what the evidence must not be."""
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)
        evidence = OrderAcceptance.objects.get(order=order)

        for state in (OrderStatus_Preparing, OrderStatus_Served):
            order.refresh_from_db()
            self.assertEqual(
                update_order_status(order, state, self.owner).get('status'),
                200,
            )

        refreshed = OrderAcceptance.objects.get(order=order)
        self.assertEqual(refreshed.accepted_at, evidence.accepted_at)
        self.assertEqual(refreshed.quote_ref, evidence.quote_ref)

    def test_a_cancellation_does_not_erase_it(self):
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)
        evidence = OrderAcceptance.objects.get(order=order)

        order.refresh_from_db()
        update_order_status(order, OrderStatus_Cancelled, self.owner)

        self.assertEqual(
            OrderAcceptance.objects.get(order=order).accepted_at,
            evidence.accepted_at,
        )


# ---------------------------------------------------------------------------
# B. THE REPLAY OUTCOME MATRIX
# ---------------------------------------------------------------------------

class TheReplayOutcomeMatrixTests(AcceptanceFixture):
    """What a second submit gets, in every state the order can be in."""

    def test_same_quote_is_an_idempotent_success(self):
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)

        replay = self._submit(order, ref)
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(OrderAcceptance.objects.filter(order=order).count(), 1)

    def test_a_different_quote_is_a_conflict(self):
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)

        conflict = self._submit(order, ref='a-different-quote')
        self.assertEqual(conflict.get('status'), 409, conflict)
        self.assertEqual(conflict.get('reason'), REASON_ALREADY_ACCEPTED)
        self.assertEqual(OrderAcceptance.objects.filter(order=order).count(), 1)

    def test_an_absent_quote_is_a_conflict_not_a_false_failure(self):
        """It cannot be PROVEN the same acceptance, so it is not replayed —
        but it is still not reported as a failed submission, which it was."""
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)

        conflict = update_order_status(order, OrderStatus_Pending, None)
        self.assertEqual(conflict.get('status'), 409, conflict)
        self.assertEqual(conflict.get('reason'), REASON_ALREADY_ACCEPTED)

    def test_a_replay_after_the_kitchen_has_started_still_succeeds(self):
        """The case the old `initiated` re-check got most wrong: by the time a
        slow retry arrives the kitchen may already have advanced the order."""
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Preparing, self.owner)

        order.refresh_from_db()
        replay = self._submit(order, ref)
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay['idempotent'])
        # and the kitchen's progress was not rewound
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Preparing)

    def test_a_replay_after_cancellation_reports_the_acceptance_truthfully(self):
        """The submission DID land; the cancellation is a later, separate fact
        the client reads off the order. Answering "your submission failed"
        would be false about the only thing this route is asked."""
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Cancelled, self.owner)

        order.refresh_from_db()
        replay = self._submit(order, ref)
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay['idempotent'])
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Cancelled)

    def test_a_replay_survives_the_restaurant_being_suspended(self):
        """THE LIFECYCLE GATE MUST NOT REPORT A COMPLETED ACCEPTANCE AS FAILED.

        Accepted while `live`, response lost, restaurant suspended, diner
        retries. `admit()` refuses new submissions at a suspended restaurant
        and rightly so — but this submission is not new, it already happened,
        and answering the retry with the lifecycle 400 is the exact
        failure-after-success this whole change exists to remove. The
        suspension is also not something the diner did or can see.

        THE SAME SPLIT `_create_order` ALREADY MAKES: the advisory lock is
        taken where the lock ordering requires, and the verdict is APPLIED
        only once the request is known to be new work. Found by the Codex
        review of PR #317, and valid — the reasoning was written out in
        `_create_order`'s step 1d and simply not carried across.
        """
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)

        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Suspended)

        order.refresh_from_db()
        replay = self._submit(order, ref)
        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(OrderAcceptance.objects.filter(order=order).count(), 1)

    def test_a_conflicting_retry_at_a_suspended_restaurant_still_conflicts(self):
        """And the OTHER outcome the gate was swallowing: a different quote
        against an already-accepted order is a conflict, not a lifecycle
        refusal. Both answers describe the order; neither describes the
        restaurant's trading state."""
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)

        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Suspended)

        order.refresh_from_db()
        conflict = self._submit(order, ref='a-different-quote')
        self.assertEqual(conflict.get('status'), 409, conflict)
        self.assertEqual(conflict.get('reason'), REASON_ALREADY_ACCEPTED)

    def test_a_FIRST_submission_is_still_refused_when_suspended(self):
        """The negative control, and the reason the gate stays: deferring WHEN
        the verdict applies must not stop it applying. A draft that was never
        accepted cannot reach the kitchen at a suspended restaurant."""
        order = self._draft()
        ref = quote_ref(order)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Suspended)

        refused = self._submit(order, ref)
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_an_order_with_no_evidence_keeps_the_old_refusal(self):
        """The negative control, and the pre-D04 case: nothing recorded that
        acceptance, so nothing may be claimed about it."""
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        OrderAcceptance.objects.filter(order=order).delete()

        order.refresh_from_db()
        second = self._submit(order, ref)
        self.assertEqual(second.get('status'), 400, second)
        self.assertEqual(second['message'], 'This order cannot be submitted.')

    def test_a_first_submission_still_has_to_name_its_quote(self):
        """The acceptance bar is unchanged: the replay check only ever fires
        for an order that ALREADY carries evidence."""
        order = self._draft()
        refused = update_order_status(order, OrderStatus_Pending, None)
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(refused.get('reason'), REASON_QUOTE_REQUIRED)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())

    def test_a_replay_claims_no_second_table(self):
        """A replay performs no transition, so it takes nothing the first
        acceptance did not already take."""
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        before = Order.objects.filter(table=self.table).count()

        self._submit(order, ref)

        self.assertEqual(Order.objects.filter(table=self.table).count(), before)


# ---------------------------------------------------------------------------
# C. THE RECOVERY READ
# ---------------------------------------------------------------------------

class RecoveringAnOrderByItsIntentKeyTests(AcceptanceFixture):
    """`orders/journey/order-details/?intent=<client_order_id>`.

    A client whose response was lost holds no order id — that is what it
    lost — but it does hold the key it minted before sending.
    """

    def test_the_key_resolves_to_the_order_it_created(self):
        key = uuid.uuid4()
        order = self._draft(key=key)
        response = self._read(intent=str(key))
        self.assertEqual(response.get('status'), 200, response)
        self.assertEqual(str(response['data']['id']), str(order.id))

    def test_it_is_the_same_projection_as_the_order_id_form(self):
        """Byte-identical, because it IS the same read — the alternative
        selector exists so recovery does not define a second contract."""
        key = uuid.uuid4()
        order = self._draft(key=key)
        self.assertEqual(self._submit(order).get('status'), 200)

        by_id = self._read(order=str(order.id))
        by_intent = self._read(intent=str(key))
        self.assertEqual(by_intent, by_id)

    def test_a_key_from_another_table_resolves_to_nothing(self):
        """Holding a key is not authority. The lookup is scoped to the
        session's restaurant AND table exactly as the order-id form is."""
        key = uuid.uuid4()
        self._draft(table=self.table_b, key=key)
        response = self._read(table=self.table, intent=str(key))
        self.assertEqual(response.get('status'), 404, response)

    def test_an_unknown_key_is_a_non_disclosing_404(self):
        self.assertEqual(
            self._read(intent=str(uuid.uuid4())).get('status'), 404)

    def test_a_malformed_key_is_the_same_404_never_a_500(self):
        """It is validated by the SAME rule the write path uses, so it never
        reaches a `UUIDField` filter — where an integer would be silently
        coerced into a fabricated key."""
        for malformed in ('not-a-uuid', '', '5', 'null', '../etc'):
            self.assertEqual(
                self._read(intent=malformed).get('status'), 404, malformed)

    def test_naming_both_selectors_is_refused(self):
        """A request naming two identifiers has not said which it means, and
        silently preferring one would answer a question nobody asked."""
        key = uuid.uuid4()
        order = self._draft(key=key)
        response = self._read(order=str(order.id), intent=str(key))
        self.assertEqual(response.get('status'), 400, response)

    def test_naming_neither_keeps_the_existing_refusal(self):
        self.assertEqual(self._read().get('status'), 400)

    def test_the_read_still_requires_a_diner_session(self):
        """The recovery selector widened no authority: no session, no read."""
        request = APIRequestFactory().get(
            '/api/v1/orders/journey/', {'intent': str(uuid.uuid4())})
        self.assertNotEqual(handle_show_order_details(request).get('status'),
                            200)

    def test_a_keyless_draft_cannot_be_recovered_and_says_so_by_404(self):
        """Keyless callers keep their explicitly weaker guarantees: there is
        no key to resolve, so there is nothing to recover."""
        self._draft()
        self.assertEqual(
            self._read(intent=str(uuid.uuid4())).get('status'), 404)


class TheReadAnswersDidMySubmissionLandTests(AcceptanceFixture):

    def _payload(self, order):
        return SerializerPublicOrderDetails(order).data

    def test_a_draft_reads_not_accepted(self):
        payload = self._payload(self._draft())
        self.assertFalse(payload['accepted'])
        self.assertIsNone(payload['accepted_at'])

    def test_an_accepted_order_reads_accepted_with_its_moment(self):
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)
        payload = self._payload(Order.objects.get(pk=order.pk))
        self.assertTrue(payload['accepted'])
        self.assertEqual(
            payload['accepted_at'],
            OrderAcceptance.objects.get(order=order).accepted_at,
        )

    def test_a_cancelled_but_accepted_order_still_reads_accepted(self):
        """Two independent facts in one response: the submission landed, and
        the order was later cancelled. Collapsing them would tell a diner
        their order never went through."""
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Cancelled, self.owner)

        payload = self._payload(Order.objects.get(pk=order.pk))
        self.assertTrue(payload['accepted'])
        self.assertEqual(payload['order_status'], OrderStatus_Cancelled)

    def test_accepted_does_not_track_the_kitchen(self):
        order = self._draft()
        self.assertEqual(self._submit(order).get('status'), 200)
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Served, self.owner)

        payload = self._payload(Order.objects.get(pk=order.pk))
        self.assertTrue(payload['accepted'])
        self.assertEqual(payload['order_status'], OrderStatus_Served)

    def test_the_read_states_the_capability_level(self):
        """A client deciding whether a retry is safe needs the answer from
        whichever response it actually has."""
        payload = self._payload(self._draft())
        self.assertEqual(payload['checkout_protocol'], CHECKOUT_PROTOCOL)
        self.assertGreaterEqual(payload['checkout_protocol'],
                                CHECKOUT_PROTOCOL_RECOVERABLE)

    def test_the_recovery_read_answers_the_whole_question_in_one_call(self):
        """The end-to-end point of C: key in, and out comes "yes it landed,
        here is when, here is the server's own itemised quote and payable" —
        with no order id ever having been held by the client."""
        key = uuid.uuid4()
        order = self._draft(key=key)
        self.assertEqual(self._submit(order).get('status'), 200)

        recovered = self._read(intent=str(key))
        self.assertEqual(recovered.get('status'), 200, recovered)
        data = recovered['data']
        self.assertTrue(data['accepted'])
        self.assertIsNotNone(data['accepted_at'])
        self.assertEqual(str(data['id']), str(order.id))
        self.assertEqual(data['quote_total'], '10000.00')
        self.assertTrue(data['quote_complete'])
        self.assertEqual(len(data['quote']), 1)


class WhatTheEvidenceCostsTests(AcceptanceFixture):
    """Exact counts, because recovery that is expensive is recovery a client
    will not use — and because an upper bound would not notice a per-line
    read creeping into the replay."""

    def _measure(self, fn):
        with CaptureQueriesContext(connection) as captured:
            result = fn()
        return len(captured.captured_queries), result

    def test_a_first_submission_costs_two_more_than_before(self):
        """One SELECT to ask whether evidence exists, one INSERT to write it.
        Nothing per line, and no second read of anything already in hand."""
        self._submit(self._draft())                   # warm
        order = self._draft(table=self.table_b)
        # the reference is computed OUTSIDE the measurement, as a real client
        # sends one it already holds — otherwise the helper's own
        # `quote_ref()` read lands inside the count and describes the test
        # rather than the path.
        ref = quote_ref(order)
        count, result = self._measure(lambda: self._submit(order, ref))
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(count, 12)

    def test_a_replay_costs_less_than_a_submission(self):
        """It returns at the evidence read: no occupancy query, no quote
        reconciliation over the lines, no UPDATE and no INSERT."""
        self._submit(self._draft())                   # warm
        order = self._draft(table=self.table_b)
        ref = quote_ref(order)
        self._submit(order, ref)
        count, result = self._measure(lambda: self._submit(order, ref))
        self.assertTrue(result['idempotent'], result)
        self.assertEqual(count, 7)

    def test_a_replay_is_still_decided_under_the_locks(self):
        """It is CHEAPER, not unserialised. The evidence is read after the
        admission advisory lock, the table row lock and the order re-read —
        the same ordering an acceptance takes — so two concurrent submissions
        cannot both conclude there is no evidence. Reading it before the
        locks would be faster and wrong.
        """
        self._submit(self._draft())                   # warm
        order = self._draft(table=self.table_b)
        ref = quote_ref(order)
        self._submit(order, ref)
        with CaptureQueriesContext(connection) as captured:
            self._submit(order, ref)
        statements = [q['sql'] for q in captured.captured_queries]
        lock_at = next(i for i, sql in enumerate(statements)
                       if 'pg_advisory_xact_lock_shared' in sql)
        table_at = next(i for i, sql in enumerate(statements)
                        if 'FROM "tables"' in sql)
        evidence_at = next(i for i, sql in enumerate(statements)
                           if 'order_acceptances' in sql)
        self.assertLess(lock_at, table_at)
        self.assertLess(table_at, evidence_at)
