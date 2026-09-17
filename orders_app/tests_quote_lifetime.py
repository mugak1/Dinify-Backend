"""
D06 — the conditions under which a saved draft may become a newly accepted order.

Every defect these pin was REPRODUCED over real HTTP on unmodified `main` before
anything here was written, and each fix has a NEGATIVE CONTROL beside it — the
case that must keep working, so a rule cannot be "fixed" by refusing more.

The twelve dimensions, and where each lives:

  1. availability at creation ........ `PauseBindsBothBoundariesTests`
  2. availability at acceptance ...... `PauseBindsBothBoundariesTests`
  3. the staff exception ............. `TheStaffExceptionIsPreservedWhereItMeansSomethingTests`
  4. table / tenant liveness ......... `LivenessBindsEveryProvenanceTests`
  5. the quote lifetime .............. `TheQuoteLifetimeTests`
  6. an unreadable anchor ............ `AnUnreadableAnchorIsItsOwnAnswerTests`
  7. purchase integrity .............. `ThePurchaseMustStillBeThePurchaseTests`
  8. closure durability .............. `AClosureIsDurableAndTerminalTests`
  9. transient refusals close nothing  `ATransientRefusalClosesNothingTests`
 10. the retire-for-review path ...... `RetireForReviewTests`
 11. capability re-verification ...... `TheCapabilityIsRecheckedUnderTheLockTests`
 12. the response contract ........... `TheResponseContractTests`

WHAT IS NOT CLAIMED. Not that a quote reserves stock — it does not, and
dimension 7 is what answers the case where it sells out. Not that the deadline
is enforced at COMMIT: what the server establishes is that the deadline had not
passed when it DECIDED, which is a different and honest claim. And not that a
closure can be undone; it cannot, which is the whole point of writing one.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from dinify_backend.configss.string_definitions import (
    OrderStatus_Pending, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers.orders.serializers import (
    serialize_order_details,
)
from orders_app.controllers.manage_order import (
    retire_quote_for_review, update_order_status,
)
from orders_app.controllers.services import order_eligibility as eligibility
from orders_app.controllers.services import quote_closure, quote_policy
from orders_app.controllers.services.create_order import _create_order
from orders_app.controllers.services.checkout_protocol import CHECKOUT_PROTOCOL
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.controllers.services.purchase_integrity import (
    REASON_PURCHASE_NEEDS_REVIEW,
)
from orders_app.controllers.services.quote_protocol import QUOTE_PROTOCOL
from orders_app.models import Order, OrderAcceptance, OrderQuoteClosure
from restaurants_app.controllers.diner_capability import (
    capability_from_table, issue_table_session,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


class QuoteFixtureMixin:
    """One live restaurant, two orderable tables, one dish, one staff member.

    A MIXIN rather than a base class so the one case that must run in
    autocommit can pair it with `TransactionTestCase`. Inheriting from
    `TestCase` here would make that combination an MRO conflict, and working
    around it by duplicating the fixture is how two fixtures drift apart.
    """

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Quote', last_name='Owner', email='quote_owner@test.com',
            phone_number='256700000881', username='256700000881',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Quote R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
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

    def _draft(self, table=None, created_by=None, items=None):
        return _create_order(
            restaurant=self.restaurant, table=table or self.table,
            items=items or [{'item': str(self.item.pk), 'quantity': 1}],
            created_by=created_by,
        )

    def _draft_order(self, **kwargs):
        result = self._draft(**kwargs)
        self.assertEqual(result.get('status'), 200, result)
        return result['order']

    def _submit(self, order, ref=None, capability=None):
        return update_order_status(
            order, OrderStatus_Pending, None,
            quote_ref=ref if ref is not None else quote_ref(order),
            capability=capability,
        )

    def _age_draft(self, order, minutes):
        """Move a draft's anchor into the past.

        A direct UPDATE, because `time_created` is `auto_now_add` and nothing in
        production can move it — which is exactly the property that makes it the
        anchor. A fixture that could set it through an ordinary save would be
        testing a column this code does not have.
        """
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() - timedelta(minutes=minutes))
        order.refresh_from_db()
        return order


class QuoteFixture(QuoteFixtureMixin, TestCase):
    """The ordinary, fast base: one transaction per test, rolled back."""


# ---------------------------------------------------------------------------
# 1 + 2. A PAUSE PAUSES BOTH HALVES OF ORDER CREATION
# ---------------------------------------------------------------------------

class PauseBindsBothBoundariesTests(QuoteFixture):
    """`accepting_orders=False` used to stop roughly half of ordering.

    It was read once, in the controller preflight, on an instance loaded before
    any transaction opened — so it stopped NEW drafts and every draft already
    initiated still reached the kitchen. An owner reading "pause ordering" in
    settings was told something the switch did not do.
    """

    def test_a_pause_refuses_a_new_diner_draft(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        refused = self._draft()
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_RESTAURANT_PAUSED)

    def test_a_pause_that_lands_AFTER_the_draft_refuses_the_acceptance(self):
        """THE REGRESSION. The draft is legitimately created while the
        restaurant is open; the pause lands before submit."""
        order = self._draft_order()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        refused = self._submit(order)

        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_RESTAURANT_PAUSED)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)
        self.assertFalse(OrderAcceptance.objects.filter(order=order).exists())

    def test_the_negative_control_an_open_restaurant_still_accepts(self):
        order = self._draft_order()
        self.assertEqual(self._submit(order).get('status'), 200)

    def test_the_legacy_message_is_byte_identical(self):
        """The machine code is additive. A deployed client that reads the
        sentence and ignores `reason` must behave exactly as before."""
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        refused = self._draft()
        self.assertEqual(
            refused['message'],
            'This restaurant is not currently accepting orders',
        )


# ---------------------------------------------------------------------------
# 3. THE STAFF EXCEPTION — PRESERVED WHERE IT MEANS SOMETHING
# ---------------------------------------------------------------------------

class TheStaffExceptionIsPreservedWhereItMeansSomethingTests(QuoteFixture):
    """A pause stops the QR public. It does not stop a member of staff taking
    an order on somebody's behalf — that is what the control means, and D06
    deliberately does not widen it into an emergency stop."""

    def test_staff_may_create_while_the_restaurant_is_paused(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        result = self._draft(created_by=self.owner)
        self.assertEqual(result.get('status'), 200, result)

    def test_staff_may_create_at_a_menu_only_table(self):
        Table.objects.filter(pk=self.table.pk).update(qr_mode='menu_only')
        result = self._draft(created_by=self.owner)
        self.assertEqual(result.get('status'), 200, result)

    def test_a_staff_origin_draft_is_accepted_while_paused(self):
        order = self._draft_order(created_by=self.owner)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        self.assertEqual(self._submit(order).get('status'), 200)

    def test_provenance_is_the_ORDERS_not_the_submitters(self):
        """A diner's draft stays a diner's draft however senior the person who
        taps submit. `_submit_order` passes `order.created_by_id`, so a staff
        member submitting a diner draft does not convert it into a management
        action that walks past the pause."""
        order = self._draft_order()                     # created_by is None
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        refused = update_order_status(
            order, OrderStatus_Pending, self.owner, quote_ref=quote_ref(order))

        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_RESTAURANT_PAUSED)

    def test_the_exception_does_not_override_lifecycle_suspension(self):
        """A staff exception on the OPERATIONAL axis says nothing about the
        COMMERCIAL one. `order_admission` still refuses."""
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status='suspended')
        refused = self._draft(created_by=self.owner)
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertNotEqual(
            refused.get('reason'), eligibility.REASON_RESTAURANT_PAUSED)


# ---------------------------------------------------------------------------
# 4. LIVENESS BINDS EVERY PROVENANCE
# ---------------------------------------------------------------------------

class LivenessBindsEveryProvenanceTests(QuoteFixture):
    """The one behaviour change. A table that has been removed is not a place an
    order can exist, and that is true whoever is asking — unlike a pause, which
    is ordering policy for the public."""

    def _unusable(self, **fields):
        Table.objects.filter(pk=self.table.pk).update(**fields)

    def test_an_out_of_service_table_refuses_a_staff_draft(self):
        self._unusable(status='out_of_service', is_active=False)
        refused = self._draft(created_by=self.owner)
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_TABLE_UNAVAILABLE)

    def test_a_disabled_table_refuses_a_staff_draft(self):
        self._unusable(enabled=False)
        refused = self._draft(created_by=self.owner)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_TABLE_UNAVAILABLE)

    def test_a_table_removed_AFTER_the_draft_refuses_the_acceptance(self):
        order = self._draft_order(created_by=self.owner)
        self._unusable(status='out_of_service', is_active=False)

        refused = self._submit(order)

        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_TABLE_UNAVAILABLE)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_a_soft_deleted_restaurant_refuses_every_provenance(self):
        order = self._draft_order(created_by=self.owner)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)

        refused = self._submit(order)

        self.assertEqual(
            refused.get('reason'), eligibility.REASON_RESTAURANT_UNAVAILABLE)

    def test_liveness_is_answered_BEFORE_the_policy_gates(self):
        """A removed table is the more accurate answer than a pause, and it is
        true for everyone — so it is decided first and provenance-blind."""
        verdict = eligibility.evaluate(
            eligibility.OperationalFacts(
                accepting_orders=False,
                table_present=True,
                table_qr_mode='menu_only',
                table_scannable=False,
            ),
            created_by=None,
        )
        self.assertEqual(verdict.reason, eligibility.REASON_TABLE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 5. THE QUOTE LIFETIME
# ---------------------------------------------------------------------------

class TheQuoteLifetimeTests(QuoteFixture):
    """A draft priced last week used to be acceptable at last week's prices,
    indefinitely."""

    def test_a_fresh_quote_is_accepted(self):
        order = self._draft_order()
        self.assertEqual(self._submit(order).get('status'), 200)

    def test_a_quote_just_inside_the_window_is_accepted(self):
        order = self._age_draft(self._draft_order(), minutes=29)
        self.assertEqual(self._submit(order).get('status'), 200)

    def test_a_quote_past_the_window_is_refused(self):
        order = self._age_draft(self._draft_order(), minutes=31)
        refused = self._submit(order)
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), quote_policy.REASON_QUOTE_EXPIRED)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_the_exact_deadline_instant_is_expired(self):
        """A boundary needs one side, and refusing the exact instant is the
        side that never accepts a quote whose promise has run out."""
        anchor = timezone.now() - quote_policy.QUOTE_LIFETIME
        order = self._draft_order()
        Order.objects.filter(pk=order.pk).update(time_created=anchor)
        order.refresh_from_db()

        age = quote_policy.assess(order, anchor + quote_policy.QUOTE_LIFETIME)

        self.assertTrue(age.is_expired)
        self.assertFalse(age.is_live)

    def test_the_rule_reads_no_clock_of_its_own(self):
        """`assess` takes `now` so the requirement that the comparison happen
        AFTER the caller's lock waits cannot be quietly bypassed."""
        order = self._draft_order()
        past = order.time_created + timedelta(minutes=1)
        future = order.time_created + timedelta(minutes=60)
        self.assertTrue(quote_policy.assess(order, past).is_live)
        self.assertTrue(quote_policy.assess(order, future).is_expired)

    def test_an_existing_draft_gets_the_rule_from_the_column_it_already_has(self):
        """NO NEW COLUMN and no backfill: the deadline is derived, so a draft
        written before this policy existed is governed by it from `time_created`
        and a rollback removes the rule rather than stranding data."""
        order = self._age_draft(self._draft_order(), minutes=45)
        self.assertEqual(
            quote_policy.deadline_for(order.time_created, now=timezone.now()),
            order.time_created + quote_policy.QUOTE_LIFETIME,
        )


