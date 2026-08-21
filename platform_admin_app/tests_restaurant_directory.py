"""
The admin-plane restaurant directory and detail reads (Phase 1, Step 1).

Covers the HTTP contract — authentication, filtering, pagination, error shape — and
pins the representations that Step 1 deliberately leaves transitional, so that a
later change to readiness, payment mode or subscription is a decision somebody makes
on purpose rather than a shape that drifts.
"""
import itertools

from django.conf import settings as dj_settings
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_LIFECYCLE_STATES,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from platform_admin_app import restaurant_reads, sessions
from platform_admin_app.audit_actions import ADMIN_RESTAURANT_LIFECYCLE_TRANSITION
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import RESULT_SUCCESS, AdminAuditLog
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import DiningArea, Restaurant, Table
from support_app.models import SupportIssue
from users_app.models import User

# Distinct phone range from the other admin suites. Unbounded: this suite creates an
# owner per restaurant and several fixtures build many, so a fixed range runs dry.
_PHONE = (f'25670900{n:05d}' for n in itertools.count(1))

PASSWORD = 'correct-horse-battery'

_ADMIN_OVERRIDES = dict(
    ROOT_URLCONF='dinify_backend.urls_admin',
    MIDDLEWARE=[
        'platform_admin_app.middleware.RequestIDMiddleware',
        'platform_admin_app.middleware.ClientIPMiddleware',
        *dj_settings.MIDDLEWARE,
    ],
    REST_FRAMEWORK={
        **dj_settings.REST_FRAMEWORK,
        'DEFAULT_AUTHENTICATION_CLASSES': (
            'platform_admin_app.authentication.AdminSessionAuthentication',
        ),
        'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
    },
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)

LIST_URL = '/admin/v1/restaurants/'


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=[],
        account_type=account_type,
    )


def _make_admin(email='dir-admin@t.com', username='dir-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name, *, status=RestaurantStatus_Live, deleted=False,
                     location=None, is_test=False, owner=None):
    return Restaurant.objects.create(
        name=name,
        location=location if location is not None else f'{name} Road',
        status=status,
        deleted=deleted,
        is_test=is_test,
        owner=owner or _make_user(f'owner-{name}@t.com'.replace(' ', '-')),
    )


def _detail_url(restaurant):
    return f'/admin/v1/restaurants/{restaurant.id}/'


