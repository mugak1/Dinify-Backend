"""
D06 completion, G1a — THE CATALOGUE-WRITER INVENTORY AND ITS EXEMPTIONS
(CATALOGUE-ADMISSION-00).

`tests_catalogue_admission_concurrency` proves the barrier WORKS. This file
proves it is in the right places and stays there, and — the part that matters
more over time — that every writer deliberately left OUTSIDE it still satisfies
the reason it was left out.

AN INVENTORY RATHER THAN AN AST GUESS. Catalogue writes reach the database
through generic machinery (`Secretary`, a queryset held in a local, a serializer's
`update`), so a scan for "writes a MenuItem" either misses those or drowns in
false positives. Two properties are enforced instead, and together they are
tighter than a scan: every ENLISTED surface is asserted to take the barrier by
DRIVING IT and watching, and no module may reference the barrier without being
named here.

EXEMPTIONS ARE ENFORCED, NOT RECORDED. Each one below is a claim about why that
writer cannot change an acceptance verdict, and each has a test that fails if the
claim stops being true. A writer that starts doing something its exemption does
not cover fails this file and has to be enlisted.
"""
import ast
import os
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from misc_app.management.commands.vacuum_deleted_records import (
    ConVacuumDeletedRecords,
)
from restaurants_app.controllers.first_time_batch_approval import (
    first_time_batch_approval,
)
from restaurants_app.controllers.menu_publication import (
    group_live, section_live,
)
from restaurants_app.endpoints.restaurant_setup import (
    _ADMISSION_BARRIER_RECORDS, _CATALOGUE_DELETE_BLOCKERS,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee,
    RestaurantTag, SectionGroup, Table,
)
from users_app.models import User

BARRIER = 'lock_catalogue_for_write'

#: WHO MAY TAKE THE BARRIER, and why each one needs it. A module referencing
#: `lock_catalogue_for_write` and absent from here fails the scan below: a new
#: participant is a lock-ordering decision and must be a deliberate edit.
ENLISTED = {
    'restaurants_app/controllers/catalogue_admission.py':
        'the barrier itself',
    'restaurants_app/endpoints/restaurant_setup.py':
        'the menu PUT and DELETE, plus the restaurants pause writer',
    'restaurants_app/endpoints/restaurant_tags.py':
        'a tag rename or delete moves the allergen labels acceptance compares',
    'orders_app/endpoints_kitchen.py':
        'the 86 route writes in_stock, which acceptance reads',
}

#: WHO DELIBERATELY DOES NOT, and the claim that keeps each one out. Every entry
#: has a test below.
EXEMPT = {
    'catalogue CREATE':
        'a row that does not exist cannot be named by a saved quote line, so '
        'creating one changes no verdict — and CREATE is where Secretary makes '
        'its one SYNCHRONOUS MongoDB call, which must never sit inside a lock '
        'every diner order waits on',
    'first_time_batch_approval':
        'its only catalogue write widens publication (approve/enable); a stale '
        'read can refuse and be retried, never accept something it should not',
    'ConVacuumDeletedRecords.vacuum':
        'a multi-tenant sweep whose every catalogue write lands on a row already '
        'under a non-live parent, so no acceptance verdict can move',
    'reorder (listing_position / display_order)':
        'acceptance reads neither column',
}


