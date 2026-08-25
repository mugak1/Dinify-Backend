"""
The canonical ``commercial`` read projection on the Admin restaurant reads
(Phase 1, Step 3D.1).

Steps 3B and 3C made a restaurant's commercial facts storable and writable. This
suite pins what the Admin plane is TOLD about them, and — at least as importantly —
what it is deliberately NOT told: that terms exist is not that they were paid, a
collection mode is not a tender, and a legacy column that disagrees with the
commercial domain does not get to win.

The whole projection is exercised through the real HTTP endpoints, because the
contract that matters is the response an operator's browser receives, not the shape
of an intermediate dict.
"""
import json
import re
from decimal import Decimal

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from commercial_app import service_configuration, subscription_terms
from commercial_app.models import (
    BILLING_INTERVAL_UNIT_VALUES,
    PAYMENT_COLLECTION_MODE_OFFLINE,
    PAYMENT_COLLECTION_MODE_PSP_ONLINE,
    PAYMENT_TIMING_PAY_AFTER,
    PAYMENT_TIMING_PAY_FIRST,
    RestaurantServiceConfiguration,
    RestaurantSubscriptionTerms,
)
from dinify_backend.configss.string_definitions import RestaurantStatus_Onboarding
from platform_admin_app import commercial_reads, restaurant_reads
from platform_admin_app.models import AdminAuditLog
from platform_admin_app.permissions import require_recent_elevation
from platform_admin_app.tests_restaurant_directory import (
    _ADMIN_OVERRIDES,
    LIST_URL,
    _AdminReadTestCase,
    _detail_url,
    _make_restaurant,
)
from support_app.models import SupportIssue

UGX = 'UGX'


# --- fixtures ----------------------------------------------------------------

def _configure(restaurant, actor, *, timing=None, mode=None, at=None):
    """
    Write a service-configuration row directly.

    Direct ORM writes, not the Step 3C services: this is a READ suite, and it must
    be able to construct states (one axis set, the other NULL; a bare row with both
    NULL) without also depending on the writer's validation rules. One test —
    ``test_the_read_reflects_what_the_step_3c_writer_wrote`` — closes that gap by
    going through the real writer end to end.
    """
    at = at or timezone.now()
    fields = {}
    if timing is not None:
        fields.update(
            payment_timing=timing,
            payment_timing_set_at=at,
            payment_timing_set_by=actor,
        )
    if mode is not None:
        fields.update(
            payment_collection_mode=mode,
            payment_collection_mode_set_at=at,
            payment_collection_mode_set_by=actor,
        )
    return RestaurantServiceConfiguration.objects.create(
        restaurant=restaurant, **fields,
    )


def _terms(restaurant, actor, *, amount='150000.00', currency=UGX, unit='month',
           count=1, effective_from=None, ended_at=None, recorded_at=None):
    now = timezone.now()
    return RestaurantSubscriptionTerms.objects.create(
        restaurant=restaurant,
        recurring_amount=Decimal(amount),
        currency=currency,
        billing_interval_unit=unit,
        billing_interval_count=count,
        effective_from=effective_from or (now - timezone.timedelta(days=30)),
        ended_at=ended_at,
        recorded_at=recorded_at or now,
        recorded_by=actor,
    )


def _history(restaurant, actor, rows=10):
    """``rows`` ended terms, none of them open. Chronologically non-overlapping."""
    now = timezone.now()
    created = []
    for index in range(rows):
        created.append(_terms(
            restaurant, actor,
            amount=f'{100 + index}.00',
            effective_from=now - timezone.timedelta(days=(rows * 2) - (index * 2)),
            ended_at=now - timezone.timedelta(days=(rows * 2) - (index * 2) - 1),
        ))
    return created


class _CommercialReadTestCase(_AdminReadTestCase):
    """Shared accessors for the two places ``commercial`` appears."""

    def row(self, restaurant):
        response = self.get(LIST_URL, search=restaurant.name)
        self.assertEqual(response.status_code, 200, response.content)
        results = response.json()['data']['results']
        matches = [r for r in results if r['id'] == str(restaurant.id)]
        self.assertEqual(len(matches), 1, f'expected exactly one row, got {results}')
        return matches[0]

    def detail(self, restaurant):
        response = self.get(_detail_url(restaurant))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def commercial(self, restaurant):
        """The directory and detail answers, asserted identical, returned once."""
        row = self.row(restaurant)['commercial']
        detail = self.detail(restaurant)['commercial']
        self.assertEqual(row, detail, 'directory and detail disagree')
        return row


# --- §24 unconfigured --------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialUnconfiguredTests(_CommercialReadTestCase):
    """
    Absence of a decision is reported as absence, never as a default.

    Distinguishing "nobody decided" from "somebody chose the first option" is the
    reason both axes are nullable with no default in the first place; a read that
    collapsed the two would throw that away at the last step.
    """

    def test_no_service_configuration_row_reads_unconfigured(self):
        restaurant = _make_restaurant('No Config')
        commercial = self.commercial(restaurant)
        for axis in ('payment_timing', 'payment_collection_mode'):
            self.assertEqual(
                commercial[axis],
                {'configured': False, 'value': None, 'set_at': None},
                axis,
            )

    def test_a_row_with_both_axes_null_reads_the_same(self):
        """
        A configuration row existing is not itself a decision.

        The row can be created by a write to one axis; the other stays NULL. A read
        that keyed ``configured`` off the row's existence would report a restaurant
        as configured on an axis nobody has touched.
        """
        restaurant = _make_restaurant('Bare Row')
        RestaurantServiceConfiguration.objects.create(restaurant=restaurant)
        commercial = self.commercial(restaurant)
        for axis in ('payment_timing', 'payment_collection_mode'):
            self.assertEqual(
                commercial[axis],
                {'configured': False, 'value': None, 'set_at': None},
                axis,
            )

    def test_no_terms_reads_unconfigured(self):
        restaurant = _make_restaurant('No Terms')
        self.assertEqual(
            self.commercial(restaurant)['subscription_terms'],
            {'configured': False, 'current': None},
        )

    def test_historical_terms_only_reads_unconfigured(self):
        """Ten ended rows are history, not a current arrangement."""
        restaurant = _make_restaurant('History Only')
        _history(restaurant, self.admin, rows=10)
        self.assertEqual(
            self.commercial(restaurant)['subscription_terms'],
            {'configured': False, 'current': None},
        )


