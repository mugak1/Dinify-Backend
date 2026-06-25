"""
Tests for reviews_app.

Model-level (``ReviewModelTests``):
* restaurant is denormalised from the order on creation;
* is_public is seeded from the rating band (high -> public, low -> private),
  honouring the threshold boundary, and an explicit override survives a later
  update (it is never re-derived once the row exists);
* the is_critical convenience property.

API-level (``ReviewSubmissionTests`` / ``RestaurantReviewListTests``):
* diner submission (AllowAny) — happy path + auto-set restaurant/is_public,
  and the 404/400/409/400 friendly-error branches;
* owner retrieval (JWT) — tenant isolation, the non-widening ?restaurant=
  param, and the rating/critical/resolution filters.
"""
import json
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee, Table
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RESTAURANT_OWNER, RESTAURANT_MANAGER,
    OrderStatus_Cancelled,
)
from reviews_app.models import PUBLIC_RATING_THRESHOLD, Review, ReviewTag
from reviews_app.serializers import ReviewRestaurantReadSerializer
from reviews_app.controllers.review_analytics import DEFAULT_ANALYTICS_WINDOW_DAYS


def make_user(phone):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=[],
    )


class ReviewModelTests(TestCase):
    def setUp(self):
        self.owner = make_user('256700000010')
        self.restaurant = Restaurant.objects.create(
            name='Test Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

    def make_order(self):
        # A fresh order per review — the Review.order relation is one-per-order.
        return Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
        )

    # --- restaurant denormalisation -------------------------------------
    def test_restaurant_set_from_order_on_create(self):
        order = self.make_order()
        review = Review.objects.create(order=order, overall_rating=5)
        self.assertEqual(review.restaurant_id, order.restaurant_id)

    # --- is_public rating-band seeding ----------------------------------
    def test_high_rating_is_public(self):
        review = Review.objects.create(order=self.make_order(), overall_rating=5)
        self.assertTrue(review.is_public)

    def test_low_rating_is_not_public(self):
        review = Review.objects.create(order=self.make_order(), overall_rating=2)
        self.assertFalse(review.is_public)

    def test_threshold_boundary_is_public(self):
        # overall_rating == PUBLIC_RATING_THRESHOLD is public (>= band).
        review = Review.objects.create(
            order=self.make_order(), overall_rating=PUBLIC_RATING_THRESHOLD,
        )
        self.assertTrue(review.is_public)

    # --- explicit override preserved on update --------------------------
    def test_is_public_override_survives_update(self):
        review = Review.objects.create(order=self.make_order(), overall_rating=5)
        self.assertTrue(review.is_public)
        review.is_public = False
        review.save()
        review.refresh_from_db()
        # Not re-derived from the rating band on update.
        self.assertFalse(review.is_public)

    # --- is_critical property -------------------------------------------
    def test_is_critical(self):
        low = Review.objects.create(order=self.make_order(), overall_rating=2)
        high = Review.objects.create(order=self.make_order(), overall_rating=4)
        self.assertTrue(low.is_critical)
        self.assertFalse(high.is_critical)


SUBMIT_URL = '/api/v1/reviews/submit/'
REVIEWS_URL = '/api/v1/reviews/'


