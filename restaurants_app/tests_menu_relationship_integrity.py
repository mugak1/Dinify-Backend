"""
Persistent menu relationship integrity — write-boundary tests (PR3).

Covers the ONE write-time authority (restaurants_app/controllers/menu_relationships.py)
behind SerializerPutMenuItem and the deletion_blockers on MenuItem / MenuSection /
SectionGroup:

  A. typed extras_applicable input + canonical storage
  B. extras tenancy / role (same-restaurant, is_extra, non-deleted, self, dup)
  C. has_extras + selection-limit effective-state
  D. section/group cohesion (omitted vs explicit-null aware) — real endpoint path
  E. referenced-extra lifecycle (demote / delete / section+group cascade block)
  F/I. operator vs public/diner extras serialization
  J. query-count / performance boundedness

Granular validation is exercised directly through SerializerPutMenuItem (each test
runs inside the TestCase transaction, so the write-time select_for_update locks are
valid); the integration-critical cases (section move, delete 409, cascade) go
through the real restaurant-setup endpoint via JWT.
"""
import uuid

from django.test import TestCase

from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, SectionGroup, MenuItem,
)
from restaurants_app.serializers import (
    SerializerPutMenuItem, SerializerPublicGetMenuItem,
)
from restaurants_app.controllers import menu_relationships as mr
from users_app.models import User
from dinify_backend.configs import ROLES
from dinify_backend.configss.string_definitions import RestaurantStatus_Active