# --- §9 purity ---------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialReadPurityTests(_CommercialReadTestCase):
    """
    A GET leaves the database as it found it.

    The tempting shortcut in a projection like this is ``get_or_create`` — it makes
    the code read more simply because every restaurant then has a row. It would also
    mean that merely LOOKING at a restaurant records a commercial decision for it,
    destroying the distinction the schema exists to preserve.
    """

    def test_reading_an_unconfigured_restaurant_creates_no_commercial_rows(self):
        restaurant = _make_restaurant('Pure Read')
        self.detail(restaurant)
        self.row(restaurant)
        self.assertEqual(RestaurantServiceConfiguration.objects.count(), 0)
        self.assertEqual(RestaurantSubscriptionTerms.objects.count(), 0)

    def test_reading_ended_history_opens_nothing(self):
        restaurant = _make_restaurant('Pure History')
        _history(restaurant, self.admin, rows=5)
        self.detail(restaurant)
        self.row(restaurant)
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.filter(
                ended_at__isnull=True,
            ).count(),
            0,
        )

    def test_reads_write_no_audit_row(self):
        restaurant = _make_restaurant('Pure Audit')
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        _terms(restaurant, self.admin)
        before = AdminAuditLog.objects.count()
        self.detail(restaurant)
        self.row(restaurant)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_repeated_reads_leave_state_byte_for_byte_unchanged(self):
        restaurant = _make_restaurant('Idempotent Read')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_AFTER)
        terms = _terms(restaurant, self.admin)

        def snapshot():
            config = RestaurantServiceConfiguration.objects.get(
                restaurant=restaurant,
            )
            row = RestaurantSubscriptionTerms.objects.get(pk=terms.pk)
            return (
                config.payment_timing, config.payment_timing_set_at,
                config.payment_collection_mode, config.updated_at,
                row.recurring_amount, row.ended_at, row.recorded_at,
                AdminAuditLog.objects.count(),
                RestaurantServiceConfiguration.objects.count(),
                RestaurantSubscriptionTerms.objects.count(),
            )

        first = snapshot()
        for _ in range(3):
            self.detail(restaurant)
            self.row(restaurant)
        self.assertEqual(first, snapshot())


# --- §25 payment timing ------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialPaymentTimingTests(_CommercialReadTestCase):

    def test_pay_first(self):
        restaurant = _make_restaurant('Timing First')
        at = timezone.now() - timezone.timedelta(days=2)
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST, at=at)
        self.assertEqual(
            self.commercial(restaurant)['payment_timing'],
            {'configured': True, 'value': 'pay_first', 'set_at': at.isoformat()},
        )

    def test_pay_after(self):
        restaurant = _make_restaurant('Timing After')
        at = timezone.now() - timezone.timedelta(days=9)
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_AFTER, at=at)
        self.assertEqual(
            self.commercial(restaurant)['payment_timing'],
            {'configured': True, 'value': 'pay_after', 'set_at': at.isoformat()},
        )

    def test_the_value_stays_machine_vocabulary(self):
        """
        No display prose. "Pay first" / "Prepaid" / "Pay at counter" are the
        portal's job — the API carries the vocabulary the writer, the
        ``CheckConstraint`` and any future ``expected_current`` assertion all use.
        """
        restaurant = _make_restaurant('Timing Machine')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
        value = self.commercial(restaurant)['payment_timing']['value']
        self.assertEqual(value, PAYMENT_TIMING_PAY_FIRST)
        self.assertNotIn(' ', value)
        self.assertEqual(value, value.lower())

    def test_require_order_prepayments_is_never_consulted(self):
        """
        The legacy checkout toggle is not payment timing, in either direction.

        ``require_order_prepayments=True`` looks like ``pay_first``, which is exactly
        why inferring from it would be a confident wrong answer for any restaurant
        that set it for its own reasons.
        """
        restaurant = _make_restaurant('Prepay Toggle')
        restaurant.require_order_prepayments = True
        restaurant.save(update_fields=['require_order_prepayments'])
        self.assertEqual(
            self.commercial(restaurant)['payment_timing'],
            {'configured': False, 'value': None, 'set_at': None},
        )

    def test_a_legacy_toggle_contradicting_the_domain_does_not_win(self):
        restaurant = _make_restaurant('Prepay Contradiction')
        restaurant.require_order_prepayments = True
        restaurant.save(update_fields=['require_order_prepayments'])
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_AFTER)
        self.assertEqual(
            self.commercial(restaurant)['payment_timing']['value'],
            PAYMENT_TIMING_PAY_AFTER,
        )

    def test_no_actor_is_exposed(self):
        restaurant = _make_restaurant('Timing Actor')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
        axis = self.commercial(restaurant)['payment_timing']
        self.assertEqual(set(axis), {'configured', 'value', 'set_at'})


