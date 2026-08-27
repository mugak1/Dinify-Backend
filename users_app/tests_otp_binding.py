"""
``verify_otp``'s optional purpose and destination bindings (Step 2F.2).

WHY THIS FILE EXISTS SEPARATELY FROM THE REDEMPTION SUITE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``OtpManager.verify_otp`` is an AUTHENTICATION PRIMITIVE with four existing production
callers — ``self_register``, ``reset_password``, the ``verify-otp`` endpoint and
``create_employee``. Owner-claim redemption needed it to be able to bind a verification
to a purpose and a delivery destination, and the change had to be strictly additive.

The redemption suite proves that redemption uses the bindings. THIS suite proves the
other half: that a caller passing NEITHER argument gets byte-for-byte the behaviour it
had before, that the bindings NARROW the query rather than filtering after it, and that
a mismatch leaves the other challenge completely untouched.

A silent change here would be felt in three shipped authentication flows, so it is
pinned by exercising the primitive rather than by reading its signature.
"""
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from users_app.controllers.otp_manager import OTP_MAX_ATTEMPTS, OtpManager
from users_app.models import User, UserOtp

# ENV=dev hardcodes every OTP to this, which is exactly the condition that makes the
# bindings load-bearing: the digits cannot possibly distinguish two challenges.
DEV_OTP = '1234'
WRONG_OTP = '9999'

PHONE = '256772930001'
OTHER_PHONE = '256772930002'


def _user(suffix='a', phone=PHONE):
    return User.objects.create_user(
        first_name='Otp', last_name='Bind', email=f'otpbind-{suffix}@t.com',
        phone_number=phone, username=phone, country='UG', password='x', roles=[],
    )


class UnboundVerificationIsUnchangedTests(TestCase):
    """
    THE BACKWARD-COMPATIBILITY HALF. Omitting both arguments must select exactly the row
    it always selected: the most recent live challenge for the identity, whatever
    purpose it carries.
    """

    def setUp(self):
        super().setUp()
        self.user = _user()

    def test_a_login_otp_verifies_and_mints(self):
        self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))
        result = OtpManager().verify_otp(user_id=str(self.user.pk), otp=DEV_OTP)
        self.assertTrue(result['data']['valid'])
        self.assertIn('token', result['data'])

    def test_a_reset_otp_verifies_without_minting(self):
        self.assertTrue(
            OtpManager().make_otp(user=self.user, purpose='reset-password')
        )
        result = OtpManager().verify_otp(user_id=str(self.user.pk), otp=DEV_OTP)
        self.assertTrue(result['data']['valid'])
        self.assertNotIn('token', result['data'])

    def test_it_still_reads_the_purpose_off_the_row(self):
        """
        THE PRE-EXISTING BEHAVIOUR, unchanged and pinned. An unbound caller gets the most
        recent live challenge whatever it was issued for — which is precisely why
        redemption must NOT be an unbound caller.
        """
        OtpManager().make_otp(user=self.user, purpose='reset-password')
        result = OtpManager().verify_otp(user_id=str(self.user.pk), otp=DEV_OTP)
        self.assertTrue(result['data']['valid'])
        self.assertNotIn(
            'token', result['data'],
            'an unbound verify read the purpose off the row, as it always has',
        )

    def test_an_msisdn_keyed_challenge_still_verifies(self):
        """``self_register`` verifies with no user at all."""
        self.assertTrue(OtpManager().make_otp(msisdn=PHONE, purpose='register'))
        result = OtpManager().verify_otp(msisdn=PHONE, otp=DEV_OTP)
        self.assertTrue(result['data']['valid'])

    def test_a_wrong_code_still_counts_an_attempt(self):
        OtpManager().make_otp(user=self.user, purpose='login')
        OtpManager().verify_otp(user_id=str(self.user.pk), otp=WRONG_OTP)
        self.assertEqual(UserOtp.objects.get().attempts, 1)

    def test_the_attempt_cap_still_locks_the_row(self):
        OtpManager().make_otp(user=self.user, purpose='login')
        for _ in range(OTP_MAX_ATTEMPTS):
            OtpManager().verify_otp(user_id=str(self.user.pk), otp=WRONG_OTP)
        # The next attempt — even a CORRECT one — is refused and consumes the row.
        result = OtpManager().verify_otp(user_id=str(self.user.pk), otp=DEV_OTP)
        self.assertFalse(result['data']['valid'])
        self.assertIsNotNone(UserOtp.objects.get().consumed_at)

    def test_a_correct_code_is_still_single_use(self):
        OtpManager().make_otp(user=self.user, purpose='login')
        self.assertTrue(
            OtpManager().verify_otp(user_id=str(self.user.pk), otp=DEV_OTP
                                    )['data']['valid'],
        )
        self.assertFalse(
            OtpManager().verify_otp(user_id=str(self.user.pk), otp=DEV_OTP
                                    )['data']['valid'],
        )

    def test_a_missing_identity_is_still_invalid_rather_than_a_crash(self):
        self.assertFalse(OtpManager().verify_otp(otp=DEV_OTP)['data']['valid'])

    def test_a_none_code_is_still_invalid_rather_than_a_crash(self):
        OtpManager().make_otp(user=self.user, purpose='login')
        self.assertFalse(
            OtpManager().verify_otp(user_id=str(self.user.pk), otp=None
                                    )['data']['valid'],
        )


