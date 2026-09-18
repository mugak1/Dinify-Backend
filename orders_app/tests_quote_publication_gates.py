"""
D06 completion, G2-C — WHICH CATALOGUE FACTS BIND WHICH PROVENANCE AT
ACCEPTANCE, AND WHY THE SAVED PRICE IS NOT ONE OF THEM.

`purchase_integrity`'s own docstring already states both rules. The code did not
implement either, and in each case the gap is destructive rather than merely
strict, because `purchase_needs_review` is one of the two reasons that CLOSE a
quote — a durable, irreversible record that this saved quote may never be
accepted through any channel.

  "STAFF ORIGIN CHANGES EXACTLY ONE THING. Publication and scheduling are
   skipped for a staff-origin order, mirroring `enforce_publication` on the
   create path, because those orders never passed that gate in the first place."

`_check_parent` called `section_structurally_published` and
`group_structurally_published` UNCONDITIONALLY, and those predicates are
`approved AND enabled AND not deleted` — two thirds publication. So the exact
order the exemption exists for, a member of staff taking an order against a menu
that has not been approved yet (the Phase-1 rehearsal `CAP_ORDER_CREATE` keeps
open at `onboarding`), was created happily and then refused AND CLOSED at
submit. The draft could not be accepted, and no replacement could be accepted
either while the section stayed unapproved.

  "It does NOT ask what they would cost now: an unexpired, otherwise-valid quote
   is honoured at the saved amounts, and nothing here reprices, re-reads a
   discount window for money, or replaces a saved figure."

`item_orderable` calls `item_priceable`, which is `item.price_verdict(now).usable`
— a live read of `primary_price` and `discount_details` against the current
clock. An operator mistyping a discount AFTER the diner reviewed their order
therefore destroyed that quote, over a figure nobody was going to charge: the
saved amount is what the diner agreed to and what acceptance honours. It is the
same principle `quote_unverifiable` records — never refuse a diner over a data
fault they did not cause.

THE SPLIT THIS FILE PINS. Deletion is LIVENESS and binds EVERY provenance, for
the reason D06 already gives about a table: a soft-deleted section is not a place
a dish can exist. `approved` / `enabled` / `available` / the schedule are
PUBLICATION POLICY FOR THE QR PUBLIC and bind the diner path only — which is
exactly the line the create path draws. Price READABILITY is neither: it is a
question about money, and this boundary honours the money it saved.

WHAT IS NOT WIDENED. Stock, tenant scope, item deletion, the extras
relationship, selection validity and allergens still bind every provenance, and
every negative control below says so. Creating a NEW order against an unpriceable
item is still refused — the create path has to price it, and `item_orderable` is
unchanged for that caller.
"""
from decimal import Decimal

from django.test import TestCase

from dinify_backend.configss.string_definitions import OrderStatus_Pending
from orders_app.controllers.services.purchase_integrity import (
    REASON_PURCHASE_NEEDS_REVIEW,
)
from orders_app.models import OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin
from restaurants_app.models import MenuItem, MenuSection, SectionGroup

#: A discount whose magnitude cannot be read, scheduled with no bounds so it is
#: LIVE now — which is the only shape that makes an item unpriceable. An expired
#: broken discount is simply not applied.
UNREADABLE_DISCOUNT = {'discount_percentage': 'abc', 'recurring_days': []}


class PublicationGateFixture(QuoteFixtureMixin, TestCase):
    """The shared fixture plus the two provenances, stated once."""

    def _staff_draft(self, **kwargs):
        return self._draft_order(created_by=self.owner, **kwargs)

    def _diner_draft(self, **kwargs):
        return self._draft_order(**kwargs)

    def _section(self, **fields):
        MenuSection.objects.filter(pk=self.section.pk).update(**fields)

    def _item(self, **fields):
        MenuItem.objects.filter(pk=self.item.pk).update(**fields)

    def _assert_accepted(self, order):
        result = self._submit(order)
        self.assertEqual(result.get('status'), 200, result)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.assertFalse(
            OrderQuoteClosure.objects.filter(order=order).exists(),
            'an accepted order must not also have had its quote closed',
        )
        return result

    def _assert_needs_review(self, order):
        result = self._submit(order)
        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(result.get('reason'), REASON_PURCHASE_NEEDS_REVIEW)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)
        return result


