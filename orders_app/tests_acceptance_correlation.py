"""
D04 completion — the shared, authorized acceptance projection.

WHAT D04/C LEFT OPEN. It made the acceptance fact DURABLE (`OrderAcceptance`,
written with the transition and never moved) and RESOLVABLE by intent key. What
it did not do is let a client PROVE that the answer it got belongs to the
checkout it issued, or tell it apart from the one case that looks identical and
is not:

  1. `accepted` IS A BOOLEAN OVER "IS THERE A ROW". Its own docstring concedes
     the conflation — "FALSE MEANS 'NO EVIDENCE', which covers a genuine draft
     and an order accepted BEFORE D04 alike". Those two are opposite instructions
     to a recovering client. A draft must be reviewed and may still be accepted;
     a pre-D04 accepted order is ALREADY IN THE KITCHEN and must never be
     re-accepted. A client that cannot separate them either re-accepts a live
     order or abandons a real one, and the deployed client picks the first.

  2. THE SUCCESS RESULTS CARRY NO CORRELATION. Both returns are
     `{status, message, idempotent}` — no order, no key, no scope, no reference.
     A client validating "is this the outcome of MY command?" has nothing to
     validate against, so a response that arrived late, or against a different
     order, is indistinguishable from the right one.

  3. THE ORIGINAL ACCEPTED REFERENCE IS NEVER PUBLISHED. `OrderAcceptance`
     stores the exact `quote_ref` the diner confirmed, and no surface returns
     it. A client wanting to check what it accepted has only `quote_ref(order)`
     recomputed from the CURRENT rows — which is a different question, and
     answers differently the moment anything about the order changes.

WHAT THIS FILE PINS. One shared projection, consumed identically by the
mutation result and the diner's own read, that states: which order and which
keyed intent; the scope the SERVER resolved; a three-state acceptance verdict
that never collapses a legacy acceptance into a draft; the ORIGINAL reference
and moment, read from the stored row and never recomputed; the current order
and fulfilment state, labelled separately from the acceptance; and the
capability level actually implemented.

WHAT IS NOT CLAIMED. Nothing here is exactly-once delivery, global
deduplication, or payment idempotency. `evidence_unavailable` is not backfilled
into an acceptance — it is the server saying it does not know, which is the
honest answer for an order that predates the evidence table.
"""
import uuid

from django.utils import timezone

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from dinify_backend.configss.string_definitions import (
    CancellationReason_CustomerChangedMind, OrderStatus_Cancelled,
    OrderStatus_Initiated, OrderStatus_Pending, OrderStatus_Preparing,
    OrderStatus_Served,
)
from orders_app.controllers.manage_order import update_order_status
from orders_app.endpoints_kitchen import KitchenOrderCancelView
from orders_app.controllers.services import checkout_protocol as protocol
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderAcceptance, OrderItem
from orders_app.serializers import SerializerPublicOrderDetails
from orders_app.tests_order_acceptance import AcceptanceFixture


def _projection():
    """The projection under test, imported at CALL TIME.

    Deliberate: before the module exists, a top-level import would collapse
    every regression below into a single collection error, and "1 error" is a
    far weaker statement of what is missing than the list of behaviours that
    individually fail. The cost is one indirection in tests that would
    otherwise read `acceptance_result(...)` directly.
    """
    from orders_app.controllers.services.acceptance_result import (
        acceptance_result,
    )
    return acceptance_result


class CorrelationFixture(AcceptanceFixture):
    """Adds the two states D04/C cannot currently tell apart."""

    def _accepted(self, key=None, table=None):
        """A real accepted order, with its evidence row."""
        order = self._draft(table=table, key=key)
        result = self._submit(order)
        self.assertEqual(result.get('status'), 200, result)
        order.refresh_from_db()
        return order

    def _legacy_accepted(self, key=None):
        """An order accepted BEFORE the evidence table existed.

        Modelled by removing the row, which is exactly the state a pre-D04/C
        acceptance left behind: a non-draft order with nothing recording when
        it was accepted or against which quote. There is no live path that
        produces it today, and that is the point — it is HISTORY, and the
        server must describe it truthfully rather than calling it a draft.
        """
        order = self._accepted(key=key)
        OrderAcceptance.objects.filter(order=order).delete()
        order.refresh_from_db()
        return order

    def _payload(self, order):
        """The diner's read of this order. Named apart from the fixture's
        ``_read``, which issues the real HTTP-shaped journey call."""
        return SerializerPublicOrderDetails(
            Order.objects.get(pk=order.pk)).data

    def _checkout(self, order):
        return self._payload(order)['checkout']


