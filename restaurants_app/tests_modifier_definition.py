"""
The pure structural reading of a stored ``MenuItem.options`` definition.

These tests are about ONE question: given a catalogue row, what does it mean?
They exercise the inspector directly (no database, no HTTP) and then prove that
checkout reaches the same verdict through it — because the whole point of the
module is that the order path and the read-only preflight cannot hold two
different opinions about the same row.

Capacity is not validity, and the split is asserted in both directions: a big
optional catalogue stays orderable, while a requirement that cannot be met is
reported as a concern without being called a definition error.
"""
from django.test import SimpleTestCase, TestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from orders_app.controllers.con_orders import ConOrder
from restaurants_app.controllers import modifier_definition as md
from restaurants_app.models import MenuItem, MenuSection, Restaurant
from users_app.models import User


def _options(groups, has_modifiers=True):
    return {'hasModifiers': has_modifiers, 'groups': groups}


def _group(group_id='g1', choices=('c1',), **extra):
    spec = {'id': group_id, 'choices': [{'id': c} for c in choices]}
    spec.update(extra)
    return spec


class InactiveDefinitionTests(SimpleTestCase):
    """Legitimate "this item has no options" states. All orderable."""

    def test_the_established_inactive_forms(self):
        for options in (
            None, {}, 'a json string', [], 42,
            {'hasModifiers': False},
            {'hasModifiers': False, 'groups': [_group()]},
            {'hasModifiers': True},
            {'hasModifiers': True, 'groups': None},
            {'hasModifiers': True, 'groups': []},
        ):
            with self.subTest(options=repr(options)[:40]):
                verdict = md.inspect_modifier_definition(options)
                self.assertEqual(verdict.kind, md.KIND_INACTIVE)
                self.assertFalse(verdict.is_invalid)


