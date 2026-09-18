"""
D06 completion, G3a — A CLOSED QUOTE IS DISCOVERABLE, NOT ONLY ANNOUNCED.

WHAT WAS MISSING. D06 made a terminal refusal DURABLE: the row is what lets a
replacement quote be minted safely, because an acceptance still in flight for the
old one can never execute afterwards. It published that row on the REFUSAL — the
response to the request that closed it — and nowhere else.

A refusal response is the one thing a client can lose. Lose it and the diner holds
a draft whose quote is permanently dead, with no way to find that out: the order
read published the DEADLINE (`quote_policy`) and said nothing about the closure, so
a quote closed for `purchase_needs_review` INSIDE its window read `live` on every
surface a recovering client could reach. Its only remaining move was to attempt an
acceptance — which is exactly the thing D06 built `retire-quote` to avoid, because
when the quote IS still good that attempt succeeds, claims a table and sends food
to a kitchen in order to ask a question.

So the closure joins the authorized read. It is the SAME projection the refusal
carries, from the SAME function, so the two cannot drift into a response that says
closed and a read that says nothing.

THREE THINGS THIS DOES NOT DO. It does not reprice the old order, it does not
delete or move a closure, and it does not mint a replacement — a new quote is a
new, explicitly requested purchase, and making one is the client's decision
(G3b). It is a READ.

THE DEADLINE AND THE CLOSURE ARE INDEPENDENT FACTS AND ARE LABELLED APART, for
the reason D04 keeps `current` apart from `acceptance`: collapsing them is how one
fact silently answers for another. A quote closed for `purchase_needs_review`
while still inside its window legitimately reads `quote_policy.status == 'live'`
beside a closure, and that is not a contradiction — the deadline has not passed
and the quote is finished anyway. The CLOSURE is what decides acceptability.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dinify_backend.configss.string_definitions import OrderStatus_Pending
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.manage_order import retire_quote_for_review
from orders_app.controllers.services import quote_closure
from orders_app.controllers.services.checkout_protocol import CHECKOUT_PROTOCOL
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.quote_protocol import (
    QUOTE_PROTOCOL, QUOTE_PROTOCOL_ENFORCED,
)
from orders_app.models import Order, OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin
from restaurants_app.controllers.diner_capability import (
    SESSION_HEADER, capability_from_table, issue_table_session,
)
from restaurants_app.models import MenuItem, Table

DETAILS_URL = '/api/v1/orders/journey/order-details/'

#: Exactly what a client may read about a closure, and nothing else. Asserted as
#: an EXACT set rather than a subset: the risk here is a future field ARRIVING —
#: an actor, an amount, a catalogue reason — not one going missing.
CLOSURE_KEYS = {'closed_at', 'reason', 'quote_ref', 'policy_version'}


class ClosureReadFixture(QuoteFixtureMixin, TestCase):

    def _session_header(self):
        return {
            'HTTP_' + SESSION_HEADER.upper().replace('-', '_'):
                issue_table_session(self.table),
        }

    def _draft_with_key(self, key):
        result = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(self.item.pk), 'quantity': 1}],
            created_by=None, client_order_id=key,
        )
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def _read(self, **params):
        response = self.client.get(
            DETAILS_URL, params, **self._session_header())
        return response, json.loads(response.content.decode())

    def _details(self, order):
        """The read's payload is FLAT — `checkout_protocol` already sits there
        rather than under an `order_details` object, which is the INITIATE
        response's nesting, and the three new keys join their sibling."""
        _response, body = self._read(order=str(order.pk))
        return body['data']

    def _retire(self, order, ref=None):
        return retire_quote_for_review(
            order,
            ref if ref is not None else quote_ref(order),
            capability=capability_from_table(self.table),
        )

    def _close_by_expiry(self, order):
        """The real route, driven to a real terminal refusal."""
        self._age_draft(order, 31)
        result = self._retire(order)
        self.assertEqual(result.get('outcome'), 'quote_closed', result)
        return result

    def _close_by_purchase_change(self, order):
        """Closed while the DEADLINE has not passed — the case the read used to
        report as `live` with nothing beside it."""
        MenuItem.objects.filter(pk=self.item.pk).update(available=False)
        result = self._retire(order)
        self.assertEqual(result.get('outcome'), 'quote_closed', result)
        return result