class ReviewApiTestBase(TestCase):
    """
    Two restaurants, each with an owner (active owner employment) and a table,
    plus an authenticated outsider with no employment. Helpers mirror
    support_app/tests.py: JWT via RefreshToken, and a paginated list shape under
    ``data.records``.
    """

    def setUp(self):
        # Restaurant A + owner + table.
        self.owner_a = make_user('256700000110')
        self.restaurant_a = Restaurant.objects.create(
            name='Restaurant A', location='loc-a',
            status=RestaurantStatus_Active, owner=self.owner_a,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_OWNER],
        )
        self.table_a = Table.objects.create(number=1, restaurant=self.restaurant_a)

        # Restaurant B + owner + table.
        self.owner_b = make_user('256700000120')
        self.restaurant_b = Restaurant.objects.create(
            name='Restaurant B', location='loc-b',
            status=RestaurantStatus_Active, owner=self.owner_b,
        )
        RestaurantEmployee.objects.create(
            user=self.owner_b, restaurant=self.restaurant_b,
            roles=[RESTAURANT_OWNER],
        )
        self.table_b = Table.objects.create(number=2, restaurant=self.restaurant_b)

        # Authenticated, but employed nowhere.
        self.outsider = make_user('256700000130')

    # --- fixtures -------------------------------------------------------
    def make_order(self, restaurant, table, **kwargs):
        # A fresh order per review — Review.order is one-per-order.
        defaults = dict(
            total_cost=1000, discounted_cost=1000, savings=0, actual_cost=1000,
        )
        defaults.update(kwargs)
        return Order.objects.create(
            restaurant=restaurant, table=table, **defaults,
        )

    def make_review(self, order, **kwargs):
        defaults = dict(overall_rating=5)
        defaults.update(kwargs)
        return Review.objects.create(order=order, **defaults)

    # --- request helpers ------------------------------------------------
    def auth(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def post_submit(self, body):
        # Submission is AllowAny — deliberately no auth header.
        return self.client.post(
            SUBMIT_URL, data=json.dumps(body),
            content_type='application/json',
        )

    def get_reviews(self, user, query=''):
        return self.client.get(f'{REVIEWS_URL}{query}', **self.auth(user))


class ReviewSubmissionTests(ReviewApiTestBase):
    def test_happy_path_sets_restaurant_and_public(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({
            'order': str(order.id), 'overall_rating': 5,
            'food_rating': 4, 'comment': 'Great food!',
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['status'], 201)
        review = Review.objects.get(order=order)
        # restaurant + is_public are auto-set by Review.save().
        self.assertEqual(str(review.restaurant_id), str(self.restaurant_a.id))
        self.assertTrue(review.is_public)
        # submission_channel keeps its 'in_app' default.
        self.assertEqual(review.submission_channel, 'in_app')
        self.assertEqual(resp.json()['data']['overall_rating'], 5)

    def test_low_rating_is_not_public(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({'order': str(order.id), 'overall_rating': 3})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(Review.objects.get(order=order).is_public)

    def test_boundary_rating_four_is_public(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({
            'order': str(order.id), 'overall_rating': PUBLIC_RATING_THRESHOLD,
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(Review.objects.get(order=order).is_public)

    def test_unknown_order_returns_404(self):
        resp = self.post_submit({
            'order': '00000000-0000-0000-0000-000000000000',
            'overall_rating': 5,
        })
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()['status'], 404)

    def test_cancelled_order_returns_400(self):
        order = self.make_order(
            self.restaurant_a, self.table_a,
            order_status=OrderStatus_Cancelled,
        )
        resp = self.post_submit({'order': str(order.id), 'overall_rating': 5})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Review.objects.filter(order=order).exists())

    def test_second_submission_returns_409(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        first = self.post_submit({'order': str(order.id), 'overall_rating': 5})
        self.assertEqual(first.status_code, 201, first.content)
        second = self.post_submit({'order': str(order.id), 'overall_rating': 3})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Review.objects.filter(order=order).count(), 1)

    def test_out_of_range_rating_returns_400(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({'order': str(order.id), 'overall_rating': 7})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('overall_rating', resp.json()['message'])
        self.assertFalse(Review.objects.filter(order=order).exists())

    def test_missing_overall_rating_returns_400(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({'order': str(order.id), 'food_rating': 4})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Review.objects.filter(order=order).exists())

    def test_response_includes_order_context(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({'order': str(order.id), 'overall_rating': 5})
        data = resp.json()['data']
        self.assertEqual(data['order_id'], str(order.id))
        self.assertEqual(data['table_label'], 'Table 1')
        order.refresh_from_db()
        self.assertEqual(data['spend'], str(order.actual_cost))
        self.assertFalse(data['is_critical'])

    # --- quick-chip tags ------------------------------------------------
    def test_tags_round_trip(self):
        # Selected keys are persisted and echoed back in the read representation.
        order = self.make_order(self.restaurant_a, self.table_a)
        tags = [ReviewTag.GREAT_FLAVOUR, ReviewTag.SPOTLESS]
        resp = self.post_submit({
            'order': str(order.id), 'overall_rating': 5, 'tags': tags,
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['data']['tags'], tags)
        self.assertEqual(Review.objects.get(order=order).tags, tags)

    def test_tags_unknown_key_dropped_and_logged(self):
        # Unknown keys never block the submission: the review saves, only the
        # valid subset is stored, and the drop is logged via standard
        # application logging (NOT the MongoDB action-log pipeline).
        order = self.make_order(self.restaurant_a, self.table_a)
        with self.assertLogs('reviews_app.serializers', level='WARNING') as logs:
            resp = self.post_submit({
                'order': str(order.id), 'overall_rating': 5,
                'tags': ['great_flavour', 'not_a_real_tag', 'spotless'],
            })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(
            resp.json()['data']['tags'], ['great_flavour', 'spotless'],
        )
        self.assertEqual(
            Review.objects.get(order=order).tags,
            ['great_flavour', 'spotless'],
        )
        # The dropped key is visible in the warning log.
        self.assertTrue(
            any('not_a_real_tag' in line for line in logs.output),
            logs.output,
        )

    def test_tags_all_unknown_saves_review_with_empty_tags(self):
        # Even an all-unknown list must not block the stars/comment from saving.
        order = self.make_order(self.restaurant_a, self.table_a)
        with self.assertLogs('reviews_app.serializers', level='WARNING'):
            resp = self.post_submit({
                'order': str(order.id), 'overall_rating': 5,
                'tags': ['nope', 'still_nope'],
            })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['data']['tags'], [])
        self.assertEqual(Review.objects.get(order=order).tags, [])

    def test_tags_non_list_returns_400(self):
        # A non-list payload is the one malformed-tags case that 400s (a string
        # would otherwise be iterated per-character) — no review is created.
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({
            'order': str(order.id), 'overall_rating': 5,
            'tags': 'great_flavour',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Review.objects.filter(order=order).exists())

    def test_tags_empty_list_allowed(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({
            'order': str(order.id), 'overall_rating': 5, 'tags': [],
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['data']['tags'], [])
        self.assertEqual(Review.objects.get(order=order).tags, [])

    def test_tags_omitted_defaults_to_empty(self):
        # No tags key at all -> the model's [] default, never null.
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({'order': str(order.id), 'overall_rating': 5})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['data']['tags'], [])
        self.assertEqual(Review.objects.get(order=order).tags, [])

    def test_tags_deduplicated_preserving_order(self):
        order = self.make_order(self.restaurant_a, self.table_a)
        resp = self.post_submit({
            'order': str(order.id), 'overall_rating': 5,
            'tags': ['quick_service', 'good_value', 'quick_service'],
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(
            Review.objects.get(order=order).tags,
            ['quick_service', 'good_value'],
        )


class RestaurantReviewListTests(ReviewApiTestBase):
    def _ids(self, resp):
        return {record['id'] for record in resp.json()['data']['records']}

    def test_owner_sees_only_own_restaurant(self):
        review_a = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        review_b = self.make_review(self.make_order(self.restaurant_b, self.table_b))
        resp = self.get_reviews(self.owner_a)
        self.assertEqual(resp.status_code, 200)
        ids = self._ids(resp)
        self.assertIn(review_a.id, ids)
        self.assertNotIn(review_b.id, ids)

    def test_restaurant_param_cannot_widen(self):
        review_a = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        review_b = self.make_review(self.make_order(self.restaurant_b, self.table_b))
        # owner A asks for B's reviews — must still see only A's.
        resp = self.get_reviews(
            self.owner_a, f'?restaurant={self.restaurant_b.id}',
        )
        self.assertEqual(resp.status_code, 200)
        ids = self._ids(resp)
        self.assertIn(review_a.id, ids)
        self.assertNotIn(review_b.id, ids)

    def test_outsider_list_is_empty(self):
        self.make_review(self.make_order(self.restaurant_a, self.table_a))
        resp = self.get_reviews(self.outsider)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['records'], [])

    def test_unauthenticated_list_rejected(self):
        resp = self.client.get(REVIEWS_URL)
        self.assertEqual(resp.status_code, 401)

    def test_filter_rating_exact(self):
        five = self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=5)
        two = self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=2)
        resp = self.get_reviews(self.owner_a, '?rating=2')
        ids = self._ids(resp)
        self.assertIn(two.id, ids)
        self.assertNotIn(five.id, ids)

    def test_filter_rating_non_integer_is_ignored(self):
        self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=5)
        self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=2)
        # A junk rating must be skipped (no 500) and return all reviews.
        resp = self.get_reviews(self.owner_a, '?rating=abc')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['data']['records']), 2)

    def test_filter_critical(self):
        high = self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=5)
        low = self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=2)
        resp = self.get_reviews(self.owner_a, '?critical=true')
        ids = self._ids(resp)
        self.assertIn(low.id, ids)
        self.assertNotIn(high.id, ids)

    def test_filter_resolution_status(self):
        open_review = self.make_review(
            self.make_order(self.restaurant_a, self.table_a))
        resolved = self.make_review(
            self.make_order(self.restaurant_a, self.table_a))
        # .update() bypasses save() so the row is deterministically resolved.
        Review.objects.filter(id=resolved.id).update(resolution_status='resolved')
        resp = self.get_reviews(self.owner_a, '?resolution_status=resolved')
        ids = self._ids(resp)
        self.assertIn(resolved.id, ids)
        self.assertNotIn(open_review.id, ids)

    def test_critical_and_rating_coexist(self):
        two = self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=2)
        self.make_review(
            self.make_order(self.restaurant_a, self.table_a), overall_rating=5)
        # critical (<4) AND exact rating 2 -> the rating-2 review only.
        resp = self.get_reviews(self.owner_a, '?critical=true&rating=2')
        self.assertEqual(self._ids(resp), {two.id})
        # rating 5 AND critical (<4) -> empty.
        resp2 = self.get_reviews(self.owner_a, '?critical=true&rating=5')
        self.assertEqual(resp2.json()['data']['records'], [])


SUMMARY_URL = '/api/v1/reviews/summary/'
ANALYTICS_URL = '/api/v1/reviews/analytics/'


class ReviewAnalyticsTestBase(ReviewApiTestBase):
    """
    Extends the shared base with a manager fixture (a second read role at
    restaurant A), request helpers for the two analytics endpoints, and a
    review factory that backdates created_at deterministically.
    """

    def setUp(self):
        super().setUp()
        # A manager (also a READ role) at restaurant A, distinct from the owner.
        self.manager_a = make_user('256700000140')
        RestaurantEmployee.objects.create(
            user=self.manager_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_MANAGER],
        )

    def get_summary(self, user, query=''):
        return self.client.get(f'{SUMMARY_URL}{query}', **self.auth(user))

    def get_analytics(self, user, query=''):
        return self.client.get(f'{ANALYTICS_URL}{query}', **self.auth(user))

    def _review(self, days_ago=0, **kwargs):
        """
        Create a review for restaurant A, then pin created_at to noon `days_ago`
        days back. auto_now_add ignores create-kwargs, so we set it with a
        queryset .update() (bypasses save()). Noon keeps __date / Trunc bucketing
        clear of midnight regardless of the active timezone.
        """
        review = self.make_review(
            self.make_order(self.restaurant_a, self.table_a), **kwargs)
        when = (timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
                - timedelta(days=days_ago))
        Review.objects.filter(id=review.id).update(created_at=when)
        return review


