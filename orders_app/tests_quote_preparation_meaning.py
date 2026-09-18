"""
D06 completion, G2-B — THE SELECTION IS COMPARED BY WHAT IT SAYS, NOT ONLY BY
WHICH IDS IT NAMES.

`purchase_integrity` stated the gap in its own docstring rather than closing it:

  "an operator who edits a choice's LABEL so that the same id now means
   something else has changed the preparation in a way no stored field records,
   and this check cannot see it."

The premise was wrong: a stored field DOES record it. `OrderItem.modifiers_
snapshot` is the resolved human-readable selection — `"Size: Large"` — written
at creation, immutable afterwards, ALREADY inside the quote fingerprint, and it
is literally what the kitchen ticket renders (`serializers_kitchen._line` emits
it as `modifiers`). So "no onions" relabelled to "extra chilli" under the same
id is a change to the exact text the kitchen prepares from, and acceptance can
see it by re-deriving that text from the saved selection against the catalogue
as it is now.

RE-DERIVED FROM THE SAVED SELECTION, NEVER FROM THE CURRENT MENU'S IDEA OF THE
ORDER. The comparison reads the saved `selected_modifiers` — the ids the diner
actually chose, canonicalised at creation — and asks what those ids say today.
Nothing about today's catalogue is treated as past intent, and no column is
added or backfilled.

WHAT IS IN SCOPE, AND WHY IT STOPS THERE. Only the SELECTED groups' operational
text. The dish's own name is deliberately excluded: the approved decision table
accepts a rename, a dish rename is overwhelmingly cosmetic, and comparing it
would refuse an order for every menu tidy-up. A renamed GROUP does trip the
comparison, and that is the same deliberate false positive the module already
records for a renamed allergen tag — the safe direction of the only error this
comparison can make, bounded to the drafts open inside a thirty-minute window.

IT READS NO MONEY. The re-derivation resolves labels only, so a modifier cost
that has changed — or become unreadable — since the quote is honoured at the
saved amount, exactly as G2-C established for the item's own price. A comparison
that refused there would destroy a quote over a figure nobody was going to
charge.
"""
from decimal import Decimal

from django.test import TestCase

from dinify_backend.configss.string_definitions import OrderStatus_Pending
from orders_app.controllers.services.purchase_integrity import (
    REASON_PURCHASE_NEEDS_REVIEW,
)
from orders_app.models import OrderItem, OrderQuoteClosure
from orders_app.tests_quote_lifetime import QuoteFixtureMixin
from restaurants_app.models import MenuItem


def _options(size_choices=None, group_name='Size', extra_group=None):
    groups = [{
        'id': 'g-size',
        'name': group_name,
        'minSelections': 1,
        'maxSelections': 1,
        'choices': size_choices if size_choices is not None else [
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 0},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ],
    }]
    if extra_group is not None:
        groups.append(extra_group)
    return {'hasModifiers': True, 'groups': groups}


class PreparationMeaningFixture(QuoteFixtureMixin, TestCase):
    """One dish with one required single-select group, one choice taken."""

    SELECTION = {'g-size': ['c-large']}

    def setUp(self):
        super().setUp()
        MenuItem.objects.filter(pk=self.item.pk).update(options=_options())
        self.item.refresh_from_db()

    def _ordered(self, created_by=None):
        return self._draft_order(created_by=created_by, items=[{
            'item': str(self.item.pk), 'quantity': 1,
            'selected_modifiers': dict(self.SELECTION),
        }])

    def _redefine(self, options):
        MenuItem.objects.filter(pk=self.item.pk).update(options=options)

    def _assert_accepted(self, order):
        result = self._submit(order)
        self.assertEqual(result.get('status'), 200, result)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)
        self.assertFalse(OrderQuoteClosure.objects.filter(order=order).exists())

    def _assert_needs_review(self, order):
        result = self._submit(order)
        self.assertEqual(result.get('status'), 400, result)
        self.assertEqual(result.get('reason'), REASON_PURCHASE_NEEDS_REVIEW)
        order.refresh_from_db()
        self.assertNotEqual(order.order_status, OrderStatus_Pending)


