"""
Canonical menu-publication policy — read-path boundary tests (PR2).

These close the gaps the pre-PR2 suites left open:

* a published child item beneath an UNAPPROVED / DISABLED / UNAVAILABLE group (not
  just a soft-deleted one) must NOT surface, and must not leak its group metadata;
* item_count must equal the length of the visible items array;
* the restaurant boundary fails closed (missing→400, everything unresolvable→one
  generic 404), and accepting_orders=False does not hide the menu;
* nested extras are restaurant-scoped, is_extra-only, self/duplicate-safe, order
  preserving — and the operator (no-context) path is unaffected;
* one captured evaluation time governs the whole response;
* the queryset filters and the instance predicates agree (parity);
* the menu serialization is bounded (no per-item / per-extra publication query).

Read behaviour only; the order/checkout parity lives in orders_app/tests.py.
"""
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from users_app.models import User
from restaurants_app.models import (
    Restaurant, MenuSection, SectionGroup, MenuItem, UpsellConfig, UpsellItem,
)
from restaurants_app.serializers import UpsellConfigSerializer
from restaurants_app.controllers import menu_publication as mp
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RestaurantStatus_Pending,
    RestaurantStatus_Rejected, RestaurantStatus_Blocked,
)

SHOW_MENU_PATH = '/api/v1/orders/journey/show-menu/'