# ---------------------------------------------------------------------------
# 6. AN UNREADABLE ANCHOR IS NEITHER FRESH NOR EXPIRED
# ---------------------------------------------------------------------------

class AnUnreadableAnchorIsItsOwnAnswerTests(QuoteFixture):
    """Treating it as fresh grants an indefinite quote; treating it as expired
    permanently retires a diner's quote over a data fault they did not cause.
    It is a statement of ignorance and gets its own answer."""

    def test_a_missing_anchor_is_unavailable(self):
        order = self._draft_order()
        order.time_created = None
        age = quote_policy.assess(order, timezone.now())
        self.assertTrue(age.is_unavailable)
        self.assertIsNone(age.expires_at)

    def test_an_anchor_far_in_the_future_is_unavailable(self):
        order = self._draft_order()
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() + timedelta(hours=2))
        order.refresh_from_db()
        self.assertTrue(quote_policy.assess(order, timezone.now()).is_unavailable)

    def test_ordinary_clock_skew_is_still_live(self):
        """The negative control: a stamp a few seconds ahead is ordinary skew
        between the application server and the database, not a fault."""
        order = self._draft_order()
        skewed = timezone.now() + timedelta(seconds=5)
        Order.objects.filter(pk=order.pk).update(time_created=skewed)
        order.refresh_from_db()
        self.assertTrue(quote_policy.assess(order, timezone.now()).is_live)

    def test_it_is_refused_at_acceptance_and_closes_NOTHING(self):
        order = self._draft_order()
        ref = quote_ref(order)
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() + timedelta(hours=2))
        order.refresh_from_db()

        refused = self._submit(order, ref)

        self.assertEqual(
            refused.get('reason'), quote_policy.REASON_QUOTE_UNVERIFIABLE)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'ignorance must never be converted into a terminal fact',
        )


