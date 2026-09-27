"""
D12 (B-1) — a historical order line is labelled with the name SAVED when it was
ordered, never with whatever the catalogue says now.

WHAT THIS PINS. Two readers used to dereference the live ``MenuItem`` for a
line's name: ``SerializerListOrderItem`` (the diner's ``order-details`` read,
``items[].item.name`` and ``items[].extra_items[].name``) and
``serialize_order_item_details`` (every line list of the ``initiate`` response,
including a D04 replay of an order placed long ago). A rename therefore
relabelled history, and a menu soft-delete — whose vacuum runs INLINE on the
same request and appends ``_autodelN`` to the catalogue row — relabelled it as
``"Beef Burger_autodel1"``. The canonical ``quote`` already preferred the saved
name, but fell back to the live one whenever the saved name was blank, so a row
with no saved evidence was quietly given today's catalogue text as its history.

THE CONTRACT. Every historical name site returns ``item_name_snapshot``
VERBATIM (a blank stays ``""``), beside an additive provenance marker:
``"snapshot"`` when a name was saved, ``"missing"`` when it was not. The marker
describes that ONE name field's stored evidence — not the line's completeness,
not when it was created, and not its allergen safety.

THREE KINDS OF TEST, KEPT APART ON PURPOSE:
  * ``HistoricalNameTests`` — behaviour the old readers VIOLATED (live names).
  * ``NameProvenanceContractTests`` — the NEW additive fields.
  * ``*ControlTests`` — facts that held before and must still hold: money,
    identity, quote references, populations, kitchen, analytics, scope.

EVERY TEST DRIVES THE REAL ENDPOINTS: diner initiate / submit / order-details
with a real table session, restaurant-setup PUT/DELETE with a real owner JWT
(so the inline vacuum runs), the kitchen feeds and the reports endpoint. Bodies
are decoded from ``response.content``, the bytes a client parses.

PostgreSQL is required (the create path's one-statement catalogue snapshot is
PostgreSQL-only), exactly as for every other order-path test.
"""
import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RestaurantStatus_Live,
)
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderItem
from restaurants_app.controllers.diner_capability import issue_table_session
from restaurants_app.models import (
    MenuItem, MenuItemTag, MenuSection, Restaurant, RestaurantEmployee,
    RestaurantTag, SectionGroup, Table,
)
from users_app.models import User

D = Decimal

INITIATE_URL = '/api/v2/orders/initiate/'
SUBMIT_URL = '/api/v1/orders/submit/'
DETAILS_URL = '/api/v1/orders/journey/order-details/'
KITCHEN_COMPLETED_URL = '/api/v1/kitchen/orders/completed/'
MENU_SUMMARY_URL = '/api/v1/reports/restaurant/menu-summary/'

#: Every list the initiate response assembles with
#: ``serialize_order_item_details``.
DETAIL_LISTS = (
    'order_items', 'available_items', 'unavailable_items',
    'extras', 'available_extras', 'unavailable_extras',
)

SIZE_OPTIONS = {
    'hasModifiers': True,
    'groups': [{
        'id': 'g-size', 'name': 'Size', 'required': True,
        'selectionType': 'single', 'minSelections': 1, 'maxSelections': 1,
        'choices': [
            {'id': 'c-small', 'name': 'Small', 'additionalCost': 0,
             'available': True},
            {'id': 'c-large', 'name': 'Large', 'additionalCost': 2000,
             'available': True},
        ],
    }],
}


class _JsonFloat(tuple):
    """A value that arrived as a JSON float, kept with its source text."""


def decode_wire(response):
    """The structure the client PARSES — JSON floats marked, not coerced."""
    return json.loads(
        response.content.decode(),
        parse_float=lambda raw: _JsonFloat((raw,)),
    )


def _text(value):
    """A decoded JSON float back to its text; anything else unchanged."""
    return value[0] if isinstance(value, _JsonFloat) else value


