"""
``GET users/user-profile/`` — the authenticated customer profile bootstrap (Step 2F.3).

WHAT THIS SUITE IS DEFENDING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Owner-claim redemption returns ``token + refresh + restaurant_id`` and no profile,
which is correct — a claim transaction is not a profile endpoint — but leaves the
restaurant portal unable to build its principal, because its route guard reads
``profile.restaurant_roles``. This endpoint is the missing half of that handoff.

Almost every test below exists because a specific plausible shortcut would be wrong
in a specific way:

  * let the client derive ``restaurant_roles`` from the claim's ``restaurant_id``
    -> a guess about tenant authority, blind to the owner's other memberships and
       unable to compute any of the resolved permission maps;
  * emit a second, bootstrap-specific profile serializer
    -> login and bootstrap drift, and the frontend principal depends on which door
       it came through;
  * relax authentication so a fresh claim can "obviously" read its own profile
    -> the Step-2D.1 pre-claim gate becomes advisory;
  * refresh the session / stamp ``last_login`` / repair a field on the way past
    -> a GET that writes;
  * send the owner back through ordinary login to get a profile
    -> a second OTP moments after the claim consumed its own.

The authority gates themselves are NOT re-implemented here and are not re-proved
from scratch — they live in ``CustomerJWTAuthentication`` and are pinned by
``tests_customer_jwt_gate`` and ``tests_customer_access_gate``. What this suite adds
is that THIS route is genuinely behind them, which is a different claim.
"""
import json
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    CUSTOMER_ACCESS_ESTABLISHED,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_MANAGER,
    RESTAURANT_OWNER,
    RESTAURANT_STAFF,
    RestaurantStatus_Live,
    RestaurantStatus_Offboarded,
    RestaurantStatus_Onboarding,
    RestaurantStatus_Suspended,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.controllers.login import login
from users_app.controllers.permissions_check import get_any_restaurant_roles
from users_app.models import User

PROFILE_URL = '/api/v1/users/user-profile/'

# The exact key set the canonical profile emits. Asserted as an EXACT set, not a
# subset: §5's prohibitions (no token, no account_type, no customer_access_state, no
# invitation state) are only enforced by a test that fails when a key APPEARS.
PROFILE_KEYS = {
    'id', 'first_name', 'last_name', 'email', 'phone_number', 'country',
    'roles', 'prompt_password_change', 'restaurant_roles',
}

# Keys that must never appear, named individually so a failure says which leaked.
FORBIDDEN_PROFILE_KEYS = (
    'token', 'refresh', 'access', 'password', 'account_type',
    'customer_access_state', 'is_staff', 'is_superuser', 'invitation',
    'owner_invitation', 'claim_token', 'otp', 'user_permissions', 'groups',
)

_PHONE = iter(f'25675{n:07d}' for n in range(100000, 109999))

PASSWORD = 'password'


def make_user(*, state=CUSTOMER_ACCESS_ESTABLISHED, roles=None, **extra):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='Boot', last_name='Strap', email=f'{phone}@t.com',
        phone_number=phone, username=phone, country='Uganda',
        password=PASSWORD, roles=roles or [], customer_access_state=state,
        **extra,
    )


def make_restaurant(owner, *, name='R', status=RestaurantStatus_Live):
    return Restaurant.objects.create(
        name=name, location='loc', status=status, owner=owner,
    )


def employ(user, restaurant, roles, **extra):
    return RestaurantEmployee.objects.create(
        user=user, restaurant=restaurant, roles=roles, **extra,
    )


def bearer(user):
    """
    A DIRECTLY FABRICATED access token, bypassing every mint gate.

    That is deliberate for the refusal tests: it proves the refusal lives at
    PRESENTATION rather than depending on no mint path handing one out. For the
    success tests it is simply the shortest way to an ordinary customer session.
    """
    return {'HTTP_AUTHORIZATION': f'Bearer {RefreshToken.for_user(user).access_token}'}