# --- §26 collection mode -----------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialCollectionModeTests(_CommercialReadTestCase):

    def test_offline_is_a_configured_value(self):
        """
        ``offline`` is a permanent, first-class commercial mode.

        The failure this guards against is a read (or a portal) treating it as
        "hasn't been set up yet", which would make the first commercial restaurant —
        which must be able to launch offline — look perpetually unfinished.
        """
        restaurant = _make_restaurant('Mode Offline')
        at = timezone.now() - timezone.timedelta(hours=3)
        _configure(
            restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE, at=at,
        )
        self.assertEqual(
            self.commercial(restaurant)['payment_collection_mode'],
            {'configured': True, 'value': 'offline', 'set_at': at.isoformat()},
        )

    def test_psp_online(self):
        restaurant = _make_restaurant('Mode PSP')
        at = timezone.now() - timezone.timedelta(hours=8)
        _configure(
            restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE, at=at,
        )
        self.assertEqual(
            self.commercial(restaurant)['payment_collection_mode'],
            {'configured': True, 'value': 'psp_online', 'set_at': at.isoformat()},
        )

    def test_no_tender_is_inferred_from_either_mode(self):
        """
        Collection mode (who initiates) is not tender (what the diner used). A
        restaurant on ``offline`` may take cash from one diner and mobile money from
        the next, so translating the mode into a tender word would be wrong for the
        second diner every time.
        """
        forbidden = {'cash', 'momo', 'mobile_money', 'card'}
        for mode in (
            PAYMENT_COLLECTION_MODE_OFFLINE, PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        ):
            with self.subTest(mode=mode):
                restaurant = _make_restaurant(f'Tender {mode}')
                _configure(restaurant, self.admin, mode=mode)
                blob = json.dumps(self.commercial(restaurant)).lower()
                for word in forbidden:
                    self.assertNotIn(word, blob)

    def test_no_provider_is_inferred_for_psp_online(self):
        """
        §19: ``psp_online`` reports ``psp_online`` and nothing more. There is no PSP
        integration in this repository, so there is no provider-authoritative state
        to project and a locally-invented ``psp_ready`` would be an operator
        asserting something only the provider can know.
        """
        restaurant = _make_restaurant('Mode Provider')
        _configure(restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE)
        commercial = self.commercial(restaurant)
        self.assertEqual(
            set(commercial['payment_collection_mode']),
            {'configured', 'value', 'set_at'},
        )
        blob = json.dumps(commercial).lower()
        for word in (
            'provider', 'merchant', 'psp_status', 'psp_ready', 'flutterwave',
            'pesapal', 'dpo', 'webhook',
        ):
            self.assertNotIn(word, blob)

    def test_no_actor_is_exposed(self):
        restaurant = _make_restaurant('Mode Actor')
        _configure(restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        axis = self.commercial(restaurant)['payment_collection_mode']
        self.assertEqual(set(axis), {'configured', 'value', 'set_at'})


# --- §27 subscription terms --------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialSubscriptionTermsTests(_CommercialReadTestCase):

    def test_an_open_row_is_projected_in_full(self):
        restaurant = _make_restaurant('Terms Full')
        effective = timezone.now() - timezone.timedelta(days=45)
        recorded = timezone.now() - timezone.timedelta(days=44)
        terms = _terms(
            restaurant, self.admin, amount='150000.00', currency=UGX,
            unit='month', count=1, effective_from=effective, recorded_at=recorded,
        )
        self.assertEqual(
            self.commercial(restaurant)['subscription_terms'],
            {
                'configured': True,
                'current': {
                    'id': str(terms.id),
                    'recurring_amount': '150000.00',
                    'currency': UGX,
                    'billing_interval': {'unit': 'month', 'count': 1},
                    'effective_from': effective.isoformat(),
                    'recorded_at': recorded.isoformat(),
                },
            },
        )

    def test_a_zero_price_is_a_real_price_not_an_absence(self):
        """
        ``0.00`` is a recorded decision — a free pilot, a waived period, a test
        tenant rehearsing the real path. It is a DIFFERENT fact from "no terms
        exist", which is the absence of a row, and the read must not blur them into
        "free" or "trial".
        """
        restaurant = _make_restaurant('Terms Zero')
        _terms(restaurant, self.admin, amount='0.00')
        current = self.commercial(restaurant)['subscription_terms']
        self.assertTrue(current['configured'])
        self.assertEqual(current['current']['recurring_amount'], '0.00')

    def test_the_amount_is_a_string_that_keeps_its_scale(self):
        """
        Money never goes through float, and the scale is part of the value.

        DRF's JSON encoder renders a bare ``Decimal`` as a float, which would emit
        ``0.0`` for ``0.00`` and ``150000.0`` for ``150000.00``. The assertion is
        made against the raw response BYTES so it is testing what the wire carries,
        not what a Python round-trip reconstructs.
        """
        for amount in ('0.00', '150000.00', '1234567.89', '9999999999.99'):
            with self.subTest(amount=amount):
                restaurant = _make_restaurant(f'Scale {amount}')
                _terms(restaurant, self.admin, amount=amount)
                response = self.get(_detail_url(restaurant))
                body = response.content.decode()
                # A quoted string carrying the exact stored scale. A float would
                # render `0.0` / `150000.0`, unquoted.
                self.assertRegex(
                    body, r'"recurring_amount":\s*"%s"' % re.escape(amount),
                )
                value = response.json()['data']['commercial'][
                    'subscription_terms']['current']['recurring_amount']
                self.assertIsInstance(value, str)
                self.assertEqual(Decimal(value), Decimal(amount))

    def test_the_exact_terms_uuid_is_exposed(self):
        """
        The id is the ``expected_terms_id`` Step 3C's replace/end writers require,
        so a future write screen can assert the row it read is still the open one.
        """
        restaurant = _make_restaurant('Terms UUID')
        terms = _terms(restaurant, self.admin)
        current = self.commercial(restaurant)['subscription_terms']['current']
        self.assertEqual(current['id'], str(terms.id))
        # Usable as-is by the writer that consumes it.
        self.assertEqual(
            str(subscription_terms._open_terms(restaurant).id), current['id'],
        )

    def test_every_billing_interval_unit_round_trips(self):
        for unit in BILLING_INTERVAL_UNIT_VALUES:
            with self.subTest(unit=unit):
                restaurant = _make_restaurant(f'Interval {unit}')
                _terms(restaurant, self.admin, unit=unit, count=3)
                interval = self.commercial(restaurant)[
                    'subscription_terms']['current']['billing_interval']
                self.assertEqual(interval, {'unit': unit, 'count': 3})

    def test_the_currency_is_the_exact_persisted_value(self):
        restaurant = _make_restaurant('Terms Currency')
        _terms(restaurant, self.admin, currency='USD')
        self.assertEqual(
            self.commercial(restaurant)[
                'subscription_terms']['current']['currency'],
            'USD',
        )

    def test_open_is_ended_at_is_null_not_a_date_comparison(self):
        """
        A row backdated well into the past and a row effective moments ago are both
        simply OPEN. "Open" is the absence of a terminal stamp — never
        ``effective_from <= now``, never the latest ``recorded_at``.
        """
        restaurant = _make_restaurant('Terms Open Rule')
        old_open = _terms(
            restaurant, self.admin,
            effective_from=timezone.now() - timezone.timedelta(days=900),
        )
        current = self.commercial(restaurant)['subscription_terms']['current']
        self.assertEqual(current['id'], str(old_open.id))

    def test_many_historical_rows_plus_one_open_selects_exactly_the_open_row(self):
        restaurant = _make_restaurant('Terms Mixed')
        _history(restaurant, self.admin, rows=10)
        now = timezone.now()
        open_row = _terms(
            restaurant, self.admin, amount='777.00',
            effective_from=now - timezone.timedelta(hours=1),
        )
        current = self.commercial(restaurant)['subscription_terms']['current']
        self.assertEqual(current['id'], str(open_row.id))
        self.assertEqual(current['recurring_amount'], '777.00')
        self.assertEqual(
            RestaurantSubscriptionTerms.objects.filter(
                restaurant=restaurant,
            ).count(),
            11,
        )

    def test_the_terms_object_names_no_payment_or_standing_state(self):
        """
        §18. Terms prove recorded pricing intent. They do not prove Dinify collected
        anything — there is no invoice model, no receivable and no collection path —
        so no key or value may imply that it did.
        """
        restaurant = _make_restaurant('Terms Vocabulary')
        _terms(restaurant, self.admin)
        block = self.commercial(restaurant)['subscription_terms']
        self.assertEqual(set(block), {'configured', 'current'})
        self.assertEqual(
            set(block['current']),
            {'id', 'recurring_amount', 'currency', 'billing_interval',
             'effective_from', 'recorded_at'},
        )
        blob = json.dumps(block).lower()
        for word in (
            'active', 'paid', 'valid', 'good_standing', 'standing', 'trial',
            'invoice', 'overdue', 'balance', 'plan', 'tier', 'free',
        ):
            self.assertNotIn(word, blob)

    def test_no_recorded_by_or_owner_pii_is_exposed(self):
        restaurant = _make_restaurant('Terms PII')
        _terms(restaurant, self.admin)
        blob = json.dumps(self.commercial(restaurant))
        self.assertNotIn(str(self.admin.id), blob)
        self.assertNotIn(self.admin.email, blob)
        self.assertNotIn('recorded_by', blob)

    def test_legacy_subscription_columns_are_never_consulted(self):
        """
        Legacy says a subscription is valid, has an expiry in the future and a flat
        fee; the commercial domain has no open terms. The canonical answer is that
        there are no terms — every one of those legacy columns is evidence of
        nothing (``subscription_validity`` defaults True and its only writer was
        deleted).
        """
        restaurant = _make_restaurant('Legacy Says Yes')
        restaurant.subscription_validity = True
        restaurant.subscription_expiry_date = (
            timezone.now() + timezone.timedelta(days=365)
        )
        restaurant.flat_fee = Decimal('500000.00')
        restaurant.preferred_subscription_method = 'monthly'
        restaurant.save(update_fields=[
            'subscription_validity', 'subscription_expiry_date', 'flat_fee',
            'preferred_subscription_method',
        ])
        self.assertEqual(
            self.commercial(restaurant)['subscription_terms'],
            {'configured': False, 'current': None},
        )

    def test_terms_are_reported_even_when_legacy_says_invalid(self):
        """The disagreement runs both ways, and the domain wins both ways."""
        restaurant = _make_restaurant('Legacy Says No')
        restaurant.subscription_validity = False
        restaurant.save(update_fields=['subscription_validity'])
        terms = _terms(restaurant, self.admin, amount='150000.00')
        current = self.commercial(restaurant)['subscription_terms']
        self.assertTrue(current['configured'])
        self.assertEqual(current['current']['id'], str(terms.id))
        self.assertEqual(current['current']['recurring_amount'], '150000.00')


# --- §28 partial configuration ------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialPartialStateTests(_CommercialReadTestCase):
    """
    Three independent facts, every combination representable.

    A collapsed ``commercial_configured`` boolean would make all six intermediate
    states look identical, and the readiness engine needs to know precisely which
    one is missing to name a blocker.
    """

    def _expect(self, restaurant, *, timing, mode, terms):
        commercial = self.commercial(restaurant)
        self.assertEqual(commercial['payment_timing']['value'], timing)
        self.assertEqual(commercial['payment_timing']['configured'], timing is not None)
        self.assertEqual(commercial['payment_collection_mode']['value'], mode)
        self.assertEqual(
            commercial['payment_collection_mode']['configured'], mode is not None,
        )
        self.assertEqual(commercial['subscription_terms']['configured'], terms)
        self.assertEqual(commercial['subscription_terms']['current'] is not None, terms)

    def test_nothing_configured(self):
        self._expect(
            _make_restaurant('Partial None'), timing=None, mode=None, terms=False,
        )

    def test_timing_only(self):
        restaurant = _make_restaurant('Partial Timing')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
        self._expect(restaurant, timing='pay_first', mode=None, terms=False)

    def test_collection_only(self):
        restaurant = _make_restaurant('Partial Mode')
        _configure(restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        self._expect(restaurant, timing=None, mode='offline', terms=False)

    def test_terms_only(self):
        restaurant = _make_restaurant('Partial Terms')
        _terms(restaurant, self.admin)
        self._expect(restaurant, timing=None, mode=None, terms=True)

    def test_timing_and_terms_without_collection(self):
        restaurant = _make_restaurant('Partial TT')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_AFTER)
        _terms(restaurant, self.admin)
        self._expect(restaurant, timing='pay_after', mode=None, terms=True)

    def test_collection_and_terms_without_timing(self):
        restaurant = _make_restaurant('Partial CT')
        _configure(restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE)
        _terms(restaurant, self.admin)
        self._expect(restaurant, timing=None, mode='psp_online', terms=True)

    def test_both_axes_without_terms(self):
        restaurant = _make_restaurant('Partial Axes')
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_AFTER, mode=PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        self._expect(restaurant, timing='pay_after', mode='offline', terms=False)

    def test_everything_configured(self):
        restaurant = _make_restaurant('Partial All')
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        )
        _terms(restaurant, self.admin)
        self._expect(restaurant, timing='pay_first', mode='psp_online', terms=True)

    def test_there_is_no_collapsed_configured_boolean(self):
        restaurant = _make_restaurant('Partial Shape')
        commercial = self.commercial(restaurant)
        self.assertEqual(
            set(commercial),
            {'payment_timing', 'payment_collection_mode', 'subscription_terms'},
        )

    def test_one_axis_does_not_fill_in_the_other(self):
        """Writing timing must not make the untouched custody axis look decided."""
        restaurant = _make_restaurant('Partial Isolation')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
        commercial = self.commercial(restaurant)
        self.assertTrue(commercial['payment_timing']['configured'])
        self.assertEqual(
            commercial['payment_collection_mode'],
            {'configured': False, 'value': None, 'set_at': None},
        )