class _AdminReadTestCase(AuditAssertionsMixin, TestCase):
    """Shared authenticated-admin client. Sessions are NOT elevated on purpose."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        raw, self.session = sessions.create_session(self.admin)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    def get(self, url, **params):
        return self.client.get(url, params or None)


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryAuthTests(_AdminReadTestCase):
    def test_unauthenticated_list_is_401(self):
        self.assertEqual(Client().get(LIST_URL).status_code, 401)

    def test_unauthenticated_detail_is_401(self):
        restaurant = _make_restaurant('Java House')
        self.assertEqual(Client().get(_detail_url(restaurant)).status_code, 401)

    def test_authenticated_list_is_200(self):
        _make_restaurant('Java House')
        self.assertEqual(self.get(LIST_URL).status_code, 200)

    def test_authenticated_detail_is_200(self):
        restaurant = _make_restaurant('Java House')
        self.assertEqual(self.get(_detail_url(restaurant)).status_code, 200)

    def test_reads_do_not_require_elevation(self):
        """
        The whole point of the read contract: a plain session is enough.

        `setUp` never calls `sessions.elevate`, so these 200s ARE the assertion —
        this test states the intent so a future `IsRecentlyElevated` added here
        fails with a name that explains why it is wrong.
        """
        restaurant = _make_restaurant('Java House')
        self.assertIsNone(self.session.elevated_at)
        self.assertEqual(self.get(LIST_URL).status_code, 200)
        self.assertEqual(self.get(_detail_url(restaurant)).status_code, 200)

    def test_reads_are_not_audited(self):
        """AdminAuditLog is a record of action, not an access log."""
        restaurant = _make_restaurant('Java House')
        self.get(LIST_URL)
        self.get(_detail_url(restaurant))
        self.assertEqual(AdminAuditLog.objects.count(), 0)


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryVisibilityTests(_AdminReadTestCase):
    def test_soft_deleted_restaurant_is_excluded_from_the_list(self):
        _make_restaurant('Visible')
        _make_restaurant('Gone', deleted=True)
        names = [r['name'] for r in self.get(LIST_URL).json()['data']['results']]
        self.assertEqual(names, ['Visible'])

    def test_soft_deleted_restaurant_detail_is_404(self):
        deleted = _make_restaurant('Gone', deleted=True)
        self.assertEqual(self.get(_detail_url(deleted)).status_code, 404)

    def test_unknown_uuid_detail_is_404(self):
        url = '/admin/v1/restaurants/00000000-0000-0000-0000-000000000000/'
        self.assertEqual(self.get(url).status_code, 404)


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryFilterTests(_AdminReadTestCase):
    def setUp(self):
        super().setUp()
        self.java = _make_restaurant(
            'Java House', status=RestaurantStatus_Live, location='Kampala Road',
        )
        self.cafe = _make_restaurant(
            'Cafe Javas', status=RestaurantStatus_Onboarding, location='Entebbe Road',
        )
        self.old = _make_restaurant(
            'Old Kitchen', status=RestaurantStatus_Offboarded, location='Jinja Road',
        )

    def _names(self, **params):
        response = self.get(LIST_URL, **params)
        self.assertEqual(response.status_code, 200, response.content)
        return sorted(r['name'] for r in response.json()['data']['results'])

    def test_search_matches_name_case_insensitively(self):
        self.assertEqual(self._names(search='java house'), ['Java House'])

    def test_search_matches_location(self):
        self.assertEqual(self._names(search='Entebbe'), ['Cafe Javas'])

    def test_search_is_trimmed(self):
        self.assertEqual(self._names(search='  Entebbe  '), ['Cafe Javas'])

    def test_status_filter(self):
        self.assertEqual(
            self._names(status=RestaurantStatus_Onboarding), ['Cafe Javas'],
        )

    def test_invalid_status_is_400_not_an_empty_page(self):
        response = self.get(LIST_URL, status='active')
        self.assertEqual(response.status_code, 400)
        self.assertIn('status', response.json()['errors'])

    def test_attention_true_selects_onboarding_restaurants(self):
        self.assertEqual(self._names(attention='true'), ['Cafe Javas'])

    def test_attention_false_excludes_them(self):
        self.assertEqual(
            self._names(attention='false'), ['Java House', 'Old Kitchen'],
        )

    def test_invalid_attention_is_400(self):
        response = self.get(LIST_URL, attention='maybe')
        self.assertEqual(response.status_code, 400)
        self.assertIn('attention', response.json()['errors'])

    def test_every_invalid_parameter_is_reported_at_once(self):
        response = self.get(LIST_URL, status='nope', attention='maybe', page='0')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            set(response.json()['errors']), {'status', 'attention', 'page'},
        )

    # --- unknown parameters (review finding: a typo must not silently widen) ---

    def test_a_mistyped_filter_name_is_400_not_an_unfiltered_page(self):
        """
        `?stats=live` is a typo for `status`, and silently ignoring it is the worst
        failure this endpoint has: the operator believes they filtered, and the
        unfiltered page they get back looks exactly like a real answer.
        """
        response = self.get(LIST_URL, stats='live')
        self.assertEqual(response.status_code, 400)
        self.assertIn('__all__', response.json()['errors'])
        self.assertIn('stats', response.json()['errors']['__all__'][0])

    def test_unknown_parameters_are_reported_alongside_other_errors(self):
        response = self.get(LIST_URL, stats='live', attention='maybe')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            set(response.json()['errors']), {'__all__', 'attention'},
        )

    def test_all_unknown_parameters_are_named_in_one_message(self):
        response = self.get(LIST_URL, stats='live', pge='2')
        self.assertEqual(response.status_code, 400)
        message = response.json()['errors']['__all__'][0]
        self.assertIn('stats', message)
        self.assertIn('pge', message)

    def test_every_known_parameter_is_accepted(self):
        """
        THE RATCHET for the allowlist: each supported parameter must round-trip.

        Adding a filter means adding it to `KNOWN_PARAMS` too; without this a new
        parameter would ship silently rejected by the very guard meant to catch typos.
        """
        for name, value in (
            ('search', 'Java'), ('status', RestaurantStatus_Live),
            ('attention', 'true'), ('page', '1'), ('page_size', '10'),
        ):
            with self.subTest(parameter=name):
                self.assertEqual(self.get(LIST_URL, **{name: value}).status_code, 200)

    def test_the_allowlist_matches_what_the_parser_reads(self):
        self.assertEqual(
            restaurant_reads.KNOWN_PARAMS,
            frozenset({'search', 'status', 'attention', 'page', 'page_size'}),
        )


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryPaginationTests(_AdminReadTestCase):
    def setUp(self):
        super().setUp()
        for index in range(5):
            _make_restaurant(f'R{index}')

    def test_pagination_is_deterministic_across_pages(self):
        first = self.get(LIST_URL, page=1, page_size=2).json()['data']
        second = self.get(LIST_URL, page=2, page_size=2).json()['data']
        third = self.get(LIST_URL, page=3, page_size=2).json()['data']

        self.assertEqual(first['pagination'],
                         {'page': 1, 'page_size': 2, 'count': 5, 'pages': 3})
        names = (
            [r['name'] for r in first['results']]
            + [r['name'] for r in second['results']]
            + [r['name'] for r in third['results']]
        )
        # Ordered by name, and every row appears exactly once.
        self.assertEqual(names, ['R0', 'R1', 'R2', 'R3', 'R4'])

    def test_same_named_restaurants_still_paginate_without_loss(self):
        """`name` is not unique, so the `id` tiebreak is what makes paging total."""
        Restaurant.objects.all().delete()
        for index in range(4):
            _make_restaurant('Same Name', location=f'Branch {index}')
        seen = []
        for page in (1, 2):
            seen += [
                r['id'] for r in
                self.get(LIST_URL, page=page, page_size=2).json()['data']['results']
            ]
        self.assertEqual(len(set(seen)), 4)

    def test_page_beyond_the_end_is_an_empty_page_not_an_error(self):
        data = self.get(LIST_URL, page=99).json()['data']
        self.assertEqual(data['results'], [])
        self.assertEqual(data['pagination']['count'], 5)

    def test_default_page_size(self):
        data = self.get(LIST_URL).json()['data']
        self.assertEqual(
            data['pagination']['page_size'], restaurant_reads.DEFAULT_PAGE_SIZE,
        )

    def test_page_size_is_capped(self):
        response = self.get(LIST_URL, page_size=restaurant_reads.MAX_PAGE_SIZE + 1)
        self.assertEqual(response.status_code, 400)
        self.assertIn('page_size', response.json()['errors'])

    def test_invalid_page_values_are_400(self):
        for value in ('0', '-1', 'abc'):
            with self.subTest(page=value):
                self.assertEqual(self.get(LIST_URL, page=value).status_code, 400)

    def test_invalid_page_size_values_are_400(self):
        for value in ('0', '-1', 'abc'):
            with self.subTest(page_size=value):
                self.assertEqual(
                    self.get(LIST_URL, page_size=value).status_code, 400,
                )

    # --- page bound (review finding: an offset must stay representable) ---

    def test_an_enormous_page_is_400_not_a_500(self):
        """
        `page` multiplies with `page_size` into a SQL OFFSET.

        `page_size` was always capped; `page` was not, so a page number just past
        `bigint` produced `DataError: bigint out of range` on PostgreSQL — a 500 from
        the one endpoint whose contract is that a bad parameter is a 400. Values here
        straddle the `bigint` ceiling and go well beyond it.
        """
        for value in ('9223372036854775809', '1' + '0' * 30):
            with self.subTest(page=value):
                response = self.get(LIST_URL, page=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn('page', response.json()['errors'])

    def test_the_page_cap_is_the_boundary(self):
        self.assertEqual(
            self.get(LIST_URL, page=restaurant_reads.MAX_PAGE).status_code, 200,
        )
        self.assertEqual(
            self.get(LIST_URL, page=restaurant_reads.MAX_PAGE + 1).status_code, 400,
        )

    def test_the_largest_reachable_offset_stays_inside_bigint(self):
        """The cap has to actually bound the product, not just the page number."""
        largest = (restaurant_reads.MAX_PAGE - 1) * restaurant_reads.MAX_PAGE_SIZE
        self.assertLess(largest, 2 ** 63 - 1)

    def test_the_capped_page_really_reaches_the_database(self):
        """A 200 at the cap must mean the query RAN, not that it was short-circuited."""
        data = self.get(LIST_URL, page=restaurant_reads.MAX_PAGE).json()['data']
        self.assertEqual(data['results'], [])
        self.assertEqual(data['pagination']['count'], 5)


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryRowContentTests(_AdminReadTestCase):
    def setUp(self):
        super().setUp()
        self.restaurant = _make_restaurant(
            'Java House', status=RestaurantStatus_Onboarding,
        )

    def _row(self):
        return self.get(LIST_URL).json()['data']['results'][0]

    def test_is_test_defaults_false_and_is_serialized(self):
        self.assertIs(self._row()['is_test'], False)
        Restaurant.objects.filter(pk=self.restaurant.pk).update(is_test=True)
        self.assertIs(self._row()['is_test'], True)

    def test_readiness_for_onboarding_is_the_not_configured_seam(self):
        """
        Step 1 surfaces the seam's honest answer; it does not implement Step 3.

        If this ever reads `ready`, either the real checklist landed (and this test
        should be updated deliberately) or something started guessing.
        """
        self.assertEqual(self._row()['readiness'], {
            'state': restaurant_reads.READINESS_NOT_READY,
            'blocker_count': 1,
            'blockers': ['readiness_not_configured'],
        })

    def test_readiness_is_not_applicable_outside_onboarding(self):
        for status in (RestaurantStatus_Live, RestaurantStatus_Suspended,
                       RestaurantStatus_Offboarded):
            with self.subTest(status=status):
                Restaurant.objects.filter(pk=self.restaurant.pk).update(status=status)
                self.assertEqual(self._row()['readiness'], {
                    'state': restaurant_reads.READINESS_NOT_APPLICABLE,
                    'blocker_count': 0,
                    'blockers': [],
                })

    def test_payment_mode_is_unconfigured_and_never_inferred(self):
        """`require_order_prepayments` is not the commercial payment mode."""
        for prepay in (False, True):
            with self.subTest(require_order_prepayments=prepay):
                Restaurant.objects.filter(pk=self.restaurant.pk).update(
                    require_order_prepayments=prepay,
                )
                row = self._row()
                self.assertIsNone(row['payment_mode'])
                self.assertIs(row['payment_mode_configured'], False)

    def test_subscription_is_transitional_and_names_no_financial_state(self):
        subscription = self._row()['subscription']
        self.assertEqual(
            subscription['source'], restaurant_reads.SUBSCRIPTION_SOURCE_LEGACY,
        )
        self.assertIs(subscription['has_commercial_subscription'], False)
        self.assertIn('legacy_validity_flag', subscription)
        # No key may claim a financial fact the database cannot prove.
        for forbidden in ('paid', 'trial', 'invoice_current', 'no_receivables',
                          'overdue', 'balance'):
            self.assertNotIn(forbidden, subscription)

    def test_open_issue_count_excludes_resolved_and_closed(self):
        for status in (SupportIssue.Status.OPEN, SupportIssue.Status.IN_PROGRESS,
                       SupportIssue.Status.RESOLVED, SupportIssue.Status.CLOSED):
            SupportIssue.objects.create(
                restaurant=self.restaurant, category=SupportIssue.Category.BUG,
                impact=SupportIssue.Impact.QUESTION, status=status,
                title=f'{status} issue', description='x',
            )
        self.assertEqual(self._row()['open_issue_count'], 2)

    def test_open_issue_count_excludes_soft_deleted_issues(self):
        issue = SupportIssue.objects.create(
            restaurant=self.restaurant, category=SupportIssue.Category.BUG,
            impact=SupportIssue.Impact.QUESTION, status=SupportIssue.Status.OPEN,
            title='gone', description='x',
        )
        SupportIssue.objects.filter(pk=issue.pk).update(deleted=True)
        self.assertEqual(self._row()['open_issue_count'], 0)

    def test_last_activity_at_is_null_without_admin_activity(self):
        self.assertIsNone(self._row()['last_activity_at'])

    def test_last_activity_at_is_the_latest_audit_entry(self):
        older = AdminAuditLog.objects.create(
            actor=self.admin, action='admin.restaurant.a', result=RESULT_SUCCESS,
            restaurant_id=self.restaurant.id,
            created_at=timezone.now() - timezone.timedelta(hours=2),
        )
        newest = AdminAuditLog.objects.create(
            actor=self.admin, action='admin.restaurant.b', result=RESULT_SUCCESS,
            restaurant_id=self.restaurant.id, created_at=timezone.now(),
        )
        self.assertNotEqual(older.created_at, newest.created_at)
        self.assertEqual(
            self._row()['last_activity_at'], newest.created_at.isoformat(),
        )

    def test_last_activity_ignores_other_restaurants_entries(self):
        other = _make_restaurant('Other')
        AdminAuditLog.objects.create(
            actor=self.admin, action='admin.restaurant.x', result=RESULT_SUCCESS,
            restaurant_id=other.id, created_at=timezone.now(),
        )
        row = next(
            r for r in self.get(LIST_URL).json()['data']['results']
            if r['id'] == str(self.restaurant.id)
        )
        self.assertIsNone(row['last_activity_at'])

    def test_needs_attention_true_for_onboarding(self):
        self.assertIs(self._row()['needs_attention'], True)

    def test_needs_attention_false_once_live(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Live,
        )
        self.assertIs(self._row()['needs_attention'], False)

    def test_open_support_issues_alone_do_not_raise_attention(self):
        """Step 1's attention definition is narrow, and deliberately so."""
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            status=RestaurantStatus_Live,
        )
        SupportIssue.objects.create(
            restaurant=self.restaurant, category=SupportIssue.Category.BUG,
            impact=SupportIssue.Impact.BLOCKING_SERVICE,
            status=SupportIssue.Status.OPEN, title='down', description='x',
        )
        row = self._row()
        self.assertEqual(row['open_issue_count'], 1)
        self.assertIs(row['needs_attention'], False)


