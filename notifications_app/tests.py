"""
Tests for notifications mark-as-read scoping (IDOR fix).

Controller (``FlagNotificationControllerTests``):
* the update filter carries the SAME recipient ``$or`` scope the read side
  (get_notifications) uses — not just ``_id`` — so a user can only flag a
  notification they actually receive;
* matched_count (not modified_count) drives the True/False return, so an
  idempotent re-flag of an already-read notification the user owns still
  succeeds;
* a malformed ObjectId and an unreachable Mongo are handled (return False),
  never a crash.

Endpoint (``NotificationsEndpointPutTests``):
* owner → 200 with the preserved success shape;
* non-owner / nonexistent (matched_count 0) → 404, with the write provably
  scoped to the caller's identity (a real Mongo would touch nothing);
* missing id → 400, malformed id → 400 (not 404/500), unauthenticated → 401,
  none of which reach the collection.

MongoDB is stubbed globally in test_settings, so every test patches the
controller's ``MONGO_DB`` and sets ``matched_count`` to a literal int (an
unconfigured MagicMock makes ``matched_count > 0`` raise, which the controller
would swallow into False and mask real behaviour).
"""
import json
from unittest.mock import patch

from bson import ObjectId
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from users_app.models import User
from notifications_app.controllers.notifications import flag_notification_as_read


NOTIFICATIONS_URL = '/api/v1/notifications/'
# Patch the name where it is looked up — the controller bound its own MONGO_DB
# reference at import time, so patching dinify_backend.mongo_db would not help.
PATCH_TARGET = 'notifications_app.controllers.notifications.MONGO_DB'


def make_user(phone, email):
    return User.objects.create_user(
        first_name='Test', last_name='User',
        email=email, phone_number=phone,
        username=phone, country='Uganda', password='password',
        roles=[],
    )


def auth_headers(user):
    token = str(RefreshToken.for_user(user).access_token)
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


class FlagNotificationControllerTests(TestCase):

    def test_filter_includes_recipient_scope(self):
        # The regression test: on the vulnerable code the filter is {'_id': ...}
        # only, so '$or' is absent. The fix ANDs the recipient scope onto _id.
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value
            collection.update_one.return_value.matched_count = 1

            oid = str(ObjectId())
            result = flag_notification_as_read(
                oid, email='owner@x.com', phone='256700000001'
            )

        self.assertTrue(result)
        collection.update_one.assert_called_once()
        sent_filter = collection.update_one.call_args.kwargs['filter']
        self.assertEqual(sent_filter['_id'], ObjectId(oid))
        self.assertIn('$or', sent_filter)
        self.assertEqual(
            sent_filter['$or'],
            [
                {'tos': 'owner@x.com'},
                {'tos': '256700000001'},
                {'ccs': 'owner@x.com'},
                {'ccs': '256700000001'},
            ],
        )
        self.assertEqual(
            collection.update_one.call_args.kwargs['update'],
            {'$set': {'read': True}},
        )

    def test_returns_false_when_no_match(self):
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value
            collection.update_one.return_value.matched_count = 0

            result = flag_notification_as_read(
                str(ObjectId()), email='other@x.com', phone='256700000002'
            )

        self.assertFalse(result)

    def test_uses_matched_count_not_modified_count(self):
        # Owner re-flags an already-read notification: matched but not modified.
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value
            collection.update_one.return_value.matched_count = 1
            collection.update_one.return_value.modified_count = 0

            result = flag_notification_as_read(
                str(ObjectId()), email='owner@x.com', phone='256700000003'
            )

        self.assertTrue(result)

    def test_malformed_objectid_returns_false_without_calling_mongo(self):
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value

            result = flag_notification_as_read(
                'not-an-oid', email='owner@x.com', phone='256700000004'
            )

        self.assertFalse(result)
        collection.update_one.assert_not_called()

    def test_returns_false_when_mongo_unreachable(self):
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value
            collection.update_one.side_effect = Exception('boom')

            result = flag_notification_as_read(
                str(ObjectId()), email='owner@x.com', phone='256700000005'
            )

        self.assertFalse(result)


class NotificationsEndpointPutTests(TestCase):

    def test_owner_marks_own_notification_success(self):
        user = make_user('256700000010', 'owner@x.com')
        oid = str(ObjectId())
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value
            collection.update_one.return_value.matched_count = 1

            resp = self.client.put(
                NOTIFICATIONS_URL,
                data=json.dumps({'notification_id': oid}),
                content_type='application/json',
                **auth_headers(user),
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {'status': 200,
             'message': "Successfully flagged the notification as read"},
        )
        # The write was scoped to this caller and sets read=True (this is how
        # "doc read=True" is asserted against a mocked collection).
        sent_filter = collection.update_one.call_args.kwargs['filter']
        self.assertIn({'tos': 'owner@x.com'}, sent_filter['$or'])
        self.assertIn({'tos': '256700000010'}, sent_filter['$or'])
        self.assertEqual(
            collection.update_one.call_args.kwargs['update'],
            {'$set': {'read': True}},
        )

    def test_non_owner_gets_404_and_write_scoped_to_caller(self):
        attacker = make_user('256700000020', 'attacker@x.com')
        # A well-formed id conceptually owned by a victim. Because the filter
        # carries the attacker's recipient scope, a real Mongo matches 0 and
        # never touches the victim's document.
        oid = str(ObjectId())
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value
            collection.update_one.return_value.matched_count = 0

            resp = self.client.put(
                NOTIFICATIONS_URL,
                data=json.dumps({'notification_id': oid}),
                content_type='application/json',
                **auth_headers(attacker),
            )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(
            resp.json(),
            {'status': 404, 'message': "notification not found"},
        )
        sent_filter = collection.update_one.call_args.kwargs['filter']
        self.assertEqual(
            sent_filter['$or'],
            [
                {'tos': 'attacker@x.com'},
                {'tos': '256700000020'},
                {'ccs': 'attacker@x.com'},
                {'ccs': '256700000020'},
            ],
        )

    def test_missing_notification_id_returns_400(self):
        user = make_user('256700000030', 'u30@x.com')
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value

            resp = self.client.put(
                NOTIFICATIONS_URL,
                data=json.dumps({}),
                content_type='application/json',
                **auth_headers(user),
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['status'], 400)
        collection.update_one.assert_not_called()

    def test_malformed_notification_id_returns_400(self):
        user = make_user('256700000040', 'u40@x.com')
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value

            resp = self.client.put(
                NOTIFICATIONS_URL,
                data=json.dumps({'notification_id': 'not-a-valid-oid'}),
                content_type='application/json',
                **auth_headers(user),
            )

        # Bad input → 400 (not 404 "not found", not a raw 500 crash).
        self.assertEqual(resp.status_code, 400)
        collection.update_one.assert_not_called()

    def test_unauthenticated_returns_401(self):
        oid = str(ObjectId())
        with patch(PATCH_TARGET) as mongo:
            collection = mongo.__getitem__.return_value

            resp = self.client.put(
                NOTIFICATIONS_URL,
                data=json.dumps({'notification_id': oid}),
                content_type='application/json',
            )

        self.assertEqual(resp.status_code, 401)
        collection.update_one.assert_not_called()
