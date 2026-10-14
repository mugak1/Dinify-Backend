"""
D12 (B3) — the kitchen feed says whether each line's stored evidence was captured.

THE GAP. The kitchen projects ``item_name_snapshot``, ``modifiers_snapshot`` and
``allergen_tags_snapshot`` as they are stored. Migration ``orders_app/0028`` added
the three columns with defaults (``""`` / ``[]`` / ``[]``) and no backfill, so a
line created before it reads exactly like a line whose writer recorded an empty
list: a nameless quantity with no options and no allergen chips. On the board an
empty allergen list that nobody recorded looks the same as "no allergen tag was
attached when this was ordered".

THE CONTRACT. Every kitchen line AND every extra gains three additive keys, each
``"snapshot"`` or ``"missing"``:

  * ``item_name_provenance``     — ``historical_name(row)``'s answer, unchanged (D12
                                   B-1: it describes the NAME FIELD ONLY);
  * ``allergen_tags_provenance`` — ``list_snapshot_provenance(row)``;
  * ``modifiers_provenance``     — ``list_snapshot_provenance(row)``.

The existing four keys keep their values and types exactly, empty strings and
empty lists included, and extras stay nested where they were.

THE LIST MARKERS REST ON A CONVENTION, NOT ON A GUARANTEE. The production creators
(``SerializerPutOrderItem`` saves in ``con_orders``) write the name and both lists
in ONE save, so on a row that only those writers have touched a non-empty stored
name means the lists beside it are what was captured at order time. The
``CreationPathPremiseTests`` below pin that premise through the real creation
path. Nothing in the schema stores a capture marker or enforces it, so a write
outside those paths can defeat it: filling only the name of a legacy row makes its
default lists read as captured, and clearing a captured list leaves it reading as
captured. Neither is asserted to have happened; ``LIMIT`` tests below pin both,
and a whitespace-only name as a case of the first, so nobody mistakes the
classification for a guarantee.

``"missing"`` means the reader cannot establish the order-time snapshot. It does
not mean no historical information exists (a pre-0028 row can still carry
``options`` and ``selected_modifiers``), and a captured empty allergen list means
the writer recorded no allergen-category tags — never that the dish is
allergen-free.

KINDS OF TEST, KEPT APART:
  * ``CreationPathPremiseTests`` — the co-write premise through real
    initiate/submit; these pass with or without the new keys.
  * ``*ProvenanceTests`` / ``*Coverage*`` — the new metadata.
  * ``*ControlTests`` — facts that must not move: values, types, catalogue
    independence, cost, and that reading writes nothing.
  * ``LIMIT`` — the documented unsupported-writer limit.

Every test drives the real endpoints: diner initiate/submit with a table session,
the kitchen command routes, restaurant-setup PUT/DELETE with an owner JWT, and the
two kitchen feeds. Bodies are decoded from ``response.content``. Legacy rows are
made by the direct column writes the accepted neighbour suites use, because no
supported writer produces them. PostgreSQL is required, as for every order-path
test.
"""
import json
import uuid
from collections import Counter
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from orders_app.controllers.orders.serializers import historical_name
from orders_app.controllers.services.order_pricing import (
    PRICING_VERSION_CORRECTED, PRICING_VERSION_LEGACY,
)
from orders_app.controllers.services.order_quote import quote_ref
from orders_app.models import Order, OrderItem
from orders_app.serializers import SerializerPutOrderItem
from orders_app.tests_order_history_names import (
    KITCHEN_COMPLETED_URL, _HistoryFixture, decode_wire,
)
from restaurants_app.models import MenuItem, MenuItemTag, RestaurantTag

KITCHEN_ACTIVE_URL = '/api/v1/kitchen/orders/active/'

