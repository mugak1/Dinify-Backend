"""
Tests for the support_app: tenant isolation, admin gating, reference
generation, status timestamps, the create whitelist, and null-safety.
"""
import json

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
)
from platform_admin_app.testing import give_legacy_platform_role
from support_app.models import SupportIssue


ISSUES_URL = '/api/v1/support/issues/'
ADMIN_URL = '/api/v1/support/admin/issues/'


def make_user(phone, roles, first='Test', last='User'):
    return User.objects.create_user(
        first_name=first,
        last_name=last,
        email=f'{phone}@test.com',
        phone_number=phone,
        username=phone,
        country='Uganda',
        password='password',
        roles=roles,
    )


def make_issue(restaurant, **kwargs):
    defaults = {
        'category': 'bug',
        'impact': 'question',
        'title': 'An issue',
        'description': 'A description long enough.',
    }
    defaults.update(kwargs)
    return SupportIssue.objects.create(restaurant=restaurant, **defaults)


class SupportAppTestBase(TestCase):
    def setUp(self):
        # Restaurant A + owner
        self.owner_a = make_user('256700000010', [], first='Owner', last='A')
        self.restaurant_a = Restaurant.objects.create(
            name='Restaurant A', location='loc-a',
            status=RestaurantStatus_Live, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_OWNER],
        )

        # Restaurant B + owner
        self.owner_b = make_user('256700000020', [], first='Owner', last='B')
        self.restaurant_b = Restaurant.objects.create(
            name='Restaurant B', location='loc-b',
            status=RestaurantStatus_Live, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )

        # An authenticated user with no employment anywhere.
        self.outsider = make_user('256700000030', [])
        # Two accounts carrying the RETIRED platform role strings. They used to be
        # the only principals admitted to the admin triage surface; that surface is
        # gone and the strings grant nothing. Written through the ORM, since the
        # write paths refuse them.
        self.legacy_admin = give_legacy_platform_role(
            make_user('256700000040', [], first='Legacy', last='Holder'))
        self.legacy_account_manager = give_legacy_platform_role(
            make_user('256700000050', []), 'dinify_account_manager')
        # A plain support agent identity, for assigned_to fixtures.
        self.agent = make_user('256700000041', [], first='Support', last='Agent')

    # ---- request helpers -------------------------------------------------
    def auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def post_issue(self, user, body):
        return self.client.post(
            ISSUES_URL, data=json.dumps(body),
            content_type='application/json', **self.auth(user),
        )

    def admin_put(self, user, body):
        return self.client.put(
            ADMIN_URL, data=json.dumps(body),
            content_type='application/json', **self.auth(user),
        )