# --- §15 test tenants --------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialTestTenantTests(_CommercialReadTestCase):
    """
    ``is_test`` gets no special read semantics, and no restaurant is hard-coded.

    A test tenant rehearses the REAL path, so inventing configuration for it would
    make the rehearsal prove nothing.
    """

    def test_an_unconfigured_test_tenant_reads_unconfigured(self):
        restaurant = _make_restaurant('Test Tenant Bare', is_test=True)
        commercial = self.commercial(restaurant)
        self.assertFalse(commercial['payment_timing']['configured'])
        self.assertFalse(commercial['payment_collection_mode']['configured'])
        self.assertFalse(commercial['subscription_terms']['configured'])

    def test_a_configured_test_tenant_reports_exactly_its_facts(self):
        restaurant = _make_restaurant('Test Tenant Set', is_test=True)
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        _terms(restaurant, self.admin, amount='0.00')
        commercial = self.commercial(restaurant)
        self.assertEqual(commercial['payment_timing']['value'], 'pay_first')
        self.assertEqual(commercial['payment_collection_mode']['value'], 'offline')
        self.assertEqual(
            commercial['subscription_terms']['current']['recurring_amount'], '0.00',
        )

    def test_a_test_tenant_and_a_real_one_are_read_identically(self):
        real = _make_restaurant('Twin Real', is_test=False)
        fake = _make_restaurant('Twin Test', is_test=True)
        at = timezone.now() - timezone.timedelta(days=3)
        for restaurant in (real, fake):
            _configure(
                restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE, at=at,
            )
        self.assertEqual(
            self.commercial(real)['payment_collection_mode'],
            self.commercial(fake)['payment_collection_mode'],
        )