class TheSnapshotIsWhatTheKitchenPreparesFromTests(PreparationMeaningFixture):
    """The premise, pinned. If the snapshot stops recording the selection, the
    comparison below is comparing nothing."""

    def test_the_saved_line_records_what_was_chosen(self):
        order = self._ordered()
        row = OrderItem.objects.get(order=order, parent_item__isnull=True)
        self.assertEqual(row.modifiers_snapshot, ['Size: Large'])
        self.assertEqual(row.selected_modifiers, {'g-size': ['c-large']})


class TheTwoProducersAgreeByConstructionTests(TestCase):
    """The snapshot the diner's line records and the text acceptance re-derives
    come from ONE resolution.

    Asserted on the shared resolver directly, over the shapes that could make
    two traversals diverge: a repeat, several groups, a blank label, an empty
    group. If these ever came from two implementations, a relabelled choice
    would still be caught — but an ordinary order would start being refused.
    """

    def _meaning(self, options, selected):
        from restaurants_app.controllers.modifier_definition import (
            inspect_modifier_definition, selection_meaning,
        )
        return selection_meaning(inspect_modifier_definition(options), selected)

    def _snapshot(self, options, selected):
        """The checkout traversal's own output, formatted as `resolve_line`
        formats it into `modifiers_snapshot`."""
        from orders_app.controllers.con_orders import ConOrder

        class _Item:
            name = 'Dish'

        _Item.options = options
        breakdown = ConOrder.option_breakdown(_Item(), selected)
        if breakdown.get('status') != 200:
            return None
        return [f"{o['name']}: {o['choices']}" for o in breakdown['options']]

    def test_they_agree_across_the_awkward_shapes(self):
        two_groups = {'hasModifiers': True, 'groups': [
            {'id': 'g1', 'name': 'Size', 'minSelections': 0, 'maxSelections': 0,
             'choices': [{'id': 'a', 'name': 'Large', 'additionalCost': 0},
                         {'id': 'b', 'name': 'Small', 'additionalCost': 0}]},
            {'id': 'g2', 'name': 'Sauce', 'minSelections': 0, 'maxSelections': 0,
             'choices': [{'id': 'x', 'additionalCost': 0},
                         {'id': 'y', 'name': 'Hot', 'additionalCost': 0}]},
        ]}
        cases = [
            (two_groups, {'g1': ['a']}),
            (two_groups, {'g1': ['a', 'a']}),          # a repeat, collapsed
            (two_groups, {'g2': ['x']}),               # a blank label
            (two_groups, {'g1': ['a', 'b'], 'g2': ['y', 'x']}),
            (two_groups, {'g2': ['y'], 'g1': ['b']}),  # selection order governs
            (two_groups, {'g1': []}),                  # an empty group, omitted
            (two_groups, {}),
            ({'hasModifiers': False}, {}),
        ]
        for options, selected in cases:
            with self.subTest(selected=selected):
                self.assertEqual(
                    self._meaning(options, selected),
                    self._snapshot(options, selected),
                )


class ARelabelledChoiceIsAChangedPreparationTests(PreparationMeaningFixture):
    """THE REGRESSION. Same id, different instruction to the kitchen."""

    def test_renaming_the_SELECTED_choice_sends_the_order_for_review(self):
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Extra chilli', 'additionalCost': 0},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        self._assert_needs_review(order)

    def test_it_binds_a_staff_origin_order_too(self):
        """Preparation is not publication: authorization to take an order is not
        authorization to cook something else."""
        order = self._ordered(created_by=self.owner)
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Extra chilli', 'additionalCost': 0},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        self._assert_needs_review(order)

    def test_blanking_the_selected_choice_name_is_a_change(self):
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'additionalCost': 0},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        self._assert_needs_review(order)

    def test_renaming_the_selected_GROUP_sends_the_order_for_review(self):
        """The documented false positive, in the safe direction — the group name
        is part of the text the kitchen reads."""
        order = self._ordered()
        self._redefine(_options(group_name='Portion'))
        self._assert_needs_review(order)


