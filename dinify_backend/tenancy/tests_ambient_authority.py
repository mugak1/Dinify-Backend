"""
Standing gate for the single-brained identity model (TENANT-AUTH-00).

``account_type`` is the sole determinant of which plane an account belongs to, and
``roles`` carries restaurant roles only. Before PR-A those were two competing
discriminators: ``account_type`` gated the login door while a ``dinify_admin``
string in ``User.roles`` still conferred cross-tenant authority once inside, so an
ordinary customer JWT could carry platform reach.

Four things are asserted here, permanently:

1. The retired mechanism is not present in customer-plane source — AND the scanner
   that claims so actually fires. A guard tested only on a clean tree is a guard
   you trust on faith; every positive assertion below is paired with a synthetic
   violation proving the negative.
2. A ``platform_staff`` account cannot obtain a customer JWT, and cannot refresh
   one.
3. A ``restaurant_user`` cannot be given a platform-only role.
4. Holding a legacy role string grants nothing.

The cross-tenant behavioural half — that a JWT principal carrying the legacy role
is denied tenant data while a delegated principal is not — lives with the rest of
the boundary matrix in ``tests_tenant_isolation_closure.py``.

Green here proves the mechanism has not been reintroduced BY NAME and that the
identity invariants hold at their write paths. It is not a proof of tenant
isolation generally; see ``ASSURANCE.md`` for that boundary.
"""
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, tag
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken, OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RESTAURANT_OWNER,
    RESTAURANT_STAFF,
)
from dinify_backend.tenancy.ambient_authority import (
    ALLOWLIST,
    PLATFORM_ROLE_LITERALS,
    RETIRED_NAMES,
    find_violations,
    find_violations_in_source,
    is_test_module,
)
from platform_admin_app.services import (
    PLATFORM_ONLY_ROLES,
    PlatformStaffInvariantError,
    assert_no_platform_roles,
    platform_roles_in,
    promote_to_platform_staff,
    revoke_customer_tokens,
    revoke_pending_customer_otps,
)
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, Table,
)
from restaurants_app.serializers import SerializerPutRestaurantEmployee
from users_app.controllers import permissions_check
from users_app.controllers.login import login
from users_app.controllers.otp_manager import OtpManager
from users_app.models import User, UserOtp

REFRESH_URL = '/api/v1/users/auth/token/refresh/'

# make_otp fires a daemon notification thread even in ENV=dev, where its sends are
# already no-ops. Patched so the OTP tests below touch neither the network nor the mail
# outbox. Same three targets users_app/tests.py uses.
_PATCH_OTP_SMS = 'users_app.controllers.otp_manager.send_sms'
_PATCH_MESSENGER_EMAIL = (
    'notifications_app.controllers.messenger.Messenger.send_email')
_PATCH_NOTIFICATION = (
    'misc_app.controllers.notifications.notification.Notification.create_notification')

# The literal the tree must not contain, assembled at runtime so this module's own
# source stays clean for the scanner (which skips test modules, but the point is
# that the gate would be right either way).
LEGACY_ADMIN_ROLE = 'dinify' + '_admin'


def make_user(phone, roles=None, account_type=ACCOUNT_TYPE_RESTAURANT_USER):
    user = User.objects.create_user(
        first_name='Test', last_name='User',
        email=f'{phone}@test.com', phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=roles or [],
    )
    if account_type != ACCOUNT_TYPE_RESTAURANT_USER:
        User.objects.filter(pk=user.pk).update(account_type=account_type)
        user.refresh_from_db()
    return user


