"""
``POST admin/v1/restaurants/<uuid>/owner-invitation/{reissue,cancel}/`` — Step 2E.

The CONTROL-PLANE suite: authority, CSRF, the request contract, exactly one audit row
per unsafe request, the transaction that binds the mutation to that row, the no-store
response carrying the only copy of a credential, and the read/write agreement that
makes ``expected_invitation_id`` mean the same thing on both sides.

The domain rules — which states each operation is legitimate from, owner binding, the
supersede-before-insert step, the exact-retry rule — belong to
``platform_admin_app.onboarding_invitations`` and are covered by
``tests_owner_invitation_lifecycle``. What is pinned HERE is that they are reached
through HTTP unchanged and that the adapter adds nothing of its own to them.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
    RESTAURANT_OWNER,
)
from platform_admin_app import onboarding_creation, sessions
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED,
    ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED,
)
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import (
    ONBOARDING_SOURCE_LEGACY_ADOPTED,
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    AdminAuditLog,
    OwnerInvitation,
    RestaurantOnboarding,
)
from platform_admin_app.onboarding_creation import NewOwner
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User

PASSWORD = 'correct-horse-battery'
REASON = 'Rotating the claim credential after the operator lost the response.'

# Distinct phone range from every other admin suite.
_PHONE = iter(f'0772{n:06d}' for n in range(820000, 829999))

# The two actions this suite is about. Counting these rather than clearing the
# table is not merely convenient — ``AdminAuditLog`` REFUSES a bulk delete
# (``AppendOnlyViolation``), which is the model doing exactly its job. Filtering
# by action is also more precise: the creation fixture's own entry is a different
# action and can never be mistaken for one of these.
STEP_2E_ACTIONS = (
    ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED,
    ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED,
)


def lifecycle_audits():
    """Every Step-2E audit entry, newest first (the model's own ordering)."""
    return AdminAuditLog.objects.filter(action__in=STEP_2E_ACTIONS)


_ADMIN_OVERRIDES = dict(
    ROOT_URLCONF='dinify_backend.urls_admin',
    MIDDLEWARE=[
        'platform_admin_app.middleware.RequestIDMiddleware',
        'platform_admin_app.middleware.ClientIPMiddleware',
        *dj_settings.MIDDLEWARE,
    ],
    REST_FRAMEWORK={
        **dj_settings.REST_FRAMEWORK,
        'DEFAULT_AUTHENTICATION_CLASSES': (
            'platform_admin_app.authentication.AdminSessionAuthentication',
        ),
        'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
    },
    ALLOWED_HOSTS=['testserver', 'admin.dinifyapp.com'],
)


def _make_admin(email='oie-admin@t.com', username='oie-admin'):
    return User.objects.create_user(
        first_name='Ada', last_name='Min', email=email, username=username,
        country='UG', password=PASSWORD, roles=[],
        account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
    )


def _reissue_url(restaurant):
    return f'/admin/v1/restaurants/{restaurant.id}/owner-invitation/reissue/'


def _cancel_url(restaurant):
    return f'/admin/v1/restaurants/{restaurant.id}/owner-invitation/cancel/'


def _detail_url(restaurant):
    return f'/admin/v1/restaurants/{restaurant.id}/'


@override_settings(**_ADMIN_OVERRIDES)
class _EndpointTestCase(AuditAssertionsMixin, TestCase):
    """An authenticated, RECENTLY ELEVATED admin session on an admin-created tenant."""

    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

        self.creation = onboarding_creation.create_admin_restaurant(
            name='Kampala Bistro', location='Kololo', is_test=False,
            owner=NewOwner('Jane', 'Doe', next(_PHONE), None),
            actor=self.admin, reason='Creating after the signed agreement.',
        )
        self.restaurant = self.creation.restaurant
        self.owner = self.creation.owner
        self.invitation = self.creation.invitation
        # The creation fixture audited once under its own action. Nothing is deleted:
        # the log is append-only by design, so this suite counts the two Step-2E
        # actions instead.

    # --- helpers ---

    def body(self, **overrides):
        payload = {
            'expected_invitation_id': str(self.invitation.id),
            'reason': REASON,
        }
        payload.update(overrides)
        return payload

    def post(self, url, body=None, **kwargs):
        return self.client.post(
            url,
            data=self.body() if body is None else body,
            content_type='application/json',
            **kwargs,
        )

    def reissue(self, body=None, **kwargs):
        return self.post(_reissue_url(self.restaurant), body, **kwargs)

    def cancel(self, body=None, **kwargs):
        return self.post(_cancel_url(self.restaurant), body, **kwargs)

    def assertLifecycleAudits(self, expected):
        actual = lifecycle_audits().count()
        self.assertEqual(
            actual, expected,
            f'expected {expected} Step-2E audit entries, found {actual}: '
            f'{list(lifecycle_audits().values_list("action", "result"))}',
        )

    def detail_onboarding(self):
        response = self.client.get(_detail_url(self.restaurant))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']['onboarding']


# --- §35 authority ------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class AuthorityTests(AuditAssertionsMixin, TestCase):
    """
    Both routes, every authority bar. The session in this class starts UNELEVATED so
    the step-up requirement is proved rather than assumed.
    """

    def setUp(self):
        super().setUp()
        self.admin = _make_admin(email='oie-auth@t.com', username='oie-auth')
        raw, self.session = sessions.create_session(self.admin)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Auth Cafe', location='Ntinda', is_test=False,
            owner=NewOwner('Sam', 'Kato', next(_PHONE), None),
            actor=self.admin, reason='Creating the authority fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.payload = {
            'expected_invitation_id': str(self.creation.invitation.id),
            'reason': REASON,
        }

    def _urls(self):
        return (
            (_reissue_url(self.restaurant),
             ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED),
            (_cancel_url(self.restaurant),
             ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED),
        )

    def test_anonymous_is_401_and_writes_no_audit_row(self):
        """
        A 401 is about identity, not an administrative decision — auditing it would
        manufacture a denial row for a caller the plane cannot name.
        """
        for url, _ in self._urls():
            response = Client().post(
                url, data=self.payload, content_type='application/json',
            )
            self.assertEqual(response.status_code, 401, url)
        self.assertEqual(lifecycle_audits().count(), 0)

    def test_an_unelevated_session_is_403_with_exactly_one_denial_audit(self):
        for url, action in self._urls():
            response = self.client.post(
                url, data=self.payload, content_type='application/json',
            )
            self.assertEqual(response.status_code, 403, response.content)
            entry = self.assertAudited(action, result=RESULT_DENIED)
            self.assertEqual(entry.error_code, 'elevation_required')
            self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))

    def test_a_denial_records_nothing_from_the_body(self):
        """
        The body is never read on the denial path: a refusal is recorded from the
        request's authority, not from a payload the endpoint declined to act on.
        """
        self.client.post(
            _reissue_url(self.restaurant),
            data={'reason': 'a stated reason that is long enough',
                  'expected_invitation_id': str(self.creation.invitation.id)},
            content_type='application/json',
        )
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_DENIED,
        )
        self.assertNotIn('a stated reason', entry.reason)

    def test_stale_elevation_is_403(self):
        sessions.elevate(self.session)
        window = getattr(dj_settings, 'ADMIN_ELEVATION_WINDOW', timedelta(minutes=5))
        self.session.elevated_at = timezone.now() - window - timedelta(minutes=1)
        self.session.save(update_fields=['elevated_at'])
        for url, _ in self._urls():
            response = self.client.post(
                url, data=self.payload, content_type='application/json',
            )
            self.assertEqual(response.status_code, 403, response.content)

    def test_a_valid_elevation_is_accepted(self):
        sessions.elevate(self.session)
        response = self.client.post(
            _reissue_url(self.restaurant), data=self.payload,
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_restaurant_user_can_never_reach_these_routes(self):
        """
        A session row is MINTED for the restaurant owner directly, going around the
        login flow entirely — the strongest form of the question, because it asks what
        happens if such a row somehow exists rather than whether one can be obtained.
        ``AdminSessionAuthentication`` refuses it on ``account_type``, so neither route
        is reachable and neither writes an audit row.
        """
        owner = self.creation.owner
        self.assertEqual(owner.account_type, ACCOUNT_TYPE_RESTAURANT_USER)
        raw, _ = sessions.create_session(owner)
        client = Client()
        client.cookies[cookie_name()] = raw
        for url, _action in self._urls():
            response = client.post(
                url, data=self.payload, content_type='application/json',
            )
            self.assertEqual(response.status_code, 401, f'{url}: {response.content}')
        self.assertEqual(lifecycle_audits().count(), 0)
        self.assertIsNone(
            OwnerInvitation.objects.get(
                pk=self.creation.invitation.pk,
            ).cancelled_at,
        )

    def test_neither_route_is_reachable_from_the_customer_urlconf(self):
        """
        The delegated-access allowlist names CUSTOMER-plane routes, and these live on
        the admin urlconf — so a delegated session cannot reach them however its scope
        is configured. Proved by resolution rather than by reading the allowlist.
        """
        from django.urls import Resolver404, resolve
        for url, _ in self._urls():
            with override_settings(ROOT_URLCONF='dinify_backend.urls'):
                with self.assertRaises(Resolver404, msg=url):
                    resolve(url)

    def test_the_delegated_allowlist_does_not_mention_the_routes(self):
        from platform_admin_app.configs.delegation_scopes import ALLOWED_ROUTES
        for entry in ALLOWED_ROUTES:
            self.assertNotIn('owner-invitation', str(entry))
            self.assertNotIn('owner_invitation', str(entry))


@override_settings(**_ADMIN_OVERRIDES)
class CsrfTests(TestCase):
    """
    CSRF is the existing admin policy, enforced inside ``AdminSessionAuthentication``.

    ``enforce_csrf_checks=True`` is load-bearing: the DEFAULT test client sets
    ``_dont_enforce_csrf_checks`` and would prove nothing at all.
    """

    def setUp(self):
        super().setUp()
        self.admin = _make_admin(email='oie-csrf@t.com', username='oie-csrf')
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client(enforce_csrf_checks=True)
        self.client.cookies[cookie_name()] = raw
        self.creation = onboarding_creation.create_admin_restaurant(
            name='Csrf Cafe', location='Muyenga', is_test=False,
            owner=NewOwner('Ola', 'Nsubuga', next(_PHONE), None),
            actor=self.admin, reason='Creating the CSRF fixture.',
        )
        self.restaurant = self.creation.restaurant
        self.payload = {
            'expected_invitation_id': str(self.creation.invitation.id),
            'reason': REASON,
        }

    def _post(self, url, **kwargs):
        return self.client.post(
            url, data=self.payload, content_type='application/json', **kwargs,
        )

    def _issued_token(self):
        """Have the SERVER issue a token, exactly as the SPA obtains one."""
        response = self.client.get('/admin/v1/auth/session/')
        self.assertEqual(response.status_code, 200, response.content)
        return self.client.cookies[dj_settings.CSRF_COOKIE_NAME].value

    def test_a_missing_csrf_token_is_refused_on_both_routes(self):
        for url in (_reissue_url(self.restaurant), _cancel_url(self.restaurant)):
            self.assertEqual(self._post(url).status_code, 403, url)
        self.assertIsNone(
            OwnerInvitation.objects.get(pk=self.creation.invitation.pk).cancelled_at,
        )

    def test_a_wrong_csrf_token_is_refused(self):
        self._issued_token()
        response = self._post(
            _reissue_url(self.restaurant),
            HTTP_X_CSRFTOKEN='not-the-token-the-server-issued',
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(OwnerInvitation.objects.count(), 1)

    def test_the_server_issued_token_is_accepted(self):
        token = self._issued_token()
        response = self._post(
            _reissue_url(self.restaurant), HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_csrf_failure_writes_no_audit_row(self):
        """Refused inside authentication, before any administrative decision exists."""
        self._post(_reissue_url(self.restaurant))
        self.assertEqual(lifecycle_audits().count(), 0)


# --- §35 the request contract -------------------------------------------------

class RequestContractTests(_EndpointTestCase):

    def _assert_400(self, response, field):
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(field, response.json()['errors'])

    def test_an_omitted_expected_id_is_a_400_on_both_routes(self):
        body = {'reason': REASON}
        self._assert_400(self.reissue(body), 'expected_invitation_id')
        self._assert_400(self.cancel(body), 'expected_invitation_id')

    def test_an_omitted_expected_id_never_acts_on_the_current_invitation(self):
        """
        The whole point of the field. A "whatever is current" fallback would hand a
        forgetful client the assertion the field exists to make them state.
        """
        self.reissue({'reason': REASON})
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.superseded_at)
        self.assertEqual(OwnerInvitation.objects.count(), 1)

    def test_a_null_expected_id_is_a_400(self):
        self._assert_400(
            self.reissue(self.body(expected_invitation_id=None)),
            'expected_invitation_id',
        )

    def test_a_blank_expected_id_is_a_400(self):
        self._assert_400(
            self.reissue(self.body(expected_invitation_id='   ')),
            'expected_invitation_id',
        )

    def test_a_numeric_expected_id_is_a_400_and_never_a_409(self):
        """
        A DRF ``UUIDField`` would evaluate ``uuid.UUID(int=42)`` and produce a
        well-formed UUID no invitation has carried — the request would then miss under
        the lock and come back a CONFLICT. A conflict is the one error here that means
        the world moved, and it must never be manufactured by a coercion table.
        """
        response = self.reissue(self.body(expected_invitation_id=42))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('expected_invitation_id', response.json()['errors'])

    def test_a_boolean_expected_id_is_a_400(self):
        self._assert_400(
            self.reissue(self.body(expected_invitation_id=True)),
            'expected_invitation_id',
        )

    def test_a_malformed_uuid_string_is_a_400(self):
        self._assert_400(
            self.reissue(self.body(expected_invitation_id='not-a-uuid')),
            'expected_invitation_id',
        )

    def test_a_missing_reason_is_a_400_on_both_routes(self):
        body = {'expected_invitation_id': str(self.invitation.id)}
        self._assert_400(self.reissue(body), 'reason')
        self._assert_400(self.cancel(body), 'reason')

    def test_a_short_reason_is_a_400(self):
        self._assert_400(self.reissue(self.body(reason='too short')), 'reason')

    def test_a_whitespace_only_reason_is_a_400(self):
        self._assert_400(self.reissue(self.body(reason='          ')), 'reason')

    def test_an_over_long_reason_is_a_400(self):
        self._assert_400(self.reissue(self.body(reason='x' * 1001)), 'reason')

    def test_malformed_json_is_a_400_with_the_house_envelope(self):
        response = self.client.post(
            _reissue_url(self.restaurant), data='{not json',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('__all__', response.json()['errors'])

    def test_an_unsupported_media_type_is_a_415(self):
        """
        ``UnsupportedMediaType`` is NOT a ``ParseError`` subclass, and folding one into
        the other deletes the clue: a 400 says the body was wrong, a 415 says send JSON.
        """
        response = self.client.post(
            _reissue_url(self.restaurant), data='<xml/>', content_type='application/xml',
        )
        self.assertEqual(response.status_code, 415, response.content)
        self.assertIn('__all__', response.json()['errors'])

    def test_an_invalid_body_writes_no_domain_change(self):
        self.reissue(self.body(reason='no'))
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.superseded_at)
        self.assertEqual(OwnerInvitation.objects.count(), 1)


# --- §22 target resolution ----------------------------------------------------

class TargetResolutionTests(_EndpointTestCase):
    """
    The target is resolved BEFORE the body is read, so these routes cannot be used to
    probe which restaurant ids are real.
    """

    def _absent(self):
        import uuid
        return f'/admin/v1/restaurants/{uuid.uuid4()}/owner-invitation/reissue/'

    def test_an_unknown_restaurant_is_a_silent_404(self):
        response = self.client.post(
            self._absent(), data=self.body(), content_type='application/json',
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_an_unknown_restaurant_writes_no_audit_row(self):
        self.client.post(
            self._absent(), data=self.body(), content_type='application/json',
        )
        self.assertEqual(lifecycle_audits().count(), 0)

    def test_a_soft_deleted_restaurant_is_the_same_404(self):
        Restaurant.objects.filter(pk=self.restaurant.pk).update(deleted=True)
        for url in (_reissue_url(self.restaurant), _cancel_url(self.restaurant)):
            response = self.client.post(
                url, data=self.body(), content_type='application/json',
            )
            self.assertEqual(response.status_code, 404, url)
        self.assertEqual(lifecycle_audits().count(), 0)

    def test_a_malformed_body_against_a_missing_target_is_still_a_404(self):
        """
        Body validation must never become an existence oracle: an unreadable body aimed
        at a restaurant that does not exist answers 404, exactly as a valid one does.
        """
        response = self.client.post(
            self._absent(), data='{not json', content_type='application/json',
        )
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(lifecycle_audits().count(), 0)

    def test_an_unsupported_media_type_against_a_missing_target_is_a_404(self):
        response = self.client.post(
            self._absent(), data='<xml/>', content_type='application/xml',
        )
        self.assertEqual(response.status_code, 404, response.content)


# --- §20 / §14 the reissue success contract ----------------------------------

class ReissueSuccessTests(_EndpointTestCase):

    def setUp(self):
        super().setUp()
        self.response = self.reissue()
        self.assertEqual(self.response.status_code, 200, self.response.content)
        self.payload = self.response.json()
        self.data = self.payload['data']
        self.invitation.refresh_from_db()

    def test_the_house_envelope(self):
        self.assertEqual(self.payload['status'], 200)
        self.assertEqual(self.payload['message'], 'Owner invitation reissued.')
        self.assertIs(self.data['changed'], True)

    def test_the_raw_claim_token_is_returned_once(self):
        token = self.data['owner_invitation']['claim_token']
        self.assertTrue(token)
        self.assertEqual(len(token), len(self.creation.claim_token))

    def test_the_persisted_hash_matches_the_returned_token(self):
        new_id = self.data['owner_invitation']['id']
        row = OwnerInvitation.objects.get(pk=new_id)
        self.assertEqual(
            row.token_hash,
            sessions.hash_token(self.data['owner_invitation']['claim_token']),
        )

    def test_the_raw_token_is_in_no_database_column(self):
        token = self.data['owner_invitation']['claim_token']
        for row in OwnerInvitation.objects.all():
            self.assertNotIn(token, row.token_hash)

    def test_the_raw_token_is_in_no_audit_row(self):
        token = self.data['owner_invitation']['claim_token']
        for entry in AdminAuditLog.objects.all():
            blob = json.dumps({
                'before': entry.before_state, 'after': entry.after_state,
                'reason': entry.reason, 'error_code': entry.error_code,
            })
            self.assertNotIn(token, blob)

    def test_no_token_hash_reaches_the_response(self):
        body = json.dumps(self.payload)
        for row in OwnerInvitation.objects.all():
            self.assertNotIn(row.token_hash, body)

    def test_no_claim_url_is_fabricated(self):
        body = json.dumps(self.payload).lower()
        for term in ('claim_url', 'claim_link', 'invite_url', 'http://', 'https://'):
            self.assertNotIn(term, body, f'{term!r} leaked into the response')

    def test_the_response_is_no_store_private(self):
        self.assertEqual(self.response['Cache-Control'], 'no-store, private')

    def test_the_response_sets_pragma_no_cache(self):
        self.assertEqual(self.response['Pragma'], 'no-cache')

    def test_the_response_sets_expires_zero(self):
        self.assertEqual(self.response['Expires'], '0')

    def test_the_credential_lives_in_its_own_object(self):
        """
        Never merged into ``onboarding``, so no future change to the canonical
        projection can start carrying it by accident.
        """
        self.assertNotIn('claim_token', json.dumps(self.data['onboarding']))
        self.assertIn('claim_token', self.data['owner_invitation'])

    def test_the_onboarding_object_is_the_canonical_projection(self):
        self.assertEqual(self.data['onboarding'], self.detail_onboarding())

    def test_the_old_invitation_is_superseded(self):
        self.assertIsNotNone(self.invitation.superseded_at)

    def test_exactly_one_audit_row(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_SUCCESS,
        )
        self.assertLifecycleAudits(1)
        self.assertEqual(entry.reason, REASON)
        self.assertEqual(str(entry.restaurant_id), str(self.restaurant.id))

    def test_the_audit_states_name_the_two_invitations(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_SUCCESS,
        )
        before = entry.before_state['owner_invitation']
        after = entry.after_state['owner_invitation']
        self.assertEqual(before['id'], str(self.invitation.id))
        self.assertEqual(before['status'], 'pending')
        self.assertEqual(after['id'], self.data['owner_invitation']['id'])
        self.assertEqual(after['status'], 'pending')

    def test_the_audit_snapshot_carries_no_owner_pii(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_SUCCESS,
        )
        blob = json.dumps([entry.before_state, entry.after_state])
        self.assertNotIn(self.owner.email or '@@', blob)
        self.assertNotIn(self.owner.phone_number, blob)
        self.assertNotIn(str(self.owner.id), blob)
        self.assertNotIn('Jane', blob)

    def test_the_audit_snapshot_keys_are_exactly_the_safe_four(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_SUCCESS,
        )
        for state in (entry.before_state, entry.after_state):
            self.assertEqual(set(state), {'owner_invitation'})
            self.assertEqual(
                set(state['owner_invitation']),
                {'status', 'id', 'issued_at', 'expires_at'},
            )


class ReissueFromExpiredRecordsExpiredTests(_EndpointTestCase):
    """A rotation out of expired must not be logged as a rotation out of pending."""

    def test_the_before_state_reads_expired(self):
        past = timezone.now() - timedelta(days=3)
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=past - timedelta(days=1), expires_at=past,
        )
        self.assertEqual(self.reissue().status_code, 200)
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.before_state['owner_invitation']['status'], 'expired')


# --- §20 the cancel success contract -----------------------------------------

class CancelSuccessTests(_EndpointTestCase):

    def setUp(self):
        super().setUp()
        self.response = self.cancel()
        self.assertEqual(self.response.status_code, 200, self.response.content)
        self.payload = self.response.json()
        self.data = self.payload['data']
        self.invitation.refresh_from_db()

    def test_the_house_envelope(self):
        self.assertEqual(self.payload['status'], 200)
        self.assertEqual(self.payload['message'], 'Owner invitation cancelled.')
        self.assertIs(self.data['changed'], True)

    def test_the_response_carries_no_credential_of_any_kind(self):
        body = json.dumps(self.payload).lower()
        for term in ('claim_token', 'token', 'claim_url', 'credential', 'secret'):
            self.assertNotIn(term, body, f'{term!r} leaked into a cancel response')
        self.assertNotIn('owner_invitation', self.data)

    def test_no_token_hash_reaches_the_response(self):
        self.assertNotIn(self.invitation.token_hash, json.dumps(self.payload))

    def test_the_onboarding_object_is_the_canonical_projection(self):
        self.assertEqual(self.data['onboarding'], self.detail_onboarding())

    def test_the_invitation_is_cancelled_and_attributed(self):
        self.assertIsNotNone(self.invitation.cancelled_at)
        self.assertEqual(self.invitation.cancelled_by_id, self.admin.id)

    def test_exactly_one_audit_row(self):
        self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED, result=RESULT_SUCCESS,
        )
        self.assertLifecycleAudits(1)

    def test_the_audit_states_describe_one_invitation_moving_to_cancelled(self):
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED, result=RESULT_SUCCESS,
        )
        before = entry.before_state['owner_invitation']
        after = entry.after_state['owner_invitation']
        self.assertEqual(before['id'], after['id'], str(self.invitation.id))
        self.assertEqual(before['status'], 'pending')
        self.assertEqual(after['status'], 'cancelled')


