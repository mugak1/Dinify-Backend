"""
Tests for delegation grants (PR-4a): the model, minting, listing and revocation.

Covers:
* mint requires an authenticated admin session, and recent elevation — a stale
  session is refused AND audited as ``mint_denied`` (a permission-layer denial
  would otherwise produce no entry at all);
* the raw exchange code is returned exactly once, is unrecoverable from the row,
  and appears in NO audit field anywhere;
* validation: reason blank / too short, unknown and soft-deleted restaurants,
  invalid scope, TTL default and ceiling;
* supersession of an earlier unredeemed grant for the same admin+restaurant;
* the live-grant cap;
* revocation: works, idempotent, needs NO elevation, and drops out of the default list;
* listing: live-only by default, ``?all=true`` for history, never the code hash;
* exposure guards over ``exchange_code_hash``.

The exchange path itself is PR-4b — nothing here redeems a code.
"""
from datetime import timedelta

from django.conf import settings as dj_settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
    RestaurantStatus_Live,
)
from dinify_backend.tenancy.discovery import all_project_serializers
from platform_admin_app import delegation, sessions
from platform_admin_app.audit_actions import (
    ADMIN_DELEGATION_MINT_DENIED,
    ADMIN_DELEGATION_MINTED,
    ADMIN_DELEGATION_REVOKED,
    ADMIN_DELEGATION_SUPERSEDED,
)
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import (
    RESULT_DENIED,
    RESULT_SUCCESS,
    SCOPE_SUPPORT,
    SCOPE_VIEW,
    AdminAuditLog,
    DelegationGrant,
)
from platform_admin_app.sessions import hash_token
from platform_admin_app.testing import AuditAssertionsMixin
from restaurants_app.models import Restaurant
from users_app.models import User

# Distinct phone range from tests.py (…01…), tests_transport.py (…02…),
# tests_audit.py (…03…) and tests_auth.py (…04…).
_PHONE = iter(f'2567050000{n:02d}' for n in range(1, 99))

PASSWORD = 'correct-horse-battery'
REASON = 'Investigating a reported billing discrepancy.'

_ADMIN_OVERRIDES = dict(
    # The REAL admin urlconf, so these tests exercise the production route table.
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


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, username=None,
               phone=True):
    phone_number = next(_PHONE) if phone else None
    return User.objects.create_user(
        first_name='T', last_name='U', email=email,
        phone_number=phone_number,
        username=username or (phone_number or email),
        country='Uganda', password=PASSWORD, roles=[],
        account_type=account_type,
    )


def _make_admin(email='deleg-admin@t.com', username='deleg-admin'):
    return _make_user(
        email, account_type=ACCOUNT_TYPE_PLATFORM_STAFF, username=username,
        phone=False,
    )


def _make_restaurant(name='Java House', owner=None, deleted=False):
    owner = owner or _make_user(f'owner-{name}@t.com'.replace(' ', '-'))
    return Restaurant.objects.create(
        name=name, location=f'{name} loc', status=RestaurantStatus_Live,
        owner=owner, deleted=deleted,
    )


# --- Model ------------------------------------------------------------------------