# =============================================================================
# 1. The source gate — and proof that it fires.
# =============================================================================
@tag('tenant_closure')
class AmbientAuthorityScannerTests(SimpleTestCase):
    """The scanner's own tests. No DB, no settings dependency."""

    def test_customer_plane_is_clean(self):
        violations = find_violations()
        self.assertEqual(
            violations, [],
            'Ambient role-based admin authority reintroduced:\n' + '\n'.join(
                f'  {path}:{line}: {name} — {reason}'
                for path, line, name, reason in violations
            ),
        )

    def test_allowlist_is_empty(self):
        # There is no legitimate customer-plane reason to name the retired
        # mechanism. If this ever fails, read the entry — do not widen the gate.
        self.assertEqual(
            ALLOWLIST, {},
            'The ambient-authority allowlist must stay empty; an entry means the '
            'customer plane is naming platform-identity vocabulary again.',
        )

    def test_scanner_catches_a_reintroduced_predicate_import(self):
        source = (
            'from users_app.controllers.permissions_check import is_dinify_admin\n'
            '\n'
            'def gate(user):\n'
            '    return is_dinify_admin(user)\n'
        )
        violations = find_violations_in_source(source, 'some_app/endpoints/thing.py')
        self.assertTrue(violations, 'the gate did not fire on a reintroduced import')
        self.assertEqual({name for _, name, _ in violations}, {'is_dinify_admin'})

    def test_scanner_catches_a_reintroduced_predicate_definition(self):
        source = (
            'def is_dinify_admin(user):\n'
            f'    return {LEGACY_ADMIN_ROLE!r} in user.roles\n'
        )
        violations = find_violations_in_source(source, 'some_app/controllers/x.py')
        names = {name for _, name, _ in violations}
        # Both halves are caught: the predicate name and the raw role literal.
        self.assertIn(LEGACY_ADMIN_ROLE, names)

    def test_scanner_catches_a_hardcoded_platform_role_literal(self):
        for literal in sorted(PLATFORM_ROLE_LITERALS):
            with self.subTest(literal=literal):
                source = f'ADMIN_ROLES = [{literal!r}]\n'
                violations = find_violations_in_source(source, 'some_app/configs.py')
                self.assertTrue(violations, f'the gate did not fire on {literal!r}')

    def test_scanner_catches_an_orm_lookup_selecting_platform_users(self):
        source = (
            'def admins():\n'
            f'    return User.objects.filter(roles__contains=[{LEGACY_ADMIN_ROLE!r}])\n'
        )
        violations = find_violations_in_source(source, 'some_app/notifications.py')
        self.assertTrue(
            violations, 'the gate did not fire on a platform-role ORM lookup')

    def test_scanner_catches_a_substring_roles_lookup(self):
        source = "count = User.objects.filter(roles__icontains='dinify').count()\n"
        violations = find_violations_in_source(source, 'some_app/reports.py')
        self.assertTrue(
            violations, 'the gate did not fire on a roles__icontains dinify lookup')

    def test_scanner_allows_ordinary_restaurant_role_queries(self):
        # The load-bearing negative: RestaurantEmployee.roles is legitimate data
        # and querying it must stay unremarkable. A gate that flags this would be
        # turned off within a week.
        source = (
            'def owners(restaurant_id):\n'
            '    return RestaurantEmployee.objects.filter(\n'
            "        restaurant_id=restaurant_id, roles__contains=['owner'],\n"
            '    )\n'
        )
        self.assertEqual(
            find_violations_in_source(source, 'restaurants_app/endpoints/x.py'), [])

    def test_scanner_allows_unrelated_source(self):
        source = 'def add(a, b):\n    return a + b\n'
        self.assertEqual(find_violations_in_source(source, 'misc_app/util.py'), [])

    def test_unparseable_source_is_not_reported(self):
        # A syntax error is `django check`'s to report; guessing at broken source
        # would only produce noise.
        self.assertEqual(find_violations_in_source('def (:\n', 'x/y.py'), [])

    def test_test_modules_are_excluded(self):
        for path in ('users_app/tests.py', 'app/tests_thing.py'):
            with self.subTest(path=path):
                self.assertTrue(is_test_module(path))

    def test_non_test_modules_are_not_excluded(self):
        # test_settings.py is configuration, not a test module — a `test` PREFIX
        # match would silently drop it out of the scan.
        for path in (
            'users_app/controllers/login.py',
            'dinify_backend/test_settings.py',
            'app/latest_thing.py',
        ):
            with self.subTest(path=path):
                self.assertFalse(is_test_module(path))

    def test_the_retired_predicates_are_gone_from_the_resolver(self):
        # The scanner works on source text; this asserts the runtime module too,
        # so a predicate re-added under an aliased import cannot pass unnoticed.
        for name in sorted(RETIRED_NAMES):
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(permissions_check, name),
                    f'{name} is back on the customer-plane permission resolver',
                )

    def test_the_id_resolvers_no_longer_advertise_an_unrestricted_return(self):
        # The `None` sentinel meant "caller must NOT scope" and was what turned a
        # role string into `model.objects.all()`. Its absence is part of the fix.
        for resolver in (
            permissions_check.get_module_restaurant_ids,
            permissions_check.get_employed_restaurant_ids,
        ):
            with self.subTest(resolver=resolver.__name__):
                self.assertNotIn('Optional', str(resolver.__annotations__))


