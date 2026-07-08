"""
Unit tests for the canonical MSISDN utilities (misc_app.controllers.msisdn).

All pure / no database — SimpleTestCase.
"""
from django.test import SimpleTestCase

from misc_app.controllers.msisdn import (
    normalise_msisdn,
    mask_msisdn,
    plan_msisdn_backfill,
    InvalidMsisdn,
    UnsupportedCountry,
    MsisdnError,
)

CANONICAL = '256772123456'


class NormaliseMsisdnTests(SimpleTestCase):
    def test_accepted_shapes_all_canonicalise(self):
        for raw in [
            '0772123456',
            '772123456',
            '256772123456',
            '+256772123456',
            '0772 123 456',
            '+256 772 123 456',
            '0772-123-456',
            ' 256-772-123-456 ',
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalise_msisdn(raw), CANONICAL)

    def test_country_tokens_accepted(self):
        for country in ['UG', 'ug', 'Uganda', 'uganda']:
            with self.subTest(country=country):
                self.assertEqual(normalise_msisdn('0772123456', country=country), CANONICAL)

    def test_idempotent(self):
        for raw in ['0772123456', '772123456', '256772123456', '+256772123456']:
            with self.subTest(raw=raw):
                once = normalise_msisdn(raw)
                self.assertEqual(normalise_msisdn(once), once)
                self.assertEqual(normalise_msisdn(normalise_msisdn(raw)), once)

    def test_invalid_raises(self):
        for bad in ['', '   ', None, 'abc', '123', '1234567890', '25677212345',
                    '2567721234567', '0', '1', '077212345', '+', '++256']:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidMsisdn):
                    normalise_msisdn(bad)

    def test_unsupported_country_raises(self):
        for country in ['KE', 'Kenya', 'US', '', None]:
            with self.subTest(country=country):
                with self.assertRaises(UnsupportedCountry):
                    normalise_msisdn('0772123456', country=country)

    def test_exceptions_are_msisdnerror_and_valueerror(self):
        self.assertTrue(issubclass(InvalidMsisdn, MsisdnError))
        self.assertTrue(issubclass(UnsupportedCountry, MsisdnError))
        self.assertTrue(issubclass(MsisdnError, ValueError))

    def test_error_messages_do_not_leak_the_raw_number(self):
        try:
            normalise_msisdn('0772123456garbage')
        except InvalidMsisdn as exc:
            self.assertNotIn('0772123456', str(exc))


class MaskMsisdnTests(SimpleTestCase):
    def test_examples(self):
        self.assertEqual(mask_msisdn('0772123456'), '0772****56')
        self.assertEqual(mask_msisdn('256772123456'), '2567******56')
        self.assertEqual(mask_msisdn('1234567890'), '1234****90')

    def test_defensive_short_and_empty(self):
        self.assertEqual(mask_msisdn(None), '(none)')
        self.assertEqual(mask_msisdn(''), '(empty)')
        self.assertEqual(mask_msisdn('1'), '*')
        self.assertEqual(mask_msisdn('12'), '**')
        # 3..(lead+trail) chars: reveal only the first char, never the whole value.
        self.assertEqual(mask_msisdn('123'), '1**')
        self.assertEqual(mask_msisdn('123456'), '1*****')

    def test_never_reveals_whole_value_for_short_inputs(self):
        for v in ['1', '12', '123', '1234', '12345', '123456']:
            with self.subTest(v=v):
                self.assertNotEqual(mask_msisdn(v), v)

    def test_masks_garbage_without_error(self):
        self.assertEqual(mask_msisdn('abcxyz'), 'a*****')


class PlanBackfillTests(SimpleTestCase):
    def test_convert_mirror_row(self):
        plan = plan_msisdn_backfill([(1, '0772123456', '0772123456', 'UG')])
        self.assertEqual(len(plan.writes), 1)
        self.assertEqual(plan.writes[0]['canonical'], CANONICAL)
        self.assertTrue(plan.writes[0]['changed'])
        self.assertEqual(plan.invalid, [])
        self.assertEqual(plan.collision, [])

    def test_already_canonical_is_noop_write(self):
        plan = plan_msisdn_backfill([(1, CANONICAL, CANONICAL, 'UG')])
        self.assertEqual(len(plan.writes), 1)
        self.assertFalse(plan.writes[0]['changed'])

    def test_invalid_bucket(self):
        plan = plan_msisdn_backfill([(1, '1', '1', 'UG')])
        self.assertEqual([r['id'] for r in plan.invalid], [1])
        self.assertEqual(plan.writes, [])

    def test_unsupported_country_bucket(self):
        plan = plan_msisdn_backfill([(1, '0772123456', '0772123456', 'KE')])
        self.assertEqual([r['id'] for r in plan.unsupported], [1])
        self.assertEqual(plan.writes, [])

    def test_diverged_username_left_untouched(self):
        plan = plan_msisdn_backfill([(1, '0772123456', 'admin', 'UG')])
        self.assertEqual([r['id'] for r in plan.diverged], [1])
        self.assertEqual(plan.writes, [])

    def test_collision_two_rows_same_canonical(self):
        plan = plan_msisdn_backfill([
            (1, '0772123456', '0772123456', 'UG'),
            (2, '256772123456', '256772123456', 'UG'),
        ])
        self.assertEqual(sorted(r['id'] for r in plan.collision), [1, 2])
        self.assertEqual(plan.writes, [])

    def test_collision_against_fixed_occupant(self):
        # Row 2 is diverged (kept as-is) and already occupies the canonical value;
        # row 1 would normalise onto it -> row 1 collides, row 2 stays diverged.
        plan = plan_msisdn_backfill([
            (1, '0772123456', '0772123456', 'UG'),
            (2, '256772123456', 'someone_else', 'UG'),
        ])
        self.assertEqual([r['id'] for r in plan.collision], [1])
        self.assertEqual([r['id'] for r in plan.diverged], [2])
        self.assertEqual(plan.writes, [])

    def test_distinct_numbers_do_not_collide(self):
        plan = plan_msisdn_backfill([
            (1, '0772123456', '0772123456', 'UG'),
            (2, '0700000001', '0700000001', 'UG'),
        ])
        self.assertEqual(len(plan.writes), 2)
        self.assertEqual(plan.collision, [])

    def test_every_row_accounted_for(self):
        rows = [
            (1, '0772123456', '0772123456', 'UG'),    # convert
            (2, CANONICAL, CANONICAL, 'UG'),           # convert (no-op) — but collides with row 1
            (3, '1', '1', 'UG'),                       # invalid
            (4, '0700000009', 'diverged_name', 'UG'),  # diverged
            (5, '0772123456', '0772123456', 'KE'),     # unsupported
        ]
        plan = plan_msisdn_backfill(rows)
        total = (len(plan.writes) + len(plan.invalid) + len(plan.unsupported)
                 + len(plan.diverged) + len(plan.collision))
        self.assertEqual(total, len(rows))

    def test_second_run_is_idempotent(self):
        # Simulate re-running after a prior successful backfill: all rows canonical.
        rows = [(1, CANONICAL, CANONICAL, 'UG'), (2, '256700000001', '256700000001', 'UG')]
        plan = plan_msisdn_backfill(rows)
        self.assertTrue(all(not w['changed'] for w in plan.writes))
        self.assertEqual(plan.collision, [])