class AClosedQuoteIsDiscoverableOnTheReadTests(ClosureReadFixture):
    """THE REGRESSION. Lose the refusal, read the order, learn the truth."""

    def test_a_closed_quote_is_published_on_the_order_read(self):
        order = self._draft_order()
        reference = quote_ref(order)
        self._close_by_expiry(order)

        published = self._details(order)['quote_closure']
        self.assertIsNotNone(published, 'a lost refusal must be recoverable')
        self.assertEqual(published['reason'], quote_closure.REASON_EXPIRED)
        self.assertEqual(published['quote_ref'], reference)

    def test_a_closed_quote_is_published_on_the_INTENT_read(self):
        """The selector a client that lost the response actually holds: it lost
        the order id too, and the key is what survived."""
        key = 'f47ac10b-58cc-4372-a567-0e02b2c3d479'
        order = self._draft_with_key(key)
        self._close_by_expiry(order)

        _response, body = self._read(intent=key)
        published = body['data']['quote_closure']
        self.assertIsNotNone(published)
        self.assertEqual(published['reason'], quote_closure.REASON_EXPIRED)

    def test_both_selectors_publish_byte_identical_closures(self):
        key = 'f47ac10b-58cc-4372-a567-0e02b2c3d480'
        order = self._draft_with_key(key)
        self._close_by_expiry(order)

        _r1, by_order = self._read(order=str(order.pk))
        _r2, by_intent = self._read(intent=key)
        self.assertEqual(
            by_order['data']['quote_closure'],
            by_intent['data']['quote_closure'],
        )

    def test_an_open_quote_publishes_an_explicit_null(self):
        """The key is ALWAYS present, so a client never has to branch on whether
        a field exists to learn that nothing has happened."""
        order = self._draft_order()
        details = self._details(order)
        self.assertIn('quote_closure', details)
        self.assertIsNone(details['quote_closure'])


class TheReadPublishesTheQUOTECONTRACTAtAllTests(ClosureReadFixture):
    """The half that is easy to miss: the diner's own order read published
    NEITHER the level NOR the deadline.

    `quote_protocol` and `quote_policy` were added by D06 to the INITIATE
    response's `order_details` only. So the one surface a client can reach after
    losing everything else said nothing at all about the life of the quote it was
    describing — no level, no deadline, no closure. A recovering client could
    read its order and still not know whether the amount in front of it could
    still be paid.

    They are published here through the SAME constant and the SAME projection
    the initiate response uses, never a second definition — the rule D04/U1
    already applied to `quote_total` and `quote_complete` on this serializer.
    """

    def test_the_read_publishes_the_level(self):
        order = self._draft_order()
        self.assertEqual(self._details(order)['quote_protocol'], QUOTE_PROTOCOL)

    def test_the_read_publishes_the_deadline(self):
        order = self._draft_order()
        policy = self._details(order)['quote_policy']
        self.assertEqual(policy['status'], 'live')
        self.assertIsNotNone(policy['expires_at'])
        self.assertEqual(policy['version'], 1)

    def test_the_read_and_the_initiate_response_agree_about_the_policy(self):
        """One rule, two surfaces, and no way for them to drift: a client that
        holds either response reaches the same conclusion."""
        from orders_app.controllers.orders.serializers import (
            quote_policy_projection,
        )
        from orders_app import serializers as read_serializers
        self.assertIs(
            read_serializers.quote_policy_projection, quote_policy_projection)


class TheProjectionIsBoundedAndSharedTests(ClosureReadFixture):

    def test_the_published_keys_are_exactly_the_bounded_set(self):
        order = self._draft_order()
        self._close_by_expiry(order)
        self.assertEqual(
            set(self._details(order)['quote_closure']), CLOSURE_KEYS)

    def test_the_read_and_the_refusal_agree_byte_for_byte(self):
        """ONE function, so a response that says closed and a read that says
        nothing is not a state this code can reach."""
        order = self._draft_order()
        refusal = self._close_by_expiry(order)
        self.assertEqual(
            refusal['quote_closure'], self._details(order)['quote_closure'])

    def test_both_surfaces_use_the_closure_services_own_projection(self):
        """BY IDENTITY, on BOTH modules that publish it. A second copy could
        pass by producing similar output today and drift the first time either
        is extended."""
        from orders_app import serializers as read_serializers
        from orders_app.controllers.orders import serializers as initiate
        self.assertIs(
            read_serializers.closure_projection,
            quote_closure.closure_projection,
        )
        self.assertIs(
            initiate.closure_projection, quote_closure.closure_projection)