class _HistoryFixture(TestCase):
    """A restaurant whose ordered dishes can be renamed, repriced and deleted
    through the real operator endpoints after the order was accepted."""

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Hist', last_name='Owner', email='d12-hist@test.com',
            phone_number='256700012201', username='256700012201',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='History R', location='hist', owner=self.owner,
            status=RestaurantStatus_Live, accepting_orders=True,
        )
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant,
            roles=[RESTAURANT_OWNER],
        )
        self.section = MenuSection.objects.create(
            name='Mains Section', restaurant=self.restaurant, approved=True,
            enabled=True, available=True, availability='always',
        )
        self.group = SectionGroup.objects.create(
            name='Burgers Group', section=self.section, approved=True,
            enabled=True,
        )
        self.cheese = self._extra('Cheese Slice', '1000.25')
        # Sold out BEFORE the order: it is kept at quantity 0 and reported as an
        # unavailable extra, which is how the unavailable lists get populated.
        self.bacon = self._extra('Bacon Strip', '800.00', in_stock=False)
        self.dish = MenuItem.objects.create(
            name='Beef Burger', section=self.section, section_group=self.group,
            primary_price=D('5000.50'), approved=True, enabled=True,
            available=True, in_stock=True, options=SIZE_OPTIONS,
            has_extras=True,
            extras_applicable=[str(self.cheese.id), str(self.bacon.id)],
            extras_min_selections=0, extras_max_selections=0,
        )
        # A sold-out dish: an unavailable PARENT line.
        self.fish = MenuItem.objects.create(
            name='Fish Plate', section=self.section, primary_price=D('3000.00'),
            approved=True, enabled=True, available=True, in_stock=False,
        )
        # A name that legitimately CONTAINS the vacuum's suffix text.
        self.pasta = MenuItem.objects.create(
            name='Pasta_autodel7 Classic', section=self.section,
            primary_price=D('2500.00'), approved=True, enabled=True,
            available=True, in_stock=True,
        )
        self.peanut = RestaurantTag.objects.create(
            restaurant=self.restaurant, name='Peanuts', category='allergen',
            icon='nut', colour='red',
        )
        MenuItemTag.objects.create(menu_item=self.dish, tag=self.peanut)
        self.tables = [
            Table.objects.create(number=n, str_number=str(n),
                                 restaurant=self.restaurant, qr_mode='order_pay')
            for n in range(1, 6)
        ]
        self.diner = Client()
        self.staff = APIClient()
        self.staff.force_authenticate(user=self.owner)

    def _extra(self, name, price, **kw):
        opts = dict(approved=True, enabled=True, available=True, in_stock=True,
                    is_extra=True)
        opts.update(kw)
        return MenuItem.objects.create(
            name=name, section=self.section, primary_price=D(price), **opts)

    # ------------------------------------------------------------ the order
    def lines(self):
        return [
            {'item': str(self.dish.id), 'quantity': 2,
             'selected_modifiers': {'g-size': ['c-large']},
             'extras': [str(self.cheese.id), str(self.bacon.id)]},
            {'item': str(self.fish.id), 'quantity': 1},
            {'item': str(self.pasta.id), 'quantity': 1},
        ]

    def initiate(self, table, client_order_id):
        return self.diner.post(
            INITIATE_URL,
            data=json.dumps({'items': self.lines(),
                             'client_order_id': client_order_id}),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(table),
        )

    def place(self, table_index=0, serve=True):
        """Initiate, accept against the reviewed quote, and serve."""
        table = self.tables[table_index]
        coid = str(uuid.uuid4())
        response = self.initiate(table, coid)
        self.assertEqual(response.status_code, 200, response.content[:500])
        data = decode_wire(response)['data']
        order_id = data['order_details']['id']
        submitted = self.diner.put(
            SUBMIT_URL,
            data=json.dumps({'order': order_id,
                             'quote_ref': data['order_details']['quote_ref']}),
            content_type='application/json',
            HTTP_X_DINER_SESSION=issue_table_session(table),
        )
        self.assertEqual(submitted.status_code, 200, submitted.content[:500])
        order = Order.objects.get(pk=order_id)
        if serve:
            for action in ('advance', 'advance', 'serve'):
                rev = Order.objects.values_list(
                    'fulfilment_revision', flat=True).get(pk=order.pk)
                k = self.staff.put(
                    f'/api/v1/kitchen/orders/{order.pk}/fulfilment-status/',
                    {'action': action, 'if_revision': rev}, format='json')
                self.assertEqual(k.status_code, 200, k.content[:300])
        return order, table, coid

    # -------------------------------------------------------------- readers
    def details(self, order, table, **selector):
        params = selector or {'order': str(order.pk)}
        response = self.diner.get(
            DETAILS_URL, params,
            HTTP_X_DINER_SESSION=issue_table_session(table))
        self.assertEqual(response.status_code, 200, response.content[:300])
        return decode_wire(response)['data']

    def replay(self, table, coid):
        response = self.initiate(table, coid)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return decode_wire(response)['data']

    # ------------------------------------------------------ operator edits
    def _jwt(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(self.owner).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def setup_put(self, config, body):
        response = Client().put(
            f'/api/v1/restaurant-setup/{config}/', data=json.dumps(body),
            content_type='application/json', **self._jwt())
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response

    def setup_delete(self, config, record_id):
        response = Client().delete(
            f'/api/v1/restaurant-setup/{config}/',
            data=json.dumps({'id': str(record_id),
                             'deletion_reason': 'Removed from the menu'}),
            content_type='application/json', **self._jwt())
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response

    def rename_everything(self):
        self.setup_put('menuitems', {'id': str(self.dish.id),
                                     'name': 'Chicken Burger'})
        self.setup_put('menuitems', {'id': str(self.cheese.id),
                                     'name': 'Vegan Cheese'})
        self.setup_put('menuitems', {'id': str(self.bacon.id),
                                     'name': 'Turkey Rasher'})
        self.setup_put('menuitems', {'id': str(self.fish.id),
                                     'name': 'Tilapia Platter'})
        self.setup_put('menuitems', {'id': str(self.pasta.id),
                                     'name': 'Penne Arrabbiata'})

    def delete_everything(self):
        """Parents first: an extra is blocked while a live parent offers it."""
        for item in (self.dish, self.fish, self.pasta, self.cheese, self.bacon):
            self.setup_delete('menuitems', item.id)

    def blank(self, order, *, parents=False, extras=False, only=None):
        """Simulate rows with no saved name evidence. A direct write, because no
        supported writer produces one — that is exactly why the reader has to
        say so rather than guess."""
        rows = OrderItem.objects.filter(order=order)
        if only is not None:
            rows = rows.filter(pk__in=only)
        elif parents and not extras:
            rows = rows.filter(parent_item__isnull=True)
        elif extras and not parents:
            rows = rows.filter(parent_item__isnull=False)
        rows.update(item_name_snapshot='')

    # ------------------------------------------------------------ the sites
    def saved_names(self, order):
        return dict(OrderItem.objects.filter(order=order)
                    .values_list('id', 'item_name_snapshot'))

    def children_of(self, order):
        out = {}
        for row in OrderItem.objects.filter(order=order,
                                            parent_item__isnull=False):
            out.setdefault(str(row.parent_item_id), []).append(
                row.item_name_snapshot)
        return out

    def detail_sites(self, body):
        """(site, row id, name, provenance) for every name the read emits."""
        sites = []
        for row in body['items']:
            item = row['item']
            sites.append(('items.item', row['id'], item['name'],
                          item.get('name_provenance', '<absent>')))
            for extra in row['extra_items']:
                sites.append(('items.extra_items', extra['id'], extra['name'],
                              extra.get('name_provenance', '<absent>')))
        sites += self.quote_sites(body['quote'])
        return sites

    def quote_sites(self, quote):
        sites = []
        for line in quote:
            sites.append(('quote', line['id'], line['item_name'],
                          line.get('item_name_provenance', '<absent>')))
            for extra in line['extras']:
                sites.append(('quote.extras', extra['id'], extra['item_name'],
                              extra.get('item_name_provenance', '<absent>')))
        return sites

    def replay_sites(self, data):
        """Rows of every detail list carry ids; their nested extras do not, so
        those are returned separately keyed by the parent row id."""
        rows, nested = [], []
        for key in DETAIL_LISTS:
            for row in data[key]:
                rows.append((key, row['id'], row['item_name'],
                             row.get('item_name_provenance', '<absent>')))
                for extra in row['extras']:
                    nested.append((f'{key}.extras', row['id'],
                                   extra['item_name'],
                                   extra.get('item_name_provenance',
                                             '<absent>')))
        rows += self.quote_sites(data['quote'])
        return rows, nested

    def assert_names_are_saved(self, order, sites, nested=()):
        saved = {str(k): v for k, v in self.saved_names(order).items()}
        for site, row_id, name, _prov in sites:
            self.assertIn(str(row_id), saved, site)
            self.assertEqual(name, saved[str(row_id)],
                             f'{site} row {row_id}: {name!r} is not the saved '
                             f'name {saved[str(row_id)]!r}')
        children = self.children_of(order)
        by_parent = {}
        for site, parent_id, name, _prov in nested:
            by_parent.setdefault((site, str(parent_id)), []).append(name)
        for (site, parent_id), names in by_parent.items():
            self.assertEqual(sorted(names), sorted(children.get(parent_id, [])),
                             f'{site} under {parent_id}')

    def assert_provenance(self, order, sites, nested=()):
        saved = {str(k): v for k, v in self.saved_names(order).items()}
        for site, row_id, name, prov in sites:
            expected = 'snapshot' if saved[str(row_id)] else 'missing'
            self.assertEqual(prov, expected, f'{site} row {row_id} ({name!r})')
        for site, parent_id, name, prov in nested:
            self.assertEqual(prov, 'snapshot' if name else 'missing',
                             f'{site} under {parent_id} ({name!r})')

    def row_state(self, order):
        """Every stored column of every line, to prove reads write nothing."""
        return list(OrderItem.objects.filter(order=order).order_by('id')
                    .values())


class HistoricalNameTests(_HistoryFixture):
    """Behaviour the pre-D12 readers VIOLATED: history relabelled live."""

    def _assert_everywhere(self, order, table, coid):
        body = self.details(order, table)
        self.assert_names_are_saved(order, self.detail_sites(body))
        rows, nested = self.replay_sites(self.replay(table, coid))
        self.assert_names_are_saved(order, rows, nested)

    def test_names_survive_a_rename(self):
        order, table, coid = self.place()
        self.rename_everything()
        self._assert_everywhere(order, table, coid)

    def test_names_survive_soft_delete_and_the_inline_vacuum(self):
        order, table, coid = self.place()
        self.delete_everything()
        # The catalogue really was renamed by the vacuum.
        self.assertTrue(MenuItem.objects.get(pk=self.dish.pk)
                        .name.endswith('_autodel1'))
        self._assert_everywhere(order, table, coid)

    def test_names_survive_a_section_soft_cascade(self):
        order, table, coid = self.place()
        self.setup_delete('menusections', self.section.id)
        self.assertTrue(MenuItem.objects.get(pk=self.cheese.pk).deleted)
        self._assert_everywhere(order, table, coid)

    def test_names_survive_a_group_soft_cascade(self):
        order, table, coid = self.place()
        self.setup_delete('sectiongroups', self.group.id)
        self.assertTrue(MenuItem.objects.get(pk=self.dish.pk).deleted)
        self._assert_everywhere(order, table, coid)

    def test_a_new_namesake_does_not_capture_the_old_line(self):
        order, table, coid = self.place()
        self.delete_everything()
        namesake = MenuItem.objects.create(
            name='Beef Burger', section=self.section, primary_price=D('1.00'),
            approved=True, enabled=True)
        body = self.details(order, table)
        self.assert_names_are_saved(order, self.detail_sites(body))
        dish_line = next(q for q in body['quote']
                         if q['item'] == str(self.dish.id))
        self.assertEqual(dish_line['item_name'], 'Beef Burger')
        self.assertNotEqual(dish_line['item'], str(namesake.id))
        self.assertEqual(
            next(r for r in body['items']
                 if r['item']['id'] == str(self.dish.id))['item']['name'],
            'Beef Burger')

    def test_a_saved_name_containing_the_vacuum_suffix_is_kept_intact(self):
        order, table, coid = self.place()
        self.setup_delete('menuitems', self.pasta.id)
        self.assertEqual(MenuItem.objects.get(pk=self.pasta.pk).name,
                         'Pasta_autodel7 Classic_autodel1')
        body = self.details(order, table)
        pasta = [r['item']['name'] for r in body['items']
                 if r['item']['id'] == str(self.pasta.id)]
        self.assertEqual(pasta, ['Pasta_autodel7 Classic'])
        rows, _ = self.replay_sites(self.replay(table, coid))
        self.assertIn('Pasta_autodel7 Classic',
                      [name for _s, _i, name, _p in rows])
        self.assertNotIn('Pasta_autodel7 Classic_autodel1',
                         [name for _s, _i, name, _p in rows])

    def test_blank_saved_names_are_not_filled_from_the_catalogue(self):
        """The quote used to substitute the live name for a blank one."""
        order, table, coid = self.place()
        self.blank(order, parents=True, extras=True)
        self.rename_everything()
        self.delete_everything()
        body = self.details(order, table)
        for site, _id, name, _prov in self.detail_sites(body):
            self.assertEqual(name, '', site)
        rows, nested = self.replay_sites(self.replay(table, coid))
        for site, _id, name, _prov in rows + nested:
            self.assertEqual(name, '', site)


class NameProvenanceContractTests(_HistoryFixture):
    """The NEW additive fields: one marker beside every historical name."""

    def test_modern_rows_say_snapshot_at_every_site(self):
        order, table, coid = self.place()
        body = self.details(order, table)
        self.assert_provenance(order, self.detail_sites(body))
        rows, nested = self.replay_sites(self.replay(table, coid))
        self.assert_provenance(order, rows, nested)
        self.assertTrue(rows and nested)

    def test_missing_parent_names(self):
        order, table, coid = self.place()
        self.blank(order, parents=True)
        self.rename_everything()
        self.delete_everything()
        body = self.details(order, table)
        sites = self.detail_sites(body)
        self.assert_names_are_saved(order, sites)
        self.assert_provenance(order, sites)
        self.assertIn('missing', {p for _s, _i, _n, p in sites})
        self.assertIn('snapshot', {p for _s, _i, _n, p in sites})

    def test_missing_extra_names(self):
        order, table, coid = self.place()
        self.blank(order, extras=True)
        self.rename_everything()
        self.delete_everything()
        rows, nested = self.replay_sites(self.replay(table, coid))
        self.assert_names_are_saved(order, rows, nested)
        self.assert_provenance(order, rows, nested)
        self.assertEqual({p for _s, _i, _n, p in nested}, {'missing'})

    def test_mixed_present_and_missing_within_one_line(self):
        """One extra keeps its name, the other has none, under ONE parent."""
        order, table, coid = self.place()
        cheese_row = OrderItem.objects.get(order=order, item=self.cheese)
        self.blank(order, only=[cheese_row.pk])
        self.rename_everything()
        self.delete_everything()
        body = self.details(order, table)
        sites = self.detail_sites(body)
        self.assert_names_are_saved(order, sites)
        self.assert_provenance(order, sites)
        quote_extras = {e['id']: (e['item_name'], e['item_name_provenance'])
                        for q in body['quote'] for e in q['extras']}
        self.assertEqual(quote_extras[str(cheese_row.pk)], ('', 'missing'))
        bacon_row = OrderItem.objects.get(order=order, item=self.bacon)
        self.assertEqual(quote_extras[str(bacon_row.pk)],
                         ('Bacon Strip', 'snapshot'))

    def test_a_blank_row_without_an_intent_key(self):
        """An old-style row: no intent key, so only the order-id read reaches
        it. Missing evidence is reported the same way whatever its age."""
        order, table, coid = self.place()
        Order.objects.filter(pk=order.pk).update(client_order_id=None)
        self.blank(order, parents=True, extras=True)
        self.rename_everything()
        body = self.details(order, table)
        sites = self.detail_sites(body)
        self.assert_names_are_saved(order, sites)
        self.assert_provenance(order, sites)
        self.assertEqual({p for _s, _i, _n, p in sites}, {'missing'})

    def test_a_blank_row_that_carries_an_intent_key(self):
        """A blank snapshot does NOT imply a pre-0028 row: nothing ties the two
        columns together, so an intent-bearing blank row is reachable by the
        intent read and by a replay, and must be labelled the same way."""
        order, table, coid = self.place()
        self.blank(order, parents=True, extras=True)
        self.rename_everything()
        self.delete_everything()
        body = self.details(order, table, intent=coid)
        sites = self.detail_sites(body)
        self.assert_names_are_saved(order, sites)
        self.assert_provenance(order, sites)
        rows, nested = self.replay_sites(self.replay(table, coid))
        self.assert_names_are_saved(order, rows, nested)
        self.assert_provenance(order, rows, nested)
        self.assertEqual({p for _s, _i, _n, p in rows + nested}, {'missing'})

    def test_the_marker_is_a_sibling_and_every_name_stays_a_string(self):
        order, table, coid = self.place()
        self.blank(order, parents=True, extras=True)
        body = self.details(order, table)
        for row in body['items']:
            self.assertEqual(set(row['item']),
                             {'id', 'name', 'name_provenance', 'is_special'})
            self.assertIsInstance(row['item']['name'], str)
        for _s, _i, name, _p in self.detail_sites(body):
            self.assertIsInstance(name, str)
        rows, nested = self.replay_sites(self.replay(table, coid))
        for _s, _i, name, _p in rows + nested:
            self.assertIsInstance(name, str)


class HistoricalValueControlTests(_HistoryFixture):
    """Facts that held before D12 and must still hold."""

    def test_price_discount_and_option_edits_change_no_historical_value(self):
        order, table, coid = self.place()
        before_details = self.details(order, table)
        before_replay = self.replay(table, coid)
        opts = json.loads(json.dumps(SIZE_OPTIONS))
        opts['groups'][0]['name'] = 'Portion'
        opts['groups'][0]['choices'][1].update(name='Jumbo', additionalCost=3000)
        self.setup_put('menuitems', {
            'id': str(self.dish.id), 'options': json.dumps(opts),
            'primary_price': 9000, 'running_discount': True,
            'discount_details': json.dumps(
                {'discount_percentage': 25, 'recurring_days': []}),
        })
        self.setup_put('menuitems', {'id': str(self.cheese.id),
                                     'primary_price': 3000})
        after_details = self.details(order, table)
        after_replay = self.replay(table, coid)
        # Byte-for-byte: names, labels, money, references — nothing moved.
        self.assertEqual(after_details, before_details)
        self.assertEqual(after_replay, before_replay)
        self.assertEqual(after_details['quote_total'], '18501.50')

    def test_money_types_labels_and_references_survive_rename_and_delete(self):
        order, table, coid = self.place()
        before = self.details(order, table)
        before_replay = self.replay(table, coid)
        before_rows = self.row_state(order)
        self.rename_everything()
        self.delete_everything()
        after = self.details(order, table)
        after_replay = self.replay(table, coid)

        # Canonical money stays an exact decimal STRING…
        self.assertEqual(after['quote_total'], '18501.50')
        for line_before, line_after in zip(before['quote'], after['quote']):
            for key in ('line_actual_cost', 'line_total_with_extras',
                        'unit_price', 'reference_unit_price', 'savings'):
                self.assertIsInstance(line_after[key], str)
                self.assertEqual(line_after[key], line_before[key])
            self.assertEqual(line_after['modifiers'], line_before['modifiers'])
            self.assertEqual(line_after['options'], line_before['options'])
            self.assertEqual(line_after['selected_modifiers'],
                             line_before['selected_modifiers'])
        # …and the legacy keys keep their established JSON-number type.
        extra_cost = after['items'][0]['extra_items'] or next(
            r['extra_items'] for r in after['items'] if r['extra_items'])
        self.assertIsInstance(extra_cost[0]['actual_cost'], _JsonFloat)
        self.assertIsInstance(after_replay['order_items'][0]['actual_cost'],
                              _JsonFloat)
        self.assertIsInstance(
            after_replay['order_details']['actual_cost'], _JsonFloat)

        # Populations, ids and references are exactly what they were.
        self.assertEqual([r['id'] for r in after['items']],
                         [r['id'] for r in before['items']])
        self.assertEqual([r['item']['id'] for r in after['items']],
                         [r['item']['id'] for r in before['items']])
        for key in DETAIL_LISTS:
            self.assertEqual([r['id'] for r in after_replay[key]],
                             [r['id'] for r in before_replay[key]], key)
        self.assertEqual(after['quote_complete'], before['quote_complete'])
        self.assertEqual(after['checkout'], before['checkout'])
        self.assertEqual(after['quote_closure'], before['quote_closure'])
        self.assertEqual(after['accepted'], before['accepted'])
        self.assertEqual(after_replay['order_details']['quote_ref'],
                         before_replay['order_details']['quote_ref'])
        self.assertEqual(after_replay['order_details']['quote_ref'],
                         quote_ref(order))
        # Allergen/modifier snapshots on the rows are untouched.
        after_rows = self.row_state(order)
        for b, a in zip(before_rows, after_rows):
            for key in ('item_name_snapshot', 'modifiers_snapshot',
                        'allergen_tags_snapshot', 'options',
                        'selected_modifiers', 'actual_cost', 'unit_price'):
                self.assertEqual(a[key], b[key], key)

    def test_reads_write_nothing(self):
        order, table, coid = self.place()
        self.blank(order, parents=True)
        self.rename_everything()
        before = self.row_state(order)
        order_before = Order.objects.filter(pk=order.pk).values().get()
        self.details(order, table)
        self.details(order, table, intent=coid)
        self.replay(table, coid)
        self.assertEqual(self.row_state(order), before)
        self.assertEqual(Order.objects.filter(pk=order.pk).values().get(),
                         order_before)

    def test_the_accepted_quote_reference_is_not_moved_by_names(self):
        """Names are presentation: the fingerprint reads the SAVED snapshot,
        so neither a catalogue edit nor the new marker moves a reference."""
        order, table, coid = self.place()
        accepted = self.details(order, table)['checkout']['acceptance'][
            'quote_ref']
        live = quote_ref(order)
        self.rename_everything()
        self.delete_everything()
        self.assertEqual(quote_ref(order), live)
        self.assertEqual(
            self.details(order, table)['checkout']['acceptance']['quote_ref'],
            accepted)


class ConsumerContractControlTests(_HistoryFixture):
    """Neighbouring contracts D12 deliberately does NOT change."""

    def test_kitchen_feed_is_unchanged(self):
        """The kitchen already read snapshots; B-1 adds nothing to it. A blank
        row still shows an empty name and an empty allergen list there — a
        known gap this change does not close."""
        order, table, coid = self.place()
        self.rename_everything()
        feed = self.staff.get(KITCHEN_COMPLETED_URL,
                              {'restaurant': str(self.restaurant.pk)})
        ticket = next(t for t in decode_wire(feed)['data']
                      if t['id'] == str(order.pk))
        burger = next(i for i in ticket['items']
                      if i['item_name_snapshot'] == 'Beef Burger')
        self.assertEqual(set(burger), {'item_name_snapshot', 'quantity',
                                       'modifiers', 'allergen_tags', 'extras'})
        self.assertEqual(burger['modifiers'], ['Size: Large'])
        self.assertEqual(burger['allergen_tags'][0]['name'], 'Peanuts')
        self.assertEqual([e['item_name_snapshot'] for e in burger['extras']],
                         ['Cheese Slice'])

        self.blank(order, parents=True, extras=True)
        OrderItem.objects.filter(order=order).update(allergen_tags_snapshot=[])
        feed = self.staff.get(KITCHEN_COMPLETED_URL,
                              {'restaurant': str(self.restaurant.pk)})
        ticket = next(t for t in decode_wire(feed)['data']
                      if t['id'] == str(order.pk))
        for item in ticket['items']:
            self.assertEqual(item['item_name_snapshot'], '')
            self.assertNotIn('name_provenance', item)
            self.assertNotIn('item_name_provenance', item)

    def test_menu_performance_still_follows_the_current_menu(self):
        """A CURRENT-menu report by documented contract (reports menu.py)."""
        order, table, coid = self.place()
        self.rename_everything()
        today = timezone.localdate()
        response = self.staff.get(MENU_SUMMARY_URL, {
            'restaurant': str(self.restaurant.pk), 'grouping': 'items',
            'from': str(today - timedelta(days=1)),
            'to': str(today + timedelta(days=1)),
        })
        names = {r['name'] for r in decode_wire(response)['data']['rows']}
        self.assertIn('Chicken Burger', names)
        self.assertNotIn('Beef Burger', names)

    def test_session_scope_is_unchanged(self):
        order, table, coid = self.place()
        other = self.diner.get(
            DETAILS_URL, {'order': str(order.pk)},
            HTTP_X_DINER_SESSION=issue_table_session(self.tables[3]))
        self.assertEqual(other.status_code, 404)
        missing = self.diner.get(DETAILS_URL, {'order': str(order.pk)})
        self.assertEqual(missing.status_code, 400)
        self.assertNotIn(b'Beef Burger', other.content + missing.content)

    def test_detail_read_query_count_for_this_fixture(self):
        """For THIS fixture (3 parent lines, 2 extras on one of them), the
        detail read costs 8 queries. It cost 10 before D12, and the difference
        is exactly the two extras: reading the live name made each one lazily
        load its ``MenuItem``, and the saved name needs no join. Nothing else
        about the read changed. It is not an all-order constant, and the query
        population was deliberately not optimised: ``get_extra_items`` still
        issues one query per row it serializes, and the parent ``item`` is
        still read for ``id`` / ``is_special``."""
        order, table, coid = self.place()
        with CaptureQueriesContext(connection) as ctx:
            self.details(order, table)
        self.assertEqual(len(ctx.captured_queries), 8)
