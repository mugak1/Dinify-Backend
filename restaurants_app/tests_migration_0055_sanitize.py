"""
Tests for migration 0055_sanitize_menu_item_extras (PR3 historical repair).

Two layers:
  * SanitizeRowLogicTests — the pure, deterministic _sanitize_row transformation
    matrix (§14) + idempotency, with no DB.
  * Migration0055ExecutorTests — a real MigrationExecutor run that migrates back
    to 0054, builds a corrupt corpus with the historical models, migrates forward
    to 0055, and proves the repair end-to-end.
"""
import importlib
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

_mig = importlib.import_module(
    'restaurants_app.migrations.0055_sanitize_menu_item_extras'
)


class _Row:
    """Lightweight stand-in for a historical MenuItem row."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _row(id=None, section_id='sec', section_group_id=None, is_extra=False,
         deleted=False, has_extras=True, extras_applicable=None,
         extras_min_selections=0, extras_max_selections=None):
    return _Row(
        id=id or uuid.uuid4(), section_id=section_id, section_group_id=section_group_id,
        is_extra=is_extra, deleted=deleted, has_extras=has_extras,
        extras_applicable=[] if extras_applicable is None else extras_applicable,
        extras_min_selections=extras_min_selections,
        extras_max_selections=extras_max_selections,
    )


class SanitizeRowLogicTests(SimpleTestCase):
    def setUp(self):
        self.rid = uuid.uuid4()
        self.e1 = uuid.uuid4()
        self.e2 = uuid.uuid4()
        # valid extras for this restaurant
        self.valid = {self.rid: {str(self.e1), str(self.e2)}}

    def _sanitize(self, row, item_restaurant=None):
        item_restaurant = item_restaurant or {row.id: self.rid}
        return _mig._sanitize_row(row, item_restaurant, self.valid, {})

    def test_list_passthrough_valid(self):
        row = _row(extras_applicable=[str(self.e1)])
        self.assertEqual(self._sanitize(row), set())  # already canonical
        self.assertEqual(row.extras_applicable, [str(self.e1)])

    def test_json_string_parsed(self):
        row = _row(extras_applicable=f'["{self.e1}"]')
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e1)])

    def test_python_literal_string_parsed(self):
        row = _row(extras_applicable=f"['{self.e1}']")
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e1)])

    def test_garbage_becomes_empty(self):
        row = _row(has_extras=False, extras_applicable='not-a-list-at-all')
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [])

    def test_duplicates_first_occurrence_kept(self):
        row = _row(extras_applicable=[str(self.e1), str(self.e2), str(self.e1)])
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e1), str(self.e2)])

    def test_order_preserved(self):
        row = _row(extras_applicable=[str(self.e2), str(self.e1)])
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e2), str(self.e1)])

    def test_foreign_or_non_extra_or_deleted_or_missing_dropped(self):
        # none of these ids are in the restaurant's valid-extra set
        stranger = uuid.uuid4()
        row = _row(extras_applicable=[str(self.e1), str(stranger)])
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e1)])

    def test_self_reference_dropped(self):
        me = uuid.uuid4()
        row = _row(id=me, extras_applicable=[str(me), str(self.e1)])
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e1)])

    def test_malformed_member_dropped(self):
        row = _row(extras_applicable=['garbage', str(self.e1)])
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [str(self.e1)])

    def test_deleted_parent_zeroed(self):
        row = _row(deleted=True, has_extras=True,
                   extras_applicable=[str(self.e1)],
                   extras_min_selections=1, extras_max_selections=2)
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [])
        self.assertFalse(row.has_extras)
        self.assertEqual(row.extras_min_selections, 0)
        self.assertIsNone(row.extras_max_selections)

    def test_has_extras_false_zeroed(self):
        row = _row(has_extras=False, extras_applicable=[str(self.e1)],
                   extras_min_selections=3, extras_max_selections=4)
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [])
        self.assertEqual(row.extras_min_selections, 0)
        self.assertIsNone(row.extras_max_selections)

    def test_limits_clamped_to_repaired_list(self):
        # two valid extras; min 5 / max 9 -> clamped to len 2.
        row = _row(extras_applicable=[str(self.e1), str(self.e2)],
                   extras_min_selections=5, extras_max_selections=9)
        self._sanitize(row)
        self.assertEqual(row.extras_min_selections, 2)
        self.assertEqual(row.extras_max_selections, 2)

    def test_max_zero_normalised_to_none(self):
        row = _row(extras_applicable=[str(self.e1)], extras_max_selections=0)
        self._sanitize(row)
        self.assertIsNone(row.extras_max_selections)

    def test_empty_repaired_list_forces_min0_maxnone(self):
        stranger = uuid.uuid4()
        row = _row(extras_applicable=[str(stranger)],
                   extras_min_selections=2, extras_max_selections=3)
        self._sanitize(row)
        self.assertEqual(row.extras_applicable, [])
        self.assertEqual(row.extras_min_selections, 0)
        self.assertIsNone(row.extras_max_selections)

    def test_group_mismatch_cleared(self):
        g = uuid.uuid4()
        # group g belongs to section 'other', item is in section 'sec' -> clear
        row = _row(section_id='sec', section_group_id=g, has_extras=False)
        changed = _mig._sanitize_row(row, {row.id: self.rid}, self.valid, {g: 'other'})
        self.assertIn('section_group', changed)
        self.assertIsNone(row.section_group_id)

    def test_coherent_group_kept(self):
        g = uuid.uuid4()
        row = _row(section_id='sec', section_group_id=g, has_extras=False)
        changed = _mig._sanitize_row(row, {row.id: self.rid}, self.valid, {g: 'sec'})
        self.assertNotIn('section_group', changed)
        self.assertEqual(row.section_group_id, g)

    def test_idempotent(self):
        row = _row(extras_applicable=[str(self.e2), str(self.e1), str(self.e1), 'x'],
                   extras_min_selections=9, extras_max_selections=0)
        first = self._sanitize(row)
        self.assertTrue(first)                       # changed on first pass
        second = self._sanitize(row)
        self.assertEqual(second, set())              # no change on second pass


class Migration0055ExecutorTests(TransactionTestCase):
    """End-to-end MigrationExecutor run (0054 -> 0055) over a corrupt corpus."""

    # Pin users_app to its latest migration in BOTH targets so the historical User
    # model state matches the applied DB schema (the country_of_origin->country
    # rename otherwise drifts between project_state and the DB).
    #
    # The pin still wants bumping when a users_app migration lands — it decides which
    # historical User model THIS class's own tests see — but a stale one can no
    # longer damage the rest of the suite: ``tearDown`` now restores users_app to its
    # graph leaf as well as restaurants_app. It used to restore only restaurants_app,
    # so a stale pin left users_app migrated BACKWARD for every test ordered after
    # this class, dropping whichever column the newer migrations had added. That is
    # exactly what happened when ``users_app/0014`` added ``customer_access_state``
    # against a pin still reading ``0011``: six later TransactionTestCases died with
    # "column customer_access_state does not exist". Resolving the leaf cannot go
    # stale, which is the same fix the restaurants_app restore already carries.
    migrate_from = [
        ('restaurants_app', '0054_table_qr_version'),
        ('users_app', '0014_customer_access_state'),
    ]
    migrate_to = [
        ('restaurants_app', '0055_sanitize_menu_item_extras'),
        ('users_app', '0014_customer_access_state'),
    ]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    @staticmethod
    def _create(model, **kw):
        # Filter to fields that exist on the historical model (schema drift-safe).
        names = {f.name for f in model._meta.get_fields()}
        return model.objects.create(**{k: v for k, v in kw.items() if k in names})

    def test_forward_repairs_corpus(self):
        old_apps = self._migrate(self.migrate_from)
        User = old_apps.get_model('users_app', 'User')
        Restaurant = old_apps.get_model('restaurants_app', 'Restaurant')
        MenuSection = old_apps.get_model('restaurants_app', 'MenuSection')
        SectionGroup = old_apps.get_model('restaurants_app', 'SectionGroup')
        MenuItem = old_apps.get_model('restaurants_app', 'MenuItem')

        owner = self._create(
            User, first_name='Mig', last_name='Owner', email='mig_owner@test.com',
            phone_number='256700000900', username='256700000900', country='Uganda',
            password='x',
        )
        rest = self._create(
            Restaurant, name='Mig Rest', location='loc', status='live', owner=owner,
        )
        rest_b = self._create(
            Restaurant, name='Mig Rest B', location='loc-b', status='live', owner=owner,
        )
        sec1 = self._create(MenuSection, name='S1', restaurant=rest, approved=True, enabled=True)
        sec2 = self._create(MenuSection, name='S2', restaurant=rest, approved=True, enabled=True)
        sec_b = self._create(MenuSection, name='SB', restaurant=rest_b, approved=True, enabled=True)
        grp2 = self._create(SectionGroup, name='G2', section=sec2)

        def item(name, section, **kw):
            defaults = dict(primary_price=1000, approved=True, enabled=True)
            defaults.update(kw)
            return self._create(MenuItem, name=name, section=section, **defaults)

        valid_extra = item('Valid Extra', sec1, is_extra=True)
        unpublished_extra = item('Unpub Extra', sec1, is_extra=True, approved=False, enabled=False)
        non_extra = item('Non Extra', sec1, is_extra=False)
        deleted_extra = item('Deleted Extra', sec1, is_extra=True, deleted=True)
        foreign_extra = item('Foreign Extra', sec_b, is_extra=True)
        missing = uuid.uuid4()

        # a parent carrying every bad case + valid + unpublished, as a py-literal string
        raw = str([
            str(valid_extra.id), str(unpublished_extra.id), str(valid_extra.id),  # dup
            str(foreign_extra.id), str(non_extra.id), str(deleted_extra.id),
            str(missing), 'garbage',
        ])
        parent = item(
            'Parent', sec1, has_extras=True, extras_applicable=raw,
            extras_min_selections=5, extras_max_selections=9,
        )
        parent.extras_applicable = raw            # ensure the string form is stored
        parent.save()
        self_ref = item('Self Ref', sec1, is_extra=True,
                        has_extras=True, extras_applicable=[])
        self_ref.extras_applicable = [str(self_ref.id), str(valid_extra.id)]
        self_ref.save()
        stale_false = item('Stale False', sec1, has_extras=False,
                           extras_min_selections=4, extras_max_selections=6)
        stale_false.extras_applicable = [str(valid_extra.id)]
        stale_false.save()
        mismatched_group = item('Mismatched', sec1)   # in sec1 but group belongs to sec2
        mismatched_group.section_group = grp2
        mismatched_group.save()

        # --- migrate forward ---
        new_apps = self._migrate(self.migrate_to)
        NMenuItem = new_apps.get_model('restaurants_app', 'MenuItem')

        p = NMenuItem.objects.get(pk=parent.id)
        # only valid + unpublished survive, order preserved, dedup, all bad dropped
        self.assertEqual(
            p.extras_applicable, [str(valid_extra.id), str(unpublished_extra.id)]
        )
        # limits clamped to the repaired length (2)
        self.assertEqual(p.extras_min_selections, 2)
        self.assertEqual(p.extras_max_selections, 2)

        sr = NMenuItem.objects.get(pk=self_ref.id)
        self.assertEqual(sr.extras_applicable, [str(valid_extra.id)])  # self dropped

        sf = NMenuItem.objects.get(pk=stale_false.id)
        self.assertEqual(sf.extras_applicable, [])
        self.assertFalse(sf.has_extras)
        self.assertEqual(sf.extras_min_selections, 0)
        self.assertIsNone(sf.extras_max_selections)

        mg = NMenuItem.objects.get(pk=mismatched_group.id)
        self.assertIsNone(mg.section_group_id)     # incoherent group cleared

        # re-running the sanitizer makes NO further change (idempotent)
        item_restaurant = {
            iid: rid for iid, rid in NMenuItem.objects.values_list('id', 'section__restaurant_id')
        }
        from collections import defaultdict
        valid_map = defaultdict(set)
        for iid, rid in NMenuItem.objects.filter(is_extra=True, deleted=False).values_list('id', 'section__restaurant_id'):
            valid_map[rid].add(str(iid))
        NSectionGroup = new_apps.get_model('restaurants_app', 'SectionGroup')
        group_section = dict(NSectionGroup.objects.values_list('id', 'section_id'))
        changed_again = [
            r.id for r in NMenuItem.objects.all()
            if _mig._sanitize_row(r, item_restaurant, valid_map, group_section)
        ]
        self.assertEqual(changed_again, [])

    def test_safe_on_empty_database(self):
        # migrating forward with no MenuItem rows must not error.
        self._migrate(self.migrate_from)
        new_apps = self._migrate(self.migrate_to)
        NMenuItem = new_apps.get_model('restaurants_app', 'MenuItem')
        self.assertEqual(NMenuItem.objects.count(), 0)

    def tearDown(self):
        # Leave the schema fully migrated for the rest of the suite.
        #
        # Restores to the app's CURRENT graph leaf, not a hardcoded name. This used
        # to name `0055_sanitize_menu_item_extras` — the head when it was written —
        # which silently went stale on every later migration and left the rest of the
        # suite running against a rolled-back schema. It stayed invisible only
        # because `0056` adds a constraint rather than a column, so nothing failed;
        # `0057` adds `Restaurant.is_test`, and the drop took out every
        # `TransactionTestCase` ordered after this class with "column is_test does
        # not exist". The same trap the `migrate_from`/`migrate_to` comment above
        # describes for `users_app`, on the app this class actually rewinds.
        #
        # `leaf_nodes` cannot go stale: a new migration moves the leaf, and this
        # follows it. There is exactly one leaf per app unless the graph has been
        # forked, which `makemigrations --check` in CI already refuses.
        # BOTH apps this class rewinds, not just one. users_app is pinned in the
        # targets above, so a stale pin would otherwise leave it rolled back here.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        self._migrate(
            executor.loader.graph.leaf_nodes('restaurants_app')
            + executor.loader.graph.leaf_nodes('users_app')
        )