class TheBarrierIsWhereItSaysItIsTests(TestCase):
    """Nothing takes this lock without being written down."""

    def _production_modules(self):
        skip = ('/migrations/', '/.git/', 'node_modules', '/staticfiles/')
        for root, _dirs, files in os.walk('.'):
            if any(s in root for s in skip):
                continue
            for name in files:
                if not name.endswith('.py') or name.startswith('tests'):
                    continue
                path = os.path.join(root, name).lstrip('./')
                if any(s in path for s in skip) or 'test' in name:
                    continue
                yield path

    def test_only_the_named_modules_reference_the_barrier(self):
        referencing = set()
        for path in self._production_modules():
            try:
                source = open(path).read()
            except OSError:                        # pragma: no cover - defensive
                continue
            if BARRIER in source:
                referencing.add(path)
        self.assertEqual(
            referencing, set(ENLISTED),
            'a module joined or left the admission barrier without the '
            'inventory being updated — that is a lock-ordering decision',
        )

    def test_every_enlisted_module_still_calls_it(self):
        for path in ENLISTED:
            with self.subTest(module=path):
                tree = ast.parse(open(path).read())
                called = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == BARRIER
                    for node in ast.walk(tree)
                )
                self.assertTrue(
                    called or path.endswith('catalogue_admission.py'),
                    f'{path} imports the barrier but never calls it',
                )

    def test_the_record_sets_are_exactly_what_is_documented(self):
        self.assertEqual(
            set(_ADMISSION_BARRIER_RECORDS),
            {'restaurants', 'menusections', 'sectiongroups', 'menuitems'},
        )
        self.assertEqual(
            set(_CATALOGUE_DELETE_BLOCKERS),
            {'menusections', 'sectiongroups', 'menuitems'},
        )


class CatalogueFixture(TestCase):

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Inv', last_name='Owner', email='inv_owner@test.com',
            phone_number='256700000992', username='256700000992',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Inv R', location='loc', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER], active=True,
        )
        self.area = DiningArea.objects.create(
            name='Main', restaurant=self.restaurant)
        self.table = Table.objects.create(
            number=1, restaurant=self.restaurant, dining_area=self.area,
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

    def _bearer(self):
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


class EveryEnlistedSurfaceTakesItTests(CatalogueFixture):
    """Driven through the REAL routes, watching the barrier be called.

    The assertion is on the ARGUMENT as well as the call: a barrier taken for
    the wrong restaurant excludes nothing at the right one.
    """

    def _watch(self):
        return patch(
            'restaurants_app.controllers.catalogue_admission'
            '.lock_admission_exclusive'
        )

    def test_the_menu_item_PUT(self):
        with self._watch() as locked:
            response = self.client.put(
                '/api/v1/restaurant-setup/menuitems/',
                data={'id': str(self.item.id), 'description': 'new'},
                content_type='application/json', **self._bearer(),
            )
        self.assertIn(response.status_code, (200, 400), response.data)
        self._assert_locked(locked)

    def test_the_menu_section_PUT(self):
        with self._watch() as locked:
            self.client.put(
                '/api/v1/restaurant-setup/menusections/',
                data={'id': str(self.section.id), 'available': False},
                content_type='application/json', **self._bearer(),
            )
        self._assert_locked(locked)

    def test_the_menu_item_DELETE(self):
        with self._watch() as locked:
            self.client.delete(
                '/api/v1/restaurant-setup/menuitems/',
                data={'id': str(self.item.id)},
                content_type='application/json', **self._bearer(),
            )
        self._assert_locked(locked)

    def test_the_menu_section_DELETE(self):
        MenuItem.objects.filter(pk=self.item.pk).update(deleted=True)
        with self._watch() as locked:
            self.client.delete(
                '/api/v1/restaurant-setup/menusections/',
                data={'id': str(self.section.id)},
                content_type='application/json', **self._bearer(),
            )
        self._assert_locked(locked)

    def test_the_kitchen_86_route(self):
        with self._watch() as locked:
            response = self.client.put(
                f'/api/v1/kitchen/menu-items/{self.item.id}/stock/',
                data={'in_stock': False},
                content_type='application/json', **self._bearer(),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self._assert_locked(locked)

    def test_the_tag_rename(self):
        tag = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
        )
        with self._watch() as locked:
            response = self.client.patch(
                f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/',
                data={'name': 'Peanuts'},
                content_type='application/json', **self._bearer(),
            )
        self.assertEqual(response.status_code, 200, getattr(response, 'data', response))
        self._assert_locked(locked)

    def test_the_tag_delete(self):
        tag = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Nuts', category='allergen',
        )
        with self._watch() as locked:
            self.client.delete(
                f'/api/v1/restaurant-setup/restaurant-tags/{tag.id}/',
                **self._bearer(),
            )
        self._assert_locked(locked)

    def _assert_locked(self, locked):
        self.assertTrue(locked.called, 'the admission barrier was not taken')
        ids = {str(call.args[0]) for call in locked.call_args_list}
        self.assertEqual(
            ids, {str(self.restaurant.id)},
            'the barrier must be taken for the restaurant being edited',
        )


