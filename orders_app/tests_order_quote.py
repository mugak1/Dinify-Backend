"""
D02 — the pricing-version discriminator and the quote acknowledgement.

Three invariants must hold before a draft is ACCEPTED, and each is tested for
what it refuses as well as what it admits:

  1. the draft was priced by the CORRECTED convention;
  2. the submission names the exact saved quote it is accepting;
  3. there is at least one deliverable line to prepare.

An acknowledgement is NOT authorization: the existing diner-capability and
staff/module checks are unchanged and still decide who may act at all.
"""
import json
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    OrderStatus_Initiated, OrderStatus_Pending, RestaurantStatus_Live,
    RESTAURANT_OWNER, RESTAURANT_STAFF,
)
from orders_app.controllers.con_orders import ConOrder
from orders_app.controllers.manage_order import (
    REASON_LEGACY_PRICING, REASON_NOTHING_TO_PREPARE, REASON_QUOTE_REQUIRED,
    REASON_QUOTE_STALE, update_order_status,
)
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED, PRICING_VERSION_LEGACY,
)
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderItem
from restaurants_app.controllers.diner_capability import (
    SESSION_HEADER, issue_table_session,
)
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from users_app.models import User

D = Decimal
SUBMIT_URL = '/api/v1/orders/submit/'


class QuoteBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Q', last_name='O', email='q@t.com',
            phone_number='256700077001', username='256700077001',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Quote R', location='qr', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
        )
        self.staff = User.objects.create_user(
            first_name='Q', last_name='S', email='qs@t.com',
            phone_number='256700077002', username='256700077002',
            country='Uganda', password='password', roles=[],
        )
        RestaurantEmployee.objects.create(
            user=self.staff, restaurant=self.restaurant, roles=[RESTAURANT_STAFF],
        )
        self.section = MenuSection.objects.create(
            name='S', restaurant=self.restaurant, approved=True, enabled=True,
            available=True, availability='always',
        )
        self.item = MenuItem.objects.create(
            name='Dish', section=self.section, primary_price=D('5000'),
            approved=True, enabled=True, available=True,
        )
        self.tables = [
            Table.objects.create(number=n, str_number=str(n),
                                 restaurant=self.restaurant, qr_mode='order_pay')
            for n in range(1, 7)
        ]
        self._next = 0
        self.client = APIClient()

    def draft(self, quantity=1, item=None):
        table = self.tables[self._next]
        self._next += 1
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=[{'item': str((item or self.item).id), 'quantity': quantity}],
        )
        self.assertEqual(response.get('status'), 200, response)
        return (
            Order.objects.get(pk=response['data']['order_details']['id']),
            table,
            response['data'],
        )


class PricingVersionTests(QuoteBase):
    def test_the_corrected_service_certifies_its_own_orders(self):
        order, _table, _data = self.draft()
        self.assertEqual(order.pricing_version, PRICING_VERSION_CORRECTED)

    def test_the_model_default_is_legacy(self):
        """An order created WITHOUT naming the column is LEGACY.

        Forgetting to opt in must never certify an order as corrected.
        """
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.tables[5],
            total_cost=0, discounted_cost=0, savings=0, actual_cost=0,
            order_status=OrderStatus_Initiated,
        )
        self.assertEqual(order.pricing_version, PRICING_VERSION_LEGACY)

    def test_the_database_default_is_legacy_too(self):
        """An INSERT that does not name the column — what OLD APPLICATION CODE
        issues after a rollback onto this schema — lands on LEGACY.

        Django drops the column default after ``AddField``, so without
        ``db_default`` such an insert would hit a NOT NULL violation. It has to
        succeed, and it has to succeed as LEGACY: old code prices the old way.
        """
        from django.db import connection
        table = self.tables[4]
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO orders '
                '(id, restaurant_id, table_id, total_cost, discounted_cost, '
                ' savings, actual_cost, total_paid, balance_payable, '
                ' payment_status, order_status, prepayment_required, '
                ' order_source, is_test, fulfilment_status, priority, '
                ' deleted, archived, vacuumed, customer_match_attempted, '
                ' time_created, time_last_updated) '
                "VALUES (gen_random_uuid(), %s, %s, 0, 0, 0, 0, 0, 0, "
                "'pending', 'initiated', false, 'diner_self_service', false, "
                "'new', false, false, false, false, false, NOW(), NOW()) "
                'RETURNING id',
                [str(self.restaurant.pk), str(table.pk)],
            )
            new_id = cursor.fetchone()[0]
        self.assertEqual(
            Order.objects.get(pk=new_id).pricing_version,
            PRICING_VERSION_LEGACY,
        )

    def test_a_legacy_draft_is_refused_at_submit(self):
        order, _table, _data = self.draft()
        Order.objects.filter(pk=order.pk).update(
            pricing_version=PRICING_VERSION_LEGACY,
        )
        order.refresh_from_db()
        result = update_order_status(
            order, OrderStatus_Pending, None, quote_ref=quote_ref(order),
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['reason'], REASON_LEGACY_PRICING)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)

    def test_a_refused_legacy_draft_is_neither_repriced_nor_deleted(self):
        order, _table, _data = self.draft()
        Order.objects.filter(pk=order.pk).update(
            pricing_version=PRICING_VERSION_LEGACY, actual_cost=D('1.00'),
        )
        order.refresh_from_db()
        update_order_status(order, OrderStatus_Pending, None,
                            quote_ref=quote_ref(order))
        order.refresh_from_db()
        self.assertEqual(order.actual_cost, D('1.00'), 'not silently repriced')
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertEqual(OrderItem.objects.filter(order=order).count(), 1)

    def test_an_accepted_legacy_order_is_left_alone(self):
        """Only the initiated -> pending transition is gated. An order already
        accepted under the old convention keeps its status, its amounts and its
        ordinary kitchen handling."""
        order, _table, _data = self.draft()
        Order.objects.filter(pk=order.pk).update(
            pricing_version=PRICING_VERSION_LEGACY,
            order_status=OrderStatus_Pending,
        )
        order.refresh_from_db()
        result = update_order_status(order, 'preparing', self.owner)
        self.assertEqual(result['status'], 200, result)
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'preparing')