class MenuRelBase(TestCase):
    """Two-tenant menu fixtures + JWT endpoint helpers."""

    def setUp(self):
        self.owner_a = User.objects.create_user(
            first_name='Rel', last_name='OwnerA', email='rel_owner_a@test.com',
            phone_number='256700000610', username='256700000610',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant_a = Restaurant.objects.create(
            name='Rel Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )
        self.owner_b = User.objects.create_user(
            first_name='Rel', last_name='OwnerB', email='rel_owner_b@test.com',
            phone_number='256700000620', username='256700000620',
            country='Uganda', password='password', roles=[],
        )
        self.restaurant_b = Restaurant.objects.create(
            name='Rel Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[ROLES.get('RESTAURANT_OWNER')],
        )

        # restaurant A: two sections, a group in each.
        self.section_a1 = MenuSection.objects.create(
            name='A Sec 1', restaurant=self.restaurant_a, listing_position=0,
            approved=True, enabled=True,
        )
        self.section_a2 = MenuSection.objects.create(
            name='A Sec 2', restaurant=self.restaurant_a, listing_position=1,
            approved=True, enabled=True,
        )
        self.group_a1 = SectionGroup.objects.create(
            name='A Grp 1', section=self.section_a1, approved=True, enabled=True,
        )
        self.group_a2 = SectionGroup.objects.create(
            name='A Grp 2', section=self.section_a2, approved=True, enabled=True,
        )
        # published is_extra items in A
        self.extra1 = self._extra('A Extra 1', self.section_a1)
        self.extra2 = self._extra('A Extra 2', self.section_a1)
        # a plain (non-extra) item in A
        self.plain_a = MenuItem.objects.create(
            name='A Plain', section=self.section_a1, primary_price=1000,
            approved=True, enabled=True, is_extra=False,
        )
        # a parent that accepts extras (empty allowlist to start)
        self.parent_a = MenuItem.objects.create(
            name='A Parent', section=self.section_a1, primary_price=5000,
            approved=True, enabled=True, has_extras=True, extras_applicable=[],
        )

        # restaurant B: a section, a group, an is_extra item (the foreign targets).
        self.section_b1 = MenuSection.objects.create(
            name='B Sec 1', restaurant=self.restaurant_b, listing_position=0,
            approved=True, enabled=True,
        )
        self.group_b1 = SectionGroup.objects.create(
            name='B Grp 1', section=self.section_b1, approved=True, enabled=True,
        )
        self.extra_b = self._extra('B Extra', self.section_b1)

    # --- fixture helpers ------------------------------------------------
    def _extra(self, name, section, **kw):
        defaults = dict(
            primary_price=500, approved=True, enabled=True, is_extra=True,
            available=True, in_stock=True,
        )
        defaults.update(kw)
        return MenuItem.objects.create(name=name, section=section, **defaults)

    # --- direct serializer helpers --------------------------------------
    def _update(self, instance, data):
        """Run a partial update through SerializerPutMenuItem; return (ok, serializer)."""
        ser = SerializerPutMenuItem(instance=instance, data=data, partial=True)
        ok = ser.is_valid()
        if ok:
            ser.save()
        return ok, ser

    def _create(self, data):
        ser = SerializerPutMenuItem(data=data)
        ok = ser.is_valid()
        if ok:
            ser.save()
        return ok, ser

    # --- endpoint helpers ----------------------------------------------
    def _auth(self, user):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def _put(self, user, config_detail, body):
        return self.client.put(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )

    def _post(self, user, config_detail, body, multipart=False):
        if multipart:
            return self.client.post(
                f'/api/v1/restaurant-setup/{config_detail}/',
                data=body, **self._auth(user),
            )
        return self.client.post(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )

    def _delete(self, user, config_detail, body):
        # Secretary.delete() requires a deletion_reason; default one for brevity.
        body = {'deletion_reason': 'test cleanup', **body}
        return self.client.delete(
            f'/api/v1/restaurant-setup/{config_detail}/',
            data=body, content_type='application/json', **self._auth(user),
        )


# =====================================================================
# A. typed input + canonical storage
# =====================================================================
class InputCanonicalTests(MenuRelBase):

    def test_json_array_accepted(self):
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(self.extra1.id)]})
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, [str(self.extra1.id)])

    def test_multipart_stringified_array_accepted(self):
        # POST create through the real endpoint as multipart/form-data.
        body = {
            'name': 'MP Parent', 'section': str(self.section_a1.id),
            'primary_price': '4000.00', 'has_extras': 'true',
            'extras_applicable': f'["{self.extra1.id}"]',
        }
        resp = self._post(self.owner_a, 'menuitems', body, multipart=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        # the create path title-cases the name, so match case-insensitively.
        item = MenuItem.objects.get(name__iexact='MP Parent')
        self.assertEqual(item.extras_applicable, [str(self.extra1.id)])

    def test_empty_array_accepted_and_clears(self):
        self.parent_a.extras_applicable = [str(self.extra1.id)]
        self.parent_a.save(update_fields=['extras_applicable'])
        ok, ser = self._update(self.parent_a, {'extras_applicable': []})
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, [])

    def test_malformed_json_string_rejected(self):
        ok, ser = self._update(self.parent_a, {'extras_applicable': 'not-json'})
        self.assertFalse(ok)
        self.assertIn('extras_applicable', ser.errors)

    def test_non_list_rejected(self):
        ok, ser = self._update(self.parent_a, {'extras_applicable': '5'})
        self.assertFalse(ok)
        self.assertIn('extras_applicable', ser.errors)

    def test_malformed_uuid_member_rejected(self):
        ok, ser = self._update(self.parent_a, {'extras_applicable': ['not-a-uuid']})
        self.assertFalse(ok)
        self.assertIn('extras_applicable', ser.errors)

    def test_duplicate_id_rejected(self):
        eid = str(self.extra1.id)
        ok, ser = self._update(self.parent_a, {'extras_applicable': [eid, eid]})
        self.assertFalse(ok)
        self.assertIn('extras_applicable', ser.errors)

    def test_canonical_lowercase_strings_stored(self):
        upper = str(self.extra1.id).upper()
        ok, ser = self._update(self.parent_a, {'extras_applicable': [upper]})
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, [str(self.extra1.id)])
        self.assertTrue(all(isinstance(x, str) for x in self.parent_a.extras_applicable))

    def test_input_order_preserved(self):
        ids = [str(self.extra2.id), str(self.extra1.id)]
        # allow two extras: min 0
        ok, ser = self._update(self.parent_a, {'extras_applicable': ids})
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, ids)

    def test_omitted_field_preserves_relationship(self):
        self.parent_a.extras_applicable = [str(self.extra1.id)]
        self.parent_a.save(update_fields=['extras_applicable'])
        ok, ser = self._update(self.parent_a, {'name': 'Renamed Parent'})
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, [str(self.extra1.id)])
        self.assertEqual(self.parent_a.name, 'Renamed Parent')