@override_settings(**_ADMIN_OVERRIDES)
class AttentionDefinitionTests(TestCase):
    """
    THE RATCHET: the row predicate and the SQL filter must never disagree.

    A directory that omits a restaurant its own row would have flagged is a
    restaurant the operator never sees. Whoever fills the readiness seam in Step 3
    is told by this test that they owe `attention_filter` an update too.
    """

    def test_filter_and_row_predicate_agree_for_every_lifecycle_state(self):
        for status in RESTAURANT_LIFECYCLE_STATES:
            _make_restaurant(f'R-{status}', status=status)

        by_filter = set(
            restaurant_reads.directory_queryset()
            .filter(restaurant_reads.attention_filter())
            .values_list('id', flat=True)
        )
        by_predicate = {
            r.id for r in restaurant_reads.directory_queryset()
            if restaurant_reads.needs_attention(r)
        }
        self.assertEqual(by_filter, by_predicate)
        # And it is not vacuously equal because both are empty.
        self.assertTrue(by_predicate)

    def test_exclude_is_the_exact_complement(self):
        for status in RESTAURANT_LIFECYCLE_STATES:
            _make_restaurant(f'R-{status}', status=status)
        included = set(
            restaurant_reads.directory_queryset()
            .filter(restaurant_reads.attention_filter()).values_list('id', flat=True)
        )
        excluded = set(
            restaurant_reads.directory_queryset()
            .exclude(restaurant_reads.attention_filter()).values_list('id', flat=True)
        )
        everything = set(
            restaurant_reads.directory_queryset().values_list('id', flat=True)
        )
        self.assertEqual(included | excluded, everything)
        self.assertEqual(included & excluded, set())