# ---------------------------------------------------------------------------
# 7. THE PURCHASE MUST STILL BE THE PURCHASE
# ---------------------------------------------------------------------------

class ThePurchaseMustStillBeThePurchaseTests(QuoteFixture):
    """No catalogue fact was re-read at acceptance, so a draft priced against a
    dish that had since changed was accepted unchanged and the kitchen worked
    from a definition nobody had agreed to."""

    def test_a_sold_out_dish_refuses_the_acceptance(self):
        order = self._draft_order()
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)

        refused = self._submit(order)

        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(refused.get('reason'), REASON_PURCHASE_NEEDS_REVIEW)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_an_unpublished_dish_refuses_the_acceptance(self):
        order = self._draft_order()
        MenuItem.objects.filter(pk=self.item.pk).update(approved=False)
        self.assertEqual(
            self._submit(order).get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

    def test_a_deleted_dish_refuses_the_acceptance(self):
        order = self._draft_order()
        MenuItem.objects.filter(pk=self.item.pk).update(deleted=True)
        self.assertEqual(
            self._submit(order).get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

    def test_a_deleted_section_refuses_the_acceptance(self):
        order = self._draft_order()
        MenuSection.objects.filter(pk=self.section.pk).update(deleted=True)
        self.assertEqual(
            self._submit(order).get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

    def test_a_STAFF_origin_order_still_bypasses_publication(self):
        """The negative control, and it is the same rule `_create_order`
        applied when the draft was written: a staff order bypasses publication
        exactly as it did at creation, and never bypasses stock or tenancy."""
        order = self._draft_order(created_by=self.owner)
        MenuItem.objects.filter(pk=self.item.pk).update(approved=False)
        self.assertEqual(self._submit(order).get('status'), 200)

    def test_a_STAFF_origin_order_does_NOT_bypass_stock(self):
        order = self._draft_order(created_by=self.owner)
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)
        self.assertEqual(
            self._submit(order).get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

    def test_a_PRICE_change_does_NOT_refuse_the_acceptance(self):
        """THE PROMISE. The lifetime honours the MONETARY figures the diner
        reviewed; what integrity refuses is preparing food from a definition
        that changed. A repriced dish inside the window is accepted at the
        SAVED amount, and the saved amount does not move."""
        order = self._draft_order()
        before = order.actual_cost
        MenuItem.objects.filter(pk=self.item.pk).update(
            primary_price=Decimal('99000'))

        self.assertEqual(self._submit(order).get('status'), 200)

        order.refresh_from_db()
        self.assertEqual(order.actual_cost, before)

    def test_an_unchanged_menu_still_accepts(self):
        order = self._draft_order()
        self.assertEqual(self._submit(order).get('status'), 200)


# ---------------------------------------------------------------------------
# 8. A CLOSURE IS DURABLE AND TERMINAL
# ---------------------------------------------------------------------------

class AClosureIsDurableAndTerminalTests(QuoteFixture):
    """A refusal message is one process's opinion at one moment. The row is what
    makes minting a replacement quote safe: an acceptance already in flight for
    the old one can never execute afterwards."""

    def _expire_and_submit(self):
        order = self._age_draft(self._draft_order(), minutes=45)
        ref = quote_ref(order)
        return order, ref, self._submit(order, ref)

    def test_expiry_writes_exactly_one_closure_naming_the_reviewed_quote(self):
        order, ref, refused = self._expire_and_submit()
        self.assertEqual(
            refused.get('reason'), quote_policy.REASON_QUOTE_EXPIRED)
        closure = OrderQuoteClosure.objects.get(order=order)
        self.assertEqual(closure.reason, quote_closure.REASON_EXPIRED)
        self.assertEqual(closure.quote_ref, ref)
        self.assertEqual(closure.policy_version, quote_policy.QUOTE_POLICY_VERSION)

    def test_a_retry_after_a_closure_is_refused_definitively(self):
        order, ref, _ = self._expire_and_submit()
        again = self._submit(order, ref)
        self.assertEqual(again.get('reason'), quote_closure.REASON_QUOTE_CLOSED)
        self.assertEqual(OrderQuoteClosure.objects.filter(order=order).count(), 1)

    def test_a_closure_survives_the_condition_going_away(self):
        """THE POINT OF THE ROW. Stock comes back; a closed quote does not.
        Without the record, a queued acceptance for the old quote would execute
        the moment the dish returned."""
        order = self._draft_order()
        ref = quote_ref(order)
        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=False)
        self.assertEqual(
            self._submit(order, ref).get('reason'), REASON_PURCHASE_NEEDS_REVIEW)

        MenuItem.objects.filter(pk=self.item.pk).update(in_stock=True)

        again = self._submit(order, ref)
        self.assertEqual(again.get('reason'), quote_closure.REASON_QUOTE_CLOSED)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_a_closure_is_never_written_beside_an_acceptance(self):
        order = self._draft_order()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        self._age_draft(order, minutes=90)

        replay = self._submit(order, ref)

        self.assertEqual(replay.get('status'), 200, replay)
        self.assertTrue(replay['idempotent'])
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_the_vocabulary_is_frozen(self):
        """A transient refusal must never be spellable as a closure reason."""
        order = self._draft_order()
        with self.assertRaises(ValueError):
            quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=eligibility.REASON_RESTAURANT_PAUSED,
                now=timezone.now(),
            )



# ---------------------------------------------------------------------------
# 9. A TRANSIENT REFUSAL CLOSES NOTHING
# ---------------------------------------------------------------------------

class ATransientRefusalClosesNothingTests(QuoteFixture):
    """A restaurant that paused, a table taken out of service, a busy table —
    none of these says the purchase is finished. Closing on one would destroy a
    perfectly good quote and force a reprice the diner never asked for."""

    def _assert_no_closure(self, order, refused, reason):
        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(refused.get('reason'), reason)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_a_pause_closes_nothing(self):
        order = self._draft_order()
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        self._assert_no_closure(
            order, self._submit(order), eligibility.REASON_RESTAURANT_PAUSED)

    def test_an_out_of_service_table_closes_nothing(self):
        order = self._draft_order()
        Table.objects.filter(pk=self.table.pk).update(
            status='out_of_service', is_active=False)
        self._assert_no_closure(
            order, self._submit(order), eligibility.REASON_TABLE_UNAVAILABLE)

    def test_the_quote_survives_a_transient_refusal(self):
        """The whole reason they are kept apart: the diner waits, the
        restaurant resumes, and the SAME quote is accepted."""
        order = self._draft_order()
        ref = quote_ref(order)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)
        self.assertEqual(self._submit(order, ref).get('status'), 400)

        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=True)

        self.assertEqual(self._submit(order, ref).get('status'), 200)

    def test_every_operational_reason_is_declared_transient(self):
        self.assertEqual(
            eligibility.TRANSIENT_REASONS,
            {
                eligibility.REASON_RESTAURANT_PAUSED,
                eligibility.REASON_RESTAURANT_UNAVAILABLE,
                eligibility.REASON_TABLE_ORDERING_UNAVAILABLE,
                eligibility.REASON_TABLE_UNAVAILABLE,
            },
        )