#: The four keys every line and extra carried before B3, and still carries.
EXISTING_KEYS = {'item_name_snapshot', 'quantity', 'modifiers', 'allergen_tags'}
#: The three keys B3 adds to every line and extra.
PROVENANCE_KEYS = {'item_name_provenance', 'allergen_tags_provenance',
                   'modifiers_provenance'}
VOCABULARY = {'snapshot', 'missing'}

#: Queries one kitchen feed request costs. Measured through this suite's own
#: fixture on unmodified 9b9c38c (before B3) and unchanged by it; it does not
#: grow with the number of tickets, lines or extras.
FEED_QUERIES = {KITCHEN_ACTIVE_URL: 4, KITCHEN_COMPLETED_URL: 4}


def strip_provenance(lines):
    """The feed's lines with only B3's keys removed, extras included."""
    out = []
    for line in lines:
        kept = {k: v for k, v in line.items() if k not in PROVENANCE_KEYS}
        if 'extras' in kept:
            kept['extras'] = strip_provenance(kept['extras'])
        out.append(kept)
    return out


def every_line(lines):
    """Parents then their extras, flattened, each tagged with its role."""
    for line in lines:
        yield 'parent', line
        for extra in line.get('extras', []):
            yield 'extra', extra


class _ProvenanceFixture(_HistoryFixture):
    """The accepted D12 history fixture, plus an allergen on an extra, a dietary
    tag on the burger, and the bacon extra back in stock as an untagged extra.

    The parent order (``place``) is: Beef Burger x2 with Size: Large and the
    Cheese Slice and Bacon Strip extras, Fish Plate (sold out, so not on the
    board) and Pasta_autodel7 Classic (untagged, no options)."""

    def setUp(self):
        super().setUp()
        self.milk = self._tag('Milk', 'allergen', 'milk', 'amber')
        MenuItemTag.objects.create(menu_item=self.cheese, tag=self.milk)
        # The allergen snapshot takes only category='allergen'.
        self.halal = self._tag('Halal', 'dietary', 'leaf', 'green')
        MenuItemTag.objects.create(menu_item=self.dish, tag=self.halal)
        # An available extra with no tags: the captured-empty extra control.
        MenuItem.objects.filter(pk=self.bacon.pk).update(in_stock=True)
        self._override_lines = None

    def _tag(self, name, category, icon, colour):
        # A restaurant may already carry a seeded preset tag of this name.
        tag, _ = RestaurantTag.objects.get_or_create(
            restaurant=self.restaurant, name=name,
            defaults={'category': category, 'icon': icon, 'colour': colour})
        RestaurantTag.objects.filter(pk=tag.pk).update(category=category)
        tag.refresh_from_db()
        return tag

    def lines(self):
        return self._override_lines if self._override_lines is not None else super().lines()

    def place_with(self, lines, **kw):
        self._override_lines = lines
        try:
            return self.place(**kw)
        finally:
            self._override_lines = None

    # ------------------------------------------------------------- readers
    def feed(self, url=KITCHEN_ACTIVE_URL):
        response = self.staff.get(url, {'restaurant': str(self.restaurant.pk)})
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response

    def ticket(self, order, url=KITCHEN_ACTIVE_URL):
        for ticket in decode_wire(self.feed(url))['data']:
            if ticket['id'] == str(order.pk):
                return ticket
        return None

    def items(self, order, url=KITCHEN_ACTIVE_URL):
        ticket = self.ticket(order, url)
        self.assertIsNotNone(ticket, f'order {order.pk} is not on {url}')
        return ticket['items']

    def by_name(self, lines, name):
        found = [line for line in lines if line['item_name_snapshot'] == name]
        self.assertEqual(len(found), 1, (name, lines))
        return found[0]

    def row(self, order, name, *, parent=True):
        return OrderItem.objects.get(
            order=order, item_name_snapshot=name,
            parent_item__isnull=parent)

    # ------------------------------------------------------- stored shapes
    def make_legacy(self, rows):
        """The at-rest shape of a row created before orders_app/0028: the three
        snapshot columns at their AddField defaults. Nothing else is touched."""
        rows.update(item_name_snapshot='', modifiers_snapshot=[],
                    allergen_tags_snapshot=[])

    def provenance(self, line):
        return {key: line[key] for key in sorted(PROVENANCE_KEYS)}

    def all_missing(self):
        return dict.fromkeys(sorted(PROVENANCE_KEYS), 'missing')

    def all_snapshot(self):
        return dict.fromkeys(sorted(PROVENANCE_KEYS), 'snapshot')