class InvalidDefinitionTests(SimpleTestCase):
    """Active definitions that are malformed or self-contradictory."""

    def _assert_invalid(self, options, reason):
        verdict = md.inspect_modifier_definition(options)
        self.assertEqual(verdict.kind, md.KIND_INVALID, verdict.kind)
        self.assertEqual(verdict.reason, reason)

    def test_unreadable_group_container(self):
        # NOT downgraded to inactive: a value we cannot read may well encode
        # real requirements, so dropping them would be a bypass.
        self._assert_invalid(_options('oops'), md.GROUPS_NOT_A_LIST)
        self._assert_invalid(_options({'a': 1}), md.GROUPS_NOT_A_LIST)

    def test_unusable_identifiers(self):
        self._assert_invalid(_options([_group(group_id={'k': 1})]),
                             md.GROUP_ID_INVALID)
        self._assert_invalid(_options([_group(group_id=['g'])]),
                             md.GROUP_ID_INVALID)
        self._assert_invalid(_options([_group(group_id=None)]),
                             md.GROUP_ID_INVALID)
        self._assert_invalid(_options([_group(group_id='')]),
                             md.GROUP_ID_INVALID)
        self._assert_invalid(
            _options([{'id': 'g1', 'choices': [{'id': {'z': 1}}]}]),
            md.CHOICE_ID_INVALID)
        self._assert_invalid(
            _options([{'id': 'g1', 'choices': [{'id': ['z']}]}]),
            md.CHOICE_ID_INVALID)

    def test_malformed_containers(self):
        self._assert_invalid(_options(['not a mapping']), md.GROUP_NOT_A_MAPPING)
        self._assert_invalid(_options([{'id': 'g1', 'choices': 'oops'}]),
                             md.CHOICES_NOT_A_LIST)
        self._assert_invalid(_options([{'id': 'g1', 'choices': ['oops']}]),
                             md.CHOICE_NOT_A_MAPPING)

    def test_duplicate_group_ids_are_refused_not_resolved(self):
        # Validation resolved these first-wins while pricing resolved them
        # last-wins, so one id was validated against one definition and charged
        # against another. Refuse rather than pick.
        self._assert_invalid(
            _options([_group('g1', ('c1',)), _group('g1', ('c2',))]),
            md.DUPLICATE_GROUP_ID)

    def test_duplicate_choice_ids_within_one_group_are_refused(self):
        self._assert_invalid(
            _options([{'id': 'g1', 'choices': [
                {'id': 'c1', 'additionalCost': 0},
                {'id': 'c1', 'additionalCost': 5000},
            ]}]),
            md.DUPLICATE_CHOICE_ID)

    def test_the_same_choice_id_in_different_groups_is_legal(self):
        verdict = md.inspect_modifier_definition(
            _options([_group('g1', ('c1',)), _group('g2', ('c1',))]),
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)

    def test_selection_bounds_must_be_real_non_negative_integers(self):
        for bound in ('2', 2.0, True, False, -1, [2], None if False else {}):
            for key in ('minSelections', 'maxSelections'):
                with self.subTest(key=key, bound=repr(bound)):
                    self._assert_invalid(
                        _options([_group(**{key: bound})]),
                        md.SELECTION_BOUND_INVALID)

    def test_a_positive_maximum_below_the_minimum_is_refused(self):
        # No selection count satisfies both; inventing a default here would
        # open a required-selection bypass.
        self._assert_invalid(
            _options([_group('g1', ('c1', 'c2'),
                             minSelections=2, maxSelections=1)]),
            md.MAX_BELOW_MIN)

    def test_zero_maximum_still_means_unlimited(self):
        verdict = md.inspect_modifier_definition(
            _options([_group('g1', ('c1', 'c2'),
                             minSelections=1, maxSelections=0)]),
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertEqual(verdict.groups[0].max_selections, 0)

    def test_absent_bounds_default_to_zero(self):
        verdict = md.inspect_modifier_definition(_options([_group()]))
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertEqual(verdict.groups[0].min_selections, 0)
        self.assertEqual(verdict.groups[0].max_selections, 0)


class CapacityIsNotValidityTests(SimpleTestCase):
    """A large catalogue is capacity; only an unsatisfiable REQUIREMENT is a
    concern, and neither is a definition error."""

    def test_a_large_optional_catalogue_is_valid_and_unconcerning(self):
        verdict = md.inspect_modifier_definition(
            _options([_group('g1', tuple(f'c{i}' for i in range(500)))]),
            max_choices_per_group=64, max_groups_per_line=32,
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertEqual(verdict.concerns, ())
        self.assertEqual(verdict.max_choices_in_a_group, 500)

    def test_many_optional_groups_are_valid_and_unconcerning(self):
        verdict = md.inspect_modifier_definition(
            _options([_group(f'g{i}') for i in range(100)]),
            max_choices_per_group=64, max_groups_per_line=32,
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertEqual(verdict.concerns, ())
        self.assertEqual(verdict.required_group_count, 0)

    def test_a_minimum_above_the_defined_choices_is_a_concern(self):
        verdict = md.inspect_modifier_definition(
            _options([_group('g1', ('c1',), minSelections=2)]),
            max_choices_per_group=64, max_groups_per_line=32,
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertIn((md.MIN_EXCEEDS_DEFINED_CHOICES, 'g1'), verdict.concerns)

    def test_a_minimum_above_the_request_ceiling_is_a_concern(self):
        verdict = md.inspect_modifier_definition(
            _options([_group('g1', tuple(f'c{i}' for i in range(200)),
                             minSelections=100)]),
            max_choices_per_group=64, max_groups_per_line=32,
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertIn((md.MIN_EXCEEDS_CHOICE_CEILING, 'g1'), verdict.concerns)

    def test_too_many_REQUIRED_groups_is_a_concern(self):
        verdict = md.inspect_modifier_definition(
            _options([_group(f'g{i}', ('c1',), minSelections=1)
                      for i in range(40)]),
            max_choices_per_group=64, max_groups_per_line=32,
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertIn((md.REQUIRED_GROUPS_EXCEED_CEILING, None),
                      verdict.concerns)

    def test_the_ceiling_dependent_concerns_are_off_by_default(self):
        # Passing no ceilings suppresses only the concerns that are ABOUT a
        # ceiling. A minimum above the item's own defined choices is an
        # intrinsic contradiction in the catalogue and is always reported.
        verdict = md.inspect_modifier_definition(
            _options([_group('g1', tuple(f'c{i}' for i in range(200)),
                             minSelections=100)]),
        )
        self.assertEqual(verdict.kind, md.KIND_VALID)
        self.assertEqual(verdict.concerns, ())

        intrinsic = md.inspect_modifier_definition(
            _options([_group('g1', ('c1',), minSelections=2)]),
        )
        self.assertIn((md.MIN_EXCEEDS_DEFINED_CHOICES, 'g1'),
                      intrinsic.concerns)


class CheckoutReachesTheSameVerdictTests(TestCase):
    """The order path reads definitions through this module — asserted through
    real behaviour, not by reading the source."""

    def setUp(self):
        owner = User.objects.create_user(
            first_name='M', last_name='D', email='md@test.com',
            phone_number='256700041001', username='256700041001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='MD R', location='md', owner=owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='MD S', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always',
        )
        self._n = 0

    def _item(self, options):
        self._n += 1
        return MenuItem.objects.create(
            name=f'MD Item {self._n}', section=self.section,
            primary_price=1000, approved=True, enabled=True, available=True,
            options=options,
        )

    def test_every_invalid_definition_is_a_controlled_checkout_rejection(self):
        for options in (
            _options('oops'),
            _options([_group(group_id={'k': 1})]),
            _options([{'id': 'g1', 'choices': [{'id': ['z']}]}]),
            _options([_group(minSelections='2')]),
            _options([_group('g1', ('c1', 'c2'),
                             minSelections=2, maxSelections=1)]),
            _options([_group('g1', ('c1',)), _group('g1', ('c2',))]),
        ):
            with self.subTest(options=repr(options)[:50]):
                item = self._item(options)
                # Empty selection too: fail-closed must not depend on the diner
                # having submitted anything.
                for selection in ({}, {'g1': ['c1']}):
                    result = ConOrder.normalize_selected_modifiers(
                        item, selection,
                    )
                    self.assertEqual(result.get('status'), 400, result)
                    self.assertIn('cannot be ordered right now',
                                  result.get('message', ''))

    def test_a_required_group_is_never_dropped_to_make_an_item_orderable(self):
        item = self._item(_options('oops'))
        result = ConOrder.normalize_selected_modifiers(item, {})
        self.assertEqual(result.get('status'), 400)
        self.assertNotIn('selected_modifiers', result)

    def test_a_large_valid_catalogue_still_orders(self):
        item = self._item(
            _options([_group('g1', tuple(f'c{i}' for i in range(300)))]),
        )
        result = ConOrder.normalize_selected_modifiers(item, {'g1': ['c7']})
        self.assertEqual(result.get('status'), 200, result)
        self.assertEqual(result['selected_modifiers'], {'g1': ['c7']})

    def test_inactive_definitions_behave_exactly_as_before(self):
        item = self._item({'hasModifiers': False})
        self.assertEqual(
            ConOrder.normalize_selected_modifiers(item, {}),
            {'status': 200, 'selected_modifiers': {}},
        )
        self.assertEqual(
            ConOrder.normalize_selected_modifiers(item, None),
            {'status': 200, 'selected_modifiers': {}},
        )
        rejected = ConOrder.normalize_selected_modifiers(item, {'g1': ['c1']})
        self.assertEqual(rejected.get('status'), 400)

    def test_pricing_and_validation_resolve_one_definition(self):
        # With duplicates refused, the last-wins/first-wins divergence cannot
        # arise: the cost charged is the cost of the validated choice.
        item = self._item(_options([{'id': 'g1', 'choices': [
            {'id': 'c1', 'name': 'Cheap', 'additionalCost': 100},
            {'id': 'c2', 'name': 'Dear', 'additionalCost': 900},
        ]}]))
        normalized = ConOrder.normalize_selected_modifiers(item, {'g1': ['c1']})
        self.assertEqual(normalized['status'], 200)
        priced = ConOrder.determine_effective_unit_price(
            item, normalized['selected_modifiers'],
        )
        self.assertEqual(priced['status'], 200)
        self.assertEqual(priced['cost_of_options'], round(priced['cost_of_options'], 2))
        self.assertEqual(str(priced['cost_of_options']), '100.00')

    def test_an_unusable_choice_member_is_refused_by_the_price_path_too(self):
        item = self._item(_options([_group('g1', ('c1',))]))
        priced = ConOrder.determine_effective_unit_price(
            item, {'g1': [{'a': 1}]},
        )
        self.assertEqual(priced.get('status'), 400, priced)