# ---------------------------------------------------------------------------
# 10. THE RETIRE-FOR-REVIEW PATH
# ---------------------------------------------------------------------------

class RetireForReviewTests(QuoteFixture):
    """The second entry path. It exists because both alternatives are wrong:
    minting a replacement unilaterally leaves the old quote acceptable, and
    attempting an acceptance to read the refusal SUCCEEDS when the quote is
    fine — claiming a table and sending food to a kitchen to ask a question."""

    def test_a_good_quote_is_reported_valid_and_NOTHING_is_written(self):
        order = self._draft_order()
        result = retire_quote_for_review(order, quote_ref(order))
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['outcome'], quote_closure.OUTCOME_STILL_VALID)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'initiated')

    def test_a_good_quote_is_still_acceptable_afterwards(self):
        """It is NOT a discard verb, and cannot be used as one."""
        order = self._draft_order()
        ref = quote_ref(order)
        retire_quote_for_review(order, ref)
        self.assertEqual(self._submit(order, ref).get('status'), 200)

    def test_an_expired_quote_is_retired(self):
        order = self._age_draft(self._draft_order(), minutes=45)
        ref = quote_ref(order)
        result = retire_quote_for_review(order, ref)
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['outcome'], quote_closure.OUTCOME_CLOSED)
        self.assertEqual(result['reason'], quote_policy.REASON_QUOTE_EXPIRED)
        self.assertTrue(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_a_retired_quote_can_no_longer_be_accepted(self):
        """THE POINT. Once this returns, minting a replacement is safe."""
        order = self._age_draft(self._draft_order(), minutes=45)
        ref = quote_ref(order)
        retire_quote_for_review(order, ref)
        self.assertEqual(
            self._submit(order, ref).get('reason'),
            quote_closure.REASON_QUOTE_CLOSED,
        )

    def test_a_repeat_returns_the_ORIGINAL_row(self):
        order = self._age_draft(self._draft_order(), minutes=45)
        ref = quote_ref(order)
        first = retire_quote_for_review(order, ref)
        again = retire_quote_for_review(order, ref)
        self.assertEqual(again['outcome'], quote_closure.OUTCOME_ALREADY_CLOSED)
        self.assertEqual(
            again['quote_closure']['closed_at'],
            first['quote_closure']['closed_at'],
        )
        self.assertEqual(OrderQuoteClosure.objects.filter(order=order).count(), 1)

    def test_an_accepted_order_has_nothing_to_retire(self):
        order = self._draft_order()
        ref = quote_ref(order)
        self.assertEqual(self._submit(order, ref).get('status'), 200)

        result = retire_quote_for_review(order, ref)

        self.assertEqual(result.get('status'), 409, result)
        self.assertEqual(
            result.get('reason'), quote_closure.REASON_ALREADY_ACCEPTED)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_a_stale_reference_retires_nothing(self):
        """A closure retires ONE named reference. A caller who cannot name this
        order's saved quote is told it changed and re-reads it."""
        order = self._age_draft(self._draft_order(), minutes=45)
        result = retire_quote_for_review(order, 'not-the-saved-quote')
        self.assertEqual(result.get('status'), 400, result)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_it_works_while_the_restaurant_is_PAUSED(self):
        """The asymmetry is the design, and it is the same one the admin
        plane's owner-invitation cancel draws: a paused restaurant is exactly
        when a client most needs to establish that its held quote is dead."""
        order = self._age_draft(self._draft_order(), minutes=45)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            accepting_orders=False)

        result = retire_quote_for_review(order, quote_ref(order))

        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['outcome'], quote_closure.OUTCOME_CLOSED)