@override_settings(**_ADMIN_OVERRIDES)
class DirectoryQueryCountTests(_AdminReadTestCase):
    """
    ADMIN-DIR-N1-00: the directory's cost must not grow with the portfolio.

    A control-plane list that issues a query per row is the failure mode this test
    exists to catch, and the two columns most likely to cause it — the open support
    count and the last admin activity — are exactly the two that would be a natural
    per-row lookup. They are an aggregate and a correlated subquery instead.

    The assertion is EQUALITY between a small and a larger page, not an upper bound:
    an upper bound generous enough to pass today would not notice a per-row read
    appearing tomorrow. The absolute number is deliberately not pinned, because it
    legitimately includes session/auth reads that are not this endpoint's concern.
    """

    def _populate(self, count, *, issues_each=0, activity_each=0):
        for index in range(count):
            restaurant = _make_restaurant(
                f'Q{index:03d}', status=RestaurantStatus_Onboarding,
            )
            for issue in range(issues_each):
                SupportIssue.objects.create(
                    restaurant=restaurant, category=SupportIssue.Category.BUG,
                    impact=SupportIssue.Impact.QUESTION,
                    status=SupportIssue.Status.OPEN,
                    title=f'i{issue}', description='x',
                )
            for entry in range(activity_each):
                AdminAuditLog.objects.create(
                    actor=self.admin, action=f'admin.restaurant.q{entry}',
                    result=RESULT_SUCCESS, restaurant_id=restaurant.id,
                    created_at=timezone.now(),
                )

    def _query_count(self, page_size):
        with CaptureQueriesContext(connection) as captured:
            response = self.get(LIST_URL, page_size=page_size)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()['data']['results'])
        return len(captured)

    def test_query_count_does_not_grow_with_the_number_of_restaurants(self):
        self._populate(2, issues_each=2, activity_each=2)
        small = self._query_count(page_size=2)

        self._populate(18, issues_each=2, activity_each=2)
        large = self._query_count(page_size=20)

        self.assertEqual(
            small, large,
            f'Directory query count grew from {small} (2 rows) to {large} '
            f'(20 rows) — that is an N+1.',
        )

    def test_detail_query_count_does_not_grow_with_related_rows(self):
        """
        The detail endpoint may issue several bounded queries — but a fixed number.

        More tables, more support issues and more audit history must not each add a
        query; the Overview aggregates them.
        """
        restaurant = _make_restaurant('Detail Q', status=RestaurantStatus_Onboarding)

        def measure():
            with CaptureQueriesContext(connection) as captured:
                response = self.get(_detail_url(restaurant))
                self.assertEqual(response.status_code, 200, response.content)
            return len(captured)

        lean = measure()

        area = DiningArea.objects.create(name='Main', restaurant=restaurant)
        for index in range(10):
            Table.objects.create(
                restaurant=restaurant, number=index + 1, dining_area=area,
            )
            SupportIssue.objects.create(
                restaurant=restaurant, category=SupportIssue.Category.BUG,
                impact=SupportIssue.Impact.QUESTION,
                status=SupportIssue.Status.OPEN,
                title=f'i{index}', description='x',
            )
            AdminAuditLog.objects.create(
                actor=self.admin, action=f'admin.restaurant.d{index}',
                result=RESULT_SUCCESS, restaurant_id=restaurant.id,
                created_at=timezone.now(),
            )

        rich = measure()
        self.assertEqual(
            lean, rich,
            f'Detail query count grew from {lean} to {rich} as related rows were '
            f'added — that is an N+1.',
        )