class TheStaffExemptionSurvivesToAcceptanceTests(PublicationGateFixture):
    """The exemption the create path grants must still be there at submit.

    An exemption that is granted at creation and withdrawn at acceptance is not
    an exemption: it is a trap that produces an order which can never be placed,
    and — because the refusal closes the quote — cannot be replaced either.
    """

    def test_the_control_staff_may_create_under_an_unapproved_section(self):
        """The premise. If this ever stops being true the tests below are
        asserting something the create path no longer allows."""
        self._section(approved=False)
        self._staff_draft()

    def test_an_unapproved_section_does_not_strand_a_staff_draft(self):
        """THE REHEARSAL CASE — a menu not yet approved, at `onboarding`."""
        order = self._staff_draft()
        self._section(approved=False)
        self._assert_accepted(order)

    def test_an_unapproved_section_at_creation_too(self):
        """The same fact in the order it actually happens: the section was
        never approved, and staff ordered against it deliberately."""
        self._section(approved=False)
        order = self._staff_draft()
        self._assert_accepted(order)

    def test_a_disabled_section_does_not_strand_a_staff_draft(self):
        order = self._staff_draft()
        self._section(enabled=False)
        self._assert_accepted(order)

    def test_an_unavailable_section_does_not_strand_a_staff_draft(self):
        order = self._staff_draft()
        self._section(available=False)
        self._assert_accepted(order)

    def test_an_out_of_hours_schedule_does_not_strand_a_staff_draft(self):
        """Scheduling is ordering policy for the QR public. Staff take orders
        outside advertised hours — that is what the exemption is for."""
        order = self._staff_draft()
        self._section(
            availability='scheduled',
            schedules=[{'days': [], 'startTime': '00:00', 'endTime': '00:01'}],
        )
        self._assert_accepted(order)

    def test_an_unapproved_item_does_not_strand_a_staff_draft(self):
        order = self._staff_draft()
        self._item(approved=False)
        self._assert_accepted(order)

    def test_a_disabled_item_does_not_strand_a_staff_draft(self):
        order = self._staff_draft()
        self._item(enabled=False)
        self._assert_accepted(order)

    def test_an_unapproved_group_does_not_strand_a_staff_draft(self):
        group = SectionGroup.objects.create(
            name='Grill', section=self.section,
            approved=True, enabled=True, available=True,
        )
        self._item(section_group=group)
        order = self._staff_draft()
        SectionGroup.objects.filter(pk=group.pk).update(approved=False)
        self._assert_accepted(order)


class ThePublicationGateStillBindsTheDinerTests(PublicationGateFixture):
    """The negative controls. Nothing above may be reached by a diner."""

    def test_an_unapproved_section_still_sends_a_diner_draft_for_review(self):
        order = self._diner_draft()
        self._section(approved=False)
        self._assert_needs_review(order)

    def test_a_disabled_section_still_sends_a_diner_draft_for_review(self):
        order = self._diner_draft()
        self._section(enabled=False)
        self._assert_needs_review(order)

    def test_an_unapproved_item_still_sends_a_diner_draft_for_review(self):
        order = self._diner_draft()
        self._item(approved=False)
        self._assert_needs_review(order)

    def test_an_unavailable_section_still_sends_a_diner_draft_for_review(self):
        order = self._diner_draft()
        self._section(available=False)
        self._assert_needs_review(order)


class DeletionIsLivenessAndBindsEveryProvenanceTests(PublicationGateFixture):
    """A soft-deleted section is not a place a dish can exist.

    The same reasoning D06 already applies to a table, and the same reasoning
    `menu_item.deleted` already gets one line above in `_check_parent`. Widening
    the staff exemption must not take this with it.
    """

    def test_a_soft_deleted_section_refuses_a_staff_draft(self):
        order = self._staff_draft()
        self._section(deleted=True)
        self._assert_needs_review(order)

    def test_a_soft_deleted_section_refuses_a_diner_draft(self):
        order = self._diner_draft()
        self._section(deleted=True)
        self._assert_needs_review(order)

    def test_a_soft_deleted_group_refuses_a_staff_draft(self):
        group = SectionGroup.objects.create(
            name='Grill', section=self.section,
            approved=True, enabled=True, available=True,
        )
        self._item(section_group=group)
        order = self._staff_draft()
        SectionGroup.objects.filter(pk=group.pk).update(deleted=True)
        self._assert_needs_review(order)

    def test_a_soft_deleted_item_refuses_a_staff_draft(self):
        """Already true before this change; pinned so it stays true."""
        order = self._staff_draft()
        self._item(deleted=True)
        self._assert_needs_review(order)

    def test_a_sold_out_item_refuses_a_staff_draft(self):
        """Stock binds every provenance too: authorization to take an order is
        not authorization to prepare a dish the kitchen has run out of."""
        order = self._staff_draft()
        self._item(in_stock=False)
        self._assert_needs_review(order)