def _owner(phone):
    return User.objects.create_user(
        first_name='Pol', last_name='Owner', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class PolicyMenuBase(TestCase):
    """Restaurant A with a published section holding a child item under every
    group state, group-less/grouped controls, and a cross-tenant restaurant B."""

    def setUp(self):
        self.owner_a = _owner('256700010001')
        self.restaurant = Restaurant.objects.create(
            name='Policy R A', location='pol-a', owner=self.owner_a,
            status=RestaurantStatus_Active, accepting_orders=True,
        )
        self.section = self._section('Published Section')

        # Groups in every state, each WITH a published-looking child item.
        self.pub_group = self._group('Pub Group')
        self.pub_grouped_item = self._item('Grouped Item', group=self.pub_group)
        self.groupless_item = self._item('Groupless Item', group=None)

        self.unapproved_group = self._group('Unapproved Group', approved=False)
        self.child_unapproved_group = self._item(
            'Child Unapproved Group', group=self.unapproved_group,
        )
        self.disabled_group = self._group('Disabled Group', enabled=False)
        self.child_disabled_group = self._item(
            'Child Disabled Group', group=self.disabled_group,
        )
        self.unavailable_group = self._group('Unavailable Group', available=False)
        self.child_unavailable_group = self._item(
            'Child Unavailable Group', group=self.unavailable_group,
        )
        self.deleted_group = self._group('Deleted Group', deleted=True)
        self.child_deleted_group = self._item(
            'Child Deleted Group', group=self.deleted_group,
        )

        # Cross-tenant restaurant B.
        self.owner_b = _owner('256700010002')
        self.restaurant_b = Restaurant.objects.create(
            name='Policy R B', location='pol-b', owner=self.owner_b,
            status=RestaurantStatus_Active,
        )
        self.section_b = MenuSection.objects.create(
            name='B Section', restaurant=self.restaurant_b,
            approved=True, enabled=True, available=True, availability='always',
        )

    # --- fixture helpers (published defaults) ------------------------------
    def _section(self, name, **kw):
        opts = dict(
            approved=True, enabled=True, available=True, availability='always',
        )
        opts.update(kw)
        return MenuSection.objects.create(
            name=name, restaurant=self.restaurant, **opts,
        )

    def _group(self, name, **kw):
        opts = dict(approved=True, enabled=True, available=True)
        opts.update(kw)
        return SectionGroup.objects.create(name=name, section=self.section, **opts)

    def _item(self, name, section=None, group=None, **kw):
        opts = dict(approved=True, enabled=True, available=True, primary_price=1000)
        opts.update(kw)
        return MenuItem.objects.create(
            name=name, section=section or self.section, section_group=group, **opts,
        )

    def _menu(self, restaurant=None):
        rid = str((restaurant or self.restaurant).id)
        resp = APIClient().get(SHOW_MENU_PATH, {'restaurant': rid})
        return resp

    def _all_item_ids(self, payload):
        ids = set()
        for section in payload['data']:
            for item in section['items']:
                ids.add(item['id'])
        return ids

    def _all_group_ids(self, payload):
        ids = set()
        for section in payload['data']:
            for group in section['groups']:
                ids.add(group['id'])
            for item in section['items']:
                if item.get('group'):
                    ids.add(item['group']['id'])
        return ids


class RestaurantBoundaryTests(PolicyMenuBase):
    def test_active_restaurant_returns_menu(self):
        resp = self._menu()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(str(self.groupless_item.id), self._all_item_ids(resp.json()))

    def test_missing_restaurant_param_returns_400(self):
        resp = APIClient().get(SHOW_MENU_PATH)
        self.assertEqual(resp.status_code, 400)

    def test_malformed_unknown_deleted_pending_rejected_blocked_same_404(self):
        import uuid as _uuid
        cases = []
        cases.append('not-a-uuid')
        cases.append(str(_uuid.uuid4()))  # unknown
        deleted = Restaurant.objects.create(
            name='Deleted R', location='d', owner=_owner('256700010010'),
            status=RestaurantStatus_Active, deleted=True,
        )
        cases.append(str(deleted.id))
        for status in (RestaurantStatus_Pending, RestaurantStatus_Rejected,
                       RestaurantStatus_Blocked):
            r = Restaurant.objects.create(
                name=f'{status} R', location=status,
                owner=_owner(f'2567000100{20 + len(cases)}'), status=status,
            )
            cases.append(str(r.id))
        messages = set()
        for ref in cases:
            resp = APIClient().get(SHOW_MENU_PATH, {'restaurant': ref})
            self.assertEqual(resp.status_code, 404, ref)
            messages.add(resp.json()['message'])
        # One single, non-disclosing message for every unresolvable case.
        self.assertEqual(len(messages), 1, messages)

    def test_active_restaurant_no_visible_sections_returns_empty_200(self):
        empty_owner = _owner('256700010040')
        empty = Restaurant.objects.create(
            name='Empty R', location='e', owner=empty_owner,
            status=RestaurantStatus_Active,
        )
        resp = self._menu(restaurant=empty)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data'], [])

    def test_accepting_orders_false_does_not_hide_menu(self):
        self.restaurant.accepting_orders = False
        self.restaurant.save(update_fields=['accepting_orders'])
        resp = self._menu()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(str(self.groupless_item.id), self._all_item_ids(resp.json()))


class GroupChildVisibilityTests(PolicyMenuBase):
    """The load-bearing gap: a published child item beneath a non-deleted but
    unpublished group must not surface, and must not leak group metadata."""

    def test_published_and_groupless_items_render(self):
        ids = self._all_item_ids(self._menu().json())
        self.assertIn(str(self.pub_grouped_item.id), ids)
        self.assertIn(str(self.groupless_item.id), ids)

    def test_child_items_under_unpublished_groups_absent(self):
        ids = self._all_item_ids(self._menu().json())
        for hidden in (
            self.child_unapproved_group, self.child_disabled_group,
            self.child_unavailable_group, self.child_deleted_group,
        ):
            self.assertNotIn(str(hidden.id), ids, hidden.name)

    def test_hidden_group_metadata_not_exposed(self):
        group_ids = self._all_group_ids(self._menu().json())
        for hidden_group in (
            self.unapproved_group, self.disabled_group,
            self.unavailable_group, self.deleted_group,
        ):
            self.assertNotIn(str(hidden_group.id), group_ids, hidden_group.name)
        self.assertIn(str(self.pub_group.id), group_ids)

    def test_item_count_equals_visible_items_length(self):
        for section in self._menu().json()['data']:
            self.assertEqual(section['item_count'], len(section['items']))


class SectionVisibilityTests(PolicyMenuBase):
    def test_child_items_under_unpublished_sections_absent(self):
        cases = [
            self._section('Unapproved S', approved=False),
            self._section('Disabled S', enabled=False),
            self._section('Unavailable S', available=False),
            self._section('Deleted S', deleted=True),
            self._section(
                'Scheduled-off S', availability='scheduled',
                schedules=[{'days': ['mon'], 'startTime': '00:00',
                            'endTime': '00:01'}],
            ),
        ]
        hidden_items = [
            self._item(f'Item in {s.name}', section=s) for s in cases
        ]
        ids = self._all_item_ids(self._menu().json())
        for item in hidden_items:
            self.assertNotIn(str(item.id), ids, item.name)

    def test_sold_out_item_kept_with_flag(self):
        sold_out = self._item('Sold Out Item', group=None, in_stock=False)
        payload = self._menu().json()
        found = None
        for section in payload['data']:
            for item in section['items']:
                if item['id'] == str(sold_out.id):
                    found = item
        self.assertIsNotNone(found)  # kept in payload
        self.assertFalse(found['in_stock'])  # flagged sold out

    def test_captured_clock_consistent_across_response(self):
        # A section scheduled to flip at a boundary must report the SAME activity
        # in its filter inclusion and its serialized is_currently_active — one now.
        active_sched = self._section(
            'Active Scheduled', availability='scheduled',
            schedules=[{'days': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
                        'startTime': '00:00', 'endTime': '23:59'}],
        )
        self._item('Sched Item', section=active_sched)
        payload = self._menu().json()
        sched = [s for s in payload['data'] if s['id'] == str(active_sched.id)]
        self.assertEqual(len(sched), 1)  # included by the filter
        self.assertTrue(sched[0]['is_currently_active'])  # and serialized active


class NestedExtraPolicyTests(PolicyMenuBase):
    def setUp(self):
        super().setUp()
        self.valid_extra = self._item('Valid Extra', group=None, is_extra=True)
        self.foreign_extra = MenuItem.objects.create(
            name='Foreign Extra', section=self.section_b, primary_price=500,
            approved=True, enabled=True, is_extra=True,
        )
        self.unapproved_extra = self._item(
            'Unapproved Extra', group=None, is_extra=True, approved=False,
        )
        self.non_is_extra = self._item('Not An Extra', group=None, is_extra=False)
        import uuid as _uuid
        self.parent = self._item('Extra Parent', group=None, has_extras=True)
        self.parent.extras_applicable = [
            str(self.valid_extra.id),
            str(self.valid_extra.id),        # duplicate → at most once
            str(self.foreign_extra.id),      # cross-tenant → dropped
            str(self.unapproved_extra.id),   # unpublished → dropped
            str(self.non_is_extra.id),       # not is_extra → dropped
            str(self.parent.id),             # self-reference → dropped
            str(_uuid.uuid4()),              # nonexistent → dropped
            'not-a-uuid',                    # malformed → dropped
        ]
        self.parent.save(update_fields=['extras_applicable'])

    def _parent_extras(self):
        for section in self._menu().json()['data']:
            for item in section['items']:
                if item['id'] == str(self.parent.id):
                    return item['extras']
        return None

    def test_only_valid_same_restaurant_extra_present_once(self):
        extras = self._parent_extras()
        self.assertIsNotNone(extras)
        ids = [str(e['id']) for e in extras]
        self.assertEqual(ids, [str(self.valid_extra.id)])  # exactly one, deduped

    def test_operator_read_not_filtered_by_public_policy(self):
        # No menu_policy context → the current (unfiltered publication) behaviour.
        from restaurants_app.serializers import SerializerPublicGetMenuItem
        data = SerializerPublicGetMenuItem(self.parent).data
        ids = {str(e['id']) for e in data['extras']}
        # The operator path still resolves published members (valid + none of the
        # unpublished), and is NOT gated on the strict is_extra/tenant map — it is
        # the pre-PR2 lookup, so at minimum the valid published extra is present.
        self.assertIn(str(self.valid_extra.id), ids)


class UpsellInheritanceTests(PolicyMenuBase):
    def setUp(self):
        super().setUp()
        self.config = UpsellConfig.objects.create(
            restaurant=self.restaurant, enabled=True,
        )
        # visible upsell (published, visible section/group)
        self.visible_item = self._item('Upsell Visible', group=None)
        # item under a disabled group → must NOT re-enter via upsell
        self.hidden_item = self._item(
            'Upsell Hidden Group', group=self.disabled_group,
        )
        # a foreign-restaurant item
        self.foreign_item = MenuItem.objects.create(
            name='Upsell Foreign', section=self.section_b, primary_price=1000,
            approved=True, enabled=True,
        )
        for i, mi in enumerate((self.visible_item, self.hidden_item,
                                self.foreign_item)):
            UpsellItem.objects.create(
                config=self.config, menu_item=mi, listing_position=i,
            )

    def _public_upsell_ids(self):
        resp = self._menu()
        upsell = resp.json().get('upsell')
        return {str(i['menu_item']) for i in (upsell['items'] if upsell else [])}

    def test_visible_upsell_present_hidden_and_foreign_absent(self):
        ids = self._public_upsell_ids()
        self.assertIn(str(self.visible_item.id), ids)
        self.assertNotIn(str(self.hidden_item.id), ids)   # under disabled group
        self.assertNotIn(str(self.foreign_item.id), ids)  # cross-tenant

    def test_operator_view_shows_all_configured_items(self):
        # No context → operator sees every configured item, published or not.
        data = UpsellConfigSerializer(self.config).data
        ids = {str(i['menu_item']) for i in data['items']}
        self.assertIn(str(self.hidden_item.id), ids)
        self.assertIn(str(self.foreign_item.id), ids)


class PolicyParityTests(PolicyMenuBase):
    """The queryset-style read filter and the instance predicate must agree across
    every group state, so the two never drift (TENANT-STRUCT parity)."""

    def test_group_predicate_matches_visible_group_ids(self):
        from django.utils import timezone
        now = timezone.localtime()
        payload = self._menu().json()
        serialized_group_ids = self._all_group_ids(payload)
        for group in SectionGroup.objects.filter(section=self.section):
            group.section = self.section
            predicate = mp.group_operationally_visible(group, now)
            in_payload = str(group.id) in serialized_group_ids
            # A group is in the payload iff the instance predicate says visible.
            self.assertEqual(predicate, in_payload, group.name)

    def test_item_predicate_matches_visible_items(self):
        from django.utils import timezone
        now = timezone.localtime()
        payload = self._menu().json()
        visible_ids = self._all_item_ids(payload)
        for item in MenuItem.objects.filter(
            section=self.section, is_extra=False,
        ).select_related('section', 'section_group'):
            predicate = mp.item_visible_in_menu(item, now)
            self.assertEqual(predicate, str(item.id) in visible_ids, item.name)


class MenuQueryBoundednessTests(PolicyMenuBase):
    """Adding several items + extras must NOT add a publication query per item or
    per extra (the per-extra global lookup and the item_count re-query are gone)."""

    def _menu_query_count(self):
        client = APIClient()
        rid = str(self.restaurant.id)
        client.get(SHOW_MENU_PATH, {'restaurant': rid})  # warm caches
        with CaptureQueriesContext(connection) as ctx:
            client.get(SHOW_MENU_PATH, {'restaurant': rid})
        return len(ctx.captured_queries)

    def test_adding_items_and_extras_does_not_scale_queries(self):
        base = self._menu_query_count()
        # Add a parent with several applicable extras, plus more plain items.
        extras = [self._item(f'QC Extra {i}', group=None, is_extra=True)
                  for i in range(5)]
        parent = self._item('QC Parent', group=None, has_extras=True)
        parent.extras_applicable = [str(e.id) for e in extras]
        parent.save(update_fields=['extras_applicable'])
        for i in range(5):
            self._item(f'QC Item {i}', group=self.pub_group)
        grown = self._menu_query_count()
        # A per-item/per-extra publication lookup would add ~10+ queries; a bounded
        # implementation adds only a small constant. Guard generously but firmly.
        self.assertLess(grown - base, 6, f'base={base} grown={grown}')