# =====================================================================
# B. tenancy / role
# =====================================================================
class ExtrasTenancyRoleTests(MenuRelBase):

    def _assert_rejected(self, data, unchanged_to=None):
        before = list(MenuItem.objects.get(pk=self.parent_a.pk).extras_applicable)
        ok, ser = self._update(self.parent_a, data)
        self.assertFalse(ok)
        self.assertIn('extras_applicable', ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, before)

    def test_same_restaurant_extra_accepted(self):
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(self.extra1.id)]})
        self.assertTrue(ok, ser.errors)

    def test_multiple_valid_extras_accepted(self):
        ids = [str(self.extra1.id), str(self.extra2.id)]
        ok, ser = self._update(self.parent_a, {'extras_applicable': ids})
        self.assertTrue(ok, ser.errors)

    def test_foreign_restaurant_extra_rejected(self):
        self._assert_rejected({'extras_applicable': [str(self.extra_b.id)]})

    def test_nonexistent_extra_rejected_same_message(self):
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(uuid.uuid4())]})
        self.assertFalse(ok)
        self.assertEqual(
            str(ser.errors['extras_applicable'][0]), mr.INVALID_EXTRAS_MESSAGE,
        )

    def test_non_is_extra_item_rejected(self):
        self._assert_rejected({'extras_applicable': [str(self.plain_a.id)]})

    def test_soft_deleted_extra_rejected(self):
        self.extra1.deleted = True
        self.extra1.save(update_fields=['deleted'])
        self._assert_rejected({'extras_applicable': [str(self.extra1.id)]})

    def test_self_reference_rejected(self):
        # make the parent itself an extra so it would otherwise resolve
        self.parent_a.is_extra = True
        self.parent_a.save(update_fields=['is_extra'])
        self._assert_rejected({'extras_applicable': [str(self.parent_a.id)]})

    def test_unapproved_extra_accepted(self):
        self.extra1.approved = False
        self.extra1.save(update_fields=['approved'])
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(self.extra1.id)]})
        self.assertTrue(ok, ser.errors)

    def test_disabled_extra_accepted(self):
        self.extra1.enabled = False
        self.extra1.save(update_fields=['enabled'])
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(self.extra1.id)]})
        self.assertTrue(ok, ser.errors)

    def test_unavailable_extra_accepted(self):
        self.extra1.available = False
        self.extra1.save(update_fields=['available'])
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(self.extra1.id)]})
        self.assertTrue(ok, ser.errors)

    def test_out_of_stock_extra_accepted(self):
        self.extra1.in_stock = False
        self.extra1.save(update_fields=['in_stock'])
        ok, ser = self._update(self.parent_a, {'extras_applicable': [str(self.extra1.id)]})
        self.assertTrue(ok, ser.errors)

    def test_mixed_valid_and_invalid_changes_nothing(self):
        self._assert_rejected(
            {'extras_applicable': [str(self.extra1.id), str(self.extra_b.id)]}
        )