# ===================================================================== premise
class CreationPathPremiseTests(_ProvenanceFixture):
    """The co-write premise the list markers rest on, through the REAL creation
    path (initiate + submit). These assert stored and projected VALUES only, so
    they hold with or without B3; a creator that supplied the name but dropped
    either list would fail here."""

    def test_parent_with_options_and_allergen_stores_all_three(self):
        order, _, _ = self.place(serve=False)
        row = self.row(order, 'Beef Burger')
        self.assertEqual(row.modifiers_snapshot, ['Size: Large'])
        self.assertEqual([t['name'] for t in row.allergen_tags_snapshot],
                         ['Peanuts'])
        line = self.by_name(self.items(order), 'Beef Burger')
        self.assertEqual(line['modifiers'], row.modifiers_snapshot)
        self.assertEqual(line['allergen_tags'], row.allergen_tags_snapshot)
        self.assertEqual(line['quantity'], row.quantity)

    def test_extra_with_allergen_stores_name_and_allergen(self):
        order, _, _ = self.place(serve=False)
        row = self.row(order, 'Cheese Slice', parent=False)
        self.assertEqual(row.modifiers_snapshot, [])   # extras carry none
        self.assertEqual([t['name'] for t in row.allergen_tags_snapshot],
                         ['Milk'])
        burger = self.by_name(self.items(order), 'Beef Burger')
        extra = self.by_name(burger['extras'], 'Cheese Slice')
        self.assertEqual(extra['allergen_tags'], row.allergen_tags_snapshot)
        self.assertEqual(extra['modifiers'], [])

    def test_the_draft_holds_all_three_before_acceptance(self):
        # The premise is a property of CREATION, so it is read off the draft
        # before submit. Acceptance's purchase-integrity check would also
        # refuse a draft whose lists disagree with the catalogue, but the
        # premise must not rest on that later, separate rule.
        table = self.tables[0]
        response = self.initiate(table, str(uuid.uuid4()))
        self.assertEqual(response.status_code, 200, response.content[:300])
        order = Order.objects.get(
            pk=decode_wire(response)['data']['order_details']['id'])
        self.assertEqual(order.order_status, 'initiated')
        burger = self.row(order, 'Beef Burger')
        self.assertEqual(burger.modifiers_snapshot, ['Size: Large'])
        self.assertEqual([t['name'] for t in burger.allergen_tags_snapshot],
                         ['Peanuts'])
        cheese = self.row(order, 'Cheese Slice', parent=False)
        self.assertEqual(cheese.modifiers_snapshot, [])
        self.assertEqual([t['name'] for t in cheese.allergen_tags_snapshot],
                         ['Milk'])

    def test_dietary_tags_never_enter_the_allergen_snapshot(self):
        order, _, _ = self.place(serve=False)
        row = self.row(order, 'Beef Burger')
        self.assertNotIn('Halal', [t['name'] for t in
                                   row.allergen_tags_snapshot])
        line = self.by_name(self.items(order), 'Beef Burger')
        self.assertNotIn('Halal', [t['name'] for t in line['allergen_tags']])

    def test_captured_empty_parent_and_extra_store_empty_lists(self):
        order, _, _ = self.place(serve=False)
        pasta = self.row(order, 'Pasta_autodel7 Classic')
        bacon = self.row(order, 'Bacon Strip', parent=False)
        for row in (pasta, bacon):
            self.assertEqual(row.modifiers_snapshot, [])
            self.assertEqual(row.allergen_tags_snapshot, [])
        lines = self.items(order)
        self.assertEqual(
            self.by_name(lines, 'Pasta_autodel7 Classic')['allergen_tags'], [])
        burger = self.by_name(lines, 'Beef Burger')
        self.assertEqual(
            self.by_name(burger['extras'], 'Bacon Strip')['allergen_tags'], [])