class BootstrapTestCase(TestCase):
    """Shared assertions about the read's shape."""

    def get_profile(self, user=None, **extra):
        return self.client.get(PROFILE_URL, **(bearer(user) if user else {}), **extra)

    def assertCanonicalEnvelope(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(set(body), {'status', 'message', 'data'})
        self.assertEqual(body['status'], 200)
        self.assertEqual(body['message'], 'Profile retrieved.')
        self.assertEqual(set(body['data']), {'profile'})
        return body['data']['profile']


# ═══════════════════════════════════════════════════════════════════════════════
# §3 / §11 — authority is the existing customer stack, and nothing else
# ═══════════════════════════════════════════════════════════════════════════════

class AuthorityTests(BootstrapTestCase):
    """
    The four refusals and the one success, through the real HTTP stack.

    These do not re-implement the gates; they prove this route is behind them. The
    distinction matters because a view that set ``authentication_classes = []`` or
    ``AllowAny`` to make the claim handoff "just work" would pass no test in
    ``tests_customer_jwt_gate`` — that suite probes this URL but only asserts on
    tokens it has already decided should fail.
    """

    def test_anonymous_is_401(self):
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 401)

    def test_established_restaurant_user_is_200(self):
        self.assertCanonicalEnvelope(self.get_profile(make_user()))

    def test_pending_initial_claim_identity_is_refused(self):
        """
        The Step-2D.1 gate, reached through this route.

        The token is fabricated directly rather than obtained, because no supported
        mint path would issue one — which is exactly why presentation is gated.
        """
        pending = make_user(state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        self.assertEqual(self.get_profile(pending).status_code, 401)

    def test_platform_staff_customer_token_is_refused(self):
        staff = make_user()
        User.objects.filter(pk=staff.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.assertEqual(self.get_profile(staff).status_code, 401)

    def test_a_deactivated_user_is_refused(self):
        user = make_user()
        token = bearer(user)
        User.objects.filter(pk=user.pk).update(is_active=False)
        self.assertEqual(self.client.get(PROFILE_URL, **token).status_code, 401)

    def test_a_token_minted_before_the_transition_stops_working_after_it(self):
        """
        The ordering that makes this a presentation gate rather than a mint gate.

        Mirrors ``tests_customer_jwt_gate``'s headline case for the new axis: the
        token is built while the identity is established, and only then does the
        identity move. A mint-time-only gate passes the first assertion and fails
        the second.
        """
        user = make_user()
        token = bearer(user)
        self.assertEqual(self.client.get(PROFILE_URL, **token).status_code, 200)

        User.objects.filter(pk=user.pk).update(
            customer_access_state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.assertEqual(self.client.get(PROFILE_URL, **token).status_code, 401)

    def test_a_refused_read_returns_no_profile_material(self):
        """A 401 must not become a data leak or an account-state oracle."""
        pending = make_user(state=CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM)
        response = self.get_profile(pending)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('profile', response.content.decode())
        self.assertNotIn('restaurant_roles', response.content.decode())


# ═══════════════════════════════════════════════════════════════════════════════
# §4 / §5 — the canonical serializer, and what the response may not carry
# ═══════════════════════════════════════════════════════════════════════════════

class ResponseContractTests(BootstrapTestCase):

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.restaurant = make_restaurant(self.user, name='Contract Cafe')
        employ(self.user, self.restaurant, [RESTAURANT_OWNER])

    def test_the_envelope_is_status_message_data_profile(self):
        self.assertCanonicalEnvelope(self.get_profile(self.user))

    def test_the_profile_key_set_is_exact(self):
        profile = self.assertCanonicalEnvelope(self.get_profile(self.user))
        self.assertEqual(set(profile), PROFILE_KEYS)

    def test_no_forbidden_key_appears_anywhere_in_the_response(self):
        """
        §5's list, checked over the WHOLE body rather than the profile object.

        A nested leak (a token tucked under ``restaurant_roles``, say) would pass a
        top-level key check.
        """
        response = self.get_profile(self.user)
        body = response.json()

        def walk(node, path='data'):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(
                        key, FORBIDDEN_PROFILE_KEYS,
                        f'{path}.{key} must never be emitted by profile bootstrap',
                    )
                    walk(value, f'{path}.{key}')
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f'{path}[{index}]')

        walk(body['data'])

    def test_it_is_not_another_authentication_operation(self):
        """
        No new access token, no refresh, no rotation — this is a read.

        The header is built BEFORE the count is taken: ``RefreshToken.for_user``
        INSERTs an ``OutstandingToken``, so minting inside the measured window would
        attribute the harness's own write to the view. Same trap the Step-2F.2
        concurrency suite records.
        """
        auth = bearer(self.user)
        before = OutstandingToken.objects.count()
        body = self.client.get(PROFILE_URL, **auth).json()
        self.assertEqual(OutstandingToken.objects.count(), before)
        serialized = json.dumps(body)
        for banned in ('token', 'refresh', 'require_otp'):
            self.assertNotIn(f'"{banned}"', serialized)

    def test_prompt_password_change_comes_from_the_profile_not_a_second_source(self):
        """
        It is a profile field and stays one.

        Login emits it BOTH at the envelope's top level and inside ``profile``; the
        bootstrap read has only the profile, so the frontend has exactly one place
        to look and cannot pick the wrong one.
        """
        profile = self.assertCanonicalEnvelope(self.get_profile(self.user))
        self.assertIn('prompt_password_change', profile)
        self.assertEqual(
            profile['prompt_password_change'], self.user.prompt_password_change,
        )
        self.assertNotIn('prompt_password_change', self.get_profile(
            self.user).json()['data'])

    def test_the_serializer_is_the_canonical_one(self):
        """
        Bound by identity, not by output similarity.

        A second serializer producing the same keys today would satisfy every
        assertion above and drift tomorrow, which is the failure §4 exists to
        prevent — so the binding itself is asserted.
        """
        import users_app.endpoints.user_profile as endpoint
        from users_app.serializers import SerGetUserProfile

        self.assertIs(endpoint.SerGetUserProfile, SerGetUserProfile)

        with mock.patch.object(
            endpoint, 'SerGetUserProfile', wraps=SerGetUserProfile,
        ) as spy:
            self.assertEqual(self.get_profile(self.user).status_code, 200)
        self.assertEqual(spy.call_count, 1)
        # Called with the request user and NO restaurant_roles context, so the
        # serializer delegates to the canonical resolver rather than being fed a
        # list this view computed for itself.
        args, kwargs = spy.call_args
        self.assertEqual(args[0].pk, self.user.pk)
        self.assertNotIn('context', kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# §6 — no-store
# ═══════════════════════════════════════════════════════════════════════════════

class CacheHeaderTests(BootstrapTestCase):

    def setUp(self):
        super().setUp()
        self.user = make_user()

    def assertNoStore(self, response):
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertEqual(response['Pragma'], 'no-cache')
        self.assertEqual(response['Expires'], '0')

    def test_a_successful_read_is_no_store(self):
        self.assertNoStore(self.get_profile(self.user))

    def test_a_refused_read_is_no_store_too(self):
        """Set on the view, so no branch can forget it."""
        self.assertNoStore(self.client.get(PROFILE_URL))

    def test_the_response_varies_on_authorization(self):
        """The body depends on the bearer token; a cache key must say so."""
        vary = self.get_profile(self.user).get('Vary', '')
        self.assertIn('authorization', vary.lower())

    def test_no_cookie_is_set(self):
        self.assertFalse(self.get_profile(self.user).cookies)

    def test_the_put_response_is_no_store_as_well(self):
        """
        DELIBERATE AND ADDITIVE, and worth stating plainly.

        PUT answers with the SAME canonical profile — identity plus every membership
        and its resolved permissions — so it needs the same treatment, and setting
        the headers on the view is what guarantees a later verb gets it too. This
        changes no status code, no body, no error semantics and no request contract;
        it is the only way in which PUT's response differs from before Step 2F.3.
        """
        response = self.client.put(
            PROFILE_URL, data=json.dumps({'first_name': 'Edited'}),
            content_type='application/json', **bearer(self.user),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNoStore(response)


# ═══════════════════════════════════════════════════════════════════════════════
# §7 — genuinely read-only
# ═══════════════════════════════════════════════════════════════════════════════

class NoSideEffectTests(BootstrapTestCase):

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.restaurant = make_restaurant(self.user, name='ReadOnly Cafe')
        self.membership = employ(self.user, self.restaurant, [RESTAURANT_OWNER])

    def test_nothing_about_the_identity_moves(self):
        before = User.objects.filter(pk=self.user.pk).values().get()
        self.assertEqual(self.get_profile(self.user).status_code, 200)
        after = User.objects.filter(pk=self.user.pk).values().get()
        self.assertEqual(before, after)

    def test_last_login_is_not_stamped(self):
        """
        Named separately because it is the one a reasonable person would add.

        ``last_login`` is what ``login`` writes, and a bootstrap read is not a login.
        """
        User.objects.filter(pk=self.user.pk).update(last_login=None)
        self.assertEqual(self.get_profile(self.user).status_code, 200)
        self.assertIsNone(User.objects.get(pk=self.user.pk).last_login)

    def test_no_membership_or_restaurant_row_is_written(self):
        membership_before = RestaurantEmployee.objects.filter(
            pk=self.membership.pk).values().get()
        restaurant_before = Restaurant.objects.filter(
            pk=self.restaurant.pk).values().get()
        self.assertEqual(self.get_profile(self.user).status_code, 200)
        self.assertEqual(
            RestaurantEmployee.objects.filter(pk=self.membership.pk).values().get(),
            membership_before,
        )
        self.assertEqual(
            Restaurant.objects.filter(pk=self.restaurant.pk).values().get(),
            restaurant_before,
        )

    def test_the_read_issues_no_write_statement_at_all(self):
        """
        The general form, so a write to a table this suite never thought of fails.

        Row-count comparisons only catch tables somebody remembered to check; this
        inspects the SQL the request actually ran. The token is minted OUTSIDE the
        capture — ``RefreshToken.for_user`` INSERTs an ``OutstandingToken``, and
        capturing that would have this test failing on the harness's own write.
        """
        auth = bearer(self.user)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(PROFILE_URL, **auth).status_code, 200)
        for query in captured.captured_queries:
            statement = query['sql'].strip().lower()
            self.assertTrue(
                statement.startswith('select'),
                f'profile bootstrap issued a non-SELECT statement: {query["sql"]}',
            )

    def test_no_token_is_minted_or_rotated(self):
        auth = bearer(self.user)
        before = set(
            OutstandingToken.objects.filter(user=self.user).values_list('pk', flat=True)
        )
        self.assertEqual(self.client.get(PROFILE_URL, **auth).status_code, 200)
        self.assertEqual(
            set(OutstandingToken.objects.filter(
                user=self.user).values_list('pk', flat=True)),
            before,
        )

    def test_no_admin_audit_row_is_written(self):
        """
        A tenant-initiated read is not a platform-staff decision.

        Same reasoning Step 2F.2 gives for not auditing redemption: an
        ``AdminAuditLog`` row whose actor is a restaurant user would corrupt what
        that log means.
        """
        from platform_admin_app.models import AdminAuditLog

        before = AdminAuditLog.objects.count()
        self.assertEqual(self.get_profile(self.user).status_code, 200)
        self.assertEqual(AdminAuditLog.objects.count(), before)

    def test_no_external_io(self):
        """No SMS, no email, no MongoDB notification, no legacy action log."""
        patches = [
            mock.patch(
                'notifications_app.controllers.sms.send_sms',
                side_effect=AssertionError('sent an SMS'),
            ),
            mock.patch(
                'notifications_app.controllers.messenger.Messenger.send_email',
                side_effect=AssertionError('sent an email'),
            ),
            mock.patch(
                'misc_app.controllers.notifications.notification.Notification'
                '.create_notification',
                side_effect=AssertionError('wrote a notification'),
            ),
            mock.patch(
                'misc_app.controllers.save_action_log.save_action',
                side_effect=AssertionError('wrote an action log'),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(self.get_profile(self.user).status_code, 200)


# ═══════════════════════════════════════════════════════════════════════════════
# §9 — multi-restaurant authority: the resolver decides, not the caller
# ═══════════════════════════════════════════════════════════════════════════════

class RestaurantAuthorityTests(BootstrapTestCase):

    def roles_for(self, user):
        profile = self.assertCanonicalEnvelope(self.get_profile(user))
        return {entry['restaurant_id']: entry for entry in profile['restaurant_roles']}

    def test_an_onboarding_restaurant_owner_sees_that_membership(self):
        """
        §9A — the state a freshly claimed restaurant is in.

        ``onboarding`` grants full portal access (PR-5 widened it precisely so an
        owner can build a menu before going live), so a claim bootstrap that could
        not see its own restaurant would be useless.
        """
        owner = make_user()
        restaurant = make_restaurant(
            owner, name='Fresh', status=RestaurantStatus_Onboarding,
        )
        employ(owner, restaurant, [RESTAURANT_OWNER])

        entry = self.roles_for(owner)[str(restaurant.id)]
        self.assertEqual(entry['roles'], [RESTAURANT_OWNER])
        self.assertEqual(entry['restaurant'], 'Fresh')
        # An owner resolves to full access; asserted through the canonical resolver
        # rather than by restating the grid here.
        self.assertEqual(
            entry['permissions'],
            get_any_restaurant_roles(owner)[0]['permissions'],
        )
        self.assertTrue(any(entry['permissions'].values()))

    def test_every_eligible_membership_appears(self):
        """§9B — the restaurant_id from a claim is context, not the whole picture."""
        user = make_user()
        first = make_restaurant(user, name='One', status=RestaurantStatus_Live)
        second = make_restaurant(user, name='Two', status=RestaurantStatus_Onboarding)
        third = make_restaurant(make_user(), name='Three')
        employ(user, first, [RESTAURANT_OWNER])
        employ(user, second, [RESTAURANT_OWNER])
        employ(user, third, [RESTAURANT_MANAGER])

        self.assertEqual(
            set(self.roles_for(user)),
            {str(first.id), str(second.id), str(third.id)},
        )

    def test_inactive_and_deleted_memberships_are_absent(self):
        """§9C — current resolver semantics, not a new opinion about them."""
        user = make_user()
        live = make_restaurant(user, name='Live')
        inactive_at = make_restaurant(make_user(), name='Inactive')
        deleted_at = make_restaurant(make_user(), name='Deleted')
        employ(user, live, [RESTAURANT_STAFF])
        employ(user, inactive_at, [RESTAURANT_STAFF], active=False)
        employ(user, deleted_at, [RESTAURANT_STAFF], deleted=True)

        self.assertEqual(set(self.roles_for(user)), {str(live.id)})

    def test_lifecycle_states_follow_portal_access_states(self):
        """
        §9D — suspended and offboarded drop out; onboarding and live do not.

        Not a widening: this pins that the bootstrap read inherits
        ``portal_access_states()`` exactly as the login path does.
        """
        user = make_user()
        expected = set()
        for status, grants in (
            (RestaurantStatus_Onboarding, True),
            (RestaurantStatus_Live, True),
            (RestaurantStatus_Suspended, False),
            (RestaurantStatus_Offboarded, False),
        ):
            restaurant = make_restaurant(make_user(), name=status, status=status)
            employ(user, restaurant, [RESTAURANT_STAFF])
            if grants:
                expected.add(str(restaurant.id))

        self.assertEqual(set(self.roles_for(user)), expected)

    def test_a_user_with_no_membership_gets_an_empty_list(self):
        """Not an error, and not an absent key — the frontend branches on length."""
        profile = self.assertCanonicalEnvelope(self.get_profile(make_user()))
        self.assertEqual(profile['restaurant_roles'], [])

    def test_the_payload_matches_the_resolver_exactly(self):
        """
        The database membership resolver is authoritative, byte for byte.

        Anything the view added, reordered or dropped on the way out would show here.
        """
        user = make_user()
        for index in range(3):
            restaurant = make_restaurant(user, name=f'Auth {index}')
            employ(user, restaurant, [RESTAURANT_OWNER])

        profile = self.assertCanonicalEnvelope(self.get_profile(user))
        self.assertEqual(
            profile['restaurant_roles'],
            json.loads(json.dumps(get_any_restaurant_roles(user))),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §10 — login and bootstrap produce the same principal
# ═══════════════════════════════════════════════════════════════════════════════

class LoginEquivalenceTests(BootstrapTestCase):
    """
    THE ARCHITECTURAL REASON THE ENDPOINT EXISTS.

    Owner claim and ordinary login must hydrate the SAME frontend principal. Both
    already build it with ``SerGetUserProfile``; these tests are what stops that from
    silently becoming untrue — a bootstrap-specific serializer, an extra field, a
    differently-resolved permissions map.
    """

    def assertProfilesMatch(self, login_response, user):
        login_profile = login_response['data']['profile']
        http_profile = self.assertCanonicalEnvelope(self.get_profile(user))
        # Through JSON on both sides: the login controller returns Python objects
        # while HTTP has been serialized, and comparing them raw would fail on
        # UUID-vs-str for reasons that have nothing to do with the contract.
        self.assertEqual(json.loads(json.dumps(login_profile)), http_profile)
        return http_profile

    def test_a_staff_login_and_the_bootstrap_read_agree(self):
        """The token branch: no OTP, so login hands back a session AND a profile."""
        user = make_user()
        restaurant = make_restaurant(make_user(), name='Equivalence')
        employ(user, restaurant, [RESTAURANT_STAFF])

        response = login(username=user.username, password=PASSWORD)
        self.assertEqual(response['status'], 200, response)
        self.assertFalse(response['data']['require_otp'])

        profile = self.assertProfilesMatch(response, user)
        self.assertEqual(
            [entry['restaurant_id'] for entry in profile['restaurant_roles']],
            [str(restaurant.id)],
        )

    def test_an_owner_login_and_the_bootstrap_read_agree(self):
        """
        The OTP branch — and the case that motivates the whole endpoint.

        An owner membership sets ``require_otp``, so login stops and emits a profile
        WITHOUT a session. That is exactly why a freshly claimed owner cannot be sent
        back through login for their profile, and exactly why the profile the two
        paths produce has to be identical.
        """
        owner = make_user()
        restaurant = make_restaurant(owner, name='Owner Equivalence')
        employ(owner, restaurant, [RESTAURANT_OWNER])

        with mock.patch(
            'users_app.controllers.otp_manager.OtpManager.make_otp', return_value=True,
        ):
            response = login(username=owner.username, password=PASSWORD)
        self.assertEqual(response['status'], 200, response)
        self.assertTrue(response['data']['require_otp'])
        self.assertNotIn('token', response['data'])

        self.assertProfilesMatch(response, owner)

    def test_they_agree_across_several_memberships_and_roles(self):
        user = make_user()
        for index, roles in enumerate((
            [RESTAURANT_STAFF], [RESTAURANT_MANAGER], [RESTAURANT_STAFF],
        )):
            restaurant = make_restaurant(
                make_user(), name=f'Multi {index}',
                status=RestaurantStatus_Onboarding if index else RestaurantStatus_Live,
            )
            employ(user, restaurant, roles)

        with mock.patch(
            'users_app.controllers.otp_manager.OtpManager.make_otp', return_value=True,
        ):
            response = login(username=user.username, password=PASSWORD)
        profile = self.assertProfilesMatch(response, user)
        self.assertEqual(len(profile['restaurant_roles']), 3)

    def test_both_paths_reach_the_same_resolver(self):
        """
        Bound structurally as well as behaviourally.

        The two responses matching proves they agree TODAY; this proves they agree
        because they share one source, which is what keeps them agreeing.
        """
        import users_app.controllers.login as login_module
        from users_app.controllers.permissions_check import (
            get_any_restaurant_roles as canonical,
        )
        from users_app.serializers import SerGetUserProfile

        self.assertIs(login_module.get_any_restaurant_roles, canonical)

        user = make_user()
        employ(user, make_restaurant(user, name='Shared'), [RESTAURANT_STAFF])
        # The serializer with no context delegates to the canonical resolver, which
        # is the mechanism by which the bootstrap read cannot diverge from login.
        self.assertEqual(
            SerGetUserProfile(user).data['restaurant_roles'],
            canonical(user),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §12 — query budget
# ═══════════════════════════════════════════════════════════════════════════════

class QueryBudgetTests(BootstrapTestCase):
    """
    Bootstrap runs on every portal load; it must not scale with membership count.

    ``get_any_restaurant_roles`` reads the memberships in one query and every
    override row across those restaurants in a second. That property is the thing
    being preserved — the absolute number is incidental and is not asserted.
    """

    def measure(self, user):
        # Minted outside the capture: ``RefreshToken.for_user`` INSERTs an
        # ``OutstandingToken``, which would be counted as one of the view's queries
        # and would make the budget assertion measure the harness.
        auth = bearer(user)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(PROFILE_URL, **auth).status_code, 200)
        return len(captured)

    def test_query_count_does_not_grow_with_the_number_of_memberships(self):
        lean = make_user()
        employ(lean, make_restaurant(lean, name='Lean'), [RESTAURANT_OWNER])

        heavy = make_user()
        for index in range(12):
            employ(
                heavy,
                make_restaurant(heavy, name=f'Heavy {index}'),
                [RESTAURANT_OWNER],
            )

        lean_count = self.measure(lean)
        heavy_count = self.measure(heavy)
        self.assertEqual(
            lean_count, heavy_count,
            f'profile bootstrap grew from {lean_count} queries (1 membership) to '
            f'{heavy_count} (12) — that is an N+1 in the portal bootstrap.',
        )

    def test_role_permission_overrides_do_not_add_a_query_each(self):
        """
        The second half of the resolver's contract.

        Override rows are read in ONE query across all the restaurants; a per-role
        or per-restaurant lookup would show up here.
        """
        from restaurants_app.controllers.role_permissions import (
            ensure_role_permissions,
        )

        user = make_user()
        restaurants = [
            make_restaurant(user, name=f'Override {index}') for index in range(6)
        ]
        for restaurant in restaurants:
            employ(user, restaurant, [RESTAURANT_MANAGER])

        bare = self.measure(user)
        for restaurant in restaurants:
            ensure_role_permissions(restaurant)
        self.assertEqual(bare, self.measure(user))


# ═══════════════════════════════════════════════════════════════════════════════
# §8 — PUT is unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class PutRegressionTests(BootstrapTestCase):
    """
    Adding a read side must not disturb the write side.

    ``tests_user_profile`` owns the full PUT contract; these re-pin the load-bearing
    behaviours beside the new verb, so a change to this file that broke one of them
    fails here rather than in a distant suite.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user()

    def put(self, payload, user=None):
        return self.client.put(
            PROFILE_URL, data=json.dumps(payload),
            content_type='application/json', **bearer(user or self.user),
        )

    def test_a_self_update_still_applies_and_returns_the_profile(self):
        response = self.put({'first_name': 'Renamed'})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 200)
        self.assertEqual(body['message'], 'Your profile has been updated successfully.')
        self.assertEqual(set(body['data']), {'profile'})
        self.assertEqual(body['data']['profile']['first_name'], 'Renamed')
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Renamed')

    def test_a_phone_change_is_still_refused(self):
        original = self.user.phone_number
        response = self.put({'phone_number': '256772999111'})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()['message'], 'Phone number cannot be changed here.',
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, original)

    def test_a_duplicate_email_is_still_refused(self):
        other = make_user()
        response = self.put({'email': other.email})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()['message'], 'This email is already in use.')

    def test_put_still_requires_authentication(self):
        self.assertEqual(
            self.client.put(
                PROFILE_URL, data=json.dumps({'first_name': 'Anon'}),
                content_type='application/json',
            ).status_code,
            401,
        )

    def test_a_write_then_a_read_agree(self):
        """The two verbs are one resource, so the read reflects the write."""
        self.assertEqual(self.put({'first_name': 'Coherent'}).status_code, 200)
        profile = self.assertCanonicalEnvelope(self.get_profile(self.user))
        self.assertEqual(profile['first_name'], 'Coherent')


# ═══════════════════════════════════════════════════════════════════════════════
# A delegated administrator still cannot reach this route
# ═══════════════════════════════════════════════════════════════════════════════

class DelegationIsUnaffectedTests(TestCase):
    """
    ``users/user-profile/`` is deliberately absent from ``ALLOWED_ROUTES``.

    It acts on ``request.user``, so a delegated principal here would read the
    ADMINISTRATOR's own records rather than the tenant's — which is why the
    allowlist excludes it by name. The allowlist is keyed on ``(route, method)``,
    so adding a ``GET`` handler could in principle have been accompanied by an
    entry; this asserts that it was not.
    """

    def test_neither_verb_is_on_the_delegated_allowlist(self):
        from platform_admin_app.configs.delegation_scopes import route_rule

        route = 'api/v1/users/user-profile/'
        for method in ('GET', 'PUT', 'POST', 'DELETE', 'PATCH'):
            self.assertIsNone(
                route_rule(route, method),
                f'{method} {route} must not be reachable by a delegated session',
            )


# ═══════════════════════════════════════════════════════════════════════════════
# §13 — the Step 2F.2 handoff, end to end
# ═══════════════════════════════════════════════════════════════════════════════

class RedemptionHandoffTests(BootstrapTestCase):
    """
    THE EXACT SEQUENCE THE OWNER-CLAIM UI WILL PERFORM.

        Admin creates a restaurant with a brand-new owner
            -> owner is pending_initial_claim, no usable password, no access
            -> challenge  (token proves possession)
            -> redeem     (OTP proves current control)
            -> the ACCESS TOKEN redemption returned
            -> GET users/user-profile/
            -> 200, and restaurant_roles carries the newly claimed restaurant
               with owner authority

    Nothing here reaches past the public contract: the profile is fetched with the
    token exactly as an HTTP client would, and the assertions are about the response
    body rather than about internal state.

    The ordering assertion is the security half. BEFORE redemption a fabricated
    customer token for that same identity is refused — so the profile becomes
    readable because the claim succeeded, not because the identity exists.
    """

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

        from platform_admin_app import onboarding_creation
        from platform_admin_app.onboarding_creation import NewOwner

        self.admin = User.objects.create_user(
            first_name='Ada', last_name='Min', email=f'bootstrap-{next(_PHONE)}@t.com',
            username=f'bootstrap-admin-{next(_PHONE)}', country='UG', password='x',
            roles=[], account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Claimed Cafe', location='Ntinda', is_test=False,
            owner=NewOwner('Owen', 'Ner', next(_PHONE), None),
            actor=self.admin, reason='Creating the bootstrap handoff fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.owner = self.creation.owner
        self.claim_token = self.creation.claim_token

    def test_a_pending_owner_cannot_read_a_profile_before_redemption(self):
        """
        The precondition, and it is not incidental.

        The token is fabricated directly — no supported path would mint one for a
        pending identity — so this proves the refusal is at PRESENTATION. If this
        assertion ever stops holding, the handoff below stops proving anything.
        """
        self.assertEqual(
            self.owner.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )
        self.assertEqual(self.get_profile(self.owner).status_code, 401)

    def test_the_token_from_redemption_bootstraps_the_canonical_profile(self):
        from rest_framework.test import APIClient

        api = APIClient()

        # Two factors: the credential Admin issued, then the code it delivers.
        challenge = api.post(
            '/api/v1/users/owner-claim/challenge/', {}, format='json',
            headers={'X-Owner-Claim-Token': self.claim_token},
        )
        self.assertEqual(challenge.status_code, 200, challenge.content)
        self.assertTrue(challenge.json()['data']['credential_setup_required'])

        redeemed = api.post(
            '/api/v1/users/owner-claim/redeem/',
            {'otp': '1234', 'new_password': 'Kabalagala-Sunrise-7'}, format='json',
            headers={'X-Owner-Claim-Token': self.claim_token},
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.content)
        claim = redeemed.json()['data']
        self.assertEqual(set(claim), {'token', 'refresh', 'restaurant_id'})
        self.assertEqual(claim['restaurant_id'], str(self.restaurant.id))

        # THE HANDOFF: the access token redemption returned, and nothing else.
        response = self.client.get(
            PROFILE_URL, HTTP_AUTHORIZATION=f"Bearer {claim['token']}",
        )
        profile = self.assertCanonicalEnvelope(response)

        self.assertEqual(profile['id'], str(self.owner.id))
        memberships = {
            entry['restaurant_id']: entry for entry in profile['restaurant_roles']
        }
        self.assertIn(
            str(self.restaurant.id), memberships,
            'the newly claimed restaurant must appear in the bootstrapped principal',
        )
        entry = memberships[str(self.restaurant.id)]
        self.assertEqual(entry['roles'], [RESTAURANT_OWNER])
        self.assertEqual(entry['restaurant'], 'Claimed Cafe')
        self.assertTrue(
            entry['permissions']['settings'],
            'owner authority must resolve, not merely the membership row',
        )

        # The claim established the identity, and the profile says so without ever
        # carrying the access-state field itself.
        self.assertFalse(profile['prompt_password_change'])
        self.assertNotIn('customer_access_state', profile)

    def test_the_restaurant_id_is_context_and_the_resolver_is_authority(self):
        """
        The claim names ONE restaurant; the profile reports every membership.

        This is why the client must not synthesise ``restaurant_roles`` from
        ``restaurant_id``: an owner who already held a restaurant would silently lose
        it from their own principal.
        """
        from rest_framework.test import APIClient

        other = make_restaurant(self.owner, name='Held Already')
        employ(self.owner, other, [RESTAURANT_MANAGER])

        api = APIClient()
        self.assertEqual(
            api.post(
                '/api/v1/users/owner-claim/challenge/', {}, format='json',
                headers={'X-Owner-Claim-Token': self.claim_token},
            ).status_code,
            200,
        )
        redeemed = api.post(
            '/api/v1/users/owner-claim/redeem/',
            {'otp': '1234', 'new_password': 'Kabalagala-Sunrise-7'}, format='json',
            headers={'X-Owner-Claim-Token': self.claim_token},
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.content)
        claim = redeemed.json()['data']

        profile = self.assertCanonicalEnvelope(self.client.get(
            PROFILE_URL, HTTP_AUTHORIZATION=f"Bearer {claim['token']}",
        ))
        self.assertEqual(
            {entry['restaurant_id'] for entry in profile['restaurant_roles']},
            {str(self.restaurant.id), str(other.id)},
        )

    def test_the_bootstrap_read_needs_no_second_otp(self):
        """
        The reason redemption is not followed by ordinary login.

        An owner membership sets ``require_otp`` in ``login``, so sending a
        freshly-claimed owner back through it would demand a SECOND code moments
        after the claim transaction consumed its own. The bootstrap read asks for
        nothing: one request, one 200, and no OTP row created by it.
        """
        from rest_framework.test import APIClient
        from users_app.models import UserOtp

        api = APIClient()
        self.assertEqual(
            api.post(
                '/api/v1/users/owner-claim/challenge/', {}, format='json',
                headers={'X-Owner-Claim-Token': self.claim_token},
            ).status_code,
            200,
        )
        claim = api.post(
            '/api/v1/users/owner-claim/redeem/',
            {'otp': '1234', 'new_password': 'Kabalagala-Sunrise-7'}, format='json',
            headers={'X-Owner-Claim-Token': self.claim_token},
        ).json()['data']

        before = UserOtp.objects.count()
        response = self.client.get(
            PROFILE_URL, HTTP_AUTHORIZATION=f"Bearer {claim['token']}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UserOtp.objects.count(), before)
        self.assertNotIn('require_otp', response.json()['data'])

        # And the control: ordinary login for this same owner DOES stop for an OTP,
        # which is the cost the bootstrap read avoids.
        with mock.patch(
            'users_app.controllers.otp_manager.OtpManager.make_otp', return_value=True,
        ):
            relogin = login(
                username=self.owner.username, password='Kabalagala-Sunrise-7',
            )
        self.assertTrue(relogin['data']['require_otp'])
        self.assertNotIn('token', relogin['data'])