class TheExemptionsStillHoldTests(CatalogueFixture):
    """Each exemption is a claim. These are the claims."""

    def test_menu_CREATE_takes_no_barrier(self):
        """A row that does not exist yet cannot be in anybody's saved quote."""
        with patch(
            'restaurants_app.controllers.catalogue_admission'
            '.lock_admission_exclusive'
        ) as locked:
            self.client.post(
                '/api/v1/restaurant-setup/menuitems/',
                data={'section': str(self.section.id), 'name': 'New dish',
                      'primary_price': '5000'},
                content_type='application/json', **self._bearer(),
            )
        self.assertFalse(
            locked.called,
            'CREATE must stay outside the lock — it is where Secretary makes '
            'its synchronous MongoDB call',
        )

    def test_first_time_approval_only_ever_WIDENS_publication(self):
        """Its exemption rests on direction. If it ever narrows, enlist it."""
        MenuSection.objects.filter(pk=self.section.pk).update(
            approved=False, enabled=False)
        MenuItem.objects.filter(pk=self.item.pk).update(
            approved=False, enabled=False)

        first_time_batch_approval(
            restaurant_id=str(self.restaurant.id),
            approval_decision='approve',
            auth={'user_id': str(self.owner.id), 'username': 'inv'},
            user=self.owner,
        )

        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.assertTrue(self.section.approved and self.section.enabled)
        self.assertTrue(self.item.approved and self.item.enabled)

    def test_first_time_REJECTION_writes_no_catalogue_row(self):
        before = (self.section.approved, self.section.enabled,
                  self.item.approved, self.item.enabled)

        first_time_batch_approval(
            restaurant_id=str(self.restaurant.id),
            approval_decision='reject',
            auth={'user_id': str(self.owner.id), 'username': 'inv'},
            user=self.owner,
            rejection_reason='not ready',
        )

        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(
            (self.section.approved, self.section.enabled,
             self.item.approved, self.item.enabled),
            before,
            'a rejection that narrowed publication would need the barrier',
        )

    def test_the_vacuum_only_writes_rows_already_under_a_dead_parent(self):
        """Its exemption in one sentence, as an assertion.

        The sweep soft-deletes items under a section or group that is ALREADY
        soft-deleted — which acceptance already refuses through `section_live` /
        `group_live` — so nothing it writes can change a verdict. If it ever
        starts touching a row under a LIVE parent, it becomes an eligibility
        writer and has to be enlisted.
        """
        group = SectionGroup.objects.create(
            name='Grill', section=self.section,
            approved=True, enabled=True, available=True,
        )
        live_item = MenuItem.objects.create(
            name='Live', section=self.section, primary_price=Decimal('1000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        doomed = MenuItem.objects.create(
            name='Doomed', section=self.section, section_group=group,
            primary_price=Decimal('1000'),
            approved=True, enabled=True, available=True, in_stock=True,
        )
        SectionGroup.objects.filter(pk=group.pk).update(deleted=True)
        group.refresh_from_db()
        self.assertFalse(group_live(group))

        ConVacuumDeletedRecords().vacuum()

        doomed.refresh_from_db()
        live_item.refresh_from_db()
        self.assertTrue(
            doomed.deleted,
            'the sweep did its job on the row under the dead group',
        )
        self.assertFalse(
            live_item.deleted,
            'THE EXEMPTION: the sweep never touches a row under a live parent',
        )
        self.section.refresh_from_db()
        self.assertTrue(section_live(self.section))

    def test_reorder_writes_no_field_acceptance_reads(self):
        """`listing_position` is not in the eligibility inventory, so a reorder
        cannot move a verdict and needs no barrier."""
        from orders_app.controllers.services import purchase_integrity
        source = open(
            'orders_app/controllers/services/purchase_integrity.py').read()
        self.assertNotIn('listing_position', source)
        self.assertNotIn('display_order', source)
        self.assertIsNotNone(purchase_integrity.inspect)