# =================================================================== the gap
class LegacyAndCapturedProvenanceTests(_ProvenanceFixture):

    def test_legacy_parent_and_extra_are_missing(self):
        order, _, _ = self.place(serve=False)
        self.make_legacy(OrderItem.objects.filter(order=order))
        lines = self.items(order)
        self.assertEqual(len(lines), 2)                 # burger, pasta
        roles = Counter()
        for role, line in every_line(lines):
            roles[role] += 1
            self.assertEqual(self.provenance(line), self.all_missing(), line)
            self.assertEqual(line['item_name_snapshot'], '')
            self.assertEqual(line['modifiers'], [])
            self.assertEqual(line['allergen_tags'], [])
        self.assertEqual(roles, Counter(parent=2, extra=2))

    def test_captured_parent_and_extra_are_snapshot(self):
        order, _, _ = self.place(serve=False)
        roles = Counter()
        for role, line in every_line(self.items(order)):
            roles[role] += 1
            self.assertEqual(self.provenance(line), self.all_snapshot(), line)
        self.assertEqual(roles, Counter(parent=2, extra=2))

    def test_captured_empty_is_distinct_from_legacy_empty_with_the_same_bytes(self):
        captured, _, _ = self.place(table_index=0, serve=False)
        legacy, _, _ = self.place(table_index=1, serve=False)
        self.make_legacy(OrderItem.objects.filter(order=legacy))

        pasta = self.by_name(self.items(captured), 'Pasta_autodel7 Classic')
        old = [line for line in self.items(legacy) if line['quantity'] == 1][0]
        for key in ('allergen_tags', 'modifiers'):
            self.assertEqual(json.dumps(pasta[key]), json.dumps(old[key]))
        self.assertEqual(pasta['allergen_tags_provenance'], 'snapshot')
        self.assertEqual(pasta['modifiers_provenance'], 'snapshot')
        self.assertEqual(old['allergen_tags_provenance'], 'missing')
        self.assertEqual(old['modifiers_provenance'], 'missing')

        bacon = self.by_name(
            self.by_name(self.items(captured), 'Beef Burger')['extras'],
            'Bacon Strip')
        old_extras = [line for line in self.items(legacy)
                      if line['quantity'] == 2][0]['extras']
        self.assertEqual(json.dumps(bacon['allergen_tags']),
                         json.dumps(old_extras[0]['allergen_tags']))
        self.assertEqual(bacon['allergen_tags_provenance'], 'snapshot')
        self.assertEqual({e['allergen_tags_provenance'] for e in old_extras},
                         {'missing'})

    def test_legacy_extra_under_a_captured_parent_is_missing_on_its_own(self):
        order, _, _ = self.place(serve=False)
        cheese = self.row(order, 'Cheese Slice', parent=False)
        self.make_legacy(OrderItem.objects.filter(pk=cheese.pk))
        burger = [line for line in self.items(order) if line['quantity'] == 2][0]
        self.assertEqual(burger['item_name_snapshot'], 'Beef Burger')
        self.assertEqual(self.provenance(burger), self.all_snapshot())
        extras = {e['item_name_snapshot']: e for e in burger['extras']}
        self.assertEqual(self.provenance(extras['']), self.all_missing())
        self.assertEqual(self.provenance(extras['Bacon Strip']),
                         self.all_snapshot())

    def test_captured_extra_under_a_legacy_parent_is_snapshot_on_its_own(self):
        order, _, _ = self.place(serve=False)
        self.make_legacy(OrderItem.objects.filter(
            order=order, parent_item__isnull=True))
        burger = [line for line in self.items(order) if line['quantity'] == 2][0]
        self.assertEqual(self.provenance(burger), self.all_missing())
        for extra in burger['extras']:
            self.assertNotEqual(extra['item_name_snapshot'], '')
            self.assertEqual(self.provenance(extra), self.all_snapshot())

    def test_blank_name_with_stored_lists_keeps_them_and_is_missing(self):
        """An anomalous row: no name, but non-empty lists. The lists are
        projected exactly as stored and nothing establishes their provenance."""
        order, _, _ = self.place(serve=False)
        row = self.row(order, 'Beef Burger')
        stored_mods = row.modifiers_snapshot
        stored_tags = row.allergen_tags_snapshot
        OrderItem.objects.filter(pk=row.pk).update(item_name_snapshot='')
        burger = [line for line in self.items(order) if line['quantity'] == 2][0]
        self.assertEqual(burger['item_name_snapshot'], '')
        self.assertEqual(burger['modifiers'], stored_mods)
        self.assertEqual(burger['allergen_tags'], stored_tags)
        self.assertEqual(self.provenance(burger), self.all_missing())

    def test_name_provenance_is_historical_name_for_every_projected_row(self):
        order, _, _ = self.place(serve=False)
        cheese = self.row(order, 'Cheese Slice', parent=False)
        pasta = self.row(order, 'Pasta_autodel7 Classic')
        self.make_legacy(OrderItem.objects.filter(pk=cheese.pk))
        # Whitespace is stored content: kept verbatim, as historical_name does.
        OrderItem.objects.filter(pk=pasta.pk).update(item_name_snapshot='  ')
        projected = Counter(
            (line['item_name_snapshot'], line['item_name_provenance'])
            for _, line in every_line(self.items(order)))
        rows = OrderItem.objects.filter(order=order, deleted=False,
                                        available=True)
        self.assertEqual(projected, Counter(historical_name(r) for r in rows))
        self.assertIn(('  ', 'snapshot'), projected)

    def test_provenance_ignores_age_intent_pricing_and_acceptance(self):
        """Derived from each row alone: never from how old the order is, whether
        it has an intent key, its pricing version or acceptance evidence."""
        legacy_rows, _, _ = self.place(table_index=0, serve=False)
        captured_rows, _, _ = self.place(table_index=1, serve=False)
        # Legacy-shaped lines on an order that looks entirely modern...
        self.make_legacy(OrderItem.objects.filter(order=legacy_rows))
        Order.objects.filter(pk=legacy_rows.pk).update(
            pricing_version=PRICING_VERSION_CORRECTED)
        # ...and captured lines on an order that looks entirely legacy.
        Order.objects.filter(pk=captured_rows.pk).update(
            client_order_id=None, request_fingerprint=None,
            pricing_version=PRICING_VERSION_LEGACY,
            time_created=timezone.now() - timedelta(days=400))
        captured_rows.acceptance.delete()
        for _, line in every_line(self.items(legacy_rows)):
            self.assertEqual(self.provenance(line), self.all_missing())
        for _, line in every_line(self.items(captured_rows)):
            self.assertEqual(self.provenance(line), self.all_snapshot())