class _ScopingTestsMixin:
    """
    Shared scoping assertions for both analytics endpoints. Not a TestCase on its
    own (so it is never collected standalone); concrete classes mix it in and set
    ``endpoint_url``.
    """

    endpoint_url = None

    def _get(self, user, query=''):
        return self.client.get(f'{self.endpoint_url}{query}', **self.auth(user))

    def test_owner_can_read(self):
        resp = self._get(self.owner_a, f'?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_manager_can_read(self):
        resp = self._get(self.manager_a, f'?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_user_with_no_role_there_is_forbidden(self):
        resp = self._get(self.outsider, f'?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 403)

    def test_foreign_restaurant_is_forbidden(self):
        # owner_a holds no role at restaurant B.
        resp = self._get(self.owner_a, f'?restaurant={self.restaurant_b.id}')
        self.assertEqual(resp.status_code, 403)

    def test_missing_restaurant_returns_400(self):
        self.assertEqual(self._get(self.owner_a).status_code, 400)

    def test_malformed_restaurant_uuid_returns_400(self):
        # The UUID guard fires before any ORM call (a malformed id would 500 on
        # Postgres for a dinify admin otherwise).
        resp = self._get(self.owner_a, '?restaurant=not-a-uuid')
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_rejected(self):
        resp = self.client.get(
            f'{self.endpoint_url}?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 401)


class ReviewSummaryEndpointTests(_ScopingTestsMixin, ReviewAnalyticsTestBase):
    endpoint_url = SUMMARY_URL

    def _data(self):
        resp = self.get_summary(
            self.owner_a, f'?restaurant={self.restaurant_a.id}')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()['data']

    def test_aggregates_for_seeded_reviews(self):
        for rating in (5, 5, 4, 2):
            self._review(overall_rating=rating)
        resolved_critical = self._review(overall_rating=1)
        Review.objects.filter(id=resolved_critical.id).update(
            resolution_status='resolved')

        data = self._data()
        self.assertEqual(data['average_rating'], '3.4')   # 17 / 5
        self.assertEqual(data['total_reviews'], 5)
        self.assertEqual(data['distribution'], [
            {'stars': 5, 'count': 2}, {'stars': 4, 'count': 1},
            {'stars': 3, 'count': 0}, {'stars': 2, 'count': 1},
            {'stars': 1, 'count': 1},
        ])
        self.assertEqual(data['critical_count'], 2)            # ratings 2 and 1
        self.assertEqual(data['unresolved_critical_count'], 1)  # the 1 is resolved

    def test_recent_reviews_newest_first_with_order_context(self):
        self._review(days_ago=1, overall_rating=5)
        self._review(days_ago=2, overall_rating=4)
        self._review(days_ago=3, overall_rating=3)
        self._review(days_ago=4, overall_rating=2)   # 4th-newest, excluded

        recent = self._data()['recent_reviews']
        self.assertEqual(len(recent), 3)
        self.assertEqual([r['overall_rating'] for r in recent], [5, 4, 3])
        # order context is joined and serialized.
        self.assertEqual(recent[0]['table_label'], 'Table 1')
        self.assertIsNotNone(recent[0]['order_id'])
        self.assertIn('is_critical', recent[0])

    def test_recent_is_all_time_while_counts_are_windowed(self):
        self._review(overall_rating=5)
        self._review(overall_rating=5)
        self._review(days_ago=40, overall_rating=1)   # outside the 30-day window

        data = self._data()
        self.assertEqual(data['total_reviews'], 2)        # window excludes it
        self.assertEqual(data['average_rating'], '5.0')
        self.assertEqual(data['critical_count'], 0)       # the only critical is out
        self.assertEqual(len(data['recent_reviews']), 3)  # all-time includes it
        self.assertIn(1, [r['overall_rating'] for r in data['recent_reviews']])

    def test_zero_reviews_graceful(self):
        data = self._data()
        self.assertEqual(data['average_rating'], '0.0')
        self.assertEqual(data['total_reviews'], 0)
        self.assertEqual(
            data['distribution'],
            [{'stars': s, 'count': 0} for s in range(5, 0, -1)])
        self.assertEqual(data['critical_count'], 0)
        self.assertEqual(data['unresolved_critical_count'], 0)
        self.assertEqual(data['recent_reviews'], [])


class ReviewAnalyticsEndpointTests(_ScopingTestsMixin, ReviewAnalyticsTestBase):
    endpoint_url = ANALYTICS_URL

    def _data(self, query=''):
        resp = self.get_analytics(
            self.owner_a, f'?restaurant={self.restaurant_a.id}{query}')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()['data']

    def test_dimensions_and_weakest(self):
        self._review(overall_rating=5, food_rating=2, speed_rating=5,
                     service_rating=4)
        self._review(overall_rating=4, food_rating=2, speed_rating=4)
        self._review(overall_rating=3, food_rating=2, speed_rating=3)

        data = self._data()
        dims = data['dimensions']
        self.assertEqual(dims['food'], {'average': '2.0', 'count': 3})
        self.assertEqual(dims['speed'], {'average': '4.0', 'count': 3})
        # service was rated on only one review (null on the others).
        self.assertEqual(dims['service'], {'average': '4.0', 'count': 1})
        # value / cleanliness rated by nobody -> null average, not 0.0.
        self.assertEqual(dims['value'], {'average': None, 'count': 0})
        self.assertEqual(dims['cleanliness'], {'average': None, 'count': 0})
        # food (2.0) is the lowest-average dimension with count >= 3.
        self.assertEqual(
            data['weakest_dimension'], {'key': 'food', 'average': '2.0'})

    def test_weakest_dimension_null_below_min_count(self):
        # food rated on only two reviews -> below MIN_DIMENSION_COUNT (3).
        self._review(overall_rating=5, food_rating=3)
        self._review(overall_rating=4, food_rating=2)

        data = self._data()
        self.assertIsNone(data['weakest_dimension'])
        # the dimension still reports its average + count.
        self.assertEqual(data['dimensions']['food'], {'average': '2.5', 'count': 2})

    def test_critical_and_unresolved_counts(self):
        self._review(overall_rating=2)
        resolved_critical = self._review(overall_rating=1)
        self._review(overall_rating=5)
        Review.objects.filter(id=resolved_critical.id).update(
            resolution_status='resolved')

        data = self._data()
        self.assertEqual(data['critical_count'], 2)
        self.assertEqual(data['unresolved_critical_count'], 1)

    def test_window_excludes_out_of_range_reviews(self):
        self._review(days_ago=5, overall_rating=5)    # inside
        self._review(days_ago=50, overall_rating=5)   # outside

        today = timezone.now().date()
        frm = (today - timedelta(days=10)).isoformat()
        to = (today + timedelta(days=1)).isoformat()
        data = self._data(f'&from={frm}&to={to}')
        self.assertEqual(data['total_reviews'], 1)

    def test_trend_weekly_buckets(self):
        # 14 days apart guarantees three distinct ISO weeks.
        self._review(days_ago=0, overall_rating=5)
        self._review(days_ago=14, overall_rating=3)
        self._review(days_ago=28, overall_rating=2)

        today = timezone.now().date()
        frm = (today - timedelta(days=35)).isoformat()
        to = (today + timedelta(days=1)).isoformat()
        trend = self._data(f'&from={frm}&to={to}&category=weekly')['trend']

        self.assertEqual(len(trend), 3)
        self.assertEqual([b['count'] for b in trend], [1, 1, 1])
        # ascending by period -> oldest (2) first, newest (5) last.
        self.assertEqual([b['average'] for b in trend], ['2.0', '3.0', '5.0'])
        periods = [b['period'] for b in trend]
        self.assertEqual(periods, sorted(periods))

    def test_trend_daily_buckets(self):
        self._review(days_ago=0, overall_rating=5)
        self._review(days_ago=2, overall_rating=4)
        self._review(days_ago=4, overall_rating=3)

        today = timezone.now().date()
        frm = (today - timedelta(days=7)).isoformat()
        to = (today + timedelta(days=1)).isoformat()
        trend = self._data(f'&from={frm}&to={to}&category=daily')['trend']

        self.assertEqual(len(trend), 3)
        self.assertEqual([b['count'] for b in trend], [1, 1, 1])
        self.assertEqual([b['average'] for b in trend], ['3.0', '4.0', '5.0'])

    def test_invalid_dates_return_400(self):
        resp = self.get_analytics(
            self.owner_a, f'?restaurant={self.restaurant_a.id}&from=not-a-date')
        self.assertEqual(resp.status_code, 400)

    def test_zero_reviews_graceful(self):
        data = self._data()
        self.assertEqual(data['total_reviews'], 0)
        self.assertEqual(data['average_rating'], '0.0')
        self.assertEqual(
            data['distribution'],
            [{'stars': s, 'count': 0} for s in range(5, 0, -1)])
        self.assertEqual(data['dimensions']['food'], {'average': None, 'count': 0})
        self.assertEqual(
            data['dimensions']['cleanliness'], {'average': None, 'count': 0})
        self.assertIsNone(data['weakest_dimension'])
        self.assertEqual(data['critical_count'], 0)
        self.assertEqual(data['unresolved_critical_count'], 0)
        self.assertEqual(data['trend'], [])

    def test_default_window_is_90_days_weekly(self):
        self._review(overall_rating=5)
        period = self._data()['period']
        self.assertEqual(period['category'], 'weekly')
        span = date.fromisoformat(period['to']) - date.fromisoformat(period['from'])
        self.assertEqual(span, timedelta(days=DEFAULT_ANALYTICS_WINDOW_DAYS))

    def test_unknown_category_collapses_to_weekly(self):
        self._review(overall_rating=5)
        period = self._data('&category=monthly')['period']
        self.assertEqual(period['category'], 'weekly')


RESOLUTION_URL = '/api/v1/reviews/{review_id}/resolution/'


class ReviewResolutionEndpointTests(ReviewApiTestBase):
    """
    PATCH /api/v1/reviews/<int:review_id>/resolution/ — owner/manager mark-handled.

    Owners and managers may toggle resolution_status open<->resolved on their own
    restaurant; outsiders / cross-tenant owners are denied, and bad ids / targets
    are rejected.
    """

    def setUp(self):
        super().setUp()
        # A manager at restaurant A (distinct from the owner) — also a manage role.
        self.manager_a = make_user('256700000150')
        RestaurantEmployee.objects.create(
            user=self.manager_a, restaurant=self.restaurant_a,
            roles=[RESTAURANT_MANAGER],
        )

    def patch_resolution(self, user, review_id, target, note=None):
        # ``note`` is only added to the payload when explicitly passed, so the
        # existing tests (which omit it) keep exercising the note-absent path.
        payload = {'resolution_status': target}
        if note is not None:
            payload['resolution_note'] = note
        return self.client.patch(
            RESOLUTION_URL.format(review_id=review_id),
            data=json.dumps(payload),
            content_type='application/json',
            **self.auth(user),
        )

    def test_owner_can_resolve_and_reopen(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        self.assertEqual(review.resolution_status, 'open')
        # open -> resolved
        resp = self.patch_resolution(self.owner_a, review.id, 'resolved')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['resolution_status'], 'resolved')
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'resolved')
        # resolved -> open
        resp = self.patch_resolution(self.owner_a, review.id, 'open')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['resolution_status'], 'open')
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'open')

    def test_manager_can_resolve_and_reopen(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        resp = self.patch_resolution(self.manager_a, review.id, 'resolved')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['resolution_status'], 'resolved')
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'resolved')
        resp = self.patch_resolution(self.manager_a, review.id, 'open')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['resolution_status'], 'open')

    def test_outsider_is_forbidden(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        resp = self.patch_resolution(self.outsider, review.id, 'resolved')
        self.assertEqual(resp.status_code, 403)
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'open')

    def test_cross_tenant_owner_is_forbidden(self):
        # owner_b owns restaurant B and has no role at restaurant A.
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        resp = self.patch_resolution(self.owner_b, review.id, 'resolved')
        self.assertEqual(resp.status_code, 403)
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'open')

    def test_unknown_review_id_returns_404(self):
        resp = self.patch_resolution(self.owner_a, 999999, 'resolved')
        self.assertEqual(resp.status_code, 404)

    def test_invalid_resolution_status_returns_400(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        resp = self.patch_resolution(self.owner_a, review.id, 'archived')
        self.assertEqual(resp.status_code, 400)
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'open')

    # --- resolution_note -------------------------------------------------
    def test_resolve_with_note_saves_it_and_response_carries_it(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        resp = self.patch_resolution(
            self.owner_a, review.id, 'resolved',
            note='Comped the meal and called the guest.',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            resp.json()['data']['resolution_note'],
            'Comped the meal and called the guest.',
        )
        review.refresh_from_db()
        self.assertEqual(
            review.resolution_note, 'Comped the meal and called the guest.',
        )

    def test_note_is_stripped_and_blank_clears_to_none(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        # Surrounding whitespace is trimmed on save.
        resp = self.patch_resolution(
            self.owner_a, review.id, 'resolved', note='  Refunded the order.  ',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        review.refresh_from_db()
        self.assertEqual(review.resolution_note, 'Refunded the order.')
        # A whitespace-only note clears it back to None (strip() or None).
        resp = self.patch_resolution(self.owner_a, review.id, 'resolved', note='   ')
        self.assertEqual(resp.status_code, 200, resp.content)
        review.refresh_from_db()
        self.assertIsNone(review.resolution_note)

    def test_resolving_without_a_note_leaves_existing_note_untouched(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        # Seed a note via a resolve-with-note.
        self.patch_resolution(
            self.owner_a, review.id, 'resolved', note='Spoke to the chef.',
        )
        # Reopen WITHOUT a note — the note survives (independent of status).
        resp = self.patch_resolution(self.owner_a, review.id, 'open')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['data']['resolution_note'], 'Spoke to the chef.')
        review.refresh_from_db()
        self.assertEqual(review.resolution_status, 'open')
        self.assertEqual(review.resolution_note, 'Spoke to the chef.')
        # Re-resolve WITHOUT a note — still untouched (not wiped).
        self.patch_resolution(self.owner_a, review.id, 'resolved')
        review.refresh_from_db()
        self.assertEqual(review.resolution_note, 'Spoke to the chef.')

    def test_re_resolving_with_a_new_note_updates_it(self):
        review = self.make_review(self.make_order(self.restaurant_a, self.table_a))
        self.patch_resolution(self.owner_a, review.id, 'resolved', note='First note.')
        self.patch_resolution(self.owner_a, review.id, 'resolved', note='Updated note.')
        review.refresh_from_db()
        self.assertEqual(review.resolution_note, 'Updated note.')

    def test_resolution_note_round_trips_through_read_serializer(self):
        review = self.make_review(
            self.make_order(self.restaurant_a, self.table_a),
            resolution_status='resolved', resolution_note='Issued a voucher.',
        )
        data = ReviewRestaurantReadSerializer(review).data
        self.assertIn('resolution_note', data)
        self.assertEqual(data['resolution_note'], 'Issued a voucher.')