class WhatAPureEditMayStillDoTests(PreparationMeaningFixture):
    """The controls. A comparison that refuses everything is not a comparison."""

    def test_renaming_an_UNSELECTED_choice_changes_nothing(self):
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 0},
            {'id': 'c-small', 'name': 'Tiny', 'additionalCost': 0},
        ]))
        self._assert_accepted(order)

    def test_renaming_a_choice_in_an_UNSELECTED_group_changes_nothing(self):
        order = self._ordered()
        self._redefine(_options(extra_group={
            'id': 'g-sauce', 'name': 'Sauce',
            'minSelections': 0, 'maxSelections': 1,
            'choices': [{'id': 'c-hot', 'name': 'Ketchup', 'additionalCost': 0}],
        }))
        self._assert_accepted(order)

    def test_adding_an_OPTIONAL_choice_to_the_selected_group_changes_nothing(self):
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 0},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
            {'id': 'c-medium', 'name': 'Medium', 'additionalCost': 0},
        ]))
        self._assert_accepted(order)

    def test_reordering_the_choices_changes_nothing(self):
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 0},
        ]))
        self._assert_accepted(order)

    def test_renaming_the_DISH_changes_nothing(self):
        """Deliberately out of scope — the decision table accepts a rename, and
        comparing it would refuse an order for every menu tidy-up."""
        order = self._ordered()
        MenuItem.objects.filter(pk=self.item.pk).update(name='Rolex Special')
        self._assert_accepted(order)

    def test_a_line_with_no_modifiers_is_unaffected(self):
        """A dish carrying no modifier definition at all. Its snapshot is empty
        and stays empty, so nothing about the group above can reach it."""
        plain = MenuItem.objects.create(
            name='Chapati', section=self.section, primary_price=Decimal('3000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        order = self._draft_order(table=self.table_b, items=[
            {'item': str(plain.pk), 'quantity': 1},
        ])
        self._redefine(_options(group_name='Portion'))
        self._assert_accepted(order)


class TheComparisonReadsNoMoneyTests(PreparationMeaningFixture):
    """Money is honoured at the saved amount — the G2-C rule, one level down."""

    def test_a_changed_modifier_cost_is_honoured(self):
        order = self._ordered()
        before = order.actual_cost
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 9999},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        self._assert_accepted(order)
        order.refresh_from_db()
        self.assertEqual(order.actual_cost, before)

    def test_an_UNREADABLE_modifier_cost_does_not_destroy_the_quote(self):
        """A cost that cannot be parsed at all. Creation would refuse it; a
        saved quote is not repriced, so acceptance honours what it charged."""
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 'abc'},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        self._assert_accepted(order)

    def test_the_control_a_NEW_order_against_it_is_still_refused(self):
        self._redefine(_options(size_choices=[
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 'abc'},
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        refused = self._draft(items=[{
            'item': str(self.item.pk), 'quantity': 1,
            'selected_modifiers': dict(self.SELECTION),
        }])
        self.assertEqual(refused.get('status'), 400, refused)


class TheStructuralRulesStillApplyFirstTests(PreparationMeaningFixture):
    """The meaning check is additive: nothing it does replaces an id check."""

    def test_a_removed_selected_choice_is_still_a_missing_choice(self):
        order = self._ordered()
        self._redefine(_options(size_choices=[
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0},
        ]))
        self._assert_needs_review(order)

    def test_a_removed_group_is_still_a_missing_group(self):
        order = self._ordered()
        self._redefine({'hasModifiers': True, 'groups': [{
            'id': 'g-sauce', 'name': 'Sauce',
            'minSelections': 0, 'maxSelections': 1,
            'choices': [{'id': 'c-hot', 'name': 'Hot', 'additionalCost': 0}],
        }]})
        self._assert_needs_review(order)

    def test_an_unreadable_definition_is_still_refused(self):
        order = self._ordered()
        self._redefine({'hasModifiers': True, 'groups': 'not-a-list'})
        self._assert_needs_review(order)