class TheAnswerDescribesOneSnapshotTests(ClosureReadFixture):
    """The D04 lesson, applied to the second relation this read now needs.

    Under READ COMMITTED each statement takes its OWN snapshot, so fetching the
    order in one statement and the closure in another lets a closure commit
    between them — a correlated answer describing a moment that never existed,
    on the one surface whose whole job is to be verifiable. The fix is the same:
    fold the reads. The proof is that the closure costs NO EXTRA STATEMENT.
    """

    def _closure_statements(self, order):
        """Statements that go to the closure table ON THEIR OWN.

        NOT a query COUNT, and the difference matters. The obvious oracle —
        "a closed order costs the same read as an open one" — is BLIND here,
        because an unjoined `read_closure` issues its lookup either way and
        finds nothing for the open order: both sides move together and the
        counts stay equal while the fold is gone. It passed against the
        unfolded code. What has to be absent is a SECOND STATEMENT, which is
        the thing that takes its own snapshot.
        """
        with CaptureQueriesContext(connection) as captured:
            self._read(order=str(order.pk))
        return [
            q['sql'] for q in captured
            if 'order_quote_closures' in q['sql']
            and ' JOIN ' not in q['sql'].upper()
        ]

    def test_the_read_issues_no_second_statement_for_the_closure(self):
        closed_order = self._draft_order()
        self._close_by_expiry(closed_order)
        self.assertEqual(self._closure_statements(closed_order), [])

    def test_nor_for_an_order_that_has_no_closure(self):
        """The same rule on the ordinary path: an unjoined lookup would cost
        every diner read a statement to discover nothing."""
        self.assertEqual(self._closure_statements(self._draft_order()), [])

    def test_the_closure_relation_is_joined_by_the_read(self):
        """Stated directly as well as by cost, so the reason survives a future
        change that happens to keep the count equal for some other reason."""
        order = self._draft_order()
        self._close_by_expiry(order)
        with CaptureQueriesContext(connection) as captured:
            self._read(order=str(order.pk))
        joined = [
            q['sql'] for q in captured
            if 'order_quote_closures' in q['sql'] and ' JOIN ' in q['sql'].upper()
        ]
        self.assertTrue(joined, [q['sql'] for q in captured])


class TheDeadlineAndTheClosureAreIndependentTests(ClosureReadFixture):

    def test_a_quote_closed_inside_its_window_still_reads_live(self):
        """NOT a contradiction, and deliberately not flattened: the deadline has
        genuinely not passed, and the quote is genuinely finished. Collapsing
        them would make one fact answer for another — the mistake D04 records
        for `current` versus `acceptance`."""
        order = self._draft_order()
        self._close_by_purchase_change(order)

        details = self._details(order)
        self.assertEqual(details['quote_policy']['status'], 'live')
        self.assertEqual(
            details['quote_closure']['reason'],
            quote_closure.REASON_PURCHASE_CHANGED,
        )

    def test_an_expired_closed_quote_reads_expired_beside_its_closure(self):
        order = self._draft_order()
        self._close_by_expiry(order)
        details = self._details(order)
        self.assertEqual(details['quote_policy']['status'], 'expired')
        self.assertEqual(
            details['quote_closure']['reason'], quote_closure.REASON_EXPIRED)


class TheLevelIsANewLevelNotANewMeaningTests(TestCase):
    """#661, in the place it would next occur."""

    def test_the_quote_protocol_is_raised_to_two(self):
        self.assertEqual(QUOTE_PROTOCOL, 2)

    def test_level_one_still_names_exactly_what_it_named(self):
        """A client pinned to 1 keeps the promises 1 made and gains none: it
        was RIGHT that level 1 said nothing about reading a closure back."""
        self.assertEqual(QUOTE_PROTOCOL_ENFORCED, 1)

    def test_the_checkout_protocol_is_untouched(self):
        """D04 answers a different question and gained nothing here."""
        self.assertEqual(CHECKOUT_PROTOCOL, 3)


