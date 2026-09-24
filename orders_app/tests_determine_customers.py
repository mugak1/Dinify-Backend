"""
``determine-customers`` — which contact details an order is matched on.

THE DEFECT THIS PINS. The command started every order with ``customer_phone`` and
``customer_email`` set to ``None`` and filled them from ONE place only: the
``msisdn`` of the order's payment (``DinifyTransaction``), and only when the order
carried neither contact detail of its own. An order that did carry its own phone or
email therefore reached ``match_customer`` with nothing, was never matched, and was
still marked ``customer_match_attempted`` — so it was never retried either.

The order's own phone and email are now what it is matched on. The payment's msisdn is
still the fallback for an order carrying neither, exactly as before, and everything
downstream is unchanged: the phone goes through ``normalise_msisdn`` (an unnormalisable
one is skipped, never fatal), and an unmatched contact still goes down the existing
create-a-user path.

Every fixture is an ordinary order at a live, real restaurant, so none of it depends on
which orders the command leaves out. That rule (``counted_orders_q``: a practice order
is skipped, a test restaurant's orders are not) is pinned by ``tests_launch_boundary``
and ``tests_test_restaurant_parity``.
"""
import contextlib
import io
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    RestaurantStatus_Live,
    TransactionStatus_Success,
    TransactionType_OrderPayment,
)
from finance_app.models import DinifyTransaction
from orders_app.models import Order
from restaurants_app.models import Restaurant, Table
from users_app.models import User


PRICE = Decimal('10000')
SKIPPED = 'Skipping unnormalisable customer phone'


class DetermineCustomersFixture(TestCase):
    """One live, real restaurant with a table; orders and people are added per test."""

    def setUp(self):
        super().setUp()
        owner = self._person('256700000100', email='owner@example.com')
        self.restaurant = Restaurant.objects.create(
            name='Match Grill', location='Kampala', owner=owner,
            status=RestaurantStatus_Live,
        )
        self.table = Table.objects.create(number=1, restaurant=self.restaurant)

    def _person(self, phone, *, email):
        """An existing account, created the way registration creates one."""
        return User.objects.create_user(
            first_name='Known', last_name='Diner', email=email,
            phone_number=phone, username=phone, country='UG',
            password='password',
        )

    def _order(self, *, customer_phone=None, customer_email=None):
        """An unmatched order carrying (or not) its own contact details."""
        return Order.objects.create(
            restaurant=self.restaurant, table=self.table,
            total_cost=PRICE, discounted_cost=PRICE,
            savings=Decimal('0'), actual_cost=PRICE,
            customer_phone=customer_phone, customer_email=customer_email,
        )

    def _pay(self, order, msisdn):
        return DinifyTransaction.objects.create(
            restaurant=self.restaurant, order=order,
            transaction_type=TransactionType_OrderPayment,
            transaction_status=TransactionStatus_Success,
            transaction_amount=PRICE, payment_mode='momo', msisdn=msisdn,
        )

    def _run(self):
        """
        Run the command and return what it printed.

        It reports through ``print()`` rather than ``self.stdout``, so ``call_command``'s
        own ``stdout=`` would not capture it.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            call_command('determine-customers')
        return out.getvalue()


class OrderContactMatchingTests(DetermineCustomersFixture):

    def test_an_order_with_its_own_phone_matches_an_existing_user(self):
        # Stored in the local format, so this also shows the order's phone going
        # through the same normalisation as a payment's msisdn.
        known = self._person('256772123456', email='known@example.com')
        order = self._order(customer_phone='0772 123 456')
        before = User.objects.count()

        self._run()

        order.refresh_from_db()
        self.assertEqual(order.customer_id, known.id)
        self.assertTrue(order.customer_match_attempted)
        self.assertEqual(User.objects.count(), before)

    def test_an_order_with_its_own_email_matches_an_existing_user(self):
        known = self._person('256772123456', email='known@example.com')
        order = self._order(customer_email='known@example.com')
        before = User.objects.count()

        self._run()

        order.refresh_from_db()
        self.assertEqual(order.customer_id, known.id)
        self.assertTrue(order.customer_match_attempted)
        self.assertEqual(User.objects.count(), before)

    def test_an_order_with_neither_falls_back_to_the_payment_msisdn(self):
        """CONTROL: the one path that already worked, and must keep working."""
        payer = self._person('256772999888', email='payer@example.com')
        order = self._order()
        self._pay(order, '256772999888')

        self._run()

        order.refresh_from_db()
        self.assertEqual(order.customer_id, payer.id)
        self.assertTrue(order.customer_match_attempted)

    def test_the_payment_is_consulted_only_when_the_order_has_neither(self):
        """The order's own contact comes first; the payment is the fallback, not a rival."""
        own = self._person('256772123456', email='own@example.com')
        self._person('256772999888', email='payer@example.com')
        order = self._order(customer_phone='0772123456')
        self._pay(order, '256772999888')

        self._run()

        order.refresh_from_db()
        self.assertEqual(order.customer_id, own.id)

    def test_an_unnormalisable_phone_is_skipped_without_an_exception(self):
        known = self._person('256772123456', email='known@example.com')
        unusable = self._order(customer_phone='12345')
        matchable = self._order(customer_phone='0772123456')
        before = User.objects.count()

        output = self._run()  # a raise here would roll back the whole batch

        # Skipped BECAUSE it could not be normalised, not because it was never read.
        self.assertIn(SKIPPED, output)
        # The normaliser's messages never carry the number, so the log line does not.
        self.assertNotIn('12345', output)
        unusable.refresh_from_db()
        self.assertIsNone(unusable.customer_id)
        self.assertTrue(unusable.customer_match_attempted)
        # The rest of the batch still landed: nothing was rolled back.
        matchable.refresh_from_db()
        self.assertEqual(matchable.customer_id, known.id)
        self.assertEqual(User.objects.count(), before)

    def test_an_unnormalisable_phone_does_not_stop_the_order_matching_on_its_email(self):
        known = self._person('256772444555', email='known@example.com')
        order = self._order(customer_phone='not a phone', customer_email='known@example.com')

        output = self._run()

        self.assertIn(SKIPPED, output)
        order.refresh_from_db()
        self.assertEqual(order.customer_id, known.id)

    def test_a_blank_email_is_not_a_contact(self):
        """
        A blank value is the ABSENCE of an email, never one to look up.

        CONTROL for the fix rather than a reproduction: on the old code the order's
        email was never read at all, so this held trivially. What it guards against is
        a fix that passes the value through verbatim, and the danger is concrete.
        ``create_user`` stores a missing email as ``''``, so every account registered
        without one holds exactly that value. A blank order email would then reach
        ``User.objects.get(email='')`` and link the order to whichever account happened
        to be the only one holding ``''`` — here, ``registered`` — or, with no usable
        phone beside it, mint an account keyed on ``''``.
        """
        registered = self._person('256772000555', email=None)
        # The premise: this is how an account with no email is actually stored.
        self.assertEqual(registered.email, '')
        orders = [self._order(customer_email=''), self._order(customer_email='   ')]
        before = User.objects.count()

        self._run()

        for order in orders:
            order.refresh_from_db()
            self.assertIsNone(order.customer_id, repr(order.customer_email))
            self.assertTrue(order.customer_match_attempted)
        self.assertEqual(User.objects.count(), before)