# --- §29 directory: shape, joins, cost ----------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialDirectoryTests(_CommercialReadTestCase):

    def test_every_directory_row_carries_commercial(self):
        _make_restaurant('Dir Bare')
        configured = _make_restaurant('Dir Configured')
        _configure(configured, self.admin, timing=PAYMENT_TIMING_PAY_AFTER)
        response = self.get(LIST_URL, page_size=100)
        results = response.json()['data']['results']
        self.assertTrue(results)
        for row in results:
            self.assertIn('commercial', row)
            self.assertEqual(
                set(row['commercial']),
                {'payment_timing', 'payment_collection_mode', 'subscription_terms'},
            )

    def test_configurations_do_not_leak_between_rows_on_one_page(self):
        first = _make_restaurant('Leak A')
        second = _make_restaurant('Leak B')
        bare = _make_restaurant('Leak C')
        _configure(
            first, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        _terms(first, self.admin, amount='111.00')
        _configure(second, self.admin, timing=PAYMENT_TIMING_PAY_AFTER)
        _terms(second, self.admin, amount='222.00', currency='KES', unit='year')

        rows = {
            row['id']: row['commercial']
            for row in self.get(LIST_URL, search='Leak', page_size=100)
            .json()['data']['results']
        }
        self.assertEqual(len(rows), 3)
        a = rows[str(first.id)]
        b = rows[str(second.id)]
        c = rows[str(bare.id)]
        self.assertEqual(a['payment_timing']['value'], 'pay_first')
        self.assertEqual(a['payment_collection_mode']['value'], 'offline')
        self.assertEqual(a['subscription_terms']['current']['recurring_amount'],
                         '111.00')
        self.assertEqual(b['payment_timing']['value'], 'pay_after')
        self.assertIsNone(b['payment_collection_mode']['value'])
        self.assertEqual(b['subscription_terms']['current']['currency'], 'KES')
        self.assertFalse(c['payment_timing']['configured'])
        self.assertFalse(c['subscription_terms']['configured'])

    def test_historical_terms_do_not_duplicate_a_directory_row(self):
        """
        §11. ``subscription_terms`` is 0..N, so a naive join would return one
        restaurant once per historical row. ``FilteredRelation`` keeps
        ``ended_at IS NULL`` in the ON clause and the partial unique index
        guarantees at most one survivor.
        """
        restaurant = _make_restaurant('Dupe Risk')
        _history(restaurant, self.admin, rows=12)
        _terms(restaurant, self.admin, amount='999.00')
        results = self.get(LIST_URL, search='Dupe Risk').json()['data']['results']
        self.assertEqual(len(results), 1, results)
        self.assertEqual(
            results[0]['commercial']['subscription_terms'][
                'current']['recurring_amount'],
            '999.00',
        )

    def test_historical_terms_do_not_break_the_pagination_count(self):
        for index in range(3):
            restaurant = _make_restaurant(f'Count {index}')
            _history(restaurant, self.admin, rows=6)
            if index == 0:
                _terms(restaurant, self.admin)
        payload = self.get(LIST_URL, search='Count ').json()['data']
        self.assertEqual(payload['pagination']['count'], 3)
        self.assertEqual(len(payload['results']), 3)

    def test_historical_terms_do_not_inflate_the_support_count(self):
        """
        The support aggregate and the terms join share a query. If the terms join
        multiplied rows, three issues would be counted once per historical row.
        """
        restaurant = _make_restaurant('Support Inflate')
        _history(restaurant, self.admin, rows=8)
        _terms(restaurant, self.admin)
        for index in range(3):
            SupportIssue.objects.create(
                restaurant=restaurant, category=SupportIssue.Category.BUG,
                impact=SupportIssue.Impact.QUESTION,
                status=SupportIssue.Status.OPEN,
                title=f'i{index}', description='x',
            )
        self.assertEqual(self.row(restaurant)['open_issue_count'], 3)
        self.assertEqual(self.detail(restaurant)['support']['open_issue_count'], 3)

    def test_last_activity_at_survives_the_commercial_joins(self):
        restaurant = _make_restaurant('Activity Intact')
        _history(restaurant, self.admin, rows=5)
        _terms(restaurant, self.admin)
        stamp = timezone.now() - timezone.timedelta(minutes=5)
        AdminAuditLog.objects.create(
            actor=self.admin, action='admin.restaurant.probe',
            result='success', restaurant_id=restaurant.id, created_at=stamp,
        )
        self.assertEqual(self.row(restaurant)['last_activity_at'], stamp.isoformat())

    def test_ordering_stays_deterministic_with_commercial_state(self):
        names = ['Order C', 'Order A', 'Order B']
        for index, name in enumerate(names):
            restaurant = _make_restaurant(name)
            if index % 2 == 0:
                _terms(restaurant, self.admin)
                _history(restaurant, self.admin, rows=4)
        results = self.get(LIST_URL, search='Order ').json()['data']['results']
        self.assertEqual([r['name'] for r in results], ['Order A', 'Order B',
                                                        'Order C'])

    def test_pagination_across_pages_loses_no_row(self):
        for index in range(6):
            restaurant = _make_restaurant(f'Page {index}')
            _history(restaurant, self.admin, rows=3)
            _terms(restaurant, self.admin)
        seen = []
        for page in (1, 2, 3):
            payload = self.get(
                LIST_URL, search='Page ', page=page, page_size=2,
            ).json()['data']
            self.assertEqual(payload['pagination']['count'], 6)
            seen.extend(row['id'] for row in payload['results'])
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(set(seen)), 6)