class DelegationGrantModelTests(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()

    def _grant(self, **overrides):
        kwargs = dict(
            administrator=self.admin,
            restaurant=self.restaurant,
            scope=SCOPE_VIEW,
            reason=REASON,
            exchange_code_hash=hash_token(f'code-{timezone.now().timestamp()}'),
            code_expires_at=timezone.now() + timedelta(minutes=3),
        )
        kwargs.update(overrides)
        return DelegationGrant.objects.create(**kwargs)

    def test_fresh_grant_code_is_live_session_is_not(self):
        grant = self._grant()
        self.assertTrue(grant.is_code_live)
        self.assertFalse(grant.is_session_live)

    def test_expired_code_is_not_live(self):
        grant = self._grant(code_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(grant.is_code_live)

    def test_redeemed_grant_flips_code_to_session(self):
        grant = self._grant(redeemed_at=timezone.now())
        self.assertFalse(grant.is_code_live)
        self.assertTrue(grant.is_session_live)

    def test_session_expires_after_ttl(self):
        grant = self._grant(
            redeemed_at=timezone.now() - timedelta(seconds=1000),
            session_ttl_seconds=900,
        )
        self.assertFalse(grant.is_session_live)

    def test_revocation_kills_both_clocks(self):
        grant = self._grant(
            redeemed_at=timezone.now(), revoked_at=timezone.now(),
        )
        self.assertFalse(grant.is_code_live)
        self.assertFalse(grant.is_session_live)

    def test_declared_indexes(self):
        declared = {tuple(i.fields) for i in DelegationGrant._meta.indexes}
        self.assertIn(('administrator', 'issued_at'), declared)
        self.assertIn(('restaurant', 'issued_at'), declared)


# --- Service ----------------------------------------------------------------------

class MintServiceTests(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()

    def _mint(self, **overrides):
        kwargs = dict(
            administrator=self.admin, admin_session=None,
            restaurant=self.restaurant, scope=SCOPE_VIEW, reason=REASON,
        )
        kwargs.update(overrides)
        return delegation.mint_grant(**kwargs)

    def test_stores_only_the_hash(self):
        raw, grant = self._mint()
        self.assertEqual(grant.exchange_code_hash, hash_token(raw))
        self.assertNotEqual(grant.exchange_code_hash, raw)
        # The raw code is unrecoverable from the row.
        self.assertFalse(
            DelegationGrant.objects.filter(exchange_code_hash=raw).exists()
        )

    def test_default_ttl_applied_and_ceiling_enforced(self):
        _raw, grant = self._mint()
        self.assertEqual(grant.session_ttl_seconds, delegation.default_session_ttl())
        with self.assertRaises(delegation.DelegationValidationError):
            self._mint(session_ttl_seconds=delegation.max_session_ttl() + 1)

    def test_reason_must_be_present_and_substantive(self):
        for bad in ('', '   ', 'too short'):
            with self.assertRaises(delegation.DelegationValidationError) as ctx:
                self._mint(reason=bad)
            self.assertIn('reason', ctx.exception.errors)

    def test_unknown_and_deleted_restaurants_rejected_identically(self):
        with self.assertRaises(delegation.DelegationValidationError) as unknown:
            self._mint(restaurant=None)
        deleted = _make_restaurant(name='Gone', deleted=True)
        with self.assertRaises(delegation.DelegationValidationError) as soft:
            self._mint(restaurant=deleted)
        # Same message: a deleted tenant is simply not reachable.
        self.assertEqual(unknown.exception.errors, soft.exception.errors)

    def test_invalid_scope_rejected_both_valid_accepted(self):
        with self.assertRaises(delegation.DelegationValidationError):
            self._mint(scope='superuser')
        for scope in (SCOPE_VIEW, SCOPE_SUPPORT):
            _raw, grant = self._mint(scope=scope)
            self.assertEqual(grant.scope, scope)

    def test_supersedes_prior_unredeemed_grant_for_same_restaurant(self):
        _raw, first = self._mint()
        _raw2, second = self._mint()
        first.refresh_from_db()
        self.assertIsNotNone(first.revoked_at)
        self.assertEqual(first.revoked_reason, 'superseded')
        self.assertIsNone(second.revoked_at)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_SUPERSEDED, delegation_id=first.id,
            ).exists()
        )

    def test_does_not_supersede_other_restaurants(self):
        _raw, first = self._mint()
        other = _make_restaurant(name='Other')
        self._mint(restaurant=other)
        first.refresh_from_db()
        self.assertIsNone(first.revoked_at)

    def test_live_grant_cap_refuses_the_next_mint(self):
        cap = delegation.max_live_grants()
        for index in range(cap):
            self._mint(restaurant=_make_restaurant(name=f'R{index}'))
        with self.assertRaises(delegation.DelegationValidationError) as ctx:
            self._mint(restaurant=_make_restaurant(name='Overflow'))
        self.assertEqual(ctx.exception.code, 'live_grant_cap')

    def test_revoked_grants_do_not_count_towards_the_cap(self):
        cap = delegation.max_live_grants()
        for index in range(cap):
            _raw, grant = self._mint(restaurant=_make_restaurant(name=f'C{index}'))
            delegation.revoke_grant(grant, 'done')
        _raw, grant = self._mint(restaurant=_make_restaurant(name='Fresh'))
        self.assertIsNotNone(grant.pk)

    def test_revoke_is_idempotent_and_audits_once(self):
        _raw, grant = self._mint()
        delegation.revoke_grant(grant, 'finished')
        first_revoked_at = grant.revoked_at
        delegation.revoke_grant(grant, 'again')
        grant.refresh_from_db()
        self.assertEqual(grant.revoked_at, first_revoked_at)
        self.assertEqual(grant.revoked_reason, 'finished')
        self.assertEqual(
            AdminAuditLog.objects.filter(
                action=ADMIN_DELEGATION_REVOKED, delegation_id=grant.id,
            ).count(),
            1,
        )

    def test_redeemed_grant_can_still_be_revoked(self):
        """Revocation must kill a running session, not merely block redemption."""
        _raw, grant = self._mint()
        grant.redeemed_at = timezone.now()
        grant.save(update_fields=['redeemed_at'])
        delegation.revoke_grant(grant, 'compromised')
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)
        self.assertFalse(grant.is_session_live)

    def test_mint_audit_carries_delegation_and_restaurant_ids(self):
        _raw, grant = self._mint()
        entry = AdminAuditLog.objects.get(action=ADMIN_DELEGATION_MINTED)
        self.assertEqual(entry.delegation_id, grant.id)
        self.assertEqual(entry.restaurant_id, self.restaurant.id)
        self.assertEqual(entry.reason, REASON)
        self.assertEqual(entry.after_state['scope'], SCOPE_VIEW)
        self.assertEqual(entry.result, RESULT_SUCCESS)