class PurposeBindingTests(TestCase):
    """The bindings NARROW the locked query — they do not filter after it."""

    def setUp(self):
        super().setUp()
        self.user = _user()

    def test_a_matching_purpose_verifies(self):
        OtpManager().make_otp(user=self.user, purpose='owner-claim')
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP, expected_purpose='owner-claim',
        )
        self.assertTrue(result['data']['valid'])

    def test_a_mismatched_purpose_is_invalid(self):
        OtpManager().make_otp(user=self.user, purpose='login')
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP, expected_purpose='owner-claim',
        )
        self.assertFalse(result['data']['valid'])

    def test_a_mismatched_purpose_leaves_the_other_row_untouched(self):
        """
        THE POINT OF NARROWING THE QUERY. A row that does not match the binding is never
        selected, so its ``consumed_at`` and its attempt counter are both unmoved — a
        purpose mismatch cannot be used to burn somebody else's factor.
        """
        OtpManager().make_otp(user=self.user, purpose='login')
        OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP, expected_purpose='owner-claim',
        )
        row = UserOtp.objects.get()
        self.assertIsNone(row.consumed_at)
        self.assertEqual(row.attempts, 0)

    def test_a_mismatched_purpose_mints_nothing(self):
        """Even for the one purpose that mints."""
        OtpManager().make_otp(user=self.user, purpose='login')
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP, expected_purpose='owner-claim',
        )
        self.assertNotIn('token', result['data'])

    def test_a_bound_verify_picks_its_own_row_not_the_newest(self):
        """
        The whole reason binding is needed rather than ordering. Two live rows for one
        identity, the owner-claim one OLDER; an unbound verify would take the newer login
        row, and a bound one takes the right one.
        """
        OtpManager().make_otp(
            user=self.user, msisdn=PHONE, purpose='owner-claim',
        )
        OtpManager().make_otp(user=self.user, purpose='login')
        self.assertEqual(UserOtp.objects.count(), 2)

        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP, expected_purpose='owner-claim',
        )

        self.assertTrue(result['data']['valid'])
        self.assertIsNotNone(
            UserOtp.objects.get(purpose='owner-claim').consumed_at,
        )
        self.assertIsNone(UserOtp.objects.get(purpose='login').consumed_at)


class DestinationBindingTests(TestCase):
    """An OTP proves control of the number it was DELIVERED TO."""

    def setUp(self):
        super().setUp()
        self.user = _user()

    def test_a_matching_destination_verifies(self):
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=PHONE,
        )
        self.assertTrue(result['data']['valid'])

    def test_a_mismatched_destination_is_invalid(self):
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=OTHER_PHONE,
        )
        self.assertFalse(result['data']['valid'])

    def test_a_null_destination_row_does_not_satisfy_a_bound_verify(self):
        """
        The case that made ``make_otp`` need an explicit ``msisdn``: a row that recorded
        no destination cannot be proved to have gone anywhere in particular.
        """
        OtpManager().make_otp(user=self.user, purpose='owner-claim')
        self.assertIsNone(UserOtp.objects.get().msisdn)
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=PHONE,
        )
        self.assertFalse(result['data']['valid'])

    def test_the_expected_destination_is_matched_exactly_not_canonicalised(self):
        """
        DELIBERATELY NOT NORMALISED, unlike the ``msisdn`` IDENTITY selector.

        The caller passes a value it has already proved canonical. Normalising here would
        let ``+256…`` satisfy a ``256…`` expectation, which is precisely the widening the
        binding exists to prevent. A mismatch selects no row and answers invalid, which
        is fail-closed.
        """
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=f'+{PHONE}',
        )
        self.assertFalse(result['data']['valid'])

    def test_a_destination_mismatch_leaves_the_row_untouched(self):
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=OTHER_PHONE,
        )
        row = UserOtp.objects.get()
        self.assertIsNone(row.consumed_at)
        self.assertEqual(row.attempts, 0)


