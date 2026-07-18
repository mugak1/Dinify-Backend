"""
Two-tenant behavioural evidence for the write-surface hardening PR
(TENANT-ISO-PR5). Each class is the ``verified_by`` referenced by a
``SameTenant`` classification on a production write serializer, and proves the
runtime enforcement at the real chokepoint (endpoint + Secretary / serializer
validate), not just the classification's existence.

Also holds the input mass-assignment (privilege / generation / audit field)
proofs for the tables domain.
"""
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, SectionGroup, MenuItem,
    DiningArea, Table, Reservation, WaitlistEntry,
)
from restaurants_app.serializers import (
    SerializerPutSectionGroup, SerializerPutRestaurant, SerializerPutMenuItem,
    SerializerPutMenuSection, SerializerPutTable, SerializerPutDiningArea,
    SerializerPutRestaurantEmployee, SerializerRestaurantTag,
)
from users_app.models import User
from dinify_backend.configss.string_definitions import RestaurantStatus_Active
from dinify_backend.configs import ROLES


class _TwoTenantBase(TestCase):
    """Two active restaurants (A, B), each owned by a distinct owner, plus the
    tables/menu graph the subclasses reach across."""

    def setUp(self):
        self.client = APIClient()
        self.owner_a = User.objects.create_user(
            first_name='Ws', last_name='OwnerA', email='ws_a@test.com',
            phone_number='256700000510', username='256700000510',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='WS Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.owner_b = User.objects.create_user(
            first_name='Ws', last_name='OwnerB', email='ws_b@test.com',
            phone_number='256700000520', username='256700000520',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='WS Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        # A-side + B-side dining areas (cross-tenant target lives on B).
        self.area_a = DiningArea.objects.create(
            name='A Hall', restaurant=self.restaurant_a,
        )
        self.area_b = DiningArea.objects.create(
            name='B Hall', restaurant=self.restaurant_b,
        )

    def _auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _post(self, user, config_detail, body):
        return self.client.post(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, format='json', **self._auth(user),
        )

    def _put(self, user, path, body):
        return self.client.put(
            f'/api/v1/{path}', data=body, format='json', **self._auth(user),
        )


class TableDiningAreaTenantTests(_TwoTenantBase):
    """Table.dining_area must be same-tenant (SameTenant('restaurant_id'))."""

    def _table_body(self, dining_area, **over):
        body = {
            'restaurant': str(self.restaurant_a.id),
            'dining_area': str(dining_area.id),
            'number': 12,
            'min_capacity': 2,
            'max_capacity': 4,
            'shape': 'square',
        }
        body.update(over)
        return body

    def test_same_tenant_dining_area_succeeds(self):
        res = self._post(self.owner_a, 'tables', self._table_body(self.area_a))
        self.assertEqual(res.json().get('status'), 200)
        self.assertTrue(
            Table.objects.filter(
                restaurant=self.restaurant_a, dining_area=self.area_a
            ).exists()
        )

    def test_foreign_dining_area_is_rejected(self):
        res = self._post(self.owner_a, 'tables', self._table_body(self.area_b))
        # assert_fks_belong_to_restaurant → ValidationError → Secretary 400.
        self.assertEqual(res.json().get('status'), 400)
        self.assertFalse(
            Table.objects.filter(dining_area=self.area_b).exists()
        )


class ReservationFkTenantTests(_TwoTenantBase):
    """Reservation.table / server must be same-tenant."""

    def setUp(self):
        super().setUp()
        self.table_a = Table.objects.create(
            restaurant=self.restaurant_a, dining_area=self.area_a, number=1,
        )
        self.table_b = Table.objects.create(
            restaurant=self.restaurant_b, dining_area=self.area_b, number=1,
        )

    def _body(self, table, **over):
        body = {
            'restaurant': str(self.restaurant_a.id),
            'table': str(table.id),
            'guest_name': 'Guest', 'party_size': 2,
            'date_time': '2030-01-01T12:00:00Z',
        }
        body.update(over)
        return body

    def test_same_tenant_table_succeeds(self):
        res = self.client.post(
            '/api/v1/restaurant-setup/reservations/',
            data=self._body(self.table_a), format='json',
            **self._auth(self.owner_a),
        )
        self.assertIn(res.json().get('status'), (200, 201))

    def test_foreign_table_is_rejected(self):
        res = self.client.post(
            '/api/v1/restaurant-setup/reservations/',
            data=self._body(self.table_b), format='json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(res.json().get('status'), 400)
        self.assertFalse(Reservation.objects.filter(table=self.table_b).exists())


class WaitlistFkTenantTests(_TwoTenantBase):
    """WaitlistEntry.seated_table must be same-tenant."""

    def setUp(self):
        super().setUp()
        self.table_a = Table.objects.create(
            restaurant=self.restaurant_a, dining_area=self.area_a, number=1,
        )
        self.table_b = Table.objects.create(
            restaurant=self.restaurant_b, dining_area=self.area_b, number=1,
        )

    def _body(self, seated_table=None, **over):
        body = {
            'restaurant': str(self.restaurant_a.id),
            'guest_name': 'Guest', 'party_size': 2,
        }
        if seated_table is not None:
            body['seated_table'] = str(seated_table.id)
        body.update(over)
        return body

    def test_same_tenant_seated_table_succeeds(self):
        res = self.client.post(
            '/api/v1/restaurant-setup/waitlist/',
            data=self._body(self.table_a), format='json',
            **self._auth(self.owner_a),
        )
        self.assertIn(res.json().get('status'), (200, 201))

    def test_foreign_seated_table_is_rejected(self):
        res = self.client.post(
            '/api/v1/restaurant-setup/waitlist/',
            data=self._body(self.table_b), format='json',
            **self._auth(self.owner_a),
        )
        self.assertEqual(res.json().get('status'), 400)
        self.assertFalse(
            WaitlistEntry.objects.filter(seated_table=self.table_b).exists()
        )


class SectionGroupSectionTenantTests(_TwoTenantBase):
    """SectionGroup.section must be same-tenant (SameTenant('restaurant_id'))."""

    def setUp(self):
        super().setUp()
        self.section_a = MenuSection.objects.create(
            name='A Sec', restaurant=self.restaurant_a, listing_position=0,
        )
        self.section_b = MenuSection.objects.create(
            name='B Sec', restaurant=self.restaurant_b, listing_position=0,
        )
        self.group_a = SectionGroup.objects.create(
            name='A Grp', section=self.section_a,
        )

    def test_serializer_rejects_foreign_section_on_move(self):
        # The load-bearing validate() pin: an UPDATE that reassigns section to
        # another tenant's section is rejected (defense-in-depth; the endpoint
        # also strips section as it is not in EI_SECTION_GROUP).
        serializer = SerializerPutSectionGroup(
            self.group_a, data={'section': str(self.section_b.id)}, partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('section', serializer.errors)

    def test_same_tenant_section_move_is_accepted_by_serializer(self):
        section_a2 = MenuSection.objects.create(
            name='A Sec2', restaurant=self.restaurant_a, listing_position=1,
        )
        serializer = SerializerPutSectionGroup(
            self.group_a, data={'section': str(section_a2.id)}, partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class TableQrMassAssignmentTests(_TwoTenantBase):
    """Ordinary table create/update can never set the QR generation counter /
    rotation timestamp or audit fields (regenerate-qr stays the only rotation
    primitive)."""

    def test_create_cannot_set_qr_version_or_regenerated_at(self):
        res = self._post(self.owner_a, 'tables', {
            'restaurant': str(self.restaurant_a.id),
            'dining_area': str(self.area_a.id),
            'number': 7, 'min_capacity': 2, 'max_capacity': 4, 'shape': 'square',
            'qr_version': 99, 'qr_regenerated_at': '2020-01-01T00:00:00Z',
            'has_qr': True,
        })
        self.assertEqual(res.json().get('status'), 200)
        table = Table.objects.get(restaurant=self.restaurant_a, number='7')
        self.assertEqual(table.qr_version, 1)  # server default, not the injected 99
        self.assertIsNone(table.qr_regenerated_at)

    def test_update_cannot_rotate_qr_version_or_regenerated_at(self):
        table = Table.objects.create(
            restaurant=self.restaurant_a, dining_area=self.area_a, number=8,
            qr_version=1,
        )
        res = self._put(self.owner_a, 'restaurant-setup/tables/', {
            'id': str(table.id),
            'display_name': 'Renamed',
            'qr_version': 50,
            'qr_regenerated_at': '2020-01-01T00:00:00Z',
        })
        self.assertEqual(res.json().get('status'), 200)
        table.refresh_from_db()
        self.assertEqual(table.qr_version, 1)  # unchanged
        self.assertIsNone(table.qr_regenerated_at)
        self.assertEqual(table.display_name, 'Renamed')  # the real edit applied


class RestaurantMassAssignmentTests(_TwoTenantBase):
    """SerializerPutRestaurant cannot reassign server-owned identity / audit
    fields (owner, created_by, deleted_by) from request input (TENANT-ISO-PR5).

    (Platform-owned status / flat_fee stay in EDIT_INFORMATION but are stripped
    for non-admins at the restaurant-setup endpoint per PR #211 — a separate,
    already-tested control; here we prove the serializer-level read_only wall.)
    """

    def test_owner_and_audit_fields_are_not_writable(self):
        ser = SerializerPutRestaurant(
            instance=self.restaurant_a,
            data={
                'name': 'Renamed A Ltd',            # a legitimate edit rides along
                'owner': str(self.owner_b.id),      # adversarial: steal ownership
                'created_by': str(self.owner_b.id),
                'deleted_by': str(self.owner_b.id),
            },
            partial=True,
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        for forbidden in ('owner', 'created_by', 'deleted_by'):
            self.assertNotIn(forbidden, ser.validated_data)
        ser.save()
        self.restaurant_a.refresh_from_db()
        self.assertEqual(self.restaurant_a.owner_id, self.owner_a.id)  # unchanged
        self.assertEqual(self.restaurant_a.name, 'Renamed A Ltd')      # applied


class ServerOwnedReadOnlyContractTests(TestCase):
    """
    Tripwire: the server-owned fields on every migrated production write
    serializer stay read_only (TENANT-ISO-PR5). If a refactor makes one of these
    writable again, a mass-assignment / cross-tenant-spoof surface reopens — this
    fails loudly. Complements the behavioural proofs (the Secretary server_values
    tests write these through the trusted channel).
    """

    def _assert_read_only(self, serializer_cls, field_names):
        fields = serializer_cls().fields
        for name in field_names:
            self.assertIn(name, fields, f'{serializer_cls.__name__}.{name} missing')
            self.assertTrue(
                fields[name].read_only,
                f'{serializer_cls.__name__}.{name} must be read_only (server-owned)',
            )

    def test_menu_item_server_owned_fields_read_only(self):
        self._assert_read_only(SerializerPutMenuItem, (
            'created_by', 'approved', 'enabled', 'deleted', 'deleted_by',
            'time_deleted', 'deletion_reason',
        ))

    def test_menu_section_restaurant_read_only(self):
        self._assert_read_only(SerializerPutMenuSection, (
            'restaurant', 'approved', 'enabled', 'created_by',
        ))

    def test_table_generation_and_audit_fields_read_only(self):
        self._assert_read_only(SerializerPutTable, (
            'qr_version', 'qr_regenerated_at', 'created_by',
        ))

    def test_dining_area_and_employee_restaurant_read_only(self):
        self._assert_read_only(SerializerPutDiningArea, ('restaurant', 'created_by'))
        self._assert_read_only(SerializerPutRestaurantEmployee, ('restaurant', 'created_by'))

    def test_restaurant_tag_restaurant_read_only(self):
        self._assert_read_only(SerializerRestaurantTag, ('restaurant',))

    def test_review_order_and_support_parent_read_only(self):
        from reviews_app.serializers import ReviewWriteSerializer
        from support_app.serializers import SupportIssueWriteSerializer
        self._assert_read_only(ReviewWriteSerializer, ('order',))
        self._assert_read_only(SupportIssueWriteSerializer, ('restaurant', 'created_by'))