# ================================================================ both feeds
class CompletedFeedCoverageTests(_ProvenanceFixture):

    def test_completed_feed_carries_the_same_lines_as_active(self):
        order, _, _ = self.place(serve=False)
        active = self.items(order, KITCHEN_ACTIVE_URL)
        for action in ('advance', 'advance', 'serve'):
            self._command(order, action)
        completed = self.items(order, KITCHEN_COMPLETED_URL)
        self.assertEqual(completed, active)
        for _, line in every_line(completed):
            self.assertEqual(self.provenance(line), self.all_snapshot())

    def test_legacy_ticket_served_through_the_commands_reaches_completed(self):
        order, _, _ = self.place(serve=False)
        self.make_legacy(OrderItem.objects.filter(order=order))
        for action in ('advance', 'advance', 'serve'):
            self._command(order, action)
        self.assertIsNone(self.ticket(order, KITCHEN_ACTIVE_URL))
        lines = self.items(order, KITCHEN_COMPLETED_URL)
        self.assertEqual(len(lines), 2)
        for _, line in every_line(lines):
            self.assertEqual(line['item_name_snapshot'], '')
            self.assertEqual(line['allergen_tags'], [])
            self.assertEqual(self.provenance(line), self.all_missing())

    def _command(self, order, action):
        rev = Order.objects.values_list('fulfilment_revision',
                                        flat=True).get(pk=order.pk)
        response = self.staff.put(
            f'/api/v1/kitchen/orders/{order.pk}/fulfilment-status/',
            {'action': action, 'if_revision': rev}, format='json')
        self.assertEqual(response.status_code, 200, response.content[:300])