@override_settings(**_ADMIN_OVERRIDES)
class CommercialQueryCountTests(_CommercialReadTestCase):
    """
    ADMIN-DIR-N1-00 still holds with commercial state in the payload.

    Equality assertions, not upper bounds — the same discipline
    ``DirectoryQueryCountTests`` uses, and for the same reason: a bound generous
    enough to pass today would not notice a per-row read appearing tomorrow.
    """

    def _populate(self, count, *, prefix, configured):
        for index in range(count):
            restaurant = _make_restaurant(
                f'{prefix}{index:03d}', status=RestaurantStatus_Onboarding,
            )
            if configured:
                _configure(
                    restaurant, self.admin,
                    timing=PAYMENT_TIMING_PAY_FIRST,
                    mode=PAYMENT_COLLECTION_MODE_OFFLINE,
                )
                _history(restaurant, self.admin, rows=4)
                _terms(restaurant, self.admin)

    def _list_queries(self, page_size, search):
        with CaptureQueriesContext(connection) as captured:
            response = self.get(LIST_URL, page_size=page_size, search=search)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()['data']['results'])
        return len(captured)

    def test_query_count_does_not_grow_with_the_number_of_restaurants(self):
        self._populate(2, prefix='QA', configured=True)
        small = self._list_queries(page_size=2, search='QA')

        self._populate(18, prefix='QB', configured=True)
        large = self._list_queries(page_size=20, search='Q')

        self.assertEqual(
            small, large,
            f'Directory query count grew from {small} (2 configured rows) to '
            f'{large} (20 configured rows) — that is an N+1.',
        )

    def test_commercial_configuration_adds_no_query_at_all(self):
        """
        The stronger statement: a page of fully-configured restaurants costs the
        SAME as a page of unconfigured ones. Both joins are part of the single page
        query, so configuration changes what the row contains, never how it is read.
        """
        self._populate(5, prefix='UN', configured=False)
        bare = self._list_queries(page_size=5, search='UN')

        self._populate(5, prefix='CF', configured=True)
        rich = self._list_queries(page_size=5, search='CF')

        self.assertEqual(
            bare, rich,
            f'A configured page cost {rich} queries against {bare} for an '
            f'unconfigured one — commercial state is being read per row.',
        )

    def test_detail_query_count_does_not_grow_with_terms_history(self):
        restaurant = _make_restaurant('Detail Terms', status=RestaurantStatus_Onboarding)

        def measure():
            with CaptureQueriesContext(connection) as captured:
                response = self.get(_detail_url(restaurant))
                self.assertEqual(response.status_code, 200, response.content)
            return len(captured)

        lean = measure()
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_AFTER, mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        )
        _history(restaurant, self.admin, rows=15)
        _terms(restaurant, self.admin)
        rich = measure()

        self.assertEqual(
            lean, rich,
            f'Detail query count grew from {lean} to {rich} once commercial state '
            f'and 15 historical terms existed — that is an N+1.',
        )

    def test_the_page_is_still_retrieved_in_one_query(self):
        """
        The invariant stated positively: retrieving the annotated page — the part
        this PR touched — is ONE round trip, and stays one however many restaurants
        and historical rows it spans.
        """
        for index in range(10):
            restaurant = _make_restaurant(f'One Query {index}')
            _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
            _history(restaurant, self.admin, rows=5)
            _terms(restaurant, self.admin)

        queryset = restaurant_reads.directory_queryset()
        with CaptureQueriesContext(connection) as captured:
            rows = list(queryset[:10])
            payloads = [restaurant_reads.serialize_row(row) for row in rows]
        self.assertEqual(len(rows), 10)
        self.assertEqual(
            len(captured), 1,
            'the page retrieval plus its serialization should be exactly one query',
        )
        self.assertTrue(
            all(p['commercial']['subscription_terms']['configured']
                for p in payloads)
        )


# --- §13 directory and detail agree -------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialDirectoryDetailAgreementTests(_CommercialReadTestCase):
    """
    One commercial answer, byte-for-byte, in both places.

    A restaurant that reads ``offline`` in its workspace and ``unconfigured`` in the
    list is how an operator learns to distrust the screen. Structurally guaranteed —
    both paths call ``commercial_summary`` over the same annotations — and asserted
    anyway, because the guarantee is only as good as the next refactor.
    """

    def _states(self):
        now = timezone.now()
        bare = _make_restaurant('Agree Bare')

        timing_only = _make_restaurant('Agree Timing')
        _configure(timing_only, self.admin, timing=PAYMENT_TIMING_PAY_FIRST, at=now)

        mode_only = _make_restaurant('Agree Mode')
        _configure(mode_only, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE, at=now)

        full = _make_restaurant('Agree Full')
        _configure(
            full, self.admin, timing=PAYMENT_TIMING_PAY_AFTER,
            mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE, at=now,
        )
        _terms(full, self.admin, amount='150000.00')

        zero = _make_restaurant('Agree Zero')
        _terms(zero, self.admin, amount='0.00')

        history = _make_restaurant('Agree History')
        _history(history, self.admin, rows=7)

        return [bare, timing_only, mode_only, full, zero, history]

    def test_the_two_responses_carry_the_identical_object(self):
        for restaurant in self._states():
            with self.subTest(restaurant=restaurant.name):
                row = self.row(restaurant)['commercial']
                detail = self.detail(restaurant)['commercial']
                self.assertEqual(
                    json.dumps(row, sort_keys=True),
                    json.dumps(detail, sort_keys=True),
                )


