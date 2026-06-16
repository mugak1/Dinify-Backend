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

from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from restaurants_app.models import Restaurant, RestaurantEmployee, Table
from orders_app.models import Order
from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Active, RESTAURANT_OWNER, OrderStatus_Cancelled,
)
from reviews_app.models import PUBLIC_RATING_THRESHOLD, Review


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