# ================================================================== controls
class CatalogueIndependenceControlTests(_ProvenanceFixture):
    """Neither the values nor the metadata follow the current catalogue."""

    def test_rename_and_soft_delete_change_nothing(self):
        captured, _, _ = self.place(table_index=0, serve=False)
        legacy, _, _ = self.place(table_index=1, serve=False)
        self.make_legacy(OrderItem.objects.filter(order=legacy))
        before = {o.pk: self.items(o) for o in (captured, legacy)}

        self.rename_everything()
        renamed = {o.pk: self.items(o) for o in (captured, legacy)}
        self.delete_everything()
        deleted = {o.pk: self.items(o) for o in (captured, legacy)}

        self.assertTrue(MenuItem.objects.get(pk=self.dish.pk).deleted)
        self.assertIn('_autodel',
                      MenuItem.objects.get(pk=self.dish.pk).name)
        self.assertEqual(renamed, before)
        self.assertEqual(deleted, before)

    def test_existing_values_are_the_stored_rows_once_metadata_is_stripped(self):
        order, _, _ = self.place(serve=False)
        cheese = self.row(order, 'Cheese Slice', parent=False)
        self.make_legacy(OrderItem.objects.filter(pk=cheese.pk))
        self.rename_everything()
        stripped = strip_provenance(self.items(order))

        expected = []
        rows = OrderItem.objects.filter(order=order, deleted=False,
                                        available=True)
        mains = [r for r in rows if r.parent_item_id is None]
        for main in mains:
            line = self._stored(main)
            line['extras'] = [self._stored(child) for child in rows
                              if child.parent_item_id == main.pk]
            expected.append(line)
        key = lambda line: (line['item_name_snapshot'], line['quantity'])
        self.assertEqual(sorted(stripped, key=key), sorted(expected, key=key))

    @staticmethod
    def _stored(row):
        return {'item_name_snapshot': row.item_name_snapshot,
                'quantity': row.quantity,
                'modifiers': row.modifiers_snapshot,
                'allergen_tags': row.allergen_tags_snapshot}

    def test_the_feed_projects_no_catalogue_column(self):
        """No feed query SELECTs a ``menu_items`` column, so no line value or
        marker can come from the current catalogue.

        The line prefetch does JOIN ``menu_items``: ``OrderItem.Meta.ordering``
        is ``['-time_created', 'item__name']``, so the catalogue name breaks
        ties in line ORDER. That predates B3, decides order only, and is
        asserted here as the one reference so a new one cannot hide beside it.
        """
        active, _, _ = self.place(table_index=0, serve=False)
        served, _, _ = self.place(table_index=1, serve=True)
        self.make_legacy(OrderItem.objects.filter(
            order__in=(active, served), parent_item__isnull=False))
        # Each feed holds a ticket, so each runs its line prefetch.
        self.assertIsNotNone(self.ticket(active, KITCHEN_ACTIVE_URL))
        self.assertIsNotNone(self.ticket(served, KITCHEN_COMPLETED_URL))
        table = MenuItem._meta.db_table
        for url in (KITCHEN_ACTIVE_URL, KITCHEN_COMPLETED_URL):
            with CaptureQueriesContext(connection) as ctx:
                self.feed(url)
            touching = []
            for query in ctx.captured_queries:
                sql = query['sql']
                if f'"{table}"' not in sql:
                    continue
                projection = sql.split(' FROM ', 1)[0]
                self.assertNotIn(f'"{table}".', projection, (url, sql))
                touching.append(sql)
            self.assertEqual(len(touching), 1, (url, touching))
            self.assertIn(f'ORDER BY "order_items"."time_created" DESC, '
                          f'"{table}"."name" ASC', touching[0])


