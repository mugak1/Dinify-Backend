"""
PR 1A — anonymous unpublished-menu boundary (read side).

The public diner menu endpoint (`GET /api/v1/orders/journey/show-menu/`) is
AllowAny. It must expose ONLY records currently published for diner use
(approved + enabled + not soft-deleted, plus the existing available/schedule
rules). The retired `ignore-approval` query flag must never bypass publication
state — for ANY caller, anonymous or authenticated. Two nested paths that could
re-admit an unpublished item (the extras serializer and the upsell carousel)
are pinned here too.
"""
from rest_framework.test import APIClient
from django.test import TestCase

from restaurants_app.models import (
    Restaurant, MenuSection, SectionGroup, MenuItem, UpsellConfig, UpsellItem,
)
from restaurants_app.serializers import UpsellConfigSerializer
from users_app.models import User

SHOW_MENU_PATH = '/api/v1/orders/journey/show-menu/'


def _owner(phone):
    return User.objects.create_user(
        first_name='Menu', last_name='Owner', email=f'owner_{phone}@example.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class AnonymousMenuPublicationBoundaryTests(TestCase):
    """Read boundary: publication is enforced unconditionally; the retired
    ignore-approval flag is inert."""

    def setUp(self):
        self.owner = _owner('256700000901')
        self.restaurant = Restaurant.objects.create(
            name='Pub Boundary R', location='pub-boundary', owner=self.owner,
        )

        # A fully-published section holding the positive/negative controls.
        self.pub_section = self._section('Published Section')
        self.pub_group = self._group(self.pub_section, 'Published Group')
        self.pub_item = self._item(
            self.pub_section, 'Published Item', group=self.pub_group,
        )
        self.groupless_item = self._item(
            self.pub_section, 'Groupless Item', group=None,
        )
        # Unpublished leaves inside the published section.
        self.unapproved_item = self._item(
            self.pub_section, 'Unapproved Item', approved=False,
        )
        self.disabled_item = self._item(
            self.pub_section, 'Disabled Item', enabled=False,
        )
        self.unapproved_group = self._group(
            self.pub_section, 'Unapproved Group', approved=False,
        )
        self.disabled_group = self._group(
            self.pub_section, 'Disabled Group', enabled=False,
        )
        # Soft-deleted group with an otherwise-published item beneath it.
        self.deleted_group = self._group(
            self.pub_section, 'Deleted Group', deleted=True,
        )
        self.item_under_deleted_group = self._item(
            self.pub_section, 'Item Under Deleted Group',
            group=self.deleted_group,
        )

        # Whole sections that are themselves unpublished, each with a
        # published-looking item inside (which must NOT surface via its section).
        self.unapproved_section = self._section(
            'Unapproved Section', approved=False,
        )
        self.item_in_unapproved_section = self._item(
            self.unapproved_section, 'Item In Unapproved Section',
        )
        self.disabled_section = self._section('Disabled Section', enabled=False)
        self.item_in_disabled_section = self._item(
            self.disabled_section, 'Item In Disabled Section',
        )

    # ---- fixture helpers (published defaults, overridable) ----------------
    def _section(self, name, **kw):
        defaults = dict(
            approved=True, enabled=True, available=True, availability='always',
        )
        defaults.update(kw)
        return MenuSection.objects.create(
            name=name, restaurant=self.restaurant, **defaults,
        )

    def _group(self, section, name, **kw):
        defaults = dict(approved=True, enabled=True)
        defaults.update(kw)
        return SectionGroup.objects.create(name=name, section=section, **defaults)

    def _item(self, section, name, group=None, **kw):
        defaults = dict(approved=True, enabled=True, available=True)
        defaults.update(kw)
        return MenuItem.objects.create(
            name=name, section=section, section_group=group,
            primary_price=1000, **defaults,
        )

    # ---- request helpers -------------------------------------------------
    def _menu(self, ignore_approval=False, user=None):
        client = APIClient()
        if user is not None:
            from rest_framework_simplejwt.tokens import RefreshToken
            token = str(RefreshToken.for_user(user).access_token)
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        query = {'restaurant': str(self.restaurant.id)}
        if ignore_approval:
            query['ignore-approval'] = 'true'
        response = client.get(SHOW_MENU_PATH, query)
        self.assertEqual(response.status_code, 200)
        return response.json()

    @staticmethod
    def _section_ids(payload):
        return {s['id'] for s in payload['data']}

    @staticmethod
    def _group_ids(payload):
        ids = set()
        for section in payload['data']:
            ids.update(g['id'] for g in section['groups'])
        return ids

    @staticmethod
    def _item_ids(payload):
        ids = set()
        for section in payload['data']:
            ids.update(i['id'] for i in section['items'])
        return ids

    # ---- 1. baseline -----------------------------------------------------
    def test_anonymous_normal_request_returns_published(self):
        payload = self._menu()
        self.assertIn(str(self.pub_section.id), self._section_ids(payload))
        self.assertIn(str(self.pub_group.id), self._group_ids(payload))
        item_ids = self._item_ids(payload)
        self.assertIn(str(self.pub_item.id), item_ids)
        self.assertIn(str(self.groupless_item.id), item_ids)

    # ---- 2-7. ignore-approval cannot reveal unpublished records ----------
    def test_ignore_approval_cannot_reveal_unapproved_section(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(str(self.unapproved_section.id), self._section_ids(payload))
        self.assertNotIn(
            str(self.item_in_unapproved_section.id), self._item_ids(payload)
        )

    def test_ignore_approval_cannot_reveal_disabled_section(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(str(self.disabled_section.id), self._section_ids(payload))
        self.assertNotIn(
            str(self.item_in_disabled_section.id), self._item_ids(payload)
        )

    def test_ignore_approval_cannot_reveal_unapproved_group(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(str(self.unapproved_group.id), self._group_ids(payload))

    def test_ignore_approval_cannot_reveal_disabled_group(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(str(self.disabled_group.id), self._group_ids(payload))

    def test_ignore_approval_cannot_reveal_unapproved_item(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(str(self.unapproved_item.id), self._item_ids(payload))

    def test_ignore_approval_cannot_reveal_disabled_item(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(str(self.disabled_item.id), self._item_ids(payload))

    # ---- 8-9. group-less preserved / soft-deleted group hidden ----------
    def test_groupless_published_item_renders(self):
        payload = self._menu(ignore_approval=True)
        self.assertIn(str(self.groupless_item.id), self._item_ids(payload))

    def test_items_under_soft_deleted_group_stay_hidden(self):
        payload = self._menu(ignore_approval=True)
        self.assertNotIn(
            str(self.item_under_deleted_group.id), self._item_ids(payload)
        )

    # ---- 20. flag inert for EVERY caller --------------------------------
    def test_ignore_approval_inert_for_authenticated_roleless_diner(self):
        diner = _owner('256700000902')  # employed nowhere → role-less
        payload = self._menu(ignore_approval=True, user=diner)
        item_ids = self._item_ids(payload)
        self.assertIn(str(self.pub_item.id), item_ids)          # positive control
        self.assertNotIn(str(self.unapproved_item.id), item_ids)
        self.assertNotIn(str(self.disabled_item.id), item_ids)
        self.assertNotIn(str(self.unapproved_section.id), self._section_ids(payload))


class MenuExtrasReEntryTests(TestCase):
    """extras_applicable must not re-admit an unpublished item into the public
    payload (SerializerPublicGetMenuItem.get_extras)."""

    def setUp(self):
        self.owner = _owner('256700000903')
        self.restaurant = Restaurant.objects.create(
            name='Extras R', location='extras-r', owner=self.owner,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.published_extra = MenuItem.objects.create(
            name='Published Extra', section=self.section, primary_price=500,
            approved=True, enabled=True, is_extra=True,
        )
        self.unpublished_extra = MenuItem.objects.create(
            name='Unpublished Extra', section=self.section, primary_price=500,
            approved=False, enabled=False, is_extra=True,
        )
        self.parent = MenuItem.objects.create(
            name='Parent', section=self.section, primary_price=1000,
            approved=True, enabled=True,
            extras_applicable=[
                str(self.published_extra.id), str(self.unpublished_extra.id),
            ],
        )

    def test_unpublished_extra_absent_from_public_payload(self):
        client = APIClient()
        response = client.get(
            SHOW_MENU_PATH, {'restaurant': str(self.restaurant.id)},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        parent = None
        for section in payload['data']:
            for item in section['items']:
                if item['id'] == str(self.parent.id):
                    parent = item
        self.assertIsNotNone(parent)
        extra_ids = {str(e['id']) for e in parent['extras']}
        self.assertIn(str(self.published_extra.id), extra_ids)      # control
        self.assertNotIn(str(self.unpublished_extra.id), extra_ids)  # leak closed


class UpsellReEntryTests(TestCase):
    """The upsell carousel must drop entries whose menu item is no longer
    published on the public path, but keep them for the operator view."""

    def setUp(self):
        self.owner = _owner('256700000904')
        self.restaurant = Restaurant.objects.create(
            name='Upsell R', location='upsell-r', owner=self.owner,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant,
            approved=True, enabled=True, available=True,
        )
        self.published_item = MenuItem.objects.create(
            name='Upsell Published', section=self.section, primary_price=1000,
            approved=True, enabled=True,
        )
        self.unpublished_item = MenuItem.objects.create(
            name='Upsell Unpublished', section=self.section, primary_price=1000,
            approved=False, enabled=False,
        )
        self.config = UpsellConfig.objects.create(
            restaurant=self.restaurant, enabled=True,
        )
        UpsellItem.objects.create(
            config=self.config, menu_item=self.published_item, listing_position=1,
        )
        UpsellItem.objects.create(
            config=self.config, menu_item=self.unpublished_item, listing_position=2,
        )

    def test_public_only_context_prunes_unpublished_upsell_item(self):
        public_ids = {
            str(i['item_id'])
            for i in UpsellConfigSerializer(
                self.config, context={'public_only': True}
            ).data['items']
        }
        self.assertIn(str(self.published_item.id), public_ids)
        self.assertNotIn(str(self.unpublished_item.id), public_ids)

    def test_operator_view_still_shows_unpublished_upsell_item(self):
        operator_ids = {
            str(i['item_id'])
            for i in UpsellConfigSerializer(self.config).data['items']
        }
        self.assertIn(str(self.published_item.id), operator_ids)
        self.assertIn(str(self.unpublished_item.id), operator_ids)

    def test_public_menu_payload_excludes_unpublished_upsell_item(self):
        client = APIClient()
        response = client.get(
            SHOW_MENU_PATH, {'restaurant': str(self.restaurant.id)},
        )
        self.assertEqual(response.status_code, 200)
        upsell = response.json().get('upsell')
        self.assertIsNotNone(upsell)
        item_ids = {i['item_id'] for i in upsell['items']}
        self.assertIn(str(self.published_item.id), item_ids)
        self.assertNotIn(str(self.unpublished_item.id), item_ids)
