"""
Regression tests for the null/empty-email recipient guard (TENANT-P3-02).

``determine_receipients`` built recipient lists from ``employee.user.email`` /
``admin.email`` with no guard. Self-registered users can carry a null/empty
email (``User.email`` is ``null=True``), so ``None``/``''`` values flowed into
``tos``/``ccs`` and could degenerate a downstream recipient predicate into
``email IS NULL`` (matching every emailless user).

The fix filters null/empty identities at the querysets AND in the comprehensions.
The single-user branch previously coalesced a falsy email to ``''``; PR 4 stops
persisting that empty recipient — it now emits the real email as a scalar or an
empty list ``[]`` (never ``''`` or ``None``), consistent with the array shape the
multi-recipient branches use. These tests call ``determine_receipients`` directly
(pure predicate construction), plus one end-to-end producer-insert test proving the
persisted document never carries ``tos=''``.
"""
from django.test import TestCase

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RESTAURANT_MANAGER,
)
from misc_app.controllers.notifications.determine_recipients import (
    determine_receipients,
)
from restaurants_app.models import Restaurant, RestaurantEmployee
from users_app.models import User


def _user(phone, email):
    """Create a user whose stored email is EXACTLY ``email``.

    Django's ``create_user`` runs ``normalize_email`` (coercing ``None`` -> ``''``),
    so to plant a true DB ``NULL`` (or a bare ``''``) we seed a placeholder and
    then force the exact value with a signal-free ``update``.
    """
    user = User.objects.create_user(
        first_name='Rec', last_name='User',
        email=f'seed_{phone}@example.com',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )
    User.objects.filter(pk=user.pk).update(email=email)
    user.refresh_from_db()
    return user


class DetermineRecipientsOwnerBranchTests(TestCase):
    """The owners/managers branch must drop null/empty-email employees."""

    def setUp(self):
        self.base_owner = _user('256700000751', 'base_owner@example.com')
        self.restaurant = Restaurant.objects.create(
            name='Recipients R', location='rec-loc', owner=self.base_owner,
        )
        self.real_owner = _user('256700000752', 'real_owner@example.com')
        self.null_owner = _user('256700000753', None)
        self.empty_owner = _user('256700000754', '')
        for member in (self.real_owner, self.null_owner, self.empty_owner):
            RestaurantEmployee.objects.create(
                user=member, restaurant=self.restaurant,
                roles=[RESTAURANT_OWNER],
            )

    def test_null_and_empty_email_owners_excluded_from_tos(self):
        result = determine_receipients(
            message_type='new-menu-section',
            restaurant_id=str(self.restaurant.id),
            user_id=None,
        )
        # Only the real email survives — no None, no ''.
        self.assertEqual(result['tos'], ['real_owner@example.com'])
        self.assertNotIn(None, result['tos'])
        self.assertNotIn('', result['tos'])

    def test_positive_control_all_real_email_recipients_unchanged(self):
        manager = _user('256700000755', 'real_manager@example.com')
        RestaurantEmployee.objects.create(
            user=manager, restaurant=self.restaurant,
            roles=[RESTAURANT_MANAGER],
        )
        result = determine_receipients(
            message_type='new-menu-section',
            restaurant_id=str(self.restaurant.id),
            user_id=None,
        )
        # owners-then-managers concatenation; both real identities present.
        self.assertEqual(
            result['tos'],
            ['real_owner@example.com', 'real_manager@example.com'],
        )
        self.assertNotIn(None, result['tos'])
        self.assertNotIn('', result['tos'])


class DetermineRecipientsSingleUserBranchTests(TestCase):
    """The single-user branch must emit a real email (scalar) or [] — never '' or None."""

    def test_null_email_single_user_yields_empty_list_not_empty_string(self):
        user = _user('256700000761', None)
        result = determine_receipients(
            message_type='password-change',
            restaurant_id=None,
            user_id=str(user.pk),
        )
        # PR 4: a falsy email yields an empty list, never the empty string that
        # would linger as a stored empty recipient.
        self.assertEqual(result['tos'], [])
        self.assertNotEqual(result['tos'], '')
        self.assertIsNotNone(result['tos'])

    def test_empty_email_single_user_yields_empty_list(self):
        user = _user('256700000762', '')
        result = determine_receipients(
            message_type='password-change',
            restaurant_id=None,
            user_id=str(user.pk),
        )
        self.assertEqual(result['tos'], [])
        self.assertNotEqual(result['tos'], '')

    def test_real_email_single_user_unchanged(self):
        # A present email is still emitted as the SCALAR recipient (unchanged) —
        # a downstream caller (send_messages) consumes it as a scalar.
        user = _user('256700000763', 'single_real@example.com')
        result = determine_receipients(
            message_type='password-change',
            restaurant_id=None,
            user_id=str(user.pk),
        )
        self.assertEqual(result['tos'], 'single_real@example.com')


class CreateNotificationPersistedShapeTests(TestCase):
    """
    End-to-end producer proof (req 13): a no-email single-user notification is
    persisted with ``tos == []`` — never the empty string. Patches the Mongo handle
    the producer's ``save_to_mongodb`` binds and neutralises the inline SMS path.
    Uses the SQLite-safe ``password-change`` branch (no ``roles__contains``).
    """

    def test_no_email_single_user_persists_empty_list_not_empty_string(self):
        from unittest.mock import patch
        from misc_app.controllers.notifications.notification import Notification

        user = _user('256700000771', None)

        with patch('misc_app.controllers.save_to_mongo.MONGO_DB') as mongo, \
                patch('misc_app.controllers.notifications.notification.Messenger'):
            collection = mongo.__getitem__.return_value
            Notification(msg_data={
                'msg_type': 'password-change',
                'user_id': str(user.pk),
            }).create_notification()

        collection.insert_one.assert_called_once()
        persisted = collection.insert_one.call_args.args[0]
        self.assertEqual(persisted['tos'], [])
        self.assertNotEqual(persisted['tos'], '')

    def test_real_email_single_user_persists_scalar(self):
        # Positive control: a present email is persisted as the scalar recipient.
        from unittest.mock import patch
        from misc_app.controllers.notifications.notification import Notification

        user = _user('256700000772', 'persist_real@example.com')

        with patch('misc_app.controllers.save_to_mongo.MONGO_DB') as mongo, \
                patch('misc_app.controllers.notifications.notification.Messenger'):
            collection = mongo.__getitem__.return_value
            Notification(msg_data={
                'msg_type': 'password-change',
                'user_id': str(user.pk),
            }).create_notification()

        persisted = collection.insert_one.call_args.args[0]
        self.assertEqual(persisted['tos'], 'persist_real@example.com')