class ReadCostAndStateControlTests(_ProvenanceFixture):

    def _count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            self.feed(url)
        return len(ctx.captured_queries)

    def test_feed_queries_are_unchanged_and_flat_in_lines(self):
        """One single-line ticket, then the same feed with a second ticket of
        two parents and two extras, one of them legacy: the same cost."""
        small, _, _ = self.place_with(
            [{'item': str(self.pasta.id), 'quantity': 1}],
            table_index=0, serve=False)
        one = {url: self._count(url) for url in FEED_QUERIES}
        big, _, _ = self.place(table_index=1, serve=False)
        self.make_legacy(OrderItem.objects.filter(
            order=big, parent_item__isnull=False))
        self.assertEqual(len(list(every_line(self.items(big)))), 4)
        many = {url: self._count(url) for url in FEED_QUERIES}
        self.assertEqual(one, many)
        self.assertEqual(one[KITCHEN_ACTIVE_URL],
                         FEED_QUERIES[KITCHEN_ACTIVE_URL])
        # The completed feed holds no ticket here; its cost is pinned with one
        # in the served case below.

    def test_completed_feed_queries_are_unchanged_and_flat_in_lines(self):
        small, _, _ = self.place_with(
            [{'item': str(self.pasta.id), 'quantity': 1}], table_index=0)
        one = self._count(KITCHEN_COMPLETED_URL)
        big, _, _ = self.place(table_index=1)
        self.make_legacy(OrderItem.objects.filter(
            order=big, parent_item__isnull=False))
        many = self._count(KITCHEN_COMPLETED_URL)
        self.assertEqual(one, many)
        self.assertEqual(one, FEED_QUERIES[KITCHEN_COMPLETED_URL])

    def test_reading_writes_nothing(self):
        captured, _, _ = self.place(table_index=0, serve=False)
        served, _, _ = self.place(table_index=1, serve=True)
        legacy, _, _ = self.place(table_index=2, serve=False)
        self.make_legacy(OrderItem.objects.filter(order=legacy))
        orders = (captured, served, legacy)

        def state():
            return {
                o.pk: {
                    'order': Order.objects.filter(pk=o.pk).values().get(),
                    'lines': list(OrderItem.objects.filter(order=o)
                                  .order_by('pk').values()),
                    'quote_ref': quote_ref(Order.objects.get(pk=o.pk)),
                }
                for o in orders}

        before = state()
        for url in (KITCHEN_ACTIVE_URL, KITCHEN_COMPLETED_URL):
            self.feed(url)
            self.feed(url)
        self.assertEqual(state(), before)