class ReClosingReturnsTheOriginalRowUnchangedTests(ClosureReadFixture):
    """A closure is written once and never moves."""

    def test_a_second_retirement_returns_the_original_closure(self):
        order = self._draft_order()
        first = self._close_by_expiry(order)
        row = OrderQuoteClosure.objects.get(order=order)

        second = self._retire(order)
        row.refresh_from_db()

        self.assertEqual(second['quote_closure'], first['quote_closure'])
        self.assertEqual(
            row.closed_at.isoformat(), first['quote_closure']['closed_at'])
        self.assertEqual(OrderQuoteClosure.objects.filter(order=order).count(), 1)

    def test_a_retirement_naming_a_DIFFERENT_reference_rewrites_nothing(self):
        """AND IT ANSWERS ABOUT THE REFERENCE THAT WAS ACTUALLY RETIRED.

        The first cut of this test asserted a `quote_ref_stale` refusal, on the
        reasoning that the acknowledgement precedes the terminal checks. That is
        true of the terminal WRITES and is not true of the closure READ, on
        EITHER path: both `_submit_order` and `retire_quote_for_review`
        deliberately read the committed closure before asking which quote the
        caller means, because an already-retired quote must refuse before
        anything else happens. So the contract is what this asserts — the
        request writes NOTHING, and the closure it is told about names the
        reference the server really retired rather than echoing the one the
        caller asserted. Nothing is disclosed by that: the caller already holds
        the session, and the read now publishes the same row.
        """
        order = self._draft_order()
        self._close_by_expiry(order)
        before = OrderQuoteClosure.objects.get(order=order)

        answered = self._retire(order, ref='0' * 64)
        after = OrderQuoteClosure.objects.get(order=order)

        self.assertEqual(
            answered.get('outcome'), quote_closure.OUTCOME_ALREADY_CLOSED)
        self.assertEqual(
            answered['quote_closure']['quote_ref'], before.quote_ref)
        self.assertNotEqual(answered['quote_closure']['quote_ref'], '0' * 64)
        self.assertEqual(after.quote_ref, before.quote_ref)
        self.assertEqual(after.closed_at, before.closed_at)
        self.assertEqual(after.reason, before.reason)
        self.assertEqual(
            OrderQuoteClosure.objects.filter(order=order).count(), 1)

    def test_reading_a_closed_order_never_deletes_or_moves_the_closure(self):
        order = self._draft_order()
        self._close_by_expiry(order)
        before = OrderQuoteClosure.objects.get(order=order)

        self._details(order)
        self._details(order)

        after = OrderQuoteClosure.objects.get(order=order)
        self.assertEqual(after.pk, before.pk)
        self.assertEqual(after.closed_at, before.closed_at)

    def test_the_read_writes_nothing_at_all(self):
        """Pinned generally rather than table by table, so a write to something
        nobody thought of fails too."""
        order = self._draft_order()
        self._close_by_expiry(order)
        with CaptureQueriesContext(connection) as captured:
            self._read(order=str(order.pk))
        for query in captured:
            self.assertRegex(
                query['sql'].strip().upper(), r'^(SELECT|SAVEPOINT|RELEASE)',
                query['sql'],
            )

    def test_a_closed_quote_can_never_be_accepted(self):
        """The control that makes the rest of this suite matter: publishing the
        closure changed nothing about what the closure DOES."""
        from orders_app.controllers.manage_order import update_order_status
        order = self._draft_order()
        self._close_by_expiry(order)

        refused = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
            capability=capability_from_table(self.table),
        )
        self.assertEqual(refused.get('status'), 400, refused)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)


class AReplayOfAClosedOrderSaysSoTests(ClosureReadFixture):
    """The OTHER surface that can hand a client a retired quote.

    `initiate` does not only create: a D04 replay returns the order the key was
    already used for, and that order's quote may have been retired since. Without
    the closure in `order_details`, a replay handed back a full review screen —
    lines, total, deadline — for a purchase that can no longer be placed, and the
    only way to discover that was to try to place it.
    """

    INITIATE_URL = '/api/v2/orders/initiate/'

    def _initiate(self, key):
        return self.client.post(
            self.INITIATE_URL,
            data=json.dumps({
                'items': [{'item': str(self.item.pk), 'quantity': 1}],
                'client_order_id': key,
            }),
            content_type='application/json',
            **self._session_header(),
        )

    def test_a_replay_publishes_the_closure_it_is_replaying(self):
        key = 'f47ac10b-58cc-4372-a567-0e02b2c3d481'
        first = self._initiate(key)
        self.assertEqual(first.status_code, 200, first.content)
        order = Order.objects.get(client_order_id=key)
        self._close_by_expiry(order)

        replay = self._initiate(key)
        self.assertEqual(replay.status_code, 200, replay.content)
        details = json.loads(replay.content.decode())['data']['order_details']
        self.assertIsNotNone(
            details['quote_closure'],
            'a replay must not present a retired quote as reviewable',
        )
        self.assertEqual(
            details['quote_closure']['reason'], quote_closure.REASON_EXPIRED)

    def test_a_fresh_draft_publishes_an_explicit_null(self):
        key = 'f47ac10b-58cc-4372-a567-0e02b2c3d482'
        response = self._initiate(key)
        details = json.loads(response.content.decode())['data']['order_details']
        self.assertIn('quote_closure', details)
        self.assertIsNone(details['quote_closure'])

    def test_the_replay_costs_no_second_statement_for_the_closure(self):
        """The re-read REPLACED a plain `refresh_from_db`, so the join is free —
        and, as on the diner read, folding it is what keeps the answer one
        snapshot rather than an order read in one statement and a closure in
        another."""
        key = 'f47ac10b-58cc-4372-a567-0e02b2c3d483'
        self._initiate(key)
        order = Order.objects.get(client_order_id=key)
        self._close_by_expiry(order)

        with CaptureQueriesContext(connection) as captured:
            self._initiate(key)
        standalone = [
            q['sql'] for q in captured
            if 'order_quote_closures' in q['sql']
            and ' JOIN ' not in q['sql'].upper()
        ]
        self.assertEqual(standalone, [])


