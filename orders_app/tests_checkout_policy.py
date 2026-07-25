"""
Anonymous checkout eligibility & extra-applicability — order-path boundary (PR2).

Complements orders_app/tests.py::TestOrderPublicationGate with the drift the old
gate missed: at checkout a parent must sit in a currently-VISIBLE section
(available + schedule) and a currently-visible GROUP (approved/enabled/available),
not merely a not-deleted one; and every submitted extra must be an applicable,
same-restaurant, published is_extra item — enforced for staff too. Also proves the
authoritative check lives inside the transaction (a direct _create_order call
cannot bypass it, and a rejection leaves no Order / OrderItem / counter row), while
an idempotent replay still returns the original order after the menu changes.

available / in_stock remain the zero-and-flag reconciliation path — never a hard
publication rejection — which these tests lock in.
"""

from django.test import TestCase

from users_app.models import User
from restaurants_app.models import (
    Restaurant, MenuSection, SectionGroup, MenuItem, Table,
)
from orders_app.models import Order, OrderItem, RestaurantDailyOrderCounter
from orders_app.controllers.con_orders import ConOrder, NOT_ON_MENU_MESSAGE
from orders_app.controllers.services.create_order import _create_order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live, RESTAURANT_OWNER,
)


def _owner(phone):
    return User.objects.create_user(
        first_name='Chk', last_name='Owner', email=f'{phone}@test.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class CheckoutPolicyBase(TestCase):
    def setUp(self):
        self.owner = _owner('256700020001')
        self.restaurant = Restaurant.objects.create(
            name='Chk R', location='chk', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.staff = self.owner  # owner authorises the staff/admin order path
        self.section = self._section('Section')
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )
        # Published controls.
        self.pub_group = self._group('Pub Group')
        self.published_item = self._item('Published Item', group=None)
        self.grouped_item = self._item('Grouped Item', group=self.pub_group)

        # A valid extra + parent that accepts it.
        self.valid_extra = self._item('Valid Extra', group=None, is_extra=True,
                                       primary_price=500)
        self.parent = self._item('Extra Parent', group=None, has_extras=True)
        self.parent.extras_applicable = [str(self.valid_extra.id)]
        self.parent.save(update_fields=['extras_applicable'])

        # Cross-tenant restaurant B for foreign controls.
        self.restaurant_b = Restaurant.objects.create(
            name='Chk R B', location='chk-b', owner=_owner('256700020002'),
            status=RestaurantStatus_Live,
        )
        self.section_b = MenuSection.objects.create(
            name='B Section', restaurant=self.restaurant_b,
            approved=True, enabled=True, available=True, availability='always',
        )
        self.foreign_extra = MenuItem.objects.create(
            name='Foreign Extra', section=self.section_b, primary_price=500,
            approved=True, enabled=True, is_extra=True,
        )

    def _section(self, name, **kw):
        opts = dict(approved=True, enabled=True, available=True,
                    availability='always')
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

    def _order(self, items, created_by=None):
        return ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk),
            table_id=str(self.table.pk),
            items=items,
            created_by=created_by,
        )

    def _assert_rejected_no_row(self, items, created_by=None):
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()
        resp = self._order(items, created_by=created_by)
        self.assertEqual(resp['status'], 400)
        self.assertEqual(resp['message'], NOT_ON_MENU_MESSAGE)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)
        return resp