class CancelExactRetryEndpointTests(_EndpointTestCase):
    """§18, through HTTP."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.cancel().status_code, 200)
        self.invitation.refresh_from_db()
        self.original_at = self.invitation.cancelled_at
        self.original_by = self.invitation.cancelled_by_id
        self.retry = self.cancel()

    def test_the_retry_is_a_200_with_changed_false(self):
        self.assertEqual(self.retry.status_code, 200, self.retry.content)
        self.assertIs(self.retry.json()['data']['changed'], False)

    def test_the_stamp_and_actor_do_not_move(self):
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.cancelled_at, self.original_at)
        self.assertEqual(self.invitation.cancelled_by_id, self.original_by)

    def test_the_retry_is_still_audited_but_shows_no_domain_change(self):
        """
        The request happened, so the plane's one-entry-per-unsafe-request convention
        holds — but the states must truthfully show that nothing moved, or the log
        would say the credential was cancelled twice.
        """
        entries = lifecycle_audits().filter(
            action=ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED,
            result=RESULT_SUCCESS,
        ).order_by('created_at')
        self.assertEqual(entries.count(), 2, 'both requests should be recorded')
        entry = entries.last()
        self.assertEqual(entry.before_state, entry.after_state)
        self.assertEqual(
            entry.before_state['owner_invitation']['status'], 'cancelled',
        )


# --- §21 the conflict map -----------------------------------------------------

class ConflictTests(_EndpointTestCase):

    def _assert_409(self, response, code):
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body['code'], code)
        self.assertIn('message', body)
        return body

    def test_a_stale_expected_id_is_a_409_on_both_routes(self):
        fresh = self.reissue().json()['data']['owner_invitation']['id']
        self._assert_409(self.reissue(), 'stale_owner_invitation')
        self._assert_409(self.cancel(), 'stale_owner_invitation')
        # And the newer credential was not touched by either stale request.
        row = OwnerInvitation.objects.get(pk=fresh)
        self.assertIsNone(row.superseded_at)
        self.assertIsNone(row.cancelled_at)

    def test_a_conflict_body_carries_no_current_invitation_id(self):
        """
        The remedy is to reload the detail read, which shows the id beside the state
        that makes it meaningful — not to retry blindly against an id the error handed
        back.
        """
        self.reissue()
        body = self._assert_409(self.cancel(), 'stale_owner_invitation')
        self.assertNotIn('details', body)
        current = OwnerInvitation.objects.exclude(pk=self.invitation.pk).get()
        self.assertNotIn(str(current.id), json.dumps(body))

    def test_a_conflict_leaks_no_token_hash(self):
        self.reissue()
        body = json.dumps(self._assert_409(self.cancel(), 'stale_owner_invitation'))
        for row in OwnerInvitation.objects.all():
            self.assertNotIn(row.token_hash, body)

    def test_a_conflict_is_audited_once_as_a_failure(self):
        self.reissue()
        self.cancel()
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED, result=RESULT_FAILURE,
        )
        self.assertEqual(
            lifecycle_audits().filter(
                action=ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED,
            ).count(),
            1,
        )
        self.assertEqual(entry.error_code, 'stale_owner_invitation')
        self.assertEqual(entry.reason, REASON)
        self.assertIsNone(entry.after_state)

    def test_a_failure_before_state_is_this_restaurants_real_head(self):
        """
        Read by the endpoint from the canonical selector — never reconstructed from
        the exception's details, which can name a row the caller has no business being
        handed.
        """
        new_id = self.reissue().json()['data']['owner_invitation']['id']
        self.cancel()
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_CANCELLED, result=RESULT_FAILURE,
        )
        self.assertEqual(entry.before_state['owner_invitation']['id'], new_id)

    def test_a_consumed_invitation_cannot_be_cancelled(self):
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        self._assert_409(self.cancel(), 'owner_invitation_already_resolved')

    def test_reissue_is_refused_once_the_current_owner_has_claimed(self):
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        self._assert_409(self.reissue(), 'owner_control_already_established')

    def test_a_legacy_adopted_restaurant_is_a_409_on_both_routes(self):
        other = Restaurant.objects.create(
            name='Legacy Ltd', location='Ntinda', country='UG', owner=self.owner,
        )
        RestaurantOnboarding.objects.create(
            restaurant=other, source=ONBOARDING_SOURCE_LEGACY_ADOPTED,
            adopted_at=timezone.now(), adopted_by=self.admin,
        )
        for url in (_reissue_url(other), _cancel_url(other)):
            response = self.client.post(
                url, data=self.body(), content_type='application/json',
            )
            self._assert_409(response, 'owner_invitation_not_applicable')

    def test_an_untracked_restaurant_is_a_409(self):
        other = Restaurant.objects.create(
            name='Untracked Ltd', location='Bugolobi', country='UG',
            owner=self.owner,
        )
        response = self.client.post(
            _reissue_url(other), data=self.body(), content_type='application/json',
        )
        self._assert_409(response, 'onboarding_not_tracked')

    def test_an_untracked_failure_carries_no_before_state(self):
        """There was no invitation lifecycle to be in, so there is no state to report."""
        other = Restaurant.objects.create(
            name='Untracked Two', location='Bugolobi', country='UG',
            owner=self.owner,
        )
        self.client.post(
            _reissue_url(other), data=self.body(), content_type='application/json',
        )
        entry = self.assertAudited(
            ADMIN_RESTAURANT_OWNER_INVITATION_REISSUED, result=RESULT_FAILURE,
        )
        self.assertIsNone(entry.before_state)

    def test_owner_inconsistency_is_a_409_on_reissue_only(self):
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)
        self._assert_409(self.reissue(), 'missing_owner_membership')
        # Cancellation stays available while the tenant's ownership is broken.
        self.assertEqual(self.cancel().status_code, 200)

    def test_an_inactive_owner_is_a_409_on_reissue(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self._assert_409(self.reissue(), 'owner_account_inactive')


# --- §33 read/write agreement -------------------------------------------------

class ReadWriteAgreementTests(_EndpointTestCase):
    """
    §33: for every supported state, the id the detail read publishes is EXACTLY the id
    the write endpoints accept, and a write's canonical projection equals the next
    GET's. No second interpretation of "the current invitation" exists.
    """

    def _head_id(self):
        return self.detail_onboarding()['invitation']['id']

    def _head_status(self):
        return self.detail_onboarding()['invitation']['status']

    def test_pending(self):
        self.assertEqual(self._head_status(), 'pending')
        self.assertEqual(self._head_id(), str(self.invitation.id))
        response = self.cancel(self.body(expected_invitation_id=self._head_id()))
        self.assertEqual(response.status_code, 200, response.content)

    def test_expired(self):
        past = timezone.now() - timedelta(days=3)
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            issued_at=past - timedelta(days=1), expires_at=past,
        )
        self.assertEqual(self._head_status(), 'expired')
        self.assertEqual(self._head_id(), str(self.invitation.id))
        response = self.reissue(self.body(expected_invitation_id=self._head_id()))
        self.assertEqual(response.status_code, 200, response.content)

    def test_cancelled(self):
        self.cancel()
        self.assertEqual(self._head_status(), 'cancelled')
        self.assertEqual(self._head_id(), str(self.invitation.id))
        response = self.reissue(self.body(expected_invitation_id=self._head_id()))
        self.assertEqual(response.status_code, 200, response.content)

    def test_reissued_pending(self):
        new_id = self.reissue().json()['data']['owner_invitation']['id']
        self.assertEqual(self._head_status(), 'pending')
        self.assertEqual(self._head_id(), new_id)
        response = self.cancel(self.body(expected_invitation_id=new_id))
        self.assertEqual(response.status_code, 200, response.content)

    def test_historical_consumed_with_a_new_current_owner(self):
        OwnerInvitation.objects.filter(pk=self.invitation.pk).update(
            consumed_at=timezone.now(),
        )
        successor = User.objects.create_user(
            first_name='New', last_name='Owner', email='successor@t.com',
            phone_number=next(_PHONE), username=next(_PHONE), country='UG',
            password=PASSWORD, roles=[], account_type=ACCOUNT_TYPE_RESTAURANT_USER,
        )
        RestaurantEmployee.objects.filter(
            restaurant=self.restaurant, user=self.owner,
        ).update(active=False)
        RestaurantEmployee.objects.create(
            user=successor, restaurant=self.restaurant, roles=[RESTAURANT_OWNER],
            active=True,
        )
        Restaurant.objects.filter(pk=self.restaurant.pk).update(owner=successor)

        self.assertEqual(self._head_status(), 'consumed')
        self.assertEqual(self._head_id(), str(self.invitation.id))
        response = self.reissue(self.body(expected_invitation_id=self._head_id()))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            OwnerInvitation.objects.get(
                pk=response.json()['data']['owner_invitation']['id'],
            ).invited_user_id,
            successor.id,
        )

    def test_a_write_response_projection_equals_the_next_get(self):
        written = self.reissue().json()['data']['onboarding']
        self.assertEqual(written, self.detail_onboarding())

    def test_the_cancel_response_projection_equals_the_next_get(self):
        written = self.cancel().json()['data']['onboarding']
        self.assertEqual(written, self.detail_onboarding())


# --- §15 the lost-response rotation sequence ---------------------------------

class LostResponseRecoveryTests(_EndpointTestCase):
    """
    §15 — THE LOAD-BEARING PROPERTY.

    A committed reissue whose response was lost leaves a credential nobody knows. The
    server cannot reconstruct it, must not store plaintext, and must not pretend a
    retry returns the same token. The recovery is: retry -> 409, reload, reissue the
    NEW head, which supersedes the unknown credential and mints a known one.
    """

    def test_the_full_sequence(self):
        # 1. A reissue commits. Imagine its response never arrived.
        lost = self.reissue()
        self.assertEqual(lost.status_code, 200)
        lost_id = lost.json()['data']['owner_invitation']['id']
        lost_token = lost.json()['data']['owner_invitation']['claim_token']

        # 2. The client retries with the id it still believes is current.
        retry = self.reissue()
        self.assertEqual(retry.status_code, 409, retry.content)
        self.assertEqual(retry.json()['code'], 'stale_owner_invitation')

        # 3. It reloads. The canonical read now shows the credential it never saw.
        self.assertEqual(self.detail_onboarding()['invitation']['id'], lost_id)

        # 4. It deliberately reissues THAT one, invalidating the unknown credential.
        recovery = self.reissue(self.body(expected_invitation_id=lost_id))
        self.assertEqual(recovery.status_code, 200, recovery.content)
        recovered_token = recovery.json()['data']['owner_invitation']['claim_token']

        # The unknown credential is dead, and the new one is genuinely new.
        self.assertIsNotNone(OwnerInvitation.objects.get(pk=lost_id).superseded_at)
        self.assertNotEqual(recovered_token, lost_token)

    def test_the_lost_token_is_never_recoverable_from_the_platform(self):
        lost = self.reissue().json()['data']['owner_invitation']
        token = lost['claim_token']
        # Not in any column of any row.
        for row in OwnerInvitation.objects.all():
            self.assertNotIn(token, f'{row.token_hash}{row.id}')
        # Not in any audit entry.
        for entry in AdminAuditLog.objects.all():
            self.assertNotIn(
                token, json.dumps([entry.before_state, entry.after_state]),
            )
        # And the detail read never republishes it.
        self.assertNotIn(token, json.dumps(self.detail_onboarding()))

    def test_a_retry_never_returns_the_hash_as_a_credential(self):
        lost = self.reissue().json()['data']['owner_invitation']
        row = OwnerInvitation.objects.get(pk=lost['id'])
        retry = self.reissue()
        self.assertNotIn(row.token_hash, retry.content.decode())


# --- §25 audit atomicity ------------------------------------------------------

class AuditAtomicityTests(_EndpointTestCase):
    """
    Domain mutation + audit share ONE outer transaction. A failed audit rolls the
    credential change back, because a rotation nobody can be shown to have decided must
    not be allowed to stand.

    The fault is injected at ``AdminAuditLog.objects.create``, i.e. AFTER the domain
    genuinely mutated — the guard tests below prove that ordering rather than assuming
    it.
    """

    def _explode_audit(self):
        return patch(
            'platform_admin_app.models.AdminAuditLog.objects.create',
            side_effect=RuntimeError('audit write failed'),
        )

    def test_a_failed_audit_rolls_a_reissue_back(self):
        with self._explode_audit():
            with self.assertRaises(RuntimeError):
                self.reissue()
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.superseded_at)
        self.assertEqual(OwnerInvitation.objects.count(), 1)
        self.assertEqual(lifecycle_audits().count(), 0)

    def test_a_failed_audit_rolls_a_cancellation_back(self):
        with self._explode_audit():
            with self.assertRaises(RuntimeError):
                self.cancel()
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.cancelled_at)
        self.assertIsNone(self.invitation.cancelled_by_id)
        self.assertEqual(lifecycle_audits().count(), 0)

    def _audit_fails_after_observing(self, observe):
        """
        A fault injected at the audit write that first RECORDS what the domain had
        already done, then raises.

        Written as a real function rather than an expression: an ``or``-chained
        lambda short-circuits the moment the observation is truthy, so the exception
        never fires and the guard silently passes without guarding anything.
        """
        seen = {}

        def side_effect(*args, **kwargs):
            seen['observed'] = observe()
            raise RuntimeError('audit write failed')

        return seen, patch(
            'platform_admin_app.models.AdminAuditLog.objects.create',
            side_effect=side_effect,
        )

    def test_the_guard_the_reissue_rollback_depends_on(self):
        """
        Without this, the rollback test would pass just as happily if the mutation had
        never run. It proves the domain really did write before the audit blew up.
        """
        seen, fault = self._audit_fails_after_observing(
            lambda: OwnerInvitation.objects.get(
                pk=self.invitation.pk,
            ).superseded_at is not None,
        )
        with fault:
            with self.assertRaises(RuntimeError):
                self.reissue()
        self.assertTrue(
            seen.get('observed'),
            'the audit fired before the domain had superseded anything',
        )

    def test_the_guard_the_cancel_rollback_depends_on(self):
        seen, fault = self._audit_fails_after_observing(
            lambda: OwnerInvitation.objects.get(
                pk=self.invitation.pk,
            ).cancelled_at is not None,
        )
        with fault:
            with self.assertRaises(RuntimeError):
                self.cancel()
        self.assertTrue(
            seen.get('observed'),
            'the audit fired before the domain had cancelled anything',
        )

    def test_a_refused_mutation_is_still_recorded(self):
        """
        The other half of the same structure: the domain exception unwinds only its own
        savepoint, so the failure audit written afterwards commits.
        """
        self.reissue()
        before = lifecycle_audits().count()
        self.assertEqual(self.cancel().status_code, 409)
        self.assertEqual(lifecycle_audits().count(), before + 1)


# --- §19 customer access through HTTP ----------------------------------------

class CustomerAccessUnchangedThroughHttpTests(_EndpointTestCase):

    def _snapshot(self):
        self.owner.refresh_from_db()
        return (
            self.owner.customer_access_state, self.owner.password,
            self.owner.is_active, self.owner.prompt_password_change,
            self.owner.last_login,
        )

    def test_reissue_changes_nothing_about_the_identity(self):
        before = self._snapshot()
        self.assertEqual(self.reissue().status_code, 200)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(
            self.owner.customer_access_state, CUSTOMER_ACCESS_PENDING_INITIAL_CLAIM,
        )

    def test_cancel_changes_nothing_about_the_identity(self):
        before = self._snapshot()
        self.assertEqual(self.cancel().status_code, 200)
        self.assertEqual(self._snapshot(), before)

    def test_the_owner_control_axis_stays_not_established(self):
        """
        Issuing another credential is not evidence of anything. Owner control remains
        evidence-based, and the only evidence is a redemption that does not exist yet.
        """
        self.reissue()
        self.assertEqual(
            self.detail_onboarding()['owner_control']['status'], 'not_established',
        )
