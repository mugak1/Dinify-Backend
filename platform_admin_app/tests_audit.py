"""
Tests for the append-only administrative audit log (PR-3).

Covers:
* Append-only enforcement — update / delete / bulk update / bulk delete all raise,
  while creating is unaffected.
* Redaction — deeply nested and list-nested secrets are scrubbed, key names and
  structure survive, oversized state is truncated with an explicit marker.
* Failure is LOUD — a failing write propagates instead of being swallowed (the
  explicit contrast with the legacy threaded ``save_action``).
* Transactional integration — an action wrapped with its audit in one
  ``transaction.atomic()`` rolls back when the audit write fails.
* ``record_from_request`` / ``record_auth_event`` context extraction.
* Exposure guards — no serializer targets the model or exposes the state columns.
* The declared composite indexes.

Endpoint-level cases reuse the admin stack via ``@override_settings`` and a local
``urlpatterns``, the same harness as ``tests_transport.py``.
"""
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import path
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF,
    ACCOUNT_TYPE_RESTAURANT_USER,
)
from dinify_backend.tenancy.discovery import all_project_serializers
from platform_admin_app import audit, sessions
from platform_admin_app.audit_actions import (
    ADMIN_AUTH_LOGIN_FAILURE,
    ADMIN_AUTH_LOGIN_SUCCESS,
)
from platform_admin_app.cookies import cookie_name
from platform_admin_app.models import (
    AdminAuditLog,
    AppendOnlyViolation,
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
)
from platform_admin_app.testing import AuditAssertionsMixin
from platform_admin_app.views import AdminAPIView
from users_app.models import User

# Distinct phone range from platform_admin_app/tests.py (…01…) and
# tests_transport.py (…02…) to avoid any collision on the unique phone_number.
_PHONE = iter(f'2567030000{n:02d}' for n in range(1, 99))


def _make_user(email, account_type=ACCOUNT_TYPE_RESTAURANT_USER, is_active=True):
    phone = next(_PHONE)
    return User.objects.create_user(
        first_name='T', last_name=phone[-3:], email=email,
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[], account_type=account_type,
        is_active=is_active,
    )


def _entry(**overrides):
    """Create a minimal valid audit entry."""
    kwargs = {'action': ADMIN_AUTH_LOGIN_SUCCESS, 'result': RESULT_SUCCESS}
    kwargs.update(overrides)
    return audit.record(**kwargs)


# --- Local urlconf + stub view for the endpoint-level cases -----------------------

class _AuditedAdminView(AdminAPIView):
    """A concrete admin view exercising the AdminAPIView.audit() helper."""

    def post(self, request):
        self.audit(
            request,
            ADMIN_AUTH_LOGIN_SUCCESS,
            result=RESULT_SUCCESS,
            resource_type='thing',
            resource_id='abc',
        )
        return Response({'ok': True})


urlpatterns = [
    path('admin/v1/audited/', _AuditedAdminView.as_view(), name='t-admin-audited'),
]