class WireContractControlTests(_ProvenanceFixture):
    """The shape a deployed kitchen client already validates (Frontend
    ``kitchen-wire.ts``: ``isItem`` / ``isExtra``) is kept, and the new keys sit
    beside it."""

    def _mixed_lines(self):
        captured, _, _ = self.place(table_index=0, serve=False)
        legacy, _, _ = self.place(table_index=1, serve=False)
        self.make_legacy(OrderItem.objects.filter(order=legacy))
        return self.items(captured) + self.items(legacy)

    def test_exact_keys_on_lines_and_extras(self):
        for role, line in every_line(self._mixed_lines()):
            expected = EXISTING_KEYS | PROVENANCE_KEYS
            if role == 'parent':
                expected = expected | {'extras'}
            self.assertEqual(set(line), expected, (role, line))

    def test_existing_fields_keep_their_types_and_the_markers_their_words(self):
        for role, line in every_line(self._mixed_lines()):
            self.assertIs(type(line['item_name_snapshot']), str)
            self.assertIs(type(line['quantity']), int)
            self.assertIsInstance(line['modifiers'], list)
            self.assertTrue(all(type(m) is str for m in line['modifiers']))
            self.assertIsInstance(line['allergen_tags'], list)
            for tag in line['allergen_tags']:
                self.assertTrue(all(type(tag[k]) is str
                                    for k in ('name', 'icon', 'colour')))
            if role == 'parent':
                self.assertIsInstance(line['extras'], list)
            for key in PROVENANCE_KEYS:
                self.assertIn(line[key], VOCABULARY)

    def test_the_envelope_is_unchanged(self):
        body = decode_wire(self.feed())
        self.assertEqual(set(body), {'status', 'message', 'kitchen_protocol',
                                     'data'})


# ===================================================================== limits
class DocumentedLimitTests(_ProvenanceFixture):
    """LIMIT. The list markers are an application-level classification resting
    on the inspected writers. A write outside them can defeat it; these pin what
    the reader then says, so the classification is never read as a guarantee.
    Neither write is asserted to have happened to any real row."""

    def test_LIMIT_a_name_only_fill_on_a_legacy_row_reads_as_captured(self):
        order, _, _ = self.place(serve=False)
        pasta = self.row(order, 'Pasta_autodel7 Classic')
        self.make_legacy(OrderItem.objects.filter(pk=pasta.pk))
        OrderItem.objects.filter(pk=pasta.pk).update(
            item_name_snapshot='Filled By Hand')
        line = self.by_name(self.items(order), 'Filled By Hand')
        self.assertEqual(line['allergen_tags'], [])
        self.assertEqual(self.provenance(line), self.all_snapshot())

    def test_LIMIT_an_out_of_band_list_clear_still_reads_as_captured(self):
        order, _, _ = self.place(serve=False)
        burger = self.row(order, 'Beef Burger')
        OrderItem.objects.filter(pk=burger.pk).update(
            allergen_tags_snapshot=[])
        line = self.by_name(self.items(order), 'Beef Burger')
        self.assertEqual(line['allergen_tags'], [])
        self.assertEqual(line['allergen_tags_provenance'], 'snapshot')

    def test_LIMIT_a_whitespace_only_name_counts_as_a_name(self):
        """The creators pass the name through ``SerializerPutOrderItem``, whose
        field trims it, so a stored name that is only whitespace was written
        outside them. The reader keeps it verbatim and counts it as a name, as
        ``historical_name`` does, so the default lists beside it read as
        captured: a case of the name-only fill above, pinned so the list markers
        stay keyed on exactly the test the name marker uses."""
        field = SerializerPutOrderItem().fields['item_name_snapshot']
        self.assertEqual(field.run_validation('  '), '')
        order, _, _ = self.place(serve=False)
        pasta = self.row(order, 'Pasta_autodel7 Classic')
        self.make_legacy(OrderItem.objects.filter(pk=pasta.pk))
        OrderItem.objects.filter(pk=pasta.pk).update(item_name_snapshot='  ')
        line = self.by_name(self.items(order), '  ')
        self.assertEqual(line['modifiers'], [])
        self.assertEqual(line['allergen_tags'], [])
        self.assertEqual(self.provenance(line), self.all_snapshot())