class TheExtrasRELATIONSHIPIsNotPartOfTheExemptionTests(PublicationGateFixture):
    """Extras integrity binds every provenance, at BOTH boundaries.

    `extra_publishable` is structural inheritance, and `validate_order_selections`
    applies it to every caller at creation — "management authorization is not
    license to create a structurally invalid order graph". Acceptance agrees, so
    widening the publication exemption must not take this with it: the two
    boundaries would then disagree about the same order.
    """

    def _with_extra(self):
        extra = MenuItem.objects.create(
            name='Chips', section=self.section, primary_price=Decimal('2000'),
            approved=True, enabled=True, available=True, in_stock=True,
            is_extra=True,
        )
        self._item(has_extras=True, extras_applicable=[str(extra.pk)])
        return extra

    def test_creation_already_refuses_a_staff_order_with_an_unpublished_extra(self):
        """The premise: this is not a rule acceptance invented."""
        extra = self._with_extra()
        MenuItem.objects.filter(pk=extra.pk).update(approved=False)
        refused = self._draft(created_by=self.owner, items=[{
            'item': str(self.item.pk), 'quantity': 1,
            'extras': [str(extra.pk)],
        }])
        self.assertEqual(refused.get('status'), 400, refused)

    def test_acceptance_refuses_a_staff_order_whose_extra_became_unpublished(self):
        extra = self._with_extra()
        order = self._staff_draft(items=[{
            'item': str(self.item.pk), 'quantity': 1,
            'extras': [str(extra.pk)],
        }])
        MenuItem.objects.filter(pk=extra.pk).update(approved=False)
        self._assert_needs_review(order)

    def test_acceptance_refuses_a_staff_order_whose_extra_was_dropped(self):
        extra = self._with_extra()
        order = self._staff_draft(items=[{
            'item': str(self.item.pk), 'quantity': 1,
            'extras': [str(extra.pk)],
        }])
        self._item(extras_applicable=[])
        self._assert_needs_review(order)


class ASavedQuoteIsHonouredAtItsSavedPriceTests(PublicationGateFixture):
    """Price READABILITY is a question about money, and this boundary honours
    the money it saved.

    The diner agreed to an amount; that amount is stored on the order and is
    what acceptance charges. An operator mistyping a discount afterwards changes
    nothing about what will be prepared or what will be paid — so destroying the
    quote over it refuses a diner for a data fault they did not cause, and does
    so irreversibly.
    """

    def test_an_unreadable_current_discount_does_not_destroy_a_diner_quote(self):
        order = self._diner_draft()
        self._item(discount_details=UNREADABLE_DISCOUNT)
        self._assert_accepted(order)

    def test_an_unreadable_current_discount_does_not_destroy_a_staff_quote(self):
        order = self._staff_draft()
        self._item(discount_details=UNREADABLE_DISCOUNT)
        self._assert_accepted(order)

    def test_the_saved_amount_is_what_is_accepted(self):
        """The point of honouring it: the figure does not move."""
        order = self._diner_draft()
        before = order.actual_cost
        self._item(discount_details=UNREADABLE_DISCOUNT)
        self._assert_accepted(order)
        order.refresh_from_db()
        self.assertEqual(order.actual_cost, before)

    def test_an_unreadable_discount_on_an_EXTRA_does_not_destroy_the_quote(self):
        extra = MenuItem.objects.create(
            name='Chips', section=self.section, primary_price=Decimal('2000'),
            approved=True, enabled=True, available=True, in_stock=True,
            is_extra=True,
        )
        self._item(has_extras=True, extras_applicable=[str(extra.pk)])
        order = self._diner_draft(items=[{
            'item': str(self.item.pk), 'quantity': 1,
            'extras': [str(extra.pk)],
        }])
        MenuItem.objects.filter(pk=extra.pk).update(
            discount_details=UNREADABLE_DISCOUNT)
        self._assert_accepted(order)

    def test_the_control_a_NEW_order_against_it_is_still_refused(self):
        """Creation has to PRICE the line, so it must still refuse. This is the
        rule that would be broken by 'fixing' `item_orderable` itself instead of
        asking a narrower question at acceptance."""
        self._item(discount_details=UNREADABLE_DISCOUNT)
        refused = self._draft()
        self.assertEqual(refused.get('status'), 400, refused)