# =====================================================================
# C. has_extras + limit effective state
# =====================================================================
class EffectiveLimitTests(MenuRelBase):

    def test_has_extras_false_stores_canonical_empty(self):
        item = MenuItem.objects.create(
            name='NoExtras', section=self.section_a1, primary_price=1000,
            approved=True, enabled=True, has_extras=True,
            extras_applicable=[str(self.extra1.id)],
            extras_min_selections=1, extras_max_selections=2,
        )
        ok, ser = self._update(item, {'has_extras': False})
        self.assertTrue(ok, ser.errors)
        item.refresh_from_db()
        self.assertEqual(item.extras_applicable, [])
        self.assertEqual(item.extras_min_selections, 0)
        self.assertIsNone(item.extras_max_selections)

    def test_disabling_extras_clears_list_and_stale_limits(self):
        # frontend clears IDs but resends stale min/max — must 200 and coerce.
        self.parent_a.extras_applicable = [str(self.extra1.id)]
        self.parent_a.extras_min_selections = 1
        self.parent_a.save(update_fields=['extras_applicable', 'extras_min_selections'])
        ok, ser = self._update(
            self.parent_a,
            {'has_extras': False, 'extras_min_selections': 5, 'extras_max_selections': 3},
        )
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertEqual(self.parent_a.extras_applicable, [])
        self.assertEqual(self.parent_a.extras_min_selections, 0)
        self.assertIsNone(self.parent_a.extras_max_selections)

    def test_non_empty_list_while_has_extras_false_rejected(self):
        item = MenuItem.objects.create(
            name='FalseButList', section=self.section_a1, primary_price=1000,
            approved=True, enabled=True, has_extras=False,
        )
        ok, ser = self._update(
            item, {'has_extras': False, 'extras_applicable': [str(self.extra1.id)]},
        )
        self.assertFalse(ok)
        self.assertIn('extras_applicable', ser.errors)

    def test_has_extras_true_empty_min_zero_valid(self):
        ok, ser = self._update(
            self.parent_a,
            {'has_extras': True, 'extras_applicable': [], 'extras_min_selections': 0},
        )
        self.assertTrue(ok, ser.errors)

    def test_empty_list_positive_min_rejected(self):
        ok, ser = self._update(
            self.parent_a,
            {'has_extras': True, 'extras_applicable': [], 'extras_min_selections': 1},
        )
        self.assertFalse(ok)

    def test_min_greater_than_max_rejected(self):
        ids = [str(self.extra1.id), str(self.extra2.id)]
        ok, ser = self._update(
            self.parent_a,
            {'extras_applicable': ids, 'extras_min_selections': 2,
             'extras_max_selections': 1},
        )
        self.assertFalse(ok)

    def test_min_greater_than_count_rejected(self):
        ok, ser = self._update(
            self.parent_a,
            {'extras_applicable': [str(self.extra1.id)], 'extras_min_selections': 2},
        )
        self.assertFalse(ok)

    def test_positive_max_greater_than_count_rejected(self):
        ok, ser = self._update(
            self.parent_a,
            {'extras_applicable': [str(self.extra1.id)], 'extras_max_selections': 5},
        )
        self.assertFalse(ok)

    def test_max_null_is_unlimited(self):
        ok, ser = self._update(
            self.parent_a,
            {'extras_applicable': [str(self.extra1.id)], 'extras_max_selections': None},
        )
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertIsNone(self.parent_a.extras_max_selections)

    def test_max_zero_follows_unlimited_contract(self):
        ok, ser = self._update(
            self.parent_a,
            {'extras_applicable': [str(self.extra1.id)], 'extras_max_selections': 0},
        )
        self.assertTrue(ok, ser.errors)
        self.parent_a.refresh_from_db()
        self.assertIsNone(self.parent_a.extras_max_selections)

    def test_min_only_partial_checks_persisted_list(self):
        # persisted: one extra; raise min to 2 without touching the list -> reject.
        self.parent_a.extras_applicable = [str(self.extra1.id)]
        self.parent_a.save(update_fields=['extras_applicable'])
        ok, ser = self._update(self.parent_a, {'extras_min_selections': 2})
        self.assertFalse(ok)

    def test_max_only_partial_checks_persisted_min(self):
        # persisted: min 2 (with two extras); lower max to 1 -> min>max reject.
        self.parent_a.extras_applicable = [str(self.extra1.id), str(self.extra2.id)]
        self.parent_a.extras_min_selections = 2
        self.parent_a.save(update_fields=['extras_applicable', 'extras_min_selections'])
        ok, ser = self._update(self.parent_a, {'extras_max_selections': 1})
        self.assertFalse(ok)

    def test_list_only_partial_shrink_below_limits_rejected_atomically(self):
        # persisted: two extras, min 2; shrink list to one -> min>count reject, no change.
        self.parent_a.extras_applicable = [str(self.extra1.id), str(self.extra2.id)]
        self.parent_a.extras_min_selections = 2
        self.parent_a.save(update_fields=['extras_applicable', 'extras_min_selections'])
        ok, ser = self._update(
            self.parent_a, {'extras_applicable': [str(self.extra1.id)]},
        )
        self.assertFalse(ok)
        self.parent_a.refresh_from_db()
        self.assertEqual(len(self.parent_a.extras_applicable), 2)