# ---------------------------------------------------------------------------
# A. THE THREE-STATE VERDICT — a legacy acceptance is not a draft
# ---------------------------------------------------------------------------

class TheAcceptanceVerdictHasThreeStatesTests(CorrelationFixture):

    def test_a_draft_is_definitively_not_accepted(self):
        result = _projection()(self._draft())
        self.assertEqual(result['acceptance']['state'], 'not_accepted')

    def test_a_legacy_acceptance_is_not_reported_as_a_draft(self):
        """THE HEADLINE REGRESSION.

        Both of these read `accepted: false` today, and a client acting on
        that boolean treats the second as reviewable. It is in the kitchen.
        """
        draft = _projection()(self._draft())
        legacy = _projection()(self._legacy_accepted())
        self.assertEqual(draft['acceptance']['state'], 'not_accepted')
        self.assertEqual(legacy['acceptance']['state'],
                         'evidence_unavailable')
        self.assertNotEqual(draft['acceptance']['state'],
                            legacy['acceptance']['state'])

    def test_evidence_unavailable_invents_no_reference_and_no_moment(self):
        """Not knowing is reported as not knowing. Deriving a moment from
        `time_last_updated` or a reference from the current rows would be a
        fabricated receipt indistinguishable from a real one."""
        legacy = _projection()(self._legacy_accepted())
        self.assertEqual(legacy['acceptance']['state'],
                         'evidence_unavailable')
        self.assertIsNone(legacy['acceptance']['quote_ref'])
        self.assertIsNone(legacy['acceptance']['accepted_at'])

    def test_an_accepted_order_reports_its_reference_and_moment(self):
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        order.refresh_from_db()

        acceptance = _projection()(order)['acceptance']
        row = OrderAcceptance.objects.get(order=order)
        self.assertEqual(acceptance['state'], 'accepted')
        self.assertEqual(acceptance['quote_ref'], ref)
        self.assertEqual(acceptance['quote_ref'], row.quote_ref)
        self.assertEqual(acceptance['accepted_at'], row.accepted_at.isoformat())

    def test_a_cancelled_legacy_acceptance_still_is_not_a_draft(self):
        """Cancellation is a later fact about an order that WAS accepted. It
        must not push the verdict back to the one state that invites a
        client to accept it again."""
        order = self._legacy_accepted()
        update_order_status(order, OrderStatus_Cancelled, self.owner)
        order.refresh_from_db()

        result = _projection()(order)
        self.assertEqual(result['acceptance']['state'],
                         'evidence_unavailable')
        self.assertEqual(result['current']['order_status'],
                         OrderStatus_Cancelled)


# ---------------------------------------------------------------------------
# B. THE ORIGINAL REFERENCE IS READ, NEVER RECOMPUTED
# ---------------------------------------------------------------------------

class TheOriginalReferenceSurvivesTheOrderChangingTests(CorrelationFixture):

    SENTINEL = 'v1-original-reference-no-recomputation-can-produce-this'

    def test_the_stored_reference_is_returned_verbatim(self):
        """A value no recomputation could produce, so a recomputing
        implementation cannot accidentally agree with it."""
        order = self._accepted()
        OrderAcceptance.objects.filter(order=order).update(
            quote_ref=self.SENTINEL)

        self.assertEqual(
            _projection()(order)['acceptance']['quote_ref'], self.SENTINEL)

    def test_the_reference_does_not_move_when_the_order_does(self):
        order = self._draft()
        original = quote_ref(order)
        self.assertEqual(self._submit(order, original).get('status'), 200)
        order.refresh_from_db()

        # A row changes, so the order's CURRENT reference is a different value.
        row = OrderItem.objects.filter(order=order, parent_item=None).first()
        row.quantity = row.quantity + 1
        row.save()
        order.refresh_from_db()
        self.assertNotEqual(quote_ref(order), original)

        self.assertEqual(
            _projection()(order)['acceptance']['quote_ref'], original)

    def test_the_reference_does_not_move_when_the_kitchen_does(self):
        order = self._draft()
        original = quote_ref(order)
        self.assertEqual(self._submit(order, original).get('status'), 200)
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Served, self.owner)
        order.refresh_from_db()

        result = _projection()(order)
        self.assertEqual(result['acceptance']['quote_ref'], original)
        self.assertEqual(result['acceptance']['state'], 'accepted')
        self.assertEqual(result['current']['order_status'], OrderStatus_Served)

    def test_the_moment_is_the_acceptance_not_the_last_update(self):
        order = self._accepted()
        accepted_at = OrderAcceptance.objects.get(order=order).accepted_at
        update_order_status(order, OrderStatus_Preparing, self.owner)
        order.refresh_from_db()

        self.assertEqual(_projection()(order)['acceptance']['accepted_at'],
                         accepted_at.isoformat())
        self.assertNotEqual(order.time_last_updated, accepted_at)


