"""
D06 — the writers that were silently reverting committed policy.

Since D06 both order boundaries enforce `accepting_orders`, the table's QR mode
and table/restaurant liveness, and the diner capability channel enforces
`qr_version`. Enforcement is only as good as the LAST WRITE to those columns,
and three production paths were writing them without meaning to — by saving a
whole row from an instance loaded before their own decision.

That is not a race in the usual sense. It needs no unlucky timing to be wrong:
it is a full-row UPDATE carrying stale values for columns the writer never
looked at, so anything committed in between is overwritten by definition. Each
test below reproduces it DETERMINISTICALLY, by firing the competing write at the
real seam between the stale read and the save, and each carries a negative
control proving the writer still does its own job.

The fourth (`restaurants` PUT taking the exclusive admission lock) is NOT here:
its correctness is about lock ORDER and cross-process exclusion, which a
single-connection test cannot observe, and asserting "it blocked" without a
negative control is exactly the trap `tests_table_allocation_lock` records.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live, RestaurantStatus_Suspended,
)
from restaurants_app.controllers.first_time_batch_approval import (
    first_time_batch_approval,
)
from restaurants_app.models import (
    DiningArea, MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User


class WriterFixture(TestCase):

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(
            first_name='Writer', last_name='Owner', email='writer@test.com',
            phone_number='256700000771', username='256700000771',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Writer R', location='loc', owner=self.owner,
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
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant,
            approved=False, enabled=False, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price=Decimal('10000'),
            approved=False, enabled=False, available=True, in_stock=True,
        )

    def _auth(self):
        return {'id': str(self.owner.id), 'username': self.owner.username,
                'user_id': str(self.owner.id)}


class MenuApprovalDoesNotRevertRestaurantPolicyTests(WriterFixture):
    """`first_time_batch_approval` loaded the restaurant, did its work, and then
    called a bare `restaurant.save()` — a full-row write from an instance read
    before the transaction opened.

    So an owner pausing ordering mid-service had the pause undone by a manager
    approving the menu, with nothing reported to either of them. `status` went
    the same way, which is worse: the commercial lifecycle has exactly ONE
    writer, and keeping the field out of EDIT_INFORMATION and read_only on the
    serializer are two walls a full-row save walks straight past.
    """

    def _approve(self, on_check=None):
        """Approve the menu, optionally committing a competing write AT A REAL
        SEAM between the stale read and the save.

        The seam is the action-log lookup: the restaurant instance has been
        loaded, the approval has not been written, and the code is about to
        reach `restaurant.save()`. It is chosen because it is UNCONDITIONALLY
        reached on the approve branch — the two `is_restaurant_owner` calls
        beside it are guarded by a comparison of a `User` FK against a string
        id, so they never run and a side effect hung on them would have proved
        nothing while appearing to pass.
        """
        target = (
            'restaurants_app.controllers.first_time_batch_approval.MONGO_DB'
        )

        class _Collection:
            @staticmethod
            def find(*args, **kwargs):
                if on_check is not None:
                    on_check()
                return []

        class _Db:
            @staticmethod
            def __getitem__(name):
                return _Collection

        with patch(target, _Db()):
            return first_time_batch_approval(
                restaurant_id=str(self.restaurant.id),
                approval_decision='approve',
                auth=self._auth(),
                user=self.owner,
            )

    def test_a_pause_committed_mid_approval_survives(self):
        def pause():
            Restaurant.objects.filter(pk=self.restaurant.pk).update(
                accepting_orders=False)

        result = self._approve(on_check=pause)

        self.assertEqual(result.get('status'), 200, result)
        self.restaurant.refresh_from_db()
        self.assertFalse(
            self.restaurant.accepting_orders,
            'a menu approval must not resume trading somebody deliberately paused',
        )

    def test_a_lifecycle_transition_committed_mid_approval_survives(self):
        def suspend():
            Restaurant.objects.filter(pk=self.restaurant.pk).update(
                status=RestaurantStatus_Suspended)

        result = self._approve(on_check=suspend)

        self.assertEqual(result.get('status'), 200, result)
        self.restaurant.refresh_from_db()
        self.assertEqual(
            self.restaurant.status, RestaurantStatus_Suspended,
            'lifecycle has one writer; a full-row save is not it',
        )

    def test_it_writes_ONLY_the_columns_the_decision_owns(self):
        """The structural statement, not an inference from one scenario: a
        writer that only writes what it decided cannot revert anything, whether
        or not anybody thought of the column."""
        captured = []
        original = Restaurant.save

        def _capture(self_, *args, **kwargs):
            captured.append(kwargs.get('update_fields'))
            return original(self_, *args, **kwargs)

        with patch.object(Restaurant, 'save', _capture):
            self._approve()

        self.assertEqual(len(captured), 1, captured)
        self.assertEqual(
            set(captured[0] or []),
            {'first_time_menu_approval', 'first_time_menu_approval_decision'},
        )

    def test_the_negative_control_it_still_approves_the_menu(self):
        result = self._approve()
        self.assertEqual(result.get('status'), 200, result)
        self.restaurant.refresh_from_db()
        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.assertTrue(self.restaurant.first_time_menu_approval)
        self.assertEqual(
            self.restaurant.first_time_menu_approval_decision, 'approve')
        self.assertTrue(self.section.approved and self.section.enabled)
        self.assertTrue(self.item.approved and self.item.enabled)


class TableStatusDoesNotRevertTheQrGenerationTests(WriterFixture):
    """`_update_status` read the table unlocked, mutated it in Python and called
    a bare `table.save()`.

    `qr_version` is what REVOKES every diner credential and session outstanding
    for a table, so an operator regenerating the QR and then — or while —
    somebody marked the table out of service had the revocation written back to
    its old value. A security control undone by an unrelated operator action,
    with nothing reported.
    """

    def _post(self, on_gate=None, status='out_of_service'):
        target = 'restaurants_app.endpoints.table_actions.can_user_access_module'

        def _side_effect(*args, **kwargs):
            if on_gate is not None:
                on_gate()
            return True

        # A REAL bearer token through the real client: this endpoint decodes
        # the JWT itself, so `force_authenticate` would leave it unauthenticated
        # and every assertion below would pass against a 401.
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.owner).access_token)
        with patch(target, side_effect=_side_effect):
            return self.client.post(
                '/api/v1/restaurant-setup/table-actions/update-status/',
                data={'table_id': str(self.table.id), 'status': status},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {token}',
            )

    def test_a_qr_regeneration_committed_mid_request_survives(self):
        original = self.table.qr_version

        def regenerate():
            Table.objects.filter(pk=self.table.pk).update(
                qr_version=original + 1)

        response = self._post(on_gate=regenerate)

        self.assertEqual(response.status_code, 200, response.data)
        self.table.refresh_from_db()
        self.assertEqual(
            self.table.qr_version, original + 1,
            'a status change must not un-revoke diner credentials',
        )

    def test_a_qr_mode_change_committed_mid_request_survives(self):
        def close_ordering():
            Table.objects.filter(pk=self.table.pk).update(qr_mode='menu_only')

        self._post(on_gate=close_ordering)

        self.table.refresh_from_db()
        self.assertEqual(self.table.qr_mode, 'menu_only')

    def test_it_writes_ONLY_status_and_is_active(self):
        captured = []
        original = Table.save

        def _capture(self_, *args, **kwargs):
            captured.append(kwargs.get('update_fields'))
            return original(self_, *args, **kwargs)

        with patch.object(Table, 'save', _capture):
            self._post()

        self.assertEqual(len(captured), 1, captured)
        self.assertEqual(set(captured[0] or []), {'status', 'is_active'})

    def test_the_negative_control_it_still_updates_the_status(self):
        response = self._post()
        self.assertEqual(response.status_code, 200, response.data)
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, 'out_of_service')
        self.assertFalse(self.table.is_active)

    def test_the_negative_control_it_still_restores_is_active(self):
        Table.objects.filter(pk=self.table.pk).update(
            status='out_of_service', is_active=False)
        self.table.refresh_from_db()

        response = self._post(status='available')

        self.assertEqual(response.status_code, 200, response.data)
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, 'available')
        self.assertTrue(self.table.is_active)