# =====================================================================
# D. section/group cohesion — the decisive omitted-vs-null cases
# =====================================================================
class SectionGroupOmittedNullTests(MenuRelBase):

    def setUp(self):
        super().setUp()
        self.grouped = MenuItem.objects.create(
            name='Grouped Item', section=self.section_a1,
            section_group=self.group_a1, primary_price=1000,
            approved=True, enabled=True,
        )

    def test_section_move_group_omitted_retained_old_group_fails(self):
        # DECISIVE regression: move to section_a2, section_group OMITTED -> the
        # retained group_a1 (section_a1) violates cohesion; 400, DB unchanged.
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.grouped.id), 'section': str(self.section_a2.id)},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.section_id, self.section_a1.id)
        self.assertEqual(self.grouped.section_group_id, self.group_a1.id)

    def test_section_move_with_explicit_null_group_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.grouped.id), 'section': str(self.section_a2.id),
             'section_group': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.section_id, self.section_a2.id)
        self.assertIsNone(self.grouped.section_group_id)

    def test_section_move_with_valid_new_group_succeeds(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.grouped.id), 'section': str(self.section_a2.id),
             'section_group': str(self.group_a2.id)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.section_id, self.section_a2.id)
        self.assertEqual(self.grouped.section_group_id, self.group_a2.id)

    def test_unrelated_update_preserves_valid_group(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.grouped.id), 'name': 'Grouped Renamed'},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.section_group_id, self.group_a1.id)
        self.assertEqual(self.grouped.name, 'Grouped Renamed')

    def test_cross_restaurant_section_move_fails(self):
        resp = self._put(
            self.owner_a, 'menuitems',
            {'id': str(self.grouped.id), 'section': str(self.section_b1.id),
             'section_group': None},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.grouped.refresh_from_db()
        self.assertEqual(self.grouped.section_id, self.section_a1.id)


# =====================================================================
# E. referenced-extra lifecycle + section/group cascade block
# =====================================================================
class LifecycleTests(MenuRelBase):

    def setUp(self):
        super().setUp()
        # parent_a references extra1
        self.parent_a.extras_applicable = [str(self.extra1.id)]
        self.parent_a.save(update_fields=['extras_applicable'])

    def test_referenced_extra_cannot_be_demoted(self):
        ok, ser = self._update(self.extra1, {'is_extra': False})
        self.assertFalse(ok)
        self.assertIn('is_extra', ser.errors)
        self.extra1.refresh_from_db()
        self.assertTrue(self.extra1.is_extra)

    def test_unreferenced_extra_can_be_demoted(self):
        ok, ser = self._update(self.extra2, {'is_extra': False})
        self.assertTrue(ok, ser.errors)
        self.extra2.refresh_from_db()
        self.assertFalse(self.extra2.is_extra)

    def test_referenced_extra_delete_blocked_409(self):
        resp = self._delete(self.owner_a, 'menuitems', {'id': str(self.extra1.id)})
        self.assertEqual(resp.status_code, 409, resp.content)
        self.extra1.refresh_from_db()
        self.assertFalse(self.extra1.deleted)

    def test_unreferenced_extra_delete_ok(self):
        resp = self._delete(self.owner_a, 'menuitems', {'id': str(self.extra2.id)})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.extra2.refresh_from_db()
        self.assertTrue(self.extra2.deleted)

    def test_deletion_blockers_none_for_non_extra(self):
        self.assertIsNone(self.plain_a.deletion_blockers())

    def test_deletion_blockers_set_when_referenced(self):
        self.assertIsNotNone(self.extra1.deletion_blockers())

    def test_deleted_parent_does_not_block(self):
        self.parent_a.deleted = True
        self.parent_a.save(update_fields=['deleted'])
        self.assertIsNone(self.extra1.deletion_blockers())

    def test_has_extras_false_parent_does_not_block(self):
        self.parent_a.has_extras = False
        self.parent_a.save(update_fields=['has_extras'])
        self.assertIsNone(self.extra1.deletion_blockers())

    def test_cross_restaurant_parent_does_not_block(self):
        # a B parent referencing extra1's id must not block (restaurant-scoped).
        # Clear the same-restaurant reference from setUp so ONLY the B parent remains.
        self.parent_a.extras_applicable = []
        self.parent_a.save(update_fields=['extras_applicable'])
        MenuItem.objects.create(
            name='B Parent', section=self.section_b1, primary_price=1000,
            approved=True, enabled=True, has_extras=True,
            extras_applicable=[str(self.extra1.id)],
        )
        self.assertIsNone(self.extra1.deletion_blockers())

    def test_remove_last_reference_then_demote_succeeds(self):
        self.parent_a.extras_applicable = []
        self.parent_a.save(update_fields=['extras_applicable'])
        ok, ser = self._update(self.extra1, {'is_extra': False})
        self.assertTrue(ok, ser.errors)

    def test_section_delete_blocked_when_extra_referenced_elsewhere(self):
        # extra1 lives in section_a1; parent in section_a1 references it. Move the
        # PARENT to section_a2 so deleting section_a1 would strand the reference.
        self.parent_a.section = self.section_a2
        self.parent_a.save(update_fields=['section'])
        resp = self._delete(self.owner_a, 'menusections', {'id': str(self.section_a1.id)})
        self.assertEqual(resp.status_code, 409, resp.content)
        self.section_a1.refresh_from_db()
        self.assertFalse(self.section_a1.deleted)

    def test_section_delete_ok_when_only_internal_reference(self):
        # parent stays in section_a1 alongside extra1 -> both cascade away, no orphan.
        resp = self._delete(self.owner_a, 'menusections', {'id': str(self.section_a1.id)})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_group_delete_blocked_when_extra_referenced_from_outside_group(self):
        # put extra1 in group_a1; parent (groupless in section_a1) references it.
        self.extra1.section_group = self.group_a1
        self.extra1.save(update_fields=['section_group'])
        resp = self._delete(self.owner_a, 'sectiongroups', {'id': str(self.group_a1.id)})
        self.assertEqual(resp.status_code, 409, resp.content)
        self.group_a1.refresh_from_db()
        self.assertFalse(self.group_a1.deleted)


# =====================================================================
# F/I. operator vs public/diner extras serialization
# =====================================================================
class OperatorSerializationTests(MenuRelBase):

    def setUp(self):
        super().setUp()
        # extra1 is valid but UNPUBLISHED (unapproved); extra2 valid+published.
        self.extra1.approved = False
        self.extra1.enabled = False
        self.extra1.save(update_fields=['approved', 'enabled'])
        self.parent_a.extras_applicable = [str(self.extra2.id), str(self.extra1.id)]
        self.parent_a.save(update_fields=['extras_applicable'])

    def test_operator_returns_valid_unpublished_extra(self):
        # no menu_policy context -> operator branch: unpublished extra still surfaces.
        data = SerializerPublicGetMenuItem(self.parent_a).data
        ids = [e['id'] for e in data['extras']]
        self.assertIn(self.extra1.id, ids)
        self.assertIn(self.extra2.id, ids)

    def test_operator_preserves_configured_order(self):
        data = SerializerPublicGetMenuItem(self.parent_a).data
        ids = [str(e['id']) for e in data['extras']]
        self.assertEqual(ids, [str(self.extra2.id), str(self.extra1.id)])

    def test_operator_excludes_foreign_and_corrupt(self):
        self.parent_a.extras_applicable = [
            str(self.extra2.id), str(self.extra_b.id), 'garbage', str(self.parent_a.id),
        ]
        self.parent_a.save(update_fields=['extras_applicable'])
        data = SerializerPublicGetMenuItem(self.parent_a).data
        ids = [str(e['id']) for e in data['extras']]
        self.assertEqual(ids, [str(self.extra2.id)])

    def test_public_diner_hides_unpublished_extra(self):
        # WITH menu_policy context: the unpublished extra1 must NOT surface.
        from django.utils import timezone
        from restaurants_app.controllers.menu_publication import build_safe_extras_map
        now = timezone.localtime()
        extras_map = build_safe_extras_map([self.parent_a], self.restaurant_a.id)
        ctx = {'menu_policy': {'now': now, 'restaurant_id': self.restaurant_a.id,
                               'extras_map': extras_map}}
        data = SerializerPublicGetMenuItem(self.parent_a, context=ctx).data
        ids = [str(e['id']) for e in data['extras']]
        self.assertNotIn(str(self.extra1.id), ids)
        self.assertIn(str(self.extra2.id), ids)


# =====================================================================
# J. query-count / performance boundedness
# =====================================================================
class QueryBoundednessTests(MenuRelBase):

    def test_validating_ten_extras_is_bounded(self):
        extras = [self._extra(f'Bulk Extra {i}', self.section_a1) for i in range(10)]
        ids = [str(e.id) for e in extras]
        # one restaurant-scoped batch resolve for the extras regardless of count.
        with self.assertNumQueries(1):
            mr.resolve_and_validate_extras_tenancy(
                ids, restaurant_id=self.restaurant_a.id, self_id=None,
            )

    def test_one_extra_same_query_count_as_ten(self):
        one = [str(self.extra1.id)]
        with self.assertNumQueries(1):
            mr.resolve_and_validate_extras_tenancy(
                one, restaurant_id=self.restaurant_a.id, self_id=None,
            )

    def test_inbound_reference_check_single_query(self):
        self.parent_a.extras_applicable = [str(self.extra1.id)]
        self.parent_a.save(update_fields=['extras_applicable'])
        with self.assertNumQueries(1):
            list(mr.find_blocking_inbound_references(
                item_ids=[self.extra1.id], restaurant_id=self.restaurant_a.id,
            ))

    def test_operator_get_extras_single_query(self):
        self.parent_a.extras_applicable = [str(self.extra1.id), str(self.extra2.id)]
        self.parent_a.save(update_fields=['extras_applicable'])
        # get_extras itself is ONE query (section_id already loaded on the parent);
        # measured directly so full-item serialization (tags, group, ...) is excluded.
        ser = SerializerPublicGetMenuItem()
        with self.assertNumQueries(1):
            ser.get_extras(self.parent_a)