# ---------------------------------------------------------------------------
# C. CORRELATION — which order, which key, whose scope
# ---------------------------------------------------------------------------

class TheResultNamesWhatItIsAboutTests(CorrelationFixture):

    def test_it_names_the_order_and_the_intent_key(self):
        key = uuid.uuid4()
        order = self._accepted(key=key)

        result = _projection()(order)
        self.assertEqual(result['order_id'], str(order.pk))
        self.assertEqual(result['intent_key'], str(key))

    def test_a_keyless_order_names_no_key_rather_than_an_empty_one(self):
        result = _projection()(self._accepted())
        self.assertIsNone(result['intent_key'])

    def test_it_names_the_scope_the_server_resolved(self):
        """The client can verify the answer belongs to the table it is sitting
        at. The scope comes off the ORDER, never off the request."""
        order = self._accepted(table=self.table_b)

        scope = _projection()(order)['scope']
        self.assertEqual(scope['restaurant'], str(self.restaurant.pk))
        self.assertEqual(scope['table'], str(self.table_b.pk))

    def test_the_current_state_is_labelled_apart_from_the_acceptance(self):
        order = self._accepted()
        update_order_status(order, OrderStatus_Preparing, self.owner)
        order.refresh_from_db()

        result = _projection()(order)
        self.assertEqual(result['acceptance']['state'], 'accepted')
        self.assertEqual(result['current']['order_status'],
                         OrderStatus_Preparing)
        self.assertIn('fulfilment_status', result['current'])
        self.assertIsNone(result['current']['cancelled_at'])
        self.assertIsNone(result['current']['served_at'])

    def test_a_cancellation_is_reported_in_the_current_state(self):
        order = self._accepted()
        order.cancelled_at = timezone.now()
        order.save()
        update_order_status(order, OrderStatus_Cancelled, self.owner)
        order.refresh_from_db()

        result = _projection()(order)
        self.assertEqual(result['acceptance']['state'], 'accepted')
        self.assertEqual(result['current']['order_status'],
                         OrderStatus_Cancelled)
        self.assertEqual(result['current']['cancelled_at'],
                         order.cancelled_at.isoformat())

    def test_service_is_reported_in_the_current_state(self):
        order = self._accepted()
        order.served_at = timezone.now()
        order.fulfilment_status = 'served'
        order.save()
        order.refresh_from_db()

        current = _projection()(order)['current']
        self.assertEqual(current['fulfilment_status'], 'served')
        self.assertEqual(current['served_at'], order.served_at.isoformat())


# ---------------------------------------------------------------------------
# D. THE MUTATION RESULT — newly accepted vs already accepted
# ---------------------------------------------------------------------------