# --- §30 legacy compatibility -------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialLegacyCompatibilityTests(_CommercialReadTestCase):
    """
    The deployed Admin frontend still renders ``payment_mode`` and ``subscription``.

    These fields coexist with ``commercial`` ONLY until that frontend migrates. They
    are not upgraded underneath it: reinterpreting
    ``subscription.has_commercial_subscription`` over the new domain would put the
    word **Active** on screen for a restaurant that has never paid Dinify anything,
    and repointing ``payment_mode`` at ``payment_collection_mode`` would silently
    change what a deployed client believes it is showing.

    No new consumer should read them. They are removed deliberately, in the change
    that says so.
    """

    LEGACY_KEYS = ('payment_mode', 'payment_mode_configured', 'subscription')

    def test_the_legacy_keys_are_still_present_in_both_responses(self):
        restaurant = _make_restaurant('Compat Present')
        row = self.row(restaurant)
        detail = self.detail(restaurant)
        for key in self.LEGACY_KEYS:
            self.assertIn(key, row, f'directory row lost {key}')
            self.assertIn(key, detail, f'detail lost {key}')

    def test_the_legacy_shape_is_unchanged(self):
        restaurant = _make_restaurant('Compat Shape')
        row = self.row(restaurant)
        self.assertIsNone(row['payment_mode'])
        self.assertFalse(row['payment_mode_configured'])
        self.assertEqual(
            set(row['subscription']),
            {'source', 'has_commercial_subscription', 'legacy_validity_flag',
             'legacy_expiry_at', 'preferred_method'},
        )
        self.assertEqual(row['subscription']['source'], 'legacy_restaurant_fields')

    def test_a_configured_collection_mode_does_not_reach_payment_mode(self):
        """The two are not the same contract, and this PR refuses to pretend."""
        restaurant = _make_restaurant('Compat Mode')
        _configure(restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        row = self.row(restaurant)
        self.assertEqual(row['commercial']['payment_collection_mode']['value'],
                         'offline')
        self.assertIsNone(row['payment_mode'])
        self.assertFalse(row['payment_mode_configured'])

    def test_open_terms_do_not_flip_has_commercial_subscription(self):
        """
        The single most consequential line of the compatibility policy: the deployed
        portal renders this boolean as "Active".
        """
        restaurant = _make_restaurant('Compat Terms')
        _terms(restaurant, self.admin, amount='150000.00')
        row = self.row(restaurant)
        self.assertTrue(row['commercial']['subscription_terms']['configured'])
        self.assertFalse(row['subscription']['has_commercial_subscription'])
        self.assertEqual(self.detail(restaurant)['subscription'][
            'has_commercial_subscription'], False)

    def test_the_legacy_object_still_reports_its_own_legacy_facts(self):
        """
        Deliberate disagreement during the compatibility window: legacy reports the
        legacy columns, ``commercial`` reports the domain, and they need not agree.
        """
        restaurant = _make_restaurant('Compat Disagree')
        expiry = timezone.now() + timezone.timedelta(days=200)
        restaurant.subscription_validity = True
        restaurant.subscription_expiry_date = expiry
        restaurant.preferred_subscription_method = 'monthly'
        restaurant.save(update_fields=[
            'subscription_validity', 'subscription_expiry_date',
            'preferred_subscription_method',
        ])
        row = self.row(restaurant)
        self.assertTrue(row['subscription']['legacy_validity_flag'])
        self.assertEqual(row['subscription']['legacy_expiry_at'], expiry.isoformat())
        self.assertEqual(row['subscription']['preferred_method'], 'monthly')
        # ... while the canonical object says there are no terms.
        self.assertFalse(row['commercial']['subscription_terms']['configured'])

    def test_a_client_that_ignores_commercial_still_sees_the_old_response(self):
        """
        Backwards compatibility stated as the deployed client experiences it: every
        key it read before is still there, with the same type.
        """
        restaurant = _make_restaurant('Compat Client')
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_PSP_ONLINE,
        )
        _terms(restaurant, self.admin)
        row = self.row(restaurant)
        expected = {
            'id', 'name', 'location', 'status', 'is_test', 'readiness',
            'commercial', 'payment_mode', 'payment_mode_configured',
            'subscription', 'open_issue_count', 'last_activity_at',
            'needs_attention',
        }
        self.assertEqual(set(row), expected)