_ADMIN_OVERRIDES = dict(
    ROOT_URLCONF=__name__,
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


# --- Append-only enforcement -------------------------------------------------------

class AppendOnlyTests(TestCase):
    def test_create_is_allowed(self):
        entry = _entry()
        self.assertIsNotNone(entry.pk)
        self.assertEqual(AdminAuditLog.objects.count(), 1)

    def test_update_raises(self):
        entry = _entry()
        entry.reason = 'tampered'
        with self.assertRaises(AppendOnlyViolation):
            entry.save()

    def test_reloaded_instance_update_raises(self):
        entry = _entry()
        reloaded = AdminAuditLog.objects.get(pk=entry.pk)
        reloaded.result = RESULT_DENIED
        with self.assertRaises(AppendOnlyViolation):
            reloaded.save()

    def test_instance_delete_raises(self):
        entry = _entry()
        with self.assertRaises(AppendOnlyViolation):
            entry.delete()
        self.assertEqual(AdminAuditLog.objects.count(), 1)

    def test_queryset_update_raises(self):
        _entry()
        with self.assertRaises(AppendOnlyViolation):
            AdminAuditLog.objects.all().update(reason='tampered')

    def test_queryset_delete_raises(self):
        _entry()
        with self.assertRaises(AppendOnlyViolation):
            AdminAuditLog.objects.all().delete()
        self.assertEqual(AdminAuditLog.objects.count(), 1)


# --- Redaction ---------------------------------------------------------------------

class RedactionTests(TestCase):
    def test_nested_secret_three_levels_deep_is_redacted(self):
        entry = _entry(after_state={
            'level1': {'level2': {'level3': {'password': 'hunter2', 'keep': 'me'}}},
        })
        entry.refresh_from_db()
        level3 = entry.after_state['level1']['level2']['level3']
        self.assertEqual(level3['password'], '[redacted]')
        # Key names and surrounding structure survive.
        self.assertEqual(level3['keep'], 'me')
        self.assertIn('level1', entry.after_state)

    def test_all_denylisted_substrings_are_caught(self):
        payload = {
            'password': 'x', 'refresh_token': 'x', 'client_secret': 'x',
            'totp_secret_encrypted': 'x', 'recovery_code_hashes': 'x',
            'otp_code': 'x', 'csrf_token': 'x', 'cookie_jar': 'x',
            'authorization': 'x', 'session_key': 'x', 'api_key': 'x',
            'private_note': 'x',
        }
        entry = _entry(before_state=payload)
        entry.refresh_from_db()
        for key in payload:
            self.assertEqual(
                entry.before_state[key], '[redacted]',
                f'{key} should have been redacted',
            )

    def test_list_of_dicts_is_scrubbed(self):
        entry = _entry(after_state={
            'items': [{'password': 'a', 'name': 'first'}, {'token': 'b', 'name': 'second'}],
        })
        entry.refresh_from_db()
        items = entry.after_state['items']
        self.assertEqual(items[0]['password'], '[redacted]')
        self.assertEqual(items[0]['name'], 'first')
        self.assertEqual(items[1]['token'], '[redacted]')
        self.assertEqual(items[1]['name'], 'second')

    def test_redaction_is_case_insensitive(self):
        entry = _entry(after_state={'PassWord': 'x', 'API_KEY': 'y'})
        entry.refresh_from_db()
        self.assertEqual(entry.after_state['PassWord'], '[redacted]')
        self.assertEqual(entry.after_state['API_KEY'], '[redacted]')

    def test_non_sensitive_values_survive_untouched(self):
        entry = _entry(after_state={'status': 'live', 'count': 3, 'flag': True})
        entry.refresh_from_db()
        self.assertEqual(
            entry.after_state, {'status': 'live', 'count': 3, 'flag': True},
        )

    def test_oversized_state_is_truncated_with_marker(self):
        entry = _entry(after_state={'blob': 'x' * (audit.MAX_STATE_BYTES + 1)})
        entry.refresh_from_db()
        self.assertTrue(entry.after_state['_truncated'])
        self.assertGreater(entry.after_state['_original_bytes'], audit.MAX_STATE_BYTES)
        self.assertIn('_preview', entry.after_state)

    def test_none_state_stays_none(self):
        entry = _entry()
        entry.refresh_from_db()
        self.assertIsNone(entry.before_state)
        self.assertIsNone(entry.after_state)

    def test_non_json_types_are_normalised_not_fatal(self):
        # A UUID / datetime in a caller's payload must not break the write.
        user = _make_user('redact_types@t.com')
        entry = _entry(after_state={'id': user.id, 'when': user.date_joined})
        entry.refresh_from_db()
        self.assertEqual(entry.after_state['id'], str(user.id))
        self.assertIsInstance(entry.after_state['when'], str)


# --- Failure is loud ---------------------------------------------------------------

class LoudFailureTests(TestCase):
    def test_write_failure_propagates(self):
        """The contrast with save_action: no thread, no swallow — it raises."""
        with patch.object(
            AdminAuditLog.objects, 'create', side_effect=RuntimeError('db down'),
        ):
            with self.assertRaises(RuntimeError):
                _entry()

    def test_audit_failure_rolls_back_the_action_it_describes(self):
        user = _make_user('audit_txn@t.com')
        original_name = user.first_name

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                user.first_name = 'Changed'
                user.save(update_fields=['first_name'])
                with patch.object(
                    AdminAuditLog.objects, 'create',
                    side_effect=RuntimeError('db down'),
                ):
                    _entry(actor=user)

        user.refresh_from_db()
        self.assertEqual(user.first_name, original_name)

    def test_entry_commits_with_the_action_on_success(self):
        user = _make_user('audit_txn_ok@t.com')
        with transaction.atomic():
            user.first_name = 'Changed'
            user.save(update_fields=['first_name'])
            _entry(actor=user)

        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Changed')
        self.assertEqual(AdminAuditLog.objects.filter(actor=user).count(), 1)


# --- record_from_request / record_auth_event --------------------------------------

_factory = APIRequestFactory()


class RecordFromRequestTests(TestCase):
    def _request(self, **extra):
        request = _factory.post('/admin/v1/thing/', **extra)
        request.request_id = 'server-generated-id'
        request.client_ip = '10.0.0.9'
        return request

    def test_populates_actor_session_and_request_context(self):
        user = _make_user('rfr_ok@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        _raw, session = sessions.create_session(user)
        request = self._request(HTTP_USER_AGENT='TestAgent/1.0')
        request.user = user
        request.auth = session

        entry = audit.record_from_request(
            request, ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.actor_id, user.id)
        self.assertEqual(entry.session_id, session.id)
        self.assertEqual(entry.request_id, 'server-generated-id')
        self.assertEqual(entry.source_ip, '10.0.0.9')
        self.assertEqual(entry.user_agent, 'TestAgent/1.0')

    def test_unauthenticated_request_does_not_raise(self):
        from django.contrib.auth.models import AnonymousUser

        request = self._request()
        request.user = AnonymousUser()
        request.auth = None

        entry = audit.record_from_request(
            request, ADMIN_AUTH_LOGIN_FAILURE, result=RESULT_DENIED,
        )
        self.assertIsNone(entry.actor)
        self.assertIsNone(entry.session)
        self.assertEqual(entry.result, RESULT_DENIED)

    def test_ignores_client_supplied_request_id_header(self):
        from django.contrib.auth.models import AnonymousUser

        # A forged header must never become the correlation id — only the
        # middleware-set attribute is read.
        request = self._request(HTTP_X_REQUEST_ID='client-forged')
        request.user = AnonymousUser()
        request.auth = None

        entry = audit.record_from_request(
            request, ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.request_id, 'server-generated-id')
        self.assertNotEqual(entry.request_id, 'client-forged')

    def test_missing_middleware_attributes_do_not_raise(self):
        from django.contrib.auth.models import AnonymousUser

        bare = _factory.post('/admin/v1/thing/')  # no request_id / client_ip
        bare.user = AnonymousUser()
        bare.auth = None
        entry = audit.record_from_request(
            bare, ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS,
        )
        self.assertEqual(entry.request_id, '')
        self.assertIsNone(entry.source_ip)

    def test_explicit_kwargs_win_over_request_derived(self):
        user = _make_user('rfr_override@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        other = _make_user('rfr_other@t.com')
        request = self._request()
        request.user = user
        request.auth = None

        entry = audit.record_from_request(
            request, ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS, actor=other,
        )
        self.assertEqual(entry.actor_id, other.id)

    def test_non_admin_session_auth_is_ignored(self):
        # A stray non-AdminSession on request.auth must not be coerced onto the FK.
        user = _make_user('rfr_badauth@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        request = self._request()
        request.user = user
        request.auth = object()

        entry = audit.record_from_request(
            request, ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS,
        )
        self.assertIsNone(entry.session)


class RecordAuthEventTests(TestCase):
    def test_failed_login_has_no_actor_but_keeps_the_label(self):
        request = _factory.post('/admin/v1/login/')
        request.request_id = 'req-1'
        request.client_ip = '10.0.0.1'

        entry = audit.record_auth_event(
            request,
            ADMIN_AUTH_LOGIN_FAILURE,
            result=RESULT_FAILURE,
            actor_label='someone@example.com',
            error_code='invalid_credentials',
        )
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_label, 'someone@example.com')
        self.assertEqual(entry.result, RESULT_FAILURE)
        self.assertEqual(entry.error_code, 'invalid_credentials')
        self.assertEqual(entry.request_id, 'req-1')
        self.assertIsNone(entry.session)


# --- Endpoint integration ----------------------------------------------------------

@override_settings(**_ADMIN_OVERRIDES)
class AuditedEndpointTests(AuditAssertionsMixin, TestCase):
    def test_admin_api_view_audit_helper_writes_one_entry(self):
        user = _make_user('ep_audit@t.com', account_type=ACCOUNT_TYPE_PLATFORM_STAFF)
        raw, session = sessions.create_session(user)
        client = Client()
        client.cookies[cookie_name()] = raw

        response = client.post(
            '/admin/v1/audited/', data='{}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        entry = self.assertAudited(ADMIN_AUTH_LOGIN_SUCCESS, result=RESULT_SUCCESS)
        self.assertEqual(entry.actor_id, user.id)
        self.assertEqual(entry.session_id, session.id)
        self.assertEqual(entry.resource_type, 'thing')
        self.assertEqual(entry.resource_id, 'abc')
        # The middleware-generated request id landed on the row.
        self.assertEqual(len(entry.request_id), 32)


# --- Exposure guards ---------------------------------------------------------------

class AuditExposureGuardTests(TestCase):
    """
    Forward ratchets. Unlike the User-serializer guards, an EMPTY result set is the
    expected passing state today — no serializer touches the audit log yet. These
    assertions exist to fail the day one is added without deliberate field
    selection (Phase 1 builds the portal read endpoints).
    """

    def test_no_serializer_targets_admin_audit_log(self):
        offenders = [
            f'{cls.__module__}.{cls.__qualname__}'
            for cls in all_project_serializers()
            if getattr(getattr(cls, 'Meta', None), 'model', None) is AdminAuditLog
        ]
        self.assertEqual(
            offenders, [],
            'A serializer targets AdminAuditLog. Audit reads must use deliberate '
            f'field selection, never a ModelSerializer sweep: {offenders}',
        )

    def test_no_serializer_exposes_audit_state_fields(self):
        state_fields = {'before_state', 'after_state'}
        for cls in all_project_serializers():
            exposed = state_fields.intersection(cls().fields.keys())
            self.assertEqual(
                exposed, set(),
                f'{cls.__module__}.{cls.__qualname__} exposes audit state '
                f'field(s): {exposed}.',
            )


# --- Model meta --------------------------------------------------------------------

class ModelMetaTests(TestCase):
    def test_declared_composite_indexes(self):
        declared = {tuple(index.fields) for index in AdminAuditLog._meta.indexes}
        self.assertIn(('restaurant_id', 'created_at'), declared)
        self.assertIn(('actor', 'created_at'), declared)

    def test_restaurant_id_is_a_plain_uuid_not_a_foreign_key(self):
        # Deliberate: the log must survive a restaurant row's deletion.
        field = AdminAuditLog._meta.get_field('restaurant_id')
        self.assertFalse(field.is_relation)

    def test_no_soft_delete_or_archival_fields(self):
        names = {field.name for field in AdminAuditLog._meta.get_fields()}
        for forbidden in ('deleted', 'archived', 'vacuumed', 'time_deleted'):
            self.assertNotIn(forbidden, names)
