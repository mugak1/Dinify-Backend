"""
R22 — a malformed price configuration is contained to its own item.

``MenuItem.primary_price`` and ``discount_details`` are read by BOTH the public
menu and the order path. Before D02 a bare ``Decimal(str(...))`` on a malformed
value raised ``InvalidOperation`` out of ``MenuItem.is_discount_active`` — which
``SerializerPublicGetMenuItem`` calls — so ONE bad catalogue row returned HTTP 500
for an entire restaurant's menu, not merely for its own item.

Containment, not a catalogue rewrite: the affected item is not published and not
orderable; every valid neighbour is untouched; and there is NO fallback price. A
zero would make it free and the undiscounted price would present an unearned
charge as valid.
"""
from decimal import Decimal

from django.test import TestCase

from dinify_backend.configss.string_definitions import RestaurantStatus_Live
from orders_app.controllers.con_orders import ConOrder
from orders_app.models import Order, OrderItem
from restaurants_app.controllers.handle_diner_journey import handle_show_menu
from restaurants_app.models import MenuItem, MenuSection, Restaurant, Table
from users_app.models import User

D = Decimal

BROKEN = {'discount_percentage': 'abc', 'discount_amount': 0,
          'start_date': '', 'end_date': '', 'recurring_days': [],
          'start_time': '', 'end_time': ''}


class PriceContainmentTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(
            first_name='C', last_name='O', email='c@t.com',
            phone_number='256700044001', username='256700044001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Contain R', location='cr', owner=owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.good = MenuItem.objects.create(
            name='Good Dish', section=self.section, primary_price=D('5000'),
            approved=True, enabled=True, available=True,
        )
        self.broken = MenuItem.objects.create(
            name='Broken Dish', section=self.section, primary_price=D('7000'),
            approved=True, enabled=True, available=True,
            discount_details=BROKEN,
        )
        self.table = Table.objects.create(
            number=1, str_number='1', restaurant=self.restaurant,
            qr_mode='order_pay',
        )

    def _menu(self):
        response = handle_show_menu(restaurant_id=str(self.restaurant.pk))
        self.assertEqual(response.get('status'), 200, response)
        names = []
        for section in response['data']:
            names.extend(item['name'] for item in section.get('items', []))
            for group in section.get('groups', []):
                names.extend(item['name'] for item in group.get('items', []))
        return response, names

    def test_the_menu_still_renders_and_keeps_every_valid_neighbour(self):
        _response, names = self._menu()
        self.assertIn('Good Dish', names)

    def test_the_affected_item_is_not_published(self):
        _response, names = self._menu()
        self.assertNotIn('Broken Dish', names)

    def test_no_fake_price_is_published_for_it(self):
        """Directly through the shared serializer, which management surfaces
        also use: the price reads NULL, never 0 and never the undiscounted
        figure dressed up as valid."""
        from restaurants_app.serializers import SerializerPublicGetMenuItem
        data = SerializerPublicGetMenuItem(self.broken).data
        self.assertIsNone(data['current_price'])
        self.assertFalse(data['is_discount_active'])
        self.assertEqual(data['discount_percentage'], 0)

    def test_a_valid_neighbour_still_prices_normally_through_the_serializer(self):
        from restaurants_app.serializers import SerializerPublicGetMenuItem
        data = SerializerPublicGetMenuItem(self.good).data
        self.assertEqual(data['current_price'], '5000.00')

    def test_checkout_refuses_it_independently_of_the_menu(self):
        """The order path does NOT rely on the menu having hidden it."""
        orders_before = Order.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(self.table.pk),
            items=[{'item': str(self.broken.id), 'quantity': 1}],
        )
        self.assertEqual(response.get('status'), 400, response)
        self.assertEqual(Order.objects.count(), orders_before)

    def test_a_late_failure_rolls_back_the_real_writes(self):
        """The broken item is the SECOND line, so the first has genuinely been
        written when the refusal happens. Nothing may survive."""
        orders_before = Order.objects.count()
        items_before = OrderItem.objects.count()
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(self.table.pk),
            items=[
                {'item': str(self.good.id), 'quantity': 2},
                {'item': str(self.broken.id), 'quantity': 1},
            ],
        )
        self.assertEqual(response.get('status'), 400, response)
        self.assertEqual(Order.objects.count(), orders_before)
        self.assertEqual(OrderItem.objects.count(), items_before)

    def test_a_valid_neighbour_remains_orderable(self):
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(self.table.pk),
            items=[{'item': str(self.good.id), 'quantity': 2}],
        )
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        self.assertEqual(order.actual_cost, D('10000.00'))

    def test_an_unreadable_extra_is_not_published_as_selectable(self):
        """The diner app resolves an extra's price from the primary_price /
        discount_details handed over in the parent's `extras`, so publishing an
        unreadable one would put a selectable option on screen with no valid
        price."""
        broken_extra = MenuItem.objects.create(
            name='Broken Extra', section=self.section, primary_price=D('900'),
            approved=True, enabled=True, available=True, is_extra=True,
            discount_details=BROKEN,
        )
        ok_extra = MenuItem.objects.create(
            name='Ok Extra', section=self.section, primary_price=D('800'),
            approved=True, enabled=True, available=True, is_extra=True,
        )
        self.good.has_extras = True
        self.good.extras_applicable = [str(broken_extra.id), str(ok_extra.id)]
        self.good.save()

        response, _names = self._menu()
        published = None
        for section in response['data']:
            for item in section.get('items', []):
                if item['name'] == 'Good Dish':
                    published = item
        self.assertIsNotNone(published)
        names = [extra['name'] for extra in published.get('extras', [])]
        self.assertIn('Ok Extra', names)
        self.assertNotIn('Broken Extra', names)

    def test_the_item_recovers_once_its_discount_window_closes(self):
        """Only an ACTIVE incoherent discount makes an item unpriceable. The
        same row outside that window prices from primary_price and is published
        normally — containment is time-dependent, not a permanent sentence."""
        self.broken.discount_details = dict(
            BROKEN, start_date='2000-01-01', end_date='2000-01-02',
        )
        self.broken.save(update_fields=['discount_details'])
        _response, names = self._menu()
        self.assertIn('Broken Dish', names)
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(self.table.pk),
            items=[{'item': str(self.broken.id), 'quantity': 1}],
        )
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        self.assertEqual(order.actual_cost, D('7000.00'))