class QuoteReferenceTests(QuoteBase):
    def test_the_initiate_response_publishes_the_quote_itself(self):
        """The priced LINES reach the client, not only their total.

        Found by the browser journey, which is exactly what it is for: the
        serializer built the quote and the response assembler dropped it, so
        every unit test passed while the diner's review screen had nothing to
        render but the browser's own arithmetic — the whole defect this path
        exists to close. Assert the key is forwarded, and that a line carries
        its extras beneath it rather than beside them.
        """
        order, _table, data = self.draft()
        self.assertIn('quote', data)
        quote = data['quote']
        self.assertEqual(len(quote), OrderItem.objects.filter(
            order=order, parent_item__isnull=True, deleted=False,
        ).count())
        for line in quote:
            self.assertIn('extras', line)
            self.assertIn('line_total_with_extras', line)
            self.assertIn('line_actual_cost', line)

    def test_the_initiate_response_publishes_the_reference(self):
        order, _table, data = self.draft()
        self.assertEqual(data['order_details']['quote_ref'], quote_ref(order))
        self.assertEqual(
            data['order_details']['pricing_version'], PRICING_VERSION_CORRECTED,
        )

    def test_it_is_deterministic_for_an_unchanged_order(self):
        order, _table, _data = self.draft()
        self.assertEqual(quote_ref(order), quote_ref(order))
        # ...and independent of whether the instance was refreshed.
        order.refresh_from_db()
        self.assertEqual(quote_ref(order), quote_ref(Order.objects.get(pk=order.pk)))

    def test_it_changes_with_every_part_of_the_quote(self):
        """Quantity, amounts, deliverability and the preparation snapshots each
        move the reference. Verified by mutating ONE field at a time and
        restoring it, so nothing passes by accident."""
        order, _table, _data = self.draft(quantity=2)
        row = OrderItem.objects.get(order=order)
        baseline = quote_ref(order)
        mutations = {
            'quantity': 3,
            'actual_cost': D('1.00'),
            'available': False,
            'item_name_snapshot': 'Renamed',
            'modifiers_snapshot': ['Size: Large'],
            'allergen_tags_snapshot': [{'name': 'Nuts'}],
            'selected_modifiers': {'g': ['c']},
            'status': 'unavailable',
        }
        for field, value in mutations.items():
            original = getattr(row, field)
            setattr(row, field, value)
            row.save(update_fields=[field])
            self.assertNotEqual(
                quote_ref(Order.objects.get(pk=order.pk)), baseline,
                f'{field} did not move the reference',
            )
            setattr(row, field, original)
            row.save(update_fields=[field])
        self.assertEqual(quote_ref(Order.objects.get(pk=order.pk)), baseline)

    def test_an_extra_is_part_of_the_quote(self):
        extra = MenuItem.objects.create(
            name='Extra', section=self.section, primary_price=D('500'),
            approved=True, enabled=True, available=True, is_extra=True,
        )
        self.item.has_extras = True
        self.item.extras_applicable = [str(extra.id)]
        self.item.save()
        table = self.tables[self._next]
        self._next += 1
        response = ConOrder.initiate_order(
            restaurant_id=str(self.restaurant.pk), table_id=str(table.pk),
            items=[{'item': str(self.item.id), 'quantity': 1,
                    'extras': [str(extra.id)]}],
        )
        self.assertEqual(response.get('status'), 200, response)
        order = Order.objects.get(pk=response['data']['order_details']['id'])
        baseline = quote_ref(order)
        child = OrderItem.objects.filter(
            order=order, parent_item__isnull=False,
        ).first()
        self.assertIsNotNone(child)
        child.quantity = 7
        child.save(update_fields=['quantity'])
        self.assertNotEqual(quote_ref(Order.objects.get(pk=order.pk)), baseline)

    def test_it_does_not_depend_on_row_fetch_order(self):
        order, _table, _data = self.draft()
        rows = list(OrderItem.objects.filter(order=order, deleted=False))
        self.assertEqual(quote_ref(order, rows=rows),
                         quote_ref(order, rows=list(reversed(rows))))

    def test_it_carries_no_credential_or_catalogue_text(self):
        order, _table, _data = self.draft()
        reference = quote_ref(order)
        self.assertRegex(reference, r'^[0-9a-f]{64}$')
        self.assertNotIn(str(order.pk), reference)