class CheckoutParentPublicationTests(CheckoutPolicyBase):
    """Parent publication now matches the menu: section available + schedule and
    full group publication are enforced (previously skipped at checkout)."""

    def test_published_parent_succeeds(self):
        before = Order.objects.count()
        resp = self._order([{'item': str(self.published_item.pk), 'quantity': 1}])
        self.assertEqual(resp['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    def test_parent_in_unavailable_section_rejected(self):
        s = self._section('Unavail S', available=False)
        item = self._item('X', section=s)
        self._assert_rejected_no_row([{'item': str(item.pk), 'quantity': 1}])

    def test_parent_in_scheduled_off_section_rejected(self):
        # A scheduled section whose only slot matches NO day is always inactive
        # (deterministic — no dependence on the wall clock).
        s = self._section(
            'Sched Off', availability='scheduled',
            schedules=[{'days': [], 'startTime': '00:00', 'endTime': '23:59'}],
        )
        item = self._item('Y', section=s)
        self._assert_rejected_no_row([{'item': str(item.pk), 'quantity': 1}])

    def test_parent_under_unapproved_group_rejected(self):
        g = self._group('Unapproved G', approved=False)
        item = self._item('Z1', group=g)
        self._assert_rejected_no_row([{'item': str(item.pk), 'quantity': 1}])

    def test_parent_under_disabled_group_rejected(self):
        g = self._group('Disabled G', enabled=False)
        item = self._item('Z2', group=g)
        self._assert_rejected_no_row([{'item': str(item.pk), 'quantity': 1}])

    def test_parent_under_unavailable_group_rejected(self):
        g = self._group('Unavailable G', available=False)
        item = self._item('Z3', group=g)
        self._assert_rejected_no_row([{'item': str(item.pk), 'quantity': 1}])

    def test_published_grouped_parent_succeeds(self):
        before = Order.objects.count()
        resp = self._order([{'item': str(self.grouped_item.pk), 'quantity': 1}])
        self.assertEqual(resp['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)

    def test_unavailable_parent_zero_and_flagged_not_rejected(self):
        # available=False is stale-basket reconciliation, NOT a publication reject.
        item = self._item('Unavail Item', group=None, available=False)
        before = Order.objects.count()
        resp = self._order([{'item': str(item.pk), 'quantity': 1}])
        self.assertEqual(resp['status'], 200)
        self.assertEqual(Order.objects.count(), before + 1)
        line = OrderItem.objects.get(
            order__id=resp['data']['order_details']['id'], item=item,
        )
        self.assertEqual(line.quantity, 0)
        self.assertFalse(line.available)


class CheckoutExtraApplicabilityTests(CheckoutPolicyBase):
    def test_valid_configured_extra_succeeds(self):
        before = Order.objects.count()
        resp = self._order([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(self.valid_extra.pk)],
        }])
        self.assertEqual(resp['status'], 200, resp)
        self.assertEqual(Order.objects.count(), before + 1)

    def test_extra_not_in_allowlist_rejected(self):
        other_extra = self._item('Other Extra', group=None, is_extra=True)
        self._assert_rejected_no_row([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(other_extra.pk)],
        }])

    def test_extra_on_parent_without_has_extras_rejected(self):
        # published_item has_extras=False → an extra list is rejected.
        self._assert_rejected_no_row([{
            'item': str(self.published_item.pk), 'quantity': 1,
            'extras': [str(self.valid_extra.pk)],
        }])

    def test_foreign_extra_rejected(self):
        self._assert_rejected_no_row([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(self.foreign_extra.pk)],
        }])

    def test_non_is_extra_item_rejected(self):
        plain = self._item('Plain Not Extra', group=None, is_extra=False)
        self.parent.extras_applicable = [str(plain.pk)]
        self.parent.save(update_fields=['extras_applicable'])
        self._assert_rejected_no_row([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(plain.pk)],
        }])

    def test_self_reference_extra_rejected(self):
        self.parent.is_extra = True
        self.parent.extras_applicable = [str(self.parent.pk)]
        self.parent.save(update_fields=['is_extra', 'extras_applicable'])
        self._assert_rejected_no_row([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(self.parent.pk)],
        }])

    def test_malformed_and_nonexistent_extra_rejected(self):
        import uuid as _uuid
        for bad in ('not-a-uuid', str(_uuid.uuid4())):
            self._assert_rejected_no_row([{
                'item': str(self.parent.pk), 'quantity': 1, 'extras': [bad],
            }])

    def test_duplicate_extra_rejected(self):
        self._assert_rejected_no_row([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(self.valid_extra.pk), str(self.valid_extra.pk)],
        }])

    def test_min_selections_enforced_after_validation(self):
        self.parent.extras_min_selections = 1
        self.parent.save(update_fields=['extras_min_selections'])
        resp = self._order([{
            'item': str(self.parent.pk), 'quantity': 1, 'extras': [],
        }])
        self.assertEqual(resp['status'], 400)
        self.assertIn('at least', resp['message'])

    def test_max_selections_enforced_after_validation(self):
        second = self._item('Second Extra', group=None, is_extra=True)
        self.parent.extras_applicable = [
            str(self.valid_extra.pk), str(second.pk),
        ]
        self.parent.extras_max_selections = 1
        self.parent.save(update_fields=['extras_applicable', 'extras_max_selections'])
        resp = self._order([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(self.valid_extra.pk), str(second.pk)],
        }])
        self.assertEqual(resp['status'], 400)
        self.assertIn('maximum', resp['message'])

    def test_mixed_valid_and_invalid_extra_rejects_whole_order(self):
        self._assert_rejected_no_row([{
            'item': str(self.parent.pk), 'quantity': 1,
            'extras': [str(self.valid_extra.pk), str(self.foreign_extra.pk)],
        }])