class TheSubmitResultIsCorrelatedTests(CorrelationFixture):

    def test_a_first_acceptance_is_labelled_newly_accepted(self):
        key = uuid.uuid4()
        order = self._draft(key=key)
        ref = quote_ref(order)

        result = self._submit(order, ref)
        self.assertEqual(result.get('status'), 200, result)
        checkout = result['checkout']
        self.assertEqual(checkout['acceptance']['outcome'], 'newly_accepted')
        self.assertEqual(checkout['acceptance']['state'], 'accepted')
        self.assertEqual(checkout['acceptance']['quote_ref'], ref)
        self.assertEqual(checkout['order_id'], str(order.pk))
        self.assertEqual(checkout['intent_key'], str(key))

    def test_a_replay_is_labelled_already_accepted(self):
        key = uuid.uuid4()
        order = self._draft(key=key)
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        order.refresh_from_db()

        replay = self._submit(order, ref)
        self.assertEqual(replay.get('status'), 200, replay)
        checkout = replay['checkout']
        self.assertEqual(checkout['acceptance']['outcome'], 'already_accepted')
        self.assertEqual(checkout['acceptance']['state'], 'accepted')
        self.assertEqual(checkout['order_id'], str(order.pk))
        self.assertEqual(checkout['intent_key'], str(key))

    def test_a_replay_reports_the_original_moment_not_a_fresh_one(self):
        order = self._draft()
        ref = quote_ref(order)
        first = self._submit(order, ref)
        order.refresh_from_db()
        replay = self._submit(order, ref)

        self.assertEqual(replay['checkout']['acceptance']['accepted_at'],
                         first['checkout']['acceptance']['accepted_at'])
        self.assertEqual(
            replay['checkout']['acceptance']['accepted_at'],
            OrderAcceptance.objects.get(order=order).accepted_at.isoformat())

    def test_a_replay_after_the_kitchen_started_labels_both_facts(self):
        """The correlated answer a recovering client needs in one response:
        your submission landed, and the kitchen is already on it."""
        order = self._draft()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Preparing, self.owner)
        order.refresh_from_db()

        replay = self._submit(order, ref)
        checkout = replay['checkout']
        self.assertEqual(checkout['acceptance']['outcome'], 'already_accepted')
        self.assertEqual(checkout['current']['order_status'],
                         OrderStatus_Preparing)

    def test_a_read_carries_no_outcome_because_it_is_not_one(self):
        """A read OBSERVES; it is not the result of an acceptance attempt.
        The key is present so the shape never changes, and it is null."""
        order = self._accepted()
        self.assertIsNone(self._checkout(order)['acceptance']['outcome'])

    def test_the_existing_success_contract_is_unchanged(self):
        order = self._draft()
        ref = quote_ref(order)

        first = self._submit(order, ref)
        self.assertEqual(first['status'], 200)
        self.assertFalse(first['idempotent'])
        self.assertTrue(first['message'])

        order.refresh_from_db()
        replay = self._submit(order, ref)
        self.assertEqual(replay['status'], 200)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(replay['message'], first['message'])

    def test_a_refusal_carries_no_acceptance_claim(self):
        """A conflict is not an acceptance and must not look like one: a
        refused submit names no reference and no moment."""
        order = self._draft()
        self.assertEqual(self._submit(order, quote_ref(order)).get('status'),
                         200)
        order.refresh_from_db()

        conflict = self._submit(order, 'some-other-reference')
        self.assertEqual(conflict.get('status'), 409, conflict)
        self.assertNotIn('some-other-reference', str(conflict))
        self.assertIsNone(conflict.get('checkout'))


# ---------------------------------------------------------------------------
# E. ONE PROJECTION, BOTH SURFACES
# ---------------------------------------------------------------------------

class TheReadAndTheMutationAgreeTests(CorrelationFixture):

    def test_the_read_publishes_the_same_projection(self):
        key = uuid.uuid4()
        order = self._draft(key=key)
        ref = quote_ref(order)
        submitted = self._submit(order, ref)['checkout']
        order.refresh_from_db()

        observed = self._checkout(order)
        # Everything except the mutation-only outcome is byte-identical.
        self.assertEqual({**submitted, 'acceptance': {
            **submitted['acceptance'], 'outcome': None}}, observed)

    def test_the_recovery_read_answers_the_whole_question(self):
        key = uuid.uuid4()
        order = self._accepted(key=key)

        recovered = self._read(intent=str(key))
        self.assertEqual(recovered.get('status'), 200, recovered)
        checkout = recovered['data']['checkout']
        self.assertEqual(checkout['acceptance']['state'], 'accepted')
        self.assertEqual(checkout['intent_key'], str(key))
        self.assertEqual(checkout['order_id'], str(order.pk))
        self.assertEqual(checkout['scope']['table'], str(self.table.pk))

    def test_the_legacy_boolean_keys_are_kept_for_existing_clients(self):
        order = self._accepted()
        payload = self._payload(order)
        self.assertTrue(payload['accepted'])
        self.assertIsNotNone(payload['accepted_at'])
        self.assertIsNotNone(payload['checkout'])

    def test_the_legacy_boolean_still_reads_false_for_a_legacy_acceptance(self):
        """The old key keeps its old (conflating) meaning — it is
        compatibility, not a second answer. The new one is where the
        distinction lives, which is why a client must migrate to it."""
        order = self._legacy_accepted()
        payload = self._payload(order)
        self.assertFalse(payload['accepted'])
        self.assertEqual(payload['checkout']['acceptance']['state'],
                         'evidence_unavailable')


# ---------------------------------------------------------------------------
# F. THE CAPABILITY IS ADVERTISED, NOT REINTERPRETED
# ---------------------------------------------------------------------------

