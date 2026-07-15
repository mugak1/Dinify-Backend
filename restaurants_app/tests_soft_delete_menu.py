"""
Regression tests for the soft-delete leaks closed under TENANT-P3-03.

Several diner/read serializer methods rendered soft-deleted rows because they
omitted a ``deleted=False`` predicate that sibling methods already carried:

  * ``SerializerGetFullMenu.get_groups``     -> soft-deleted SectionGroups
  * ``SerializerGetFullMenu.get_items``/
    ``.get_item_count``                       -> items under a soft-deleted group
  * ``SerializerGetDiningArea.get_no_tables``/
    ``.get_tables``                           -> soft-deleted Tables in an area

The item fix is a null-safe anti-join (``.exclude(section_group__deleted=True)``)
NOT a dict-key filter, because ``MenuItem.section_group`` is nullable and a
dict-key ``section_group__deleted=False`` would inner-join and silently drop
every group-less item. These tests pin both the leak fix AND that null-safe
behaviour (a group-less item must still render).
"""
from django.test import TestCase

from restaurants_app.models import (
    Restaurant, MenuSection, SectionGroup, MenuItem, DiningArea, Table,
)
from restaurants_app.serializers import (
    SerializerGetFullMenu, SerializerGetDiningArea,
)
from users_app.models import User


def _owner(phone):
    return User.objects.create_user(
        first_name='Menu', last_name='Owner', email=f'owner_{phone}@example.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class SoftDeleteFullMenuGroupsTests(TestCase):
    """SerializerGetFullMenu.get_groups must hide soft-deleted groups."""

    def setUp(self):
        self.owner = _owner('256700000811')
        self.restaurant = Restaurant.objects.create(
            name='SD Groups R', location='sd-groups', owner=self.owner,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )

    def _group_ids(self, ignore_approval=None):
        context = {}
        if ignore_approval is not None:
            context['ignore_approval'] = ignore_approval
        data = SerializerGetFullMenu(self.section, context=context).data
        return {group['id'] for group in data['groups']}

    def test_soft_deleted_group_hidden_live_sibling_shown(self):
        live = SectionGroup.objects.create(
            name='Live Group', section=self.section, approved=True, enabled=True,
        )
        deleted = SectionGroup.objects.create(
            name='Deleted Group', section=self.section,
            approved=True, enabled=True, deleted=True,
        )
        ids = self._group_ids()
        self.assertIn(str(live.pk), ids)          # positive control
        self.assertNotIn(str(deleted.pk), ids)    # leak closed

    def test_soft_deleted_group_still_hidden_in_ignore_approval_mode(self):
        # A live-but-unapproved group appears once approval is ignored; a
        # soft-deleted group must STAY hidden -> proves 'deleted' survived the
        # ignore_approval pop (only 'approved'/'enabled' are popped).
        live_unapproved = SectionGroup.objects.create(
            name='Live Unapproved', section=self.section,
            approved=False, enabled=False,
        )
        deleted_unapproved = SectionGroup.objects.create(
            name='Deleted Unapproved', section=self.section,
            approved=False, enabled=False, deleted=True,
        )
        ids = self._group_ids(ignore_approval='true')
        self.assertIn(str(live_unapproved.pk), ids)         # approval ignored
        self.assertNotIn(str(deleted_unapproved.pk), ids)   # deleted survives pop


class SoftDeleteFullMenuItemsTests(TestCase):
    """get_items / get_item_count hide items under a soft-deleted group but
    preserve group-less items (null-safe anti-join)."""

    def setUp(self):
        self.owner = _owner('256700000812')
        self.restaurant = Restaurant.objects.create(
            name='SD Items R', location='sd-items', owner=self.owner,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.live_group = SectionGroup.objects.create(
            name='Live Group', section=self.section, approved=True, enabled=True,
        )
        self.deleted_group = SectionGroup.objects.create(
            name='Deleted Group', section=self.section,
            approved=True, enabled=True, deleted=True,
        )
        common = dict(
            section=self.section, primary_price=1000,
            approved=True, enabled=True, available=True,
        )
        self.item_live_group = MenuItem.objects.create(
            name='Item Under Live Group', section_group=self.live_group, **common,
        )
        self.item_deleted_group = MenuItem.objects.create(
            name='Item Under Deleted Group', section_group=self.deleted_group, **common,
        )
        self.item_no_group = MenuItem.objects.create(
            name='Item With No Group', section_group=None, **common,
        )

    def _items(self, ignore_approval=None):
        context = {}
        if ignore_approval is not None:
            context['ignore_approval'] = ignore_approval
        return SerializerGetFullMenu(self.section, context=context).data

    def test_item_under_deleted_group_hidden_others_shown(self):
        data = self._items()
        item_ids = {item['id'] for item in data['items']}
        # item under a live group -> shown
        self.assertIn(str(self.item_live_group.pk), item_ids)
        # group-less item -> STILL shown (null-safe anti-join; guards the
        # inner-join regression that would blank group-less menus)
        self.assertIn(str(self.item_no_group.pk), item_ids)
        # item under a soft-deleted group -> hidden (leak closed)
        self.assertNotIn(str(self.item_deleted_group.pk), item_ids)

    def test_item_count_matches_visible_items(self):
        data = self._items()
        # 2 visible: live-group item + group-less item; deleted-group item excluded
        self.assertEqual(data['item_count'], 2)

    def test_positive_control_no_live_item_disappears(self):
        # With NO deleted groups involved, every non-deleted item is present and
        # the count is exact -> the .exclude() never over-filters.
        section = MenuSection.objects.create(
            name='All Live', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        group = SectionGroup.objects.create(
            name='All Live Group', section=section, approved=True, enabled=True,
        )
        grouped = MenuItem.objects.create(
            name='Grouped Live', section=section, section_group=group,
            primary_price=500, approved=True, enabled=True, available=True,
        )
        ungrouped = MenuItem.objects.create(
            name='Ungrouped Live', section=section, section_group=None,
            primary_price=500, approved=True, enabled=True, available=True,
        )
        data = SerializerGetFullMenu(section, context={}).data
        item_ids = {item['id'] for item in data['items']}
        self.assertIn(str(grouped.pk), item_ids)
        self.assertIn(str(ungrouped.pk), item_ids)
        self.assertEqual(data['item_count'], 2)


class SoftDeleteDiningAreaTests(TestCase):
    """SerializerGetDiningArea.get_no_tables / get_tables exclude soft-deleted
    tables and include live ones."""

    def setUp(self):
        self.owner = _owner('256700000813')
        self.restaurant = Restaurant.objects.create(
            name='SD Tables R', location='sd-tables', owner=self.owner,
        )
        self.area = DiningArea.objects.create(
            name='Patio', restaurant=self.restaurant,
        )
        self.live_table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            dining_area=self.area,
        )
        self.deleted_table = Table.objects.create(
            number=2, str_number='2', restaurant=self.restaurant,
            dining_area=self.area, deleted=True,
        )

    def test_get_no_tables_excludes_soft_deleted(self):
        data = SerializerGetDiningArea(self.area).data
        # only the live table is counted
        self.assertEqual(data['no_tables'], 1)

    def test_get_tables_excludes_soft_deleted_includes_live(self):
        data = SerializerGetDiningArea(self.area).data
        numbers = {table['number'] for table in data['tables']}
        self.assertIn(1, numbers)       # positive control
        self.assertNotIn(2, numbers)    # leak closed