class UnmatchedContactCreationTests(DetermineCustomersFixture):
    """The existing create-a-user path, now reachable from the order's own contact."""

    def test_an_unmatched_phone_creates_a_user_keyed_on_the_canonical_number(self):
        order = self._order(customer_phone='+256 772 555 444')
        before = User.objects.count()

        self._run()

        order.refresh_from_db()
        self.assertEqual(User.objects.count(), before + 1)
        created = order.customer
        self.assertEqual(created.phone_number, '256772555444')
        self.assertEqual(created.username, '256772555444')
        self.assertIsNone(created.email)
        self.assertEqual(created.country, self.restaurant.country)

    def test_an_unmatched_email_creates_a_user_with_no_phone(self):
        """
        RECORDED, NOT ENDORSED: what the create path does with an email and no phone.

        This branch of ``match_customer`` could not be reached until the order's own
        email was read, and the account it creates has no ``phone_number``, which
        registration requires of every restaurant user (``REQUIRED_INFORMATION
        ['new_user']``). It is pinned so the behaviour is visible here, and so it
        cannot silently start crashing: an ``IntegrityError`` inside the command's one
        ``transaction.atomic()`` would roll back every order in the batch.
        """
        order = self._order(customer_email='new.diner@example.com')
        before = User.objects.count()

        self._run()

        order.refresh_from_db()
        self.assertEqual(User.objects.count(), before + 1)
        created = order.customer
        self.assertEqual(created.email, 'new.diner@example.com')
        self.assertEqual(created.username, 'new.diner@example.com')
        self.assertIsNone(created.phone_number)
