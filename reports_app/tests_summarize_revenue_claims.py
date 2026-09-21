"""D07/G1 — ``summarize_revenue`` states no trend it did not compute.

``month_growth`` was the literal string ``'up'``, with the comparison that
would have justified it commented out beside it::

    'month_growth': 'up'  # if this_month_revenue > last_month_revenue else 'down'

So the helper reported growth for every restaurant, in every month, including
one that had never taken an order — a direction asserted from no comparison at
all. It is the same class of claim as an empty card reading "no settled
payments in this period": a figure presented as a measurement when nothing
measured it.

WHAT THE CONSUMER SEARCH FOUND, because it decides the remedy. ``summarize_
revenue`` has NO production caller anywhere in this repository — it is reached
only from ``orders_app.tests_launch_boundary`` and
``reports_app.tests_timezone_clocks``, and the former's own comment calls it
"the dead-but-live all-time revenue helper". It is on no urlconf, in no
serializer and in no response. Neither test reads ``month_growth``. The
frontend's ``month_growth`` type declarations belong to ``DinifyDashboardData``,
the retired admin-plane shape, and name different fields entirely.

So there is no wire contract to keep compatible and the key is REMOVED rather
than given an honest value. A trend that is genuinely wanted later gets built
against a baseline that can be ABSENT — which is precisely what a bare
direction string cannot express, and is the lesson ``PaymentMethodData.
change_pct`` was deleted for on the other side.

AND THE TWO FIGURES THAT REMAIN ARE DISCLOSED. ``total`` and ``this_month``
both aggregate ``payment_status='paid'``, so both are permanently zero on live
data for the same reason the v1 and v2 dashboards are. The helper now publishes
the SAME module constant those two do, asserted by identity below, so a future
caller cannot read a zero as a measurement.
"""

from decimal import Decimal

from django.test import TestCase

from reports_app.controllers.restaurant.dashboard import (
    PAYMENT_TRACKING_ENABLED,
    summarize_revenue,
)
from restaurants_app.models import Restaurant
from users_app.models import User


class SummarizeRevenueClaimsTests(TestCase):

    def setUp(self):
        self.owner = User.objects.create_user(
            first_name='Claims', last_name='Owner',
            email='256700000911@test.com', phone_number='256700000911',
            username='256700000911', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Claims Test', location='Kampala', status='live',
            owner=self.owner,
        )

    def result(self):
        return summarize_revenue(str(self.restaurant.id))

    # -- the regression ---------------------------------------------------
    def test_THE_REGRESSION_no_trend_direction_is_asserted(self):
        self.assertNotIn('month_growth', self.result())

    def test_no_key_carries_the_literal_up(self):
        # Belt and braces: a rename that kept the literal would pass the test
        # above while making exactly the same claim.
        self.assertNotIn('up', self.result().values())

    # -- the disclosure ---------------------------------------------------
    def test_the_paid_gated_figures_are_disclosed(self):
        self.assertIs(self.result()['payment_tracking_enabled'], False)

    def test_ONE_CONSTANT_not_a_second_literal(self):
        self.assertIs(
            self.result()['payment_tracking_enabled'],
            PAYMENT_TRACKING_ENABLED,
        )

    # -- controls: the figures themselves are untouched -------------------
    def test_CONTROL_the_measured_keys_survive_with_their_shape(self):
        result = self.result()
        self.assertEqual(Decimal(str(result['total'])), Decimal('0'))
        self.assertEqual(Decimal(str(result['this_month'])), Decimal('0'))

    def test_CONTROL_nothing_is_rebased_onto_another_queryset(self):
        # The paid filter stays. Rebasing onto sale_filters would change what
        # the figure MEANS, which is a reporting-contract change and not a
        # disclosure — explicitly out of scope for this work.
        import inspect
        source = inspect.getsource(summarize_revenue)
        self.assertIn('payment_status=PaymentStatus_Paid', source)
        self.assertNotIn('SALE_STATUSES', source)