class RawCodeNeverAuditedTests(TestCase):
    def test_raw_code_appears_in_no_audit_field(self):
        admin = _make_admin()
        restaurant = _make_restaurant()
        raw, _grant = delegation.mint_grant(
            administrator=admin, admin_session=None, restaurant=restaurant,
            scope=SCOPE_SUPPORT, reason=REASON,
        )
        self.assertTrue(AdminAuditLog.objects.exists())
        for entry in AdminAuditLog.objects.all():
            haystack = ' '.join(
                str(value) for value in (
                    entry.action, entry.actor_label, entry.resource_type,
                    entry.resource_id, entry.reason, entry.error_code,
                    entry.before_state, entry.after_state, entry.user_agent,
                    entry.request_id,
                )
            )
            self.assertNotIn(raw, haystack)


# --- Endpoints ---------------------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class DelegationEndpointTests(AuditAssertionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.restaurant = _make_restaurant()
        raw, self.session = sessions.create_session(self.admin)
        sessions.elevate(self.session)
        self.client = Client()
        self.client.cookies[cookie_name()] = raw

    def _mint(self, **body):
        payload = dict(
            restaurant_id=str(self.restaurant.id), scope=SCOPE_VIEW, reason=REASON,
        )
        payload.update(body)
        return self.client.post(
            '/admin/v1/delegations/', data=payload,
            content_type='application/json',
        )

    def test_mint_returns_the_code_exactly_once(self):
        response = self._mint()
        self.assertEqual(response.status_code, 201)
        code = response.json()['data']['exchange_code']
        self.assertTrue(code)

        grant = DelegationGrant.objects.get()
        self.assertEqual(grant.exchange_code_hash, hash_token(code))

        # No later read can recover it.
        listed = self.client.get('/admin/v1/delegations/').json()['data']
        self.assertNotIn('exchange_code', listed[0])
        self.assertNotIn('exchange_code_hash', listed[0])

    def test_mint_is_audited(self):
        self._mint()
        grant = DelegationGrant.objects.get()
        entry = self.assertAudited(ADMIN_DELEGATION_MINTED, result=RESULT_SUCCESS)
        self.assertEqual(entry.delegation_id, grant.id)

    def test_anonymous_mint_rejected(self):
        anon = Client()
        response = anon.post(
            '/admin/v1/delegations/',
            data={'restaurant_id': str(self.restaurant.id), 'scope': SCOPE_VIEW,
                  'reason': REASON},
            content_type='application/json',
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(DelegationGrant.objects.count(), 0)
        # An unauthenticated call is about identity — it must NOT manufacture a
        # mint_denied entry for a caller we cannot name.
        self.assertFalse(
            AdminAuditLog.objects.filter(action=ADMIN_DELEGATION_MINT_DENIED).exists()
        )

    def test_stale_elevation_denied_and_audited(self):
        self.session.elevated_at = timezone.now() - timedelta(minutes=30)
        self.session.save(update_fields=['elevated_at'])

        response = self._mint()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(DelegationGrant.objects.count(), 0)
        entry = self.assertAudited(
            ADMIN_DELEGATION_MINT_DENIED, result=RESULT_DENIED,
        )
        self.assertEqual(entry.error_code, 'elevation_required')

    def test_validation_failure_returns_field_errors_and_audits(self):
        response = self._mint(reason='short')
        self.assertEqual(response.status_code, 400)
        self.assertIn('reason', response.json()['errors'])
        entry = self.assertAudited(
            ADMIN_DELEGATION_MINT_DENIED, result=RESULT_DENIED,
        )
        self.assertEqual(entry.error_code, 'reason_too_short')

    def test_unknown_restaurant_rejected(self):
        response = self._mint(
            restaurant_id='0f9a1b2c-3d4e-5f60-8172-93a4b5c6d7e8',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('restaurant_id', response.json()['errors'])

    def test_malformed_restaurant_id_is_400_not_500(self):
        response = self._mint(restaurant_id='not-a-uuid')
        self.assertEqual(response.status_code, 400)

    def test_list_shows_live_only_by_default(self):
        self._mint()
        grant = DelegationGrant.objects.get()
        self.assertEqual(len(self.client.get('/admin/v1/delegations/').json()['data']), 1)

        delegation.revoke_grant(grant, 'done')
        self.assertEqual(len(self.client.get('/admin/v1/delegations/').json()['data']), 0)
        # …but history still has it.
        history = self.client.get('/admin/v1/delegations/?all=true').json()['data']
        self.assertEqual(len(history), 1)
        self.assertIsNotNone(history[0]['revoked_at'])

    def test_list_never_exposes_the_hash(self):
        self._mint()
        body = self.client.get('/admin/v1/delegations/?all=true').content.decode()
        self.assertNotIn('exchange_code_hash', body)
        self.assertNotIn(DelegationGrant.objects.get().exchange_code_hash, body)

    def test_revoke_works_and_is_idempotent(self):
        self._mint()
        grant = DelegationGrant.objects.get()
        url = f'/admin/v1/delegations/{grant.id}/revoke/'

        first = self.client.post(
            url, data={'reason': 'no longer needed'},
            content_type='application/json',
        )
        self.assertEqual(first.status_code, 200)
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)

        second = self.client.post(url, data={}, content_type='application/json')
        self.assertEqual(second.status_code, 200)
        self.assertAudited(
            ADMIN_DELEGATION_REVOKED, delegation_id=grant.id, count=2,
        )

    def test_revoke_needs_no_elevation(self):
        """Stopping access must never depend on a second factor."""
        self._mint()
        grant = DelegationGrant.objects.get()
        self.session.elevated_at = timezone.now() - timedelta(minutes=30)
        self.session.save(update_fields=['elevated_at'])

        response = self.client.post(
            f'/admin/v1/delegations/{grant.id}/revoke/', data={},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)

    def test_revoke_unknown_grant_is_404(self):
        response = self.client.post(
            '/admin/v1/delegations/0f9a1b2c-3d4e-5f60-8172-93a4b5c6d7e8/revoke/',
            data={}, content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    def test_list_requires_authentication(self):
        self.assertIn(
            Client().get('/admin/v1/delegations/').status_code, (401, 403),
        )


# --- Exposure guards ---------------------------------------------------------------

class DelegationExposureTests(TestCase):
    """
    Forward ratchets. An empty result is the expected passing state today — these
    fail the day a serializer starts touching grant credentials.
    """

    def test_no_serializer_exposes_the_exchange_code_hash(self):
        for cls in all_project_serializers():
            self.assertNotIn(
                'exchange_code_hash', cls().fields.keys(),
                f'{cls.__module__}.{cls.__qualname__} exposes exchange_code_hash.',
            )

    def test_no_serializer_targets_delegation_grant(self):
        offenders = [
            f'{cls.__module__}.{cls.__qualname__}'
            for cls in all_project_serializers()
            if getattr(getattr(cls, 'Meta', None), 'model', None) is DelegationGrant
        ]
        self.assertEqual(offenders, [], f'DelegationGrant serialized: {offenders}')