class AcceptanceTests(QuoteBase):
    def _session(self, table):
        return issue_table_session(table)

    def _jwt(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _put(self, order, table, body):
        return self.client.put(
            SUBMIT_URL, data=json.dumps(body),
            content_type='application/json',
            **{'HTTP_' + SESSION_HEADER.upper().replace('-', '_'):
               self._session(table)},
        )

    def test_a_valid_acknowledgement_is_accepted(self):
        order, table, data = self.draft()
        response = self._put(order, table, {
            'order': str(order.pk), 'quote_ref': data['order_details']['quote_ref'],
        })
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Pending)

    def test_a_missing_acknowledgement_is_refused(self):
        """An OLDER CLIENT that sends only `order` fails CLEARLY rather than
        auto-submitting an amount it never displayed. Deliberate compatibility
        handling, not a claim of seamless backward compatibility."""
        order, table, _data = self.draft()
        response = self._put(order, table, {'order': str(order.pk)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], REASON_QUOTE_REQUIRED)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)

    def test_a_stale_acknowledgement_is_refused_and_nothing_is_repriced(self):
        order, table, data = self.draft(quantity=2)
        stale = data['order_details']['quote_ref']
        row = OrderItem.objects.get(order=order)
        row.quantity = 5
        row.save(update_fields=['quantity'])
        before = Order.objects.count()
        response = self._put(order, table, {
            'order': str(order.pk), 'quote_ref': stale,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], REASON_QUOTE_STALE)
        self.assertEqual(Order.objects.count(), before, 'no second order')
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        row.refresh_from_db()
        self.assertEqual(row.quantity, 5, 'nothing was recalculated')

    def test_a_foreign_acknowledgement_is_refused(self):
        first, table_a, data_a = self.draft()
        _second, _table_b, _data_b = self.draft()
        response = self._put(first, table_a, {
            'order': str(first.pk),
            'quote_ref': quote_ref(Order.objects.exclude(pk=first.pk).first()),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['reason'], REASON_QUOTE_STALE)

    def test_non_string_acknowledgements_are_refused_not_coerced(self):
        order, table, _data = self.draft()
        for value in (None, 1, True, [], {}, ''):
            response = self._put(order, table, {
                'order': str(order.pk), 'quote_ref': value,
            })
            self.assertEqual(response.status_code, 400, value)
            self.assertEqual(response.json()['reason'], REASON_QUOTE_REQUIRED,
                             value)

    def test_there_is_no_staff_bypass(self):
        """A staff caller with full module access is subject to the SAME
        invariant. Being trusted is not a reason to accept an amount nobody
        reviewed."""
        order, _table, _data = self.draft()
        response = self.client.put(
            SUBMIT_URL, data=json.dumps({'order': str(order.pk)}),
            content_type='application/json', **self._jwt(self.staff),
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()['reason'], REASON_QUOTE_REQUIRED)

    def test_a_staff_caller_with_a_valid_acknowledgement_succeeds(self):
        order, _table, data = self.draft()
        response = self.client.put(
            SUBMIT_URL,
            data=json.dumps({'order': str(order.pk),
                             'quote_ref': data['order_details']['quote_ref']}),
            content_type='application/json', **self._jwt(self.staff),
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_acknowledgement_is_not_authority(self):
        """A correct reference from a session bound to ANOTHER table is still a
        non-disclosing 404 — capability is checked before any of this."""
        order, _table, data = self.draft()
        other = self.tables[5]
        response = self.client.put(
            SUBMIT_URL,
            data=json.dumps({'order': str(order.pk),
                             'quote_ref': data['order_details']['quote_ref']}),
            content_type='application/json',
            **{'HTTP_' + SESSION_HEADER.upper().replace('-', '_'):
               issue_table_session(other)},
        )
        self.assertEqual(response.status_code, 404, response.content)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)

    def test_the_acceptance_check_runs_inside_the_locked_transaction(self):
        """The invariants are evaluated on the row re-read under the table lock,
        not on the instance the caller handed in."""
        order, table, data = self.draft()
        # Hand in an instance that still claims to be corrected while the stored
        # row has been demoted: the refusal must follow the STORED row.
        Order.objects.filter(pk=order.pk).update(
            pricing_version=PRICING_VERSION_LEGACY,
        )
        result = update_order_status(
            order, OrderStatus_Pending, None,
            quote_ref=data['order_details']['quote_ref'],
        )
        self.assertEqual(result['status'], 400)
        self.assertEqual(result['reason'], REASON_LEGACY_PRICING)

    def test_a_refusal_leaves_the_kitchen_board_empty(self):
        order, table, _data = self.draft()
        self._put(order, table, {'order': str(order.pk)})
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus_Initiated)
        self.assertFalse(
            ConOrder.any_present_ongoing_order(table).get('present'),
            'a refused submission must not claim the table',
        )