class LockingIsUnchangedTests(TestCase):
    """The bindings must not change HOW the row is locked, only WHICH row is chosen."""

    def test_the_query_is_still_a_single_table_select_for_update(self):
        user = _user()
        OtpManager().make_otp(user=user, msisdn=PHONE, purpose='owner-claim')

        captured = []
        from django.db import connection
        original = connection.execute_wrapper

        def record(execute, sql, params, many, context):
            captured.append(sql)
            return execute(sql, params, many, context)

        with original(record):
            OtpManager().verify_otp(
                user_id=str(user.pk), otp=DEV_OTP,
                expected_purpose='owner-claim', expected_msisdn=PHONE,
            )

        locking = [sql for sql in captured if 'FOR UPDATE' in sql.upper()]
        self.assertTrue(locking, 'the challenge is no longer locked')
        for sql in locking:
            self.assertNotIn(
                'JOIN', sql.upper(),
                'the locked query gained a join, which on PostgreSQL would lock every '
                'row in it',
            )
            self.assertIn('"user_otps"', sql)


class ExpiryAndConsumptionAreUnchangedTests(TestCase):
    """The bindings sit beside the existing filters, never instead of them."""

    def setUp(self):
        super().setUp()
        self.user = _user()

    def test_an_expired_challenge_is_still_not_selected(self):
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        UserOtp.objects.update(
            expiry_time=timezone.now() - timezone.timedelta(minutes=1),
        )
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=PHONE,
        )
        self.assertFalse(result['data']['valid'])

    def test_a_consumed_challenge_is_still_not_selected(self):
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        UserOtp.objects.update(consumed_at=timezone.now())
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=PHONE,
        )
        self.assertFalse(result['data']['valid'])

    def test_the_attempt_cap_applies_to_a_bound_verify_too(self):
        OtpManager().make_otp(user=self.user, msisdn=PHONE, purpose='owner-claim')
        for _ in range(OTP_MAX_ATTEMPTS):
            OtpManager().verify_otp(
                user_id=str(self.user.pk), otp=WRONG_OTP,
                expected_purpose='owner-claim', expected_msisdn=PHONE,
            )
        self.assertEqual(UserOtp.objects.get().attempts, OTP_MAX_ATTEMPTS)
        result = OtpManager().verify_otp(
            user_id=str(self.user.pk), otp=DEV_OTP,
            expected_purpose='owner-claim', expected_msisdn=PHONE,
        )
        self.assertFalse(result['data']['valid'])


class ExistingCallersPassNoBindingsTests(TestCase):
    """
    The four pre-existing callers must keep calling it the way they always did.

    Structural, so a future edit that quietly bound one of them — changing what a login
    or a registration accepts — is a deliberate change to this list rather than a silent
    one.
    """

    CALLERS = (
        'users_app/controllers/self_register.py',
        'users_app/controllers/reset_password.py',
        'users_app/endpoints/auth.py',
        'restaurants_app/controllers/create_employee.py',
    )

    def test_no_pre_existing_caller_passes_a_binding(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        for relative in self.CALLERS:
            tree = ast.parse((root / relative).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'verify_otp'
                ):
                    continue
                passed = {kw.arg for kw in node.keywords}
                with self.subTest(module=relative):
                    self.assertNotIn('expected_purpose', passed)
                    self.assertNotIn('expected_msisdn', passed)

    def test_redemption_is_the_only_bound_caller(self):
        """Guards the list above from passing because nothing binds at all."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        tree = ast.parse(
            (root / 'platform_admin_app/owner_claim_redemption.py').read_text(
                encoding='utf-8',
            ),
        )
        bound = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'verify_otp'
            and {'expected_purpose', 'expected_msisdn'} <= {
                kw.arg for kw in node.keywords
            }
        ]
        self.assertEqual(len(bound), 1)


class MakeOtpCallersTests(TestCase):
    """
    Which callers record a delivery destination, stated as an inventory.

    ``login`` and ``reset_password`` pass NO ``msisdn`` and store ``NULL``; ``resend_otp``
    and the owner-claim challenge pass one. That difference decides which rows
    ``make_otp``'s purpose-blind replacement DELETE collides with, so it should be visible
    rather than rediscovered.
    """

    def test_the_generic_auth_callers_still_store_no_destination(self):
        user = _user()
        OtpManager().make_otp(user=user, purpose='login')
        self.assertIsNone(UserOtp.objects.get().msisdn)

    def test_resend_stores_one_as_it_always_has(self):
        user = _user()
        with mock.patch(
            'users_app.controllers.otp_manager.UserOtp.objects.filter',
            wraps=UserOtp.objects.filter,
        ):
            OtpManager().resend_otp(
                identification='id', identifier=str(user.pk), purpose='register',
            )
        self.assertEqual(UserOtp.objects.get().msisdn, PHONE)

    def test_the_owner_claim_challenge_stores_one(self):
        """Step 2F.2's requirement, asserted at the primitive."""
        user = _user()
        OtpManager().make_otp(user=user, msisdn=PHONE, purpose='owner-claim')
        self.assertEqual(UserOtp.objects.get().msisdn, PHONE)