class CreateTests(SupportAppTestBase):
    def test_create_success_returns_reference_and_defaults_open(self):
        resp = self.post_issue(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'orders_kds', 'impact': 'blocking_service',
            'title': 'KDS not printing', 'description': 'Tickets do not print.',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertEqual(data['reference'], 'SUP-000001')
        self.assertEqual(data['status'], 'open')
        issue = SupportIssue.objects.get(id=data['id'])
        self.assertEqual(str(issue.created_by_id), str(self.owner_a.id))
        self.assertEqual(str(issue.restaurant_id), str(self.restaurant_a.id))

    def test_two_creates_are_sequential(self):
        r1 = self.post_issue(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'bug', 'impact': 'question',
            'title': 'one', 'description': 'first issue',
        })
        r2 = self.post_issue(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'bug', 'impact': 'question',
            'title': 'two', 'description': 'second issue',
        })
        self.assertEqual(r1.json()['data']['reference'], 'SUP-000001')
        self.assertEqual(r2.json()['data']['reference'], 'SUP-000002')

    def test_manager_can_create(self):
        manager = make_user('256700000060', [])
        RestaurantEmployee.objects.create(
            user=manager, restaurant=self.restaurant_a, roles=[RESTAURANT_MANAGER],
        )
        resp = self.post_issue(manager, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'menu', 'impact': 'non_urgent',
            'title': 'menu typo', 'description': 'A small menu typo.',
        })
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_non_owner_manager_employee_can_create(self):
        # Support is an UNGATED module: any ACTIVE employee (not just
        # owner/manager) may raise an issue for their own restaurant.
        waiter = make_user('256700000070', [])
        RestaurantEmployee.objects.create(
            user=waiter, restaurant=self.restaurant_a, roles=['waiter'],
        )
        resp = self.post_issue(waiter, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'menu', 'impact': 'non_urgent',
            'title': 'waiter issue', 'description': 'Reported by a waiter.',
        })
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_create_for_unaffiliated_restaurant_is_rejected(self):
        resp = self.post_issue(self.owner_a, {
            'restaurant': str(self.restaurant_b.id),
            'category': 'bug', 'impact': 'question',
            'title': 'x', 'description': 'xxxxxx',
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            SupportIssue.objects.filter(restaurant=self.restaurant_b).exists()
        )

    def test_create_missing_required_returns_400(self):
        resp = self.post_issue(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'bug',
        })
        self.assertEqual(resp.status_code, 400)

    def test_create_whitelist_drops_privileged_fields(self):
        resp = self.post_issue(self.owner_a, {
            'restaurant': str(self.restaurant_a.id),
            'category': 'bug', 'impact': 'question',
            'title': 'Legit title', 'description': 'A legitimate description.',
            # Privileged fields a malicious client might try to set:
            'status': 'resolved',
            'assigned_to': str(self.agent.id),
            'internal_notes': 'should not stick',
            'resolution_summary': 'nope',
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        issue = SupportIssue.objects.get(id=resp.json()['data']['id'])
        self.assertEqual(issue.status, 'open')
        self.assertIsNone(issue.assigned_to_id)
        self.assertEqual(issue.internal_notes, '')
        self.assertEqual(issue.resolution_summary, '')


class ReferenceTests(SupportAppTestBase):
    def test_reference_format(self):
        issue = make_issue(self.restaurant_a)
        self.assertRegex(issue.reference, r'^SUP-\d{6}$')

    def test_reference_continues_from_existing_max(self):
        make_issue(self.restaurant_a, reference='SUP-000005')
        nxt = make_issue(self.restaurant_a)
        self.assertEqual(nxt.reference, 'SUP-000006')

    def test_references_are_unique(self):
        refs = {make_issue(self.restaurant_a).reference for _ in range(3)}
        self.assertEqual(len(refs), 3)


class RestaurantListTests(SupportAppTestBase):
    def test_list_only_returns_own_restaurant_even_with_foreign_param(self):
        issue_a = make_issue(self.restaurant_a, created_by=self.owner_a)
        issue_b = make_issue(self.restaurant_b, created_by=self.owner_b)
        resp = self.client.get(
            f'{ISSUES_URL}?restaurant={self.restaurant_b.id}',
            **self.auth(self.owner_a),
        )
        self.assertEqual(resp.status_code, 200)
        ids = {r['id'] for r in resp.json()['data']['records']}
        self.assertIn(str(issue_a.id), ids)
        self.assertNotIn(str(issue_b.id), ids)

    def test_outsider_list_is_empty(self):
        make_issue(self.restaurant_a)
        resp = self.client.get(ISSUES_URL, **self.auth(self.outsider))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['records'], [])

    def test_list_serializer_hides_internal_fields(self):
        make_issue(self.restaurant_a, created_by=self.owner_a, internal_notes='secret')
        resp = self.client.get(ISSUES_URL, **self.auth(self.owner_a))
        record = resp.json()['data']['records'][0]
        self.assertNotIn('internal_notes', record)
        self.assertNotIn('assigned_to', record)


class RestaurantDetailTests(SupportAppTestBase):
    def test_detail_cross_tenant_returns_404(self):
        issue_b = make_issue(self.restaurant_b, created_by=self.owner_b)
        resp = self.client.get(
            f'{ISSUES_URL}{issue_b.id}/', **self.auth(self.owner_a),
        )
        self.assertEqual(resp.status_code, 404)

    def test_detail_own_issue_hides_internal_fields(self):
        issue = make_issue(
            self.restaurant_a, created_by=self.owner_a,
            internal_notes='secret', assigned_to=self.agent,
        )
        resp = self.client.get(
            f'{ISSUES_URL}{issue.id}/', **self.auth(self.owner_a),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertNotIn('internal_notes', data)
        self.assertNotIn('assigned_to', data)
        self.assertEqual(data['created_by_name'], 'Owner A')

    def test_detail_missing_issue_returns_404(self):
        resp = self.client.get(
            f'{ISSUES_URL}00000000-0000-0000-0000-000000000000/',
            **self.auth(self.owner_a),
        )
        self.assertEqual(resp.status_code, 404)


class AdminIssuesRetirementTests(SupportAppTestBase):
    """
    ``/api/v1/support/admin/issues/`` is RETIRED — endpoint and route both.

    It read and wrote ANY tenant's support issues (an explicit unrestricted
    ``SupportIssue.objects.all()`` write queryset, plus ``internal_notes`` and
    ``assigned_to``) on the authority of a ``dinify_admin`` string in the caller's
    ``User.roles``, and the deleted dinify-mgt frontend was its only consumer.

    Retirement SUBSUMES the old gate matrix: previously a restaurant user got 403
    and a role-holder got 200. Now nobody gets 200. A delegated administrator still
    reaches ONE restaurant's issues through the restaurant-facing routes, which is
    what delegation is for. Phase 1 rebuilds triage on /api/admin/v1.
    """

    def test_the_admin_route_is_gone_for_every_principal(self):
        for user in (
            self.owner_a, self.outsider,
            self.legacy_admin, self.legacy_account_manager,
        ):
            with self.subTest(user=user.username):
                listing = self.client.get(ADMIN_URL, **self.auth(user))
                self.assertEqual(listing.status_code, 404, listing.content)

    def test_the_admin_write_verb_is_gone_for_every_principal(self):
        issue = make_issue(self.restaurant_a)
        for user in (self.owner_a, self.legacy_admin):
            with self.subTest(user=user.username):
                resp = self.admin_put(
                    user, {'id': str(issue.id), 'status': 'resolved'})
                self.assertEqual(resp.status_code, 404, resp.content)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'open')
        self.assertIsNone(issue.resolved_at)

    def test_internal_notes_stay_unreachable_through_the_restaurant_routes(self):
        # The tenant-facing surface never exposed the triage fields, and retiring
        # the admin one must not have changed that.
        issue = make_issue(
            self.restaurant_a, created_by=self.owner_a, internal_notes='private')
        resp = self.client.get(
            f'{ISSUES_URL}{issue.id}/', **self.auth(self.owner_a))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn('internal_notes', resp.json()['data'])


class IssueStatusStampingTests(SupportAppTestBase):
    """
    The resolved_at / closed_at stamping that ``AdminUpdateTests`` used to drive
    through the retired admin PUT.

    The behaviour lives in ``SupportIssue.save()``, not in the endpoint, so the
    coverage moves down to the model rather than being dropped with the route.
    """

    def test_resolving_stamps_resolved_at(self):
        issue = make_issue(self.restaurant_a)
        self.assertIsNone(issue.resolved_at)
        issue.status = 'resolved'
        issue.save()
        issue.refresh_from_db()
        self.assertIsNotNone(issue.resolved_at)
        self.assertIsNone(issue.closed_at)

    def test_closing_stamps_closed_at(self):
        issue = make_issue(self.restaurant_a)
        issue.status = 'closed'
        issue.save()
        issue.refresh_from_db()
        self.assertIsNotNone(issue.closed_at)

    def test_resolved_at_is_preserved_on_reopen(self):
        issue = make_issue(self.restaurant_a)
        issue.status = 'resolved'
        issue.save()
        issue.refresh_from_db()
        first_resolved_at = issue.resolved_at
        self.assertIsNotNone(first_resolved_at)
        issue.status = 'open'
        issue.save()
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'open')
        self.assertEqual(issue.resolved_at, first_resolved_at)


class NullSafetyTests(SupportAppTestBase):
    def test_created_by_name_is_null_safe(self):
        reporter = make_user('256700000099', [])
        RestaurantEmployee.objects.create(
            user=reporter, restaurant=self.restaurant_a, roles=[RESTAURANT_MANAGER],
        )
        issue = make_issue(self.restaurant_a, created_by=reporter)
        reporter.delete()  # SET_NULL clears created_by
        issue.refresh_from_db()
        self.assertIsNone(issue.created_by_id)
        resp = self.client.get(
            f'{ISSUES_URL}{issue.id}/', **self.auth(self.owner_a),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()['data']['created_by_name'])