class CheckoutStaffPathTests(CheckoutPolicyBase):
    def _staff(self):
        # An owner employed at the restaurant, authorised for the admin order path.
        from restaurants_app.models import RestaurantEmployee
        RestaurantEmployee.objects.get_or_create(
            user=self.staff, restaurant=self.restaurant,
            defaults={'roles': [RESTAURANT_OWNER]},
        )
        return self.staff

    def test_staff_can_order_unpublished_parent(self):
        s = self._section('Staff Unavail S', available=False)
        item = self._item('Staff Item', section=s)
        before = Order.objects.count()
        resp = self._order(
            [{'item': str(item.pk), 'quantity': 1}], created_by=self._staff(),
        )
        self.assertEqual(resp['status'], 200, resp)
        self.assertEqual(Order.objects.count(), before + 1)

    def test_staff_cannot_attach_foreign_extra(self):
        self._assert_rejected_no_row(
            [{'item': str(self.parent.pk), 'quantity': 1,
              'extras': [str(self.foreign_extra.pk)]}],
            created_by=self._staff(),
        )

    def test_staff_cannot_attach_non_applicable_extra(self):
        other = self._item('Staff Other Extra', group=None, is_extra=True)
        self._assert_rejected_no_row(
            [{'item': str(self.parent.pk), 'quantity': 1,
              'extras': [str(other.pk)]}],
            created_by=self._staff(),
        )


class CheckoutTransactionBoundaryTests(CheckoutPolicyBase):
    """The authoritative validation lives INSIDE _create_order's transaction."""

    def test_direct_create_order_cannot_bypass_publication(self):
        # Unpublish the item, then call the service directly (no endpoint / no
        # preflight). It must still be rejected, with no rows and no counter.
        item = self._item('Unpub Direct', group=None, approved=False)
        result = _create_order(
            restaurant=self.restaurant, table=self.table,
            items=[{'item': str(item.pk), 'quantity': 1}],
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['message'], NOT_ON_MENU_MESSAGE)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertFalse(
            RestaurantDailyOrderCounter.objects.filter(
                restaurant=self.restaurant,
            ).exists()
        )

    def test_idempotent_replay_returns_original_after_unpublish(self):
        import uuid as _uuid
        key = str(_uuid.uuid4())
        self._order([{'item': str(self.published_item.pk), 'quantity': 1}])
        # Re-run with the SAME client_order_id after the item is unpublished.
        first_with_key = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(self.table.pk),
            items=[{'item': str(self.published_item.pk), 'quantity': 1}],
            client_order_id=key,
        )
        self.assertEqual(first_with_key['status'], 200)
        self.published_item.approved = False
        self.published_item.save(update_fields=['approved'])
        replay = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(self.table.pk),
            items=[{'item': str(self.published_item.pk), 'quantity': 1}],
            client_order_id=key,
        )
        # The idempotent replay returns the original order even though the item is
        # now unpublished (the key wins; only genuinely NEW orders are re-validated).
        self.assertEqual(replay['status'], 200)
        self.assertEqual(
            Order.objects.filter(client_order_id=key).count(), 1,
        )

    def test_new_order_with_now_unpublished_item_rejected(self):
        # A genuinely new submission (fresh key) for the now-unpublished item is
        # rejected — proving the in-transaction re-check, not a cached preflight.
        self.published_item.approved = False
        self.published_item.save(update_fields=['approved'])
        self._assert_rejected_no_row(
            [{'item': str(self.published_item.pk), 'quantity': 1}]
        )