class TheCapabilityLevelIsExplicitTests(CorrelationFixture):

    def test_the_new_level_is_its_own_number(self):
        """#661's lesson again: a new promise gets a new level rather than a
        new meaning for an old one, so a client pinned to 2 is never told it
        may do something only 3 supports."""
        self.assertEqual(protocol.CHECKOUT_PROTOCOL_BINDING, 1)
        self.assertEqual(protocol.CHECKOUT_PROTOCOL_RECOVERABLE, 2)
        self.assertEqual(protocol.CHECKOUT_PROTOCOL_CORRELATED, 3)

    def test_this_build_advertises_the_level_it_implements(self):
        self.assertEqual(protocol.CHECKOUT_PROTOCOL,
                         protocol.CHECKOUT_PROTOCOL_CORRELATED)

    def test_both_surfaces_state_it(self):
        order = self._draft()
        ref = quote_ref(order)
        submitted = self._submit(order, ref)
        order.refresh_from_db()

        self.assertEqual(submitted['checkout']['checkout_protocol'],
                         protocol.CHECKOUT_PROTOCOL_CORRELATED)
        self.assertEqual(self._checkout(order)['checkout_protocol'],
                         protocol.CHECKOUT_PROTOCOL_CORRELATED)
        self.assertEqual(self._payload(order)['checkout_protocol'],
                         protocol.CHECKOUT_PROTOCOL_CORRELATED)


# ---------------------------------------------------------------------------
# G. LEAVING THE DRAFT STATE IS NOT EVIDENCE OF ACCEPTANCE (Codex P2, #318)
# ---------------------------------------------------------------------------

class NotEveryNonDraftOrderWasAcceptedTests(CorrelationFixture):
    """"Not a draft" still cannot be read as "a submission landed".

    THIS CLASS CHANGED WITH D05, AND THE CHANGE IS THE POINT. It used to prove
    that a kitchen write could move an order out of `initiated` with no
    acceptance — `KitchenOrderCancelView` resolved a draft by primary key, its
    `fulfilment_status` was still `new`, so it took the free-void branch and
    needed no manager, and the fulfilment view had the same shape. D05 closed
    that: every kitchen command now refuses a draft, so the kitchen is no
    longer a PRODUCER of the third state.

    IT IS NOT A RESOLVER OF THE ROWS ALREADY PRODUCED, and that is why this
    class survives rather than being deleted. Historical rows reached that
    state by both routes and nothing on the row separates them, so
    `evidence_unavailable` remains a statement of ignorance — never a verdict,
    and never something to backfill. The tests below now prove the producer is
    closed AND that the projection still refuses to claim acceptance for a row
    that reached the state some other way.
    """

    def _cancel_through_the_kitchen(self, order, if_revision=0):
        request = APIRequestFactory().put(
            f'/api/v1/kitchen/orders/{order.pk}/cancel/',
            {'cancellation_reason': CancellationReason_CustomerChangedMind,
             'if_revision': if_revision},
            format='json',
        )
        force_authenticate(request, user=self.owner)
        request.user = self.owner
        return KitchenOrderCancelView.as_view()(request, pk=str(order.pk))

    def test_the_kitchen_can_no_longer_cancel_a_draft(self):
        """D05's draft boundary, through the REAL view.

        This is the inverse of the control this class used to carry. The
        kitchen producer of `evidence_unavailable` is closed: a draft is
        refused, nothing is written, and the order stays exactly what it was.
        """
        order = self._draft()
        response = self._cancel_through_the_kitchen(order)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data['reason'], 'order_is_draft')

        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        self.assertIsNone(order.cancelled_at)
        self.assertEqual(order.fulfilment_revision, 0)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())

    def test_a_non_draft_without_evidence_is_never_reported_as_accepted(self):
        """The historical row, produced DIRECTLY because the kitchen no longer
        can. Rows of this shape exist and the projection must keep telling the
        truth about them."""
        order = self._draft()
        order.order_status = OrderStatus_Cancelled
        order.cancelled_at = timezone.now()
        order.save(update_fields=['order_status', 'cancelled_at'])

        acceptance = _projection()(order)['acceptance']
        self.assertEqual(acceptance['state'], 'evidence_unavailable')
        self.assertNotEqual(acceptance['state'], 'accepted')
        self.assertIsNone(acceptance['quote_ref'])
        self.assertIsNone(acceptance['accepted_at'])

    def test_the_third_state_claims_no_submission_landed(self):
        """THE FINDING, stated as a contract rather than as prose.

        `evidence_unavailable` must mean "this server cannot determine
        whether the submission landed" — NOT "it landed". The module's own
        constant docstring is what a client contract is read from, so the
        over-claim is pinned absent here.
        """
        from orders_app.controllers.services import acceptance_result as mod
        doc = (mod.ACCEPTANCE_EVIDENCE_UNAVAILABLE_MEANING or '').lower()
        self.assertIn('cannot', doc)
        self.assertIn('kitchen', doc)

    def test_a_never_submitted_draft_still_reads_not_accepted(self):
        """The narrowing must not swallow the definitive case."""
        self.assertEqual(
            _projection()(self._draft())['acceptance']['state'],
            'not_accepted')


