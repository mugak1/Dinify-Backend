"""
Model-level tests for reviews_app.Review:

* restaurant is denormalised from the order on creation;
* is_public is seeded from the rating band (high -> public, low -> private),
  honouring the threshold boundary, and an explicit override survives a later
  update (it is never re-derived once the row exists);
* the is_critical convenience property.
"""
from django.test import TestCase

from users_app.models import User
from restaurants_app.models import Restaurant, Table
from orders_app.models import Order
from dinify_backend.configss.string_definitions import RestaurantStatus_Active
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