@override_settings(**_ADMIN_OVERRIDES)
class DetailContentTests(_AdminReadTestCase):
    def setUp(self):
        super().setUp()
        self.owner = _make_user('owner-detail@t.com')
        self.restaurant = _make_restaurant(
            'Java House', status=RestaurantStatus_Onboarding, owner=self.owner,
        )

    def _data(self):
        response = self.get(_detail_url(self.restaurant))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_identity_and_allowed_transitions_come_from_the_lifecycle_service(self):
        from restaurants_app.controllers import lifecycle

        data = self._data()
        self.assertEqual(data['id'], str(self.restaurant.id))
        self.assertEqual(data['status'], RestaurantStatus_Onboarding)
        self.assertIs(data['is_test'], False)
        self.assertEqual(
            data['allowed_transitions'],
            lifecycle.allowed_targets(RestaurantStatus_Onboarding),
        )

    def test_owner_block_uses_existing_fields_and_claims_no_claim_status(self):
        owner = self._data()['owner']
        self.assertEqual(owner['id'], str(self.owner.id))
        self.assertEqual(owner['email'], self.owner.email)
        self.assertIs(owner['claim_tracked'], False)
        self.assertIsNone(owner['claim_status'])

    def test_readiness_matches_the_directory_row_exactly(self):
        row = next(
            r for r in self.get(LIST_URL).json()['data']['results']
            if r['id'] == str(self.restaurant.id)
        )
        self.assertEqual(self._data()['readiness'], row['readiness'])

    def test_operations_summary_counts_tables_and_areas(self):
        area = DiningArea.objects.create(name='Main', restaurant=self.restaurant)
        Table.objects.create(restaurant=self.restaurant, number=1, dining_area=area)
        Table.objects.create(
            restaurant=self.restaurant, number=2, dining_area=area, enabled=False,
        )
        Table.objects.create(
            restaurant=self.restaurant, number=3, status='out_of_service',
        )
        deleted = Table.objects.create(restaurant=self.restaurant, number=4)
        Table.objects.filter(pk=deleted.pk).update(deleted=True)

        operations = self._data()['operations']
        self.assertEqual(operations['table_count'], 3)
        # Mirrors Table.is_available_for_scan: enabled, active, not out of service.
        self.assertEqual(operations['usable_table_count'], 1)
        self.assertEqual(operations['dining_area_count'], 1)
        self.assertIsNone(operations['latest_order'])

    def test_usable_table_count_mirrors_the_model_property(self):
        """The SQL mirror must agree with `Table.is_available_for_scan` per table."""
        Table.objects.create(restaurant=self.restaurant, number=1)
        Table.objects.create(restaurant=self.restaurant, number=2, enabled=False)
        Table.objects.create(restaurant=self.restaurant, number=3, is_active=False)
        Table.objects.create(
            restaurant=self.restaurant, number=4, status='out_of_service',
        )
        # `is_available_for_scan` is a METHOD, not a property — it must be called,
        # or every table reads as available and this comparison passes vacuously.
        expected = sum(
            1 for t in Table.objects.filter(restaurant=self.restaurant, deleted=False)
            if t.is_available_for_scan()
        )
        self.assertEqual(expected, 1)
        self.assertEqual(self._data()['operations']['usable_table_count'], expected)

    def test_recent_activity_is_newest_first_and_limited_to_four(self):
        for index in range(6):
            AdminAuditLog.objects.create(
                actor=self.admin, action=f'admin.restaurant.a{index}',
                result=RESULT_SUCCESS, restaurant_id=self.restaurant.id,
                created_at=timezone.now() - timezone.timedelta(minutes=10 - index),
            )
        entries = self._data()['recent_activity']
        self.assertEqual(len(entries), 4)
        self.assertEqual(
            [e['action'] for e in entries],
            ['admin.restaurant.a5', 'admin.restaurant.a4',
             'admin.restaurant.a3', 'admin.restaurant.a2'],
        )

    def test_recent_activity_omits_forensic_detail(self):
        AdminAuditLog.objects.create(
            actor=self.admin, action='admin.restaurant.x', result=RESULT_SUCCESS,
            restaurant_id=self.restaurant.id, source_ip='203.0.113.9',
            user_agent='secret-agent', before_state={'a': 1}, after_state={'a': 2},
            request_id='rq-1',
        )
        entry = self._data()['recent_activity'][0]
        self.assertEqual(
            set(entry), {'id', 'timestamp', 'action', 'result', 'actor'},
        )

    def test_recent_activity_is_empty_without_history(self):
        self.assertEqual(self._data()['recent_activity'], [])


@override_settings(**_ADMIN_OVERRIDES)
class TransitionContractUnchangedTests(AuditAssertionsMixin, TestCase):
    """The elevation-gated write is untouched by the new read routes."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin(email='trans-admin@t.com', username='trans-admin')
        self.restaurant = _make_restaurant('Java House', status=RestaurantStatus_Live)
        raw, self.session = sessions.create_session(self.admin)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    def _url(self):
        return f'/admin/v1/restaurants/{self.restaurant.id}/transition/'

    def test_transition_still_requires_elevation(self):
        response = self.client.post(
            self._url(),
            data={'to_state': RestaurantStatus_Suspended,
                  'reason': 'Confirmed non-payment after the third notice.'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_transition_still_works_when_elevated(self):
        sessions.elevate(self.session)
        response = self.client.post(
            self._url(),
            data={'to_state': RestaurantStatus_Suspended,
                  'reason': 'Confirmed non-payment after the third notice.'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()['data']['status'], RestaurantStatus_Suspended,
        )
        self.assertAudited(
            ADMIN_RESTAURANT_LIFECYCLE_TRANSITION,
            result=RESULT_SUCCESS, restaurant_id=self.restaurant.id,
        )