# =============================================================================
# 2. Platform staff cannot hold a customer session.
# =============================================================================
@tag('tenant_closure')
class PlatformStaffCustomerSessionTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.staff = make_user(
            '256790000001', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        self.tenant = make_user('256790000002')

    def test_platform_staff_cannot_log_in_on_the_customer_plane(self):
        result = login(username='256790000001', password='password')
        self.assertEqual(result['status'], 401)
        self.assertNotIn('data', result)

    def test_platform_staff_refusal_is_indistinguishable_from_a_bad_password(self):
        # No account-type oracle: the refusal must not be identifiable as such.
        refused = login(username='256790000001', password='password')
        wrong = login(username='256790000002', password='nope')
        self.assertEqual(refused['status'], wrong['status'])
        self.assertEqual(refused['message'], wrong['message'])

    def test_platform_staff_cannot_refresh_a_customer_token(self):
        refresh = str(RefreshToken.for_user(self.staff))
        response = self.client.post(REFRESH_URL, {'refresh': refresh}, format='json')
        self.assertEqual(response.status_code, 401, response.content)
        self.assertNotIn('access', response.data)

    def test_the_refresh_refusal_matches_an_invalid_token(self):
        # Same body and code as a forged token, so refresh is not an oracle either.
        refused = self.client.post(
            REFRESH_URL, {'refresh': str(RefreshToken.for_user(self.staff))},
            format='json',
        )
        forged = self.client.post(
            REFRESH_URL, {'refresh': 'not-a-token'}, format='json')
        self.assertEqual(refused.status_code, forged.status_code)
        self.assertEqual(
            refused.data.get('code'), forged.data.get('code'))

    def test_a_restaurant_user_refreshes_normally(self):
        # The gate only ever subtracts: the ordinary contract is untouched.
        refresh = str(RefreshToken.for_user(self.tenant))
        response = self.client.post(REFRESH_URL, {'refresh': refresh}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('access', response.data)

    def test_refresh_with_no_token_is_the_ordinary_error(self):
        response = self.client.post(REFRESH_URL, {}, format='json')
        self.assertEqual(response.status_code, 400, response.content)

    def test_promotion_revokes_an_existing_customer_session(self):
        # A token minted before promotion must not outlive it.
        refresh = str(RefreshToken.for_user(self.tenant))
        self.assertEqual(
            self.client.post(
                REFRESH_URL, {'refresh': refresh}, format='json'
            ).status_code,
            200,
        )
        revoked = revoke_customer_tokens(self.tenant)
        self.assertEqual(revoked, 1)
        self.assertEqual(
            self.client.post(
                REFRESH_URL, {'refresh': refresh}, format='json'
            ).status_code,
            401,
        )

    def test_revoking_customer_tokens_is_idempotent(self):
        RefreshToken.for_user(self.tenant)
        self.assertEqual(revoke_customer_tokens(self.tenant), 1)
        self.assertEqual(revoke_customer_tokens(self.tenant), 0)

    def test_revoking_customer_tokens_is_safe_on_an_empty_set(self):
        self.assertEqual(revoke_customer_tokens(self.staff), 0)

    def test_outstanding_platform_staff_tokens_are_blacklisted(self):
        # The shape migration 0013 leaves behind, asserted against the models it
        # writes rather than by re-running the migration.
        RefreshToken.for_user(self.staff)
        revoke_customer_tokens(self.staff)
        outstanding = OutstandingToken.objects.filter(user=self.staff)
        self.assertTrue(outstanding.exists())
        for token in outstanding:
            self.assertTrue(
                BlacklistedToken.objects.filter(token=token).exists())


# =============================================================================
# 2b. ...including through the OTP path — the last unguarded mint.
# =============================================================================
# Every other customer-token mint reads account_type: login (login.py:112), password
# reset (reset_password.py:150 via _resolve_user) and refresh (token_refresh.py:51).
# verify_otp did not, and it is reachable: a platform-staff account cannot ORIGINATE a
# login OTP, but promotion did not invalidate one already in flight. So the sequence
# below — issue, promote, verify — is the whole defect, and it is why these tests mint
# the OTP BEFORE promotion. Minting after would pass against the pre-PR code and prove
# nothing, the same discipline tests_customer_jwt_gate.py applies to access tokens.
#
# ENV is 'dev' under test settings, so make_otp writes the hardcoded '1234' and its
# delivery is a no-op; the patches below stop the fire-and-forget notification thread
# from touching the network or the outbox.
@tag('tenant_closure')
@patch(_PATCH_NOTIFICATION, return_value=None)
@patch(_PATCH_MESSENGER_EMAIL, return_value=None)
@patch(_PATCH_OTP_SMS, return_value=None)
class PlatformStaffOtpMintTests(TestCase):

    def setUp(self):
        self.user = make_user('256790000003')

    def _issue_login_otp(self):
        self.assertTrue(OtpManager().make_otp(user=self.user, purpose='login'))

    def _promote(self):
        # Bypass the service so the pending OTP survives: this test is about the SINK,
        # and promote_to_platform_staff now purges the row (asserted separately below).
        User.objects.filter(pk=self.user.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF)

    def test_platform_staff_cannot_obtain_tokens_through_the_otp_path(self, *mocks):
        self._issue_login_otp()
        self._promote()

        result = OtpManager().verify_otp(user_id=str(self.user.id), otp='1234')

        self.assertFalse(result['data']['valid'])
        self.assertNotIn('token', result['data'])
        self.assertNotIn('refresh', result['data'])

    def test_no_outstanding_token_row_is_left_behind(self, *mocks):
        # The reason this is worth closing even though CustomerJWTAuthentication would
        # refuse the token at request time: minting one writes an OutstandingToken that
        # says a platform-staff account holds a live customer session.
        self._issue_login_otp()
        self._promote()

        OtpManager().verify_otp(user_id=str(self.user.id), otp='1234')

        self.assertFalse(
            OutstandingToken.objects.filter(user=self.user).exists())

    def test_the_refusal_is_indistinguishable_from_a_wrong_code(self, *mocks):
        # No account-type oracle. verify-otp is AllowAny with a client-supplied user
        # id, so a distinct status, message or data shape would answer "is this account
        # platform staff?" to an anonymous prober.
        self._issue_login_otp()
        self._promote()
        refused = OtpManager().verify_otp(user_id=str(self.user.id), otp='1234')

        other = make_user('256790000004')
        OtpManager().make_otp(user=other, purpose='login')
        wrong = OtpManager().verify_otp(user_id=str(other.id), otp='1111')

        self.assertEqual(refused, wrong)

    def test_a_restaurant_user_still_receives_tokens(self, *mocks):
        # The gate only ever subtracts: the ordinary contract is untouched.
        self._issue_login_otp()

        result = OtpManager().verify_otp(user_id=str(self.user.id), otp='1234')

        self.assertTrue(result['data']['valid'])
        self.assertIn('token', result['data'])
        self.assertIn('refresh', result['data'])

    def test_a_non_login_purpose_is_unaffected(self, *mocks):
        # Only the login branch mints. A reset/registration OTP returns valid=True with
        # no tokens for anyone, and promotion must not change that — three controllers
        # index ['data']['valid'] on it unguarded.
        OtpManager().make_otp(user=self.user, purpose='reset')
        self._promote()

        result = OtpManager().verify_otp(user_id=str(self.user.id), otp='1234')

        self.assertTrue(result['data']['valid'])
        self.assertNotIn('token', result['data'])

    def test_promotion_purges_pending_otp_challenges(self, *mocks):
        # The source half. revoke_customer_tokens already blacklisted the tokens an
        # account held; a pending login OTP is the other customer credential in flight.
        self._issue_login_otp()

        promote_to_platform_staff(self.user)

        self.assertFalse(
            UserOtp.objects.filter(
                user=self.user, consumed_at__isnull=True).exists())

    def test_purging_pending_otps_is_idempotent_and_safe_when_empty(self, *mocks):
        self.assertEqual(revoke_pending_customer_otps(self.user), 0)
        self._issue_login_otp()
        self.assertEqual(revoke_pending_customer_otps(self.user), 1)
        self.assertEqual(revoke_pending_customer_otps(self.user), 0)

    def test_purging_does_not_extend_the_expiry_window(self, *mocks):
        # UserOtp's pre_save hook re-stamps expiry_time on every save, so a per-row
        # save() would hand each purged challenge a fresh five minutes. The queryset
        # update bypasses it.
        self._issue_login_otp()
        before = UserOtp.objects.get(user=self.user).expiry_time

        revoke_pending_customer_otps(self.user)

        self.assertEqual(UserOtp.objects.get(user=self.user).expiry_time, before)


# =============================================================================
# 3. A restaurant_user may not hold a platform-only role.
# =============================================================================
@tag('tenant_closure')
class PlatformRoleInvariantTests(TestCase):

    def setUp(self):
        self.owner = make_user('256791000001')
        self.restaurant = Restaurant.objects.create(
            name='R', location='loc', owner=self.owner)
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.member = make_user('256791000002')

    def test_platform_roles_are_detected(self):
        self.assertEqual(platform_roles_in([LEGACY_ADMIN_ROLE]), [LEGACY_ADMIN_ROLE])
        self.assertEqual(platform_roles_in([RESTAURANT_OWNER]), [])

    def test_platform_roles_detection_tolerates_legacy_shapes(self):
        # Pre-0003 User.roles rows can hold None rather than a list.
        for value in (None, '', 'dinify_admin', 42, {}):
            with self.subTest(value=value):
                self.assertEqual(platform_roles_in(value), [])

    def test_asserting_no_platform_roles_refuses_each_literal(self):
        for role in sorted(PLATFORM_ONLY_ROLES):
            with self.subTest(role=role):
                with self.assertRaises(PlatformStaffInvariantError):
                    assert_no_platform_roles([role])

    def test_asserting_no_platform_roles_allows_restaurant_roles(self):
        assert_no_platform_roles([RESTAURANT_OWNER, RESTAURANT_STAFF])
        assert_no_platform_roles([])
        assert_no_platform_roles(None)

    def test_the_denylist_matches_the_scanner_literals(self):
        # One set of strings, two consumers — they must not drift apart.
        self.assertEqual(set(PLATFORM_ONLY_ROLES), set(PLATFORM_ROLE_LITERALS))

    def test_employee_serializer_refuses_a_platform_role_on_create(self):
        serializer = SerializerPutRestaurantEmployee(data={
            'user': str(self.member.id), 'roles': [LEGACY_ADMIN_ROLE],
        })
        self.assertFalse(serializer.is_valid())

    def test_employee_serializer_refuses_a_platform_role_on_update(self):
        employee = RestaurantEmployee.objects.create(
            user=self.member, restaurant=self.restaurant, roles=[RESTAURANT_STAFF])
        serializer = SerializerPutRestaurantEmployee(
            employee, data={'roles': [LEGACY_ADMIN_ROLE]}, partial=True)
        self.assertFalse(serializer.is_valid())

    def test_employee_serializer_accepts_ordinary_roles(self):
        serializer = SerializerPutRestaurantEmployee(data={
            'user': str(self.member.id), 'roles': [RESTAURANT_STAFF],
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_reactivating_a_membership_refuses_a_platform_role(self):
        from restaurants_app.controllers.employees.create_employee import (
            create_employee_from_existing_user,
        )
        RestaurantEmployee.objects.create(
            user=self.member, restaurant=self.restaurant,
            roles=[RESTAURANT_STAFF], deleted=True, active=False,
        )
        with self.assertRaises(PlatformStaffInvariantError):
            create_employee_from_existing_user(
                user_id=str(self.member.id),
                restaurant_id=str(self.restaurant.id),
                roles=[LEGACY_ADMIN_ROLE],
            )

    def test_user_roles_is_read_only_on_the_profile_serializer(self):
        from users_app.serializers import SerGetUserProfile

        self.assertTrue(SerGetUserProfile().fields['roles'].read_only)

    def test_the_profile_serializer_never_writes_roles(self):
        from users_app.serializers import SerGetUserProfile

        serializer = SerGetUserProfile(
            self.member, data={'roles': [LEGACY_ADMIN_ROLE]}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn('roles', serializer.validated_data)

    def test_production_users_hold_no_platform_roles(self):
        # The data-side half of the invariant, over whatever this database holds.
        # Migration 0013 clears restaurant_user rows; nothing writes them back.
        offenders = [
            user.username
            for user in User.objects.filter(
                account_type=ACCOUNT_TYPE_RESTAURANT_USER
            ).only('username', 'roles')
            if platform_roles_in(user.roles)
        ]
        self.assertEqual(offenders, [])


# =============================================================================
# 4. The legacy role string grants nothing.
# =============================================================================
@tag('tenant_closure')
class LegacyRoleStringGrantsNothingTests(TestCase):
    """
    The direct answer to "what if a row still carries the string?" — set it on the
    model, bypassing every write guard, and confirm the resolver is indifferent.
    """

    def setUp(self):
        self.owner = make_user('256792000001')
        self.restaurant = Restaurant.objects.create(
            name='R', location='loc', owner=self.owner)
        RestaurantEmployee.objects.create(
            user=self.owner, restaurant=self.restaurant, roles=[RESTAURANT_OWNER])
        self.section = MenuSection.objects.create(
            restaurant=self.restaurant, name='Mains')
        self.stranger = make_user('256792000002')
        # Set through the ORM, bypassing every write guard: the question is what a
        # row that already carries the string can do, not whether one can be made.
        User.objects.filter(pk=self.stranger.pk).update(roles=[LEGACY_ADMIN_ROLE])
        self.stranger.refresh_from_db()

    def test_the_role_confers_no_module_access(self):
        for module in ('menu', 'tables', 'settings', 'reports', 'team'):
            with self.subTest(module=module):
                self.assertFalse(
                    permissions_check.can_user_access_module(
                        self.stranger, str(self.restaurant.id), module),
                )

    def test_the_role_confers_no_list_scope(self):
        self.assertEqual(
            permissions_check.get_module_restaurant_ids(self.stranger, 'menu'), set())
        self.assertEqual(
            permissions_check.get_employed_restaurant_ids(self.stranger), set())

    def test_the_role_confers_no_manage_authority(self):
        self.assertFalse(
            permissions_check.can_manage_restaurant(
                self.stranger, str(self.restaurant.id)))

    def test_the_role_confers_no_write_queryset(self):
        from restaurants_app.endpoints.restaurant_setup import (
            build_scoped_instance_queryset,
        )

        MenuItem.objects.create(
            name='Nsenene', section=self.section, primary_price=1000)
        Table.objects.create(number=1, restaurant=self.restaurant)

        for config_detail, model in (
            ('menuitems', MenuItem),
            ('tables', Table),
            ('employees', RestaurantEmployee),
        ):
            with self.subTest(config_detail=config_detail):
                # The owner's queryset is non-empty, so a zero count for the
                # role-holder means "scoped out", not "nothing to find".
                self.assertGreater(
                    build_scoped_instance_queryset(
                        self.owner, config_detail, model).count(),
                    0,
                )
                self.assertEqual(
                    build_scoped_instance_queryset(
                        self.stranger, config_detail, model).count(),
                    0,
                )

    def test_the_role_confers_no_write_permission(self):
        # The payload resolves to a REAL restaurant, so the denial is the module
        # gate itself rather than an unresolved-target fallthrough — otherwise this
        # would pass even if the bypass were still in place.
        from restaurants_app.endpoints.restaurant_setup import (
            _resolve_target_restaurant_id, check_permission,
        )

        payload = {
            'section': str(self.section.id),
            'restaurant': str(self.restaurant.id),
        }
        self.assertEqual(
            _resolve_target_restaurant_id('menuitems', 'create', payload),
            str(self.restaurant.id),
        )
        self.assertFalse(check_permission(
            user=self.stranger, record='menuitems', action='create',
            request_data=payload,
        ))
        # ...and the owner, resolved the same way, is still allowed.
        self.assertTrue(check_permission(
            user=self.owner, record='menuitems', action='create',
            request_data=payload,
        ))

    def test_the_role_does_not_escalate_the_login_payload(self):
        # get_any_restaurant_roles used to hand a role-holder a full-access grid on
        # every employment it listed.
        self.assertEqual(
            permissions_check.get_any_restaurant_roles(self.stranger), [])