# ---------------------------------------------------------------------------
# 11. THE CAPABILITY IS RE-CHECKED UNDER THE LOCK
# ---------------------------------------------------------------------------

class TheCapabilityIsRecheckedUnderTheLockTests(QuoteFixture):
    """The endpoint resolves the session in autocommit and the transition then
    waits for three locks. A QR regeneration inside that wait revokes the
    session, and nothing downstream knew what generation had been presented."""

    def test_a_regenerated_qr_revokes_an_in_flight_submission(self):
        order = self._draft_order()
        capability = capability_from_table(self.table)
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)

        refused = self._submit(order, capability=capability)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertEqual(refused.get('message'), 'Not found.')
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)

    def test_the_refusal_is_the_channels_own_opaque_404(self):
        """It must not become an oracle: a revocation landing mid-request is
        indistinguishable from a session that never resolved."""
        order = self._draft_order()
        capability = capability_from_table(self.table)
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)
        refused = self._submit(order, capability=capability)
        self.assertEqual(set(refused), {'status', 'message'})

    def test_a_current_capability_still_accepts(self):
        order = self._draft_order()
        self.assertEqual(
            self._submit(order, capability=capability_from_table(self.table))
            .get('status'),
            200,
        )

    def test_a_staff_caller_carries_none_and_is_unaffected(self):
        """A staff caller's authority is the module gate, which a QR
        regeneration does not revoke."""
        order = self._draft_order(created_by=self.owner)
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 5)
        self.assertEqual(self._submit(order, capability=None).get('status'), 200)

    def test_a_revoked_capability_cannot_even_read_an_acceptance(self):
        """Authorization comes FIRST, ahead of the D04 replay: answering
        "already placed" would disclose that an order exists."""
        order = self._draft_order()
        ref = quote_ref(order)
        capability = capability_from_table(self.table)
        self.assertEqual(self._submit(order, ref).get('status'), 200)
        Table.objects.filter(pk=self.table.pk).update(
            qr_version=self.table.qr_version + 1)

        refused = self._submit(order, ref, capability=capability)

        self.assertEqual(refused.get('status'), 404, refused)
        self.assertNotIn('checkout', refused)

    def test_it_re_checks_the_generation_and_NOT_table_availability(self):
        """One fact must not get two answers depending on how the caller
        authenticated. An out-of-service table is an OPERATIONAL refusal a diner
        can read, not a forged-session 404."""
        order = self._draft_order()
        capability = capability_from_table(self.table)
        Table.objects.filter(pk=self.table.pk).update(
            status='out_of_service', is_active=False)

        refused = self._submit(order, capability=capability)

        self.assertEqual(refused.get('status'), 400, refused)
        self.assertEqual(
            refused.get('reason'), eligibility.REASON_TABLE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 12. THE RESPONSE CONTRACT
# ---------------------------------------------------------------------------

class TheResponseContractTests(QuoteFixture):

    def _details(self, order):
        return serialize_order_details(order=order)['order']

    def test_the_order_read_publishes_the_deadline(self):
        order = self._draft_order()
        policy = self._details(order)['quote_policy']
        self.assertEqual(policy['version'], quote_policy.QUOTE_POLICY_VERSION)
        self.assertEqual(policy['status'], quote_policy.QUOTE_LIVE)
        self.assertEqual(
            policy['expires_at'],
            (order.time_created + quote_policy.QUOTE_LIFETIME).isoformat(),
        )

    def test_the_read_publishes_the_quote_protocol_level(self):
        order = self._draft_order()
        self.assertEqual(self._details(order)['quote_protocol'], QUOTE_PROTOCOL)

    def test_checkout_protocol_is_UNCHANGED_by_D06(self):
        """They answer different questions and a client can want either without
        the other. Raising level 3 for a change that added nothing to what it
        promises would be #661 in the direction that matters most."""
        order = self._draft_order()
        self.assertEqual(self._details(order)['checkout_protocol'], 3)
        self.assertEqual(CHECKOUT_PROTOCOL, 3)

    def test_the_two_levels_are_separate_numbers(self):
        self.assertIsNot(QUOTE_PROTOCOL, CHECKOUT_PROTOCOL)

    def test_an_expired_draft_reads_as_expired_without_being_retired(self):
        """The read is ADVISORY: it takes no lock, opens no transaction and
        writes nothing. The DECISION happens inside the acceptance transaction."""
        order = self._age_draft(self._draft_order(), minutes=45)
        self.assertEqual(
            self._details(order)['quote_policy']['status'],
            quote_policy.QUOTE_EXPIRED,
        )
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_every_terminal_refusal_carries_its_closure(self):
        order = self._age_draft(self._draft_order(), minutes=45)
        refused = self._submit(order, quote_ref(order))
        self.assertIn('quote_closure', refused)
        self.assertEqual(
            set(refused['quote_closure']),
            {'closed_at', 'reason', 'quote_ref', 'policy_version'},
        )

    def test_a_refusal_never_looks_like_an_acceptance(self):
        order = self._age_draft(self._draft_order(), minutes=45)
        refused = self._submit(order, quote_ref(order))
        self.assertNotIn('checkout', refused)
        self.assertEqual(refused['status'], 400)


class ClosingOutsideATransactionRaisesTests(
        QuoteFixtureMixin, TransactionTestCase):
    """A `TransactionTestCase`, and it has to be.

    An ordinary `TestCase` wraps every test in a transaction, so
    `in_atomic_block` is True throughout and this assertion would pass against a
    service that had no guard at all — the same trap
    `tests_table_allocation_lock` records from the other side. Running in
    autocommit is the only way to exercise it.
    """

    reset_sequences = True

    def test_closing_outside_a_transaction_raises(self):
        """A closure decided in autocommit, beside an acceptance still deciding,
        is exactly the double purchase the row exists to prevent — and that
        failure is SILENT, so it is asserted rather than assumed."""
        order = self._draft_order()
        with self.assertRaises(RuntimeError):
            quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=quote_closure.REASON_EXPIRED, now=timezone.now(),
            )

    def test_the_negative_control_inside_one_it_writes(self):
        """The guard must refuse autocommit, not refuse everything."""
        order = self._draft_order()
        with transaction.atomic():
            result = quote_closure.close(
                order, quote_ref=quote_ref(order),
                reason=quote_closure.REASON_EXPIRED, now=timezone.now(),
            )
        self.assertEqual(result.outcome, quote_closure.OUTCOME_CLOSED)
        self.assertTrue(OrderQuoteClosure.objects.filter(order=order).exists())


class RetiringSaysNothingItDidNotDoTests(QuoteFixture):
    """The retire route answers 200 ONLY when a closure was actually written.

    It used to infer the outcome from whether a closure object happened to be
    present, which made an anchor whose age could not be established come back
    `quote_still_valid` — the quote is not valid, it is unjudgeable, and telling
    a client it stands is the one answer this route exists to get right.
    """

    def test_an_unreadable_anchor_is_NOT_reported_as_still_valid(self):
        order = self._draft_order()
        ref = quote_ref(order)
        Order.objects.filter(pk=order.pk).update(
            time_created=timezone.now() + timedelta(hours=2))
        order.refresh_from_db()

        result = retire_quote_for_review(order, ref)

        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(
            result.get('reason'), quote_policy.REASON_QUOTE_UNVERIFIABLE)
        self.assertNotIn('outcome', result)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def test_a_real_retirement_still_answers_200(self):
        """The negative control: narrowing the claim must not withdraw the one
        answer the route is for."""
        order = self._age_draft(self._draft_order(), minutes=45)
        result = retire_quote_for_review(order, quote_ref(order))
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['outcome'], quote_closure.OUTCOME_CLOSED)
