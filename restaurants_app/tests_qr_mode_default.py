"""
D07 / PR-4 — A NEW TABLE IS `order_only`, AND AN EXISTING `order_pay` ROW STILL
WORKS.

`order_pay` names a capability this platform does not have. Nothing collects a
diner payment: the order-payment write path (`OrderPaymentTransaction` /
`initiate-order-payment/`) was DELETED in the non-custodial teardown, no PSP
integration replaced it, and the repository holds no payment-execution code of
any kind. A table provisioned today under that default therefore announced
"order and pay" to every diner who scanned it, about a pay step that cannot
happen.

THE CORRECTION IS THE DEFAULT, NOT THE VOCABULARY. `order_pay` is RETAINED as a
choice and stays inside `ORDERING_QR_MODES`, so:

  * every existing row keeps its stored value — migration 0058 is model-state
    only, with no `RunPython` and no row rewrite;
  * an `order_pay` table remains fully orderable, so no venue's diners lose the
    ability to order because Dinify corrected a default.

Operationally `order_pay` and `order_only` are IDENTICAL — both permit ordering,
`menu_only` is the one that blocks — so this changes what a new table CLAIMS and
nothing about what it DOES.

Without this file the default is pinned by nothing: the migration asserts only
model state, and the one other test that observes it
(`DinerTableScanTests.test_scan_read_query_count_and_output`) is a serializer
snapshot whose failure would read as an unrelated breakage.
"""
from django.test import TestCase

from orders_app.controllers.services.order_eligibility import (
    ORDERING_QR_MODES,
    OperationalFacts,
    evaluate,
)
from restaurants_app.models import Restaurant, Table
from users_app.models import User


def _diner_facts(qr_mode):
    """The QR public's situation at an otherwise perfectly ordinary table."""
    return OperationalFacts(
        accepting_orders=True,
        restaurant_deleted=False,
        table_present=True,
        table_qr_mode=qr_mode,
        table_scannable=True,
    )


class QrModeDefaultTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create(
            username='256700000900', phone_number='256700000900',
            first_name='Qr', last_name='Mode',
        )
        cls.restaurant = Restaurant.objects.create(
            name='QR Mode Diner', location='Kampala', owner=cls.owner,
            country='UG', status='live',
        )

    def test_REGRESSION_a_new_table_defaults_to_order_only(self):
        table = Table.objects.create(number=901, restaurant=self.restaurant)

        self.assertEqual(table.qr_mode, 'order_only')
        # And it is the STORED value, not a Python-side default the database
        # never received — an `AlterField` manages the default in Python, so a
        # read-back is the only thing that proves the column carries it.
        self.assertEqual(
            Table.objects.values_list('qr_mode', flat=True).get(pk=table.pk),
            'order_only',
        )

    def test_REGRESSION_the_default_is_not_the_retired_payment_claim(self):
        table = Table.objects.create(number=902, restaurant=self.restaurant)

        self.assertNotEqual(table.qr_mode, 'order_pay')

    def test_CONTROL_order_pay_is_still_a_legal_stored_value(self):
        # No data migration ran, so rows provisioned under the old default keep
        # their value. A choice that stopped validating would have made every
        # such row unsaveable.
        table = Table.objects.create(
            number=903, restaurant=self.restaurant, qr_mode='order_pay',
        )
        table.refresh_from_db()
        self.assertEqual(table.qr_mode, 'order_pay')

        # Validated at the FIELD rather than through `full_clean`, which drags
        # in unrelated legacy EOD columns and would fail for reasons that say
        # nothing about `qr_mode`. This is precisely "is `order_pay` still an
        # accepted choice".
        Table._meta.get_field('qr_mode').clean('order_pay', table)

    def test_CONTROL_an_existing_order_pay_table_still_lets_a_diner_order(self):
        # The load-bearing half of "retained": an operator who never touches
        # their tables must not find ordering switched off.
        self.assertIn('order_pay', ORDERING_QR_MODES)
        self.assertTrue(evaluate(_diner_facts('order_pay'), None).allowed)

    def test_CONTROL_a_new_default_table_lets_a_diner_order_too(self):
        self.assertIn('order_only', ORDERING_QR_MODES)
        self.assertTrue(evaluate(_diner_facts('order_only'), None).allowed)

    def test_CONTROL_menu_only_is_still_the_one_mode_that_blocks(self):
        # The distinction the default change must not blur: `order_pay` and
        # `order_only` differ in what they CLAIM, `menu_only` differs in what
        # it PERMITS.
        verdict = evaluate(_diner_facts('menu_only'), None)

        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.reason)