# --- §20/§21 nothing else moved -----------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialReadTouchesNothingElseTests(_CommercialReadTestCase):

    def test_readiness_is_unchanged_by_commercial_configuration(self):
        """
        §20. Making commercial state READABLE does not make a restaurant ready.
        ``check_go_live_readiness`` still fails closed with the one seam blocker.
        """
        restaurant = _make_restaurant('Readiness Intact',
                                      status=RestaurantStatus_Onboarding)
        before = self.detail(restaurant)['readiness']
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        _terms(restaurant, self.admin)
        after = self.detail(restaurant)['readiness']
        self.assertEqual(before, after)
        self.assertEqual(after['blockers'], ['readiness_not_configured'])
        self.assertEqual(after['state'], 'not_ready')

    def test_needs_attention_is_unchanged_by_commercial_configuration(self):
        restaurant = _make_restaurant('Attention Intact',
                                      status=RestaurantStatus_Onboarding)
        self.assertTrue(self.row(restaurant)['needs_attention'])
        _configure(
            restaurant, self.admin,
            timing=PAYMENT_TIMING_PAY_FIRST, mode=PAYMENT_COLLECTION_MODE_OFFLINE,
        )
        _terms(restaurant, self.admin)
        self.assertTrue(self.row(restaurant)['needs_attention'])

    def test_no_approval_or_fingerprint_field_appears(self):
        """§21. There is no owner-approval model, so nothing may imply one."""
        restaurant = _make_restaurant('No Approval')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
        _terms(restaurant, self.admin)
        blob = json.dumps(self.commercial(restaurant)).lower()
        for word in ('approval', 'approved', 'fingerprint', 'version',
                     'updated_at', 'stale'):
            self.assertNotIn(word, blob)

    def test_the_reads_are_still_unelevated_and_unaudited(self):
        """§32. Commercial state does not raise the bar for reading the portfolio."""
        restaurant = _make_restaurant('HTTP Contract')
        _configure(restaurant, self.admin, mode=PAYMENT_COLLECTION_MODE_OFFLINE)
        self.assertIsNone(self.session.elevated_at)
        self.assertFalse(require_recent_elevation(self.session))
        before = AdminAuditLog.objects.count()
        self.assertEqual(self.get(LIST_URL).status_code, 200)
        self.assertEqual(self.get(_detail_url(restaurant)).status_code, 200)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_the_commercial_read_added_no_write_verb(self):
        """
        §22. The COMMERCIAL projection is read-only, and remains so.

        ``POST`` on the collection is EXCLUDED here, and only there: Phase-1 Step 2D
        later added restaurant CREATION on that verb. That is not a commercial write
        — it records no payment timing, collection mode or subscription terms — and it
        is covered by ``tests_restaurant_creation_endpoint``. The two assertions below
        keep this test's real subject intact: every other verb is still refused, and
        the collection's ``POST`` is the elevated creation route rather than something
        the commercial read opened.
        """
        restaurant = _make_restaurant('Read Only')
        for method in ('post', 'put', 'patch', 'delete'):
            for url in (LIST_URL, _detail_url(restaurant)):
                if method == 'post' and url == LIST_URL:
                    continue
                with self.subTest(method=method, url=url):
                    response = getattr(self.client, method)(
                        url, data={}, content_type='application/json',
                    )
                    self.assertEqual(response.status_code, 405, response.content)

    def test_the_collections_post_is_the_elevated_creation_route(self):
        """
        §22, continued. This session is authenticated but NEVER elevated, so a
        ``POST`` must be refused for want of a second factor — proving the verb is
        the step-up creation route and not a write the commercial read exposed.
        """
        self.assertIsNone(self.session.elevated_at)
        response = self.client.post(
            LIST_URL, data={}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(RestaurantServiceConfiguration.objects.exists())
        self.assertFalse(RestaurantSubscriptionTerms.objects.exists())


# --- writer / reader agreement ------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class CommercialWriterReaderAgreementTests(_CommercialReadTestCase):
    """
    The read reports what the Step 3C writer actually wrote.

    Every other test in this suite builds fixtures with direct ORM writes so it can
    construct arbitrary states. This one closes the loop: it drives the real domain
    services and reads the result back through HTTP, so a divergence between what
    the writer persists and what the projection reads would fail here.
    """

    def test_the_read_reflects_what_the_step_3c_writer_wrote(self):
        restaurant = _make_restaurant('Writer Loop')
        service_configuration.set_payment_timing(
            restaurant_id=restaurant.id, value=PAYMENT_TIMING_PAY_AFTER,
            actor=self.admin, expected_current=None,
        )
        service_configuration.set_payment_collection_mode(
            restaurant_id=restaurant.id, value=PAYMENT_COLLECTION_MODE_OFFLINE,
            actor=self.admin, expected_current=None,
        )
        recorded = subscription_terms.record_subscription_terms(
            restaurant_id=restaurant.id, recurring_amount=Decimal('150000.00'),
            currency=UGX, billing_interval_unit='month', billing_interval_count=1,
            effective_from=timezone.now() - timezone.timedelta(days=10),
            actor=self.admin,
        )

        commercial = self.commercial(restaurant)
        self.assertEqual(commercial['payment_timing']['value'], 'pay_after')
        self.assertEqual(commercial['payment_collection_mode']['value'], 'offline')
        current = commercial['subscription_terms']['current']
        self.assertEqual(current['id'], str(recorded.terms.id))
        self.assertEqual(current['recurring_amount'], '150000.00')

    def test_the_response_carries_usable_optimistic_concurrency_tokens(self):
        """
        §8/§13. The exact values the response hands the portal are accepted verbatim
        by the writers as ``expected_current`` / ``expected_terms_id``. If the read
        rendered a display string instead of the machine value, this would fail.
        """
        restaurant = _make_restaurant('Token Loop')
        service_configuration.set_payment_timing(
            restaurant_id=restaurant.id, value=PAYMENT_TIMING_PAY_FIRST,
            actor=self.admin, expected_current=None,
        )
        subscription_terms.record_subscription_terms(
            restaurant_id=restaurant.id, recurring_amount=Decimal('100.00'),
            currency=UGX, billing_interval_unit='month', billing_interval_count=1,
            effective_from=timezone.now() - timezone.timedelta(days=3),
            actor=self.admin,
        )
        commercial = self.commercial(restaurant)

        # Feed the read values straight back into the writers.
        service_configuration.set_payment_timing(
            restaurant_id=restaurant.id, value=PAYMENT_TIMING_PAY_AFTER,
            actor=self.admin,
            expected_current=commercial['payment_timing']['value'],
        )
        replaced = subscription_terms.replace_subscription_terms(
            restaurant_id=restaurant.id,
            expected_terms_id=commercial['subscription_terms']['current']['id'],
            recurring_amount=Decimal('200.00'), currency=UGX,
            billing_interval_unit='month', billing_interval_count=1,
            effective_from=timezone.now() - timezone.timedelta(days=1),
            actor=self.admin,
        )
        self.assertTrue(replaced.changed)

        after = self.commercial(restaurant)
        self.assertEqual(after['payment_timing']['value'], 'pay_after')
        self.assertEqual(
            after['subscription_terms']['current']['recurring_amount'], '200.00',
        )

    def test_ending_the_terms_returns_the_restaurant_to_unconfigured(self):
        restaurant = _make_restaurant('End Loop')
        recorded = subscription_terms.record_subscription_terms(
            restaurant_id=restaurant.id, recurring_amount=Decimal('50.00'),
            currency=UGX, billing_interval_unit='week', billing_interval_count=2,
            effective_from=timezone.now() - timezone.timedelta(days=20),
            actor=self.admin,
        )
        self.assertTrue(
            self.commercial(restaurant)['subscription_terms']['configured'],
        )
        subscription_terms.end_subscription_terms(
            restaurant_id=restaurant.id, expected_terms_id=recorded.terms.id,
            ended_at=timezone.now() - timezone.timedelta(days=1),
        )
        self.assertEqual(
            self.commercial(restaurant)['subscription_terms'],
            {'configured': False, 'current': None},
        )


# --- module contract ----------------------------------------------------------

class CommercialProjectionContractTests(_AdminReadTestCase):
    """Structural facts about the projection itself, not about one response."""

    def test_the_projection_consumes_exactly_the_annotations_it_declares(self):
        """
        ``COMMERCIAL_ANNOTATIONS`` and ``annotate_commercial`` must stay in step. A
        field added to the response but not to the queryset raises AttributeError on
        the first directory row — in production, not here — so the two are asserted
        against the real queryset.
        """
        queryset = restaurant_reads.directory_queryset()
        available = set(queryset.query.annotations)
        for name in commercial_reads.COMMERCIAL_ANNOTATIONS:
            self.assertIn(name, available, f'{name} is not annotated')

    def test_the_declared_annotations_are_the_ones_the_summary_reads(self):
        import ast
        import inspect
        source = inspect.getsource(commercial_reads)
        read = {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr.startswith('commercial_')
        }
        self.assertEqual(read, set(commercial_reads.COMMERCIAL_ANNOTATIONS))

    def test_the_query_never_selects_an_actor_column(self):
        """
        §6/§31 as a property of the SQL rather than of the serializer. The
        ``*_set_by_id`` and ``recorded_by_id`` columns are never selected, so no
        future serialization mistake can leak an operator identity through this
        object.
        """
        sql = str(restaurant_reads.directory_queryset().query)
        head = sql[:sql.find(' FROM ')]
        for column in (
            'payment_timing_set_by', 'payment_collection_mode_set_by',
            'recorded_by',
        ):
            self.assertNotIn(column, head, f'{column} is in the SELECT list')

    def test_the_summary_is_pure_python_over_a_loaded_row(self):
        """No database access at all once the row is in hand."""
        restaurant = _make_restaurant('Pure Summary')
        _configure(restaurant, self.admin, timing=PAYMENT_TIMING_PAY_FIRST)
        _terms(restaurant, self.admin)
        row = restaurant_reads.directory_queryset().get(pk=restaurant.pk)
        with CaptureQueriesContext(connection) as captured:
            summary = commercial_reads.commercial_summary(row)
        self.assertEqual(len(captured), 0)
        self.assertTrue(summary['subscription_terms']['configured'])