class TheEnquiryAnswerSaysWhatItIsAboutTests(ClosureReadFixture):
    """D06 completion, G4 — the retire answer is CORRELATED to the enquiry.

    `quote_still_valid` is the answer that leads to SUBMITTING an order, and the
    answers named nothing at all: no order, no reference. A client had no way to
    establish that a 200 in its hand was the reply to the enquiry it sent, so a
    late or misrouted one read exactly like the right one. D04 closed this for
    acceptance answers; the enquiry was left behind.
    """

    def test_a_still_valid_answer_names_the_order_and_the_reference(self):
        order = self._draft_order()
        reference = quote_ref(order)
        answer = self._retire(order)

        self.assertEqual(answer['outcome'], 'quote_still_valid')
        self.assertEqual(answer['order'], str(order.pk))
        self.assertEqual(answer['quote_ref'], reference)

    def test_a_closed_answer_names_them_too(self):
        order = self._draft_order()
        reference = quote_ref(order)
        answer = self._close_by_expiry(order)

        self.assertEqual(answer['order'], str(order.pk))
        self.assertEqual(answer['quote_ref'], reference)

    def test_every_answer_states_the_level(self):
        order = self._draft_order()
        self.assertEqual(self._retire(order)['quote_protocol'], QUOTE_PROTOCOL)

    def test_the_echo_is_what_the_CALLER_named_beside_what_was_retired(self):
        """Two facts, both true, deliberately not collapsed. The echo says which
        request this answers; the closure says which reference the server really
        retired — and a caller naming a foreign one needs to see both to
        understand that the answer is not about the request it sent."""
        order = self._draft_order()
        retired = quote_ref(order)
        self._close_by_expiry(order)

        answer = self._retire(order, ref='0' * 64)
        self.assertEqual(answer['quote_ref'], '0' * 64)
        self.assertEqual(answer['quote_closure']['quote_ref'], retired)

    def test_a_business_refusal_is_correlated_as_well(self):
        """An acceptance-conflict answer is about a quote too, and a client that
        cannot tell which order it concerns cannot act on it."""
        order = self._draft_order()
        self.assertEqual(
            self._submit(order, capability=capability_from_table(self.table))
            .get('status'), 200)

        answer = self._retire(order)
        self.assertEqual(answer.get('status'), 409, answer)
        self.assertEqual(answer['order'], str(order.pk))

    def test_THE_OPAQUE_404_IS_NEVER_STAMPED(self):
        """The one refusal that must stay exactly two keys.

        Unknown, out of scope and revoked are indistinguishable in status AND in
        body by design; naming an order inside one would turn the channel's
        non-disclosing answer into the existence oracle it exists not to be. The
        rule is structural rather than a list of statuses — a body stating no
        `outcome` and no `reason` has said nothing about a quote, so there is
        nothing for it to be about.
        """
        order = self._draft_order()
        Table.objects.filter(pk=self.table.pk).update(
            status='out_of_service', is_active=False)

        from orders_app.controllers.manage_order import retire_quote_for_review
        from restaurants_app.controllers.diner_capability import (
            capability_from_table as cap,
        )
        refused = retire_quote_for_review(
            order, quote_ref(order), capability=cap(self.table))

        self.assertEqual(set(refused), {'status', 'message'}, refused)

    def test_the_correlation_costs_no_query(self):
        """It reads the order it was handed and a constant — nothing else."""
        order = self._draft_order()
        with CaptureQueriesContext(connection) as captured:
            self._retire(order)
        self.assertNotIn(
            'order_quote_closures',
            ''.join(q['sql'] for q in captured if ' JOIN ' not in q['sql'].upper()
                    and 'SELECT' not in q['sql'].upper()),
        )