# ---------------------------------------------------------------------------
# H. ONE SNAPSHOT FOR THE ORDER AND ITS EVIDENCE (Codex P2, #318)
# ---------------------------------------------------------------------------

class TheProjectionDescribesOneSnapshotTests(CorrelationFixture):
    """The order and its acceptance must be read TOGETHER.

    Two statements take two snapshots under READ COMMITTED, so a recovery
    GET racing a submission could read the order while still `initiated` and
    then find the evidence committed a moment later — publishing
    `acceptance.state == accepted` beside `current.order_status ==
    initiated`, a correlated answer describing a moment that never existed.
    The same lesson `catalogue_snapshot` records: fold the two reads into one.
    """

    def test_the_journey_read_joins_the_evidence_in_one_query(self):
        """Structural, not timing-based: if the serializer issues its own
        second read, these two counts differ and the race is reachable."""
        key = uuid.uuid4()
        order = self._accepted(key=key)

        with CaptureQueriesContext(connection) as captured:
            payload = SerializerPublicOrderDetails(
                Order.objects.select_related('acceptance').get(pk=order.pk)
            ).data
        self.assertEqual(payload['checkout']['acceptance']['state'],
                         'accepted')
        # A SEPARATE read, not the JOIN: the join names the table too, so
        # matching the bare name would fail even once the fix is in.
        separate = [
            q['sql'] for q in captured.captured_queries
            if 'from "order_acceptances"' in q['sql'].lower()
        ]
        self.assertEqual(separate, [], separate)

    def test_the_production_read_itself_joins_the_evidence(self):
        """The half the test above cannot see.

        That one hands the serializer an order IT joined, so it pins the
        serializer's behaviour and would pass just as happily if
        `handle_show_order_details` fetched without the join. This drives the
        REAL journey read, both selectors, and asserts the same thing of it.
        """
        key = uuid.uuid4()
        order = self._accepted(key=key)

        for selector in ({'order': str(order.pk)}, {'intent': str(key)}):
            with self.subTest(selector=next(iter(selector))):
                with CaptureQueriesContext(connection) as captured:
                    answer = self._read(**selector)
                self.assertEqual(answer.get('status'), 200, answer)
                self.assertEqual(
                    answer['data']['checkout']['acceptance']['state'],
                    'accepted')
                separate = [
                    q['sql'] for q in captured.captured_queries
                    if 'from "order_acceptances"' in q['sql'].lower()
                ]
                self.assertEqual(separate, [], separate)

    def test_the_answer_is_never_accepted_beside_a_draft_status(self):
        """The incoherent pair, produced directly.

        The order instance is read BEFORE the acceptance commits and
        serialized AFTER — exactly the interleaving the race produces. The
        projection must describe the snapshot it was handed, not blend two.
        """
        order = self._draft()
        stale = Order.objects.select_related('acceptance').get(pk=order.pk)
        self.assertEqual(stale.order_status, OrderStatus_Initiated)

        # the acceptance commits while the stale instance is still in hand
        self.assertEqual(self._submit(order).get('status'), 200)
        self.assertTrue(OrderAcceptance.objects.filter(order=order).exists())

        checkout = SerializerPublicOrderDetails(stale).data['checkout']
        self.assertEqual(checkout['current']['order_status'],
                         OrderStatus_Initiated)
        self.assertNotEqual(
            checkout['acceptance']['state'], 'accepted',
            'accepted beside a draft status is a moment that never existed')

    def test_the_recovery_read_is_still_correct_when_nothing_races(self):
        """The ordinary path, unchanged."""
        key = uuid.uuid4()
        self._accepted(key=key)
        recovered = self._read(intent=str(key))
        self.assertEqual(recovered.get('status'), 200, recovered)
        checkout = recovered['data']['checkout']
        self.assertEqual(checkout['acceptance']['state'], 'accepted')
        self.assertEqual(checkout['current']['order_status'],
                         OrderStatus_Pending)
