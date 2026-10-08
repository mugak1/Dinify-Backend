"""
The notification drain when MongoDB is unavailable.

``manage.py send_messages`` asks MongoDB for every notification with no ``sent``
flag and sends each one. Its try/except wrapped the ``find()`` call only. pymongo's
``find()`` performs no I/O and returns a lazy cursor, and the query runs on the
first iteration, so against a server the client could name but not reach,
``list(notifications)`` raised ``ServerSelectionTimeoutError`` one statement after
the guard and out of ``handle()``. The guard was written for exactly that failure
and never saw it. ``dinify_backend/tests_mongo_unavailability.py`` establishes the
premise against the real driver.

THE STATED OUTCOME, now pinned for both unavailable states: the command logs one
error, sends nothing, marks nothing as sent and returns normally, which is what
the guard already did when the query failed at ``find()``. Every pending
notification is left exactly as it was, so the next run picks it up.

Nothing leaves the machine. ``Messenger`` is replaced by a recording stand-in on
both sides, and the pending notification in the CONTROL is written by the real
producer (``Notification.create_notification``) into an in-memory store, so the
drain reads a document with the writer's own keys.
"""
import contextlib
import io
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from dinify_backend.tests_mongo_unavailability import (
    InMemoryMongo, UnconfiguredMongo, UnreachableMongo, unreachable_real_mongo,
)
from misc_app.controllers.notifications.notification import Notification
from users_app.models import User

COMMAND = 'notifications_app.management.commands.send_messages'
QUERY_FAILED = 'Failed to query pending notifications from MongoDB'


class Outbox:
    """Records what would have been sent, and sends nothing."""

    def __init__(self):
        self.emails = []
        self.sms = []

    def messenger(self):
        outbox = self

        class _Messenger:
            def send_email(self, to, cc, subject, message):
                outbox.emails.append({'to': to, 'cc': cc, 'subject': subject})
                return True

            def send_sms(self, msisdn, message):
                outbox.sms.append({'msisdn': msisdn})
                return True

        return _Messenger


class DrainFixture(TestCase):

    def drain(self, store):
        """Run the real command against ``store``. Returns the outbox and the
        command's stdout; an exception out of ``handle()`` propagates."""
        outbox = Outbox()
        printed = io.StringIO()
        with mock.patch(f'{COMMAND}.MONGO_DB', store), \
                mock.patch(f'{COMMAND}.Messenger', outbox.messenger()), \
                contextlib.redirect_stdout(printed):
            call_command('send_messages', stdout=printed, stderr=io.StringIO())
        return outbox, printed.getvalue()

    def assert_drained_nothing(self, outbox, printed):
        self.assertEqual(outbox.emails, [])
        self.assertEqual(outbox.sms, [])
        self.assertNotIn('=== Sending emails ===', printed)


class AnUnavailableStoreIsAStatedOutcomeTests(DrainFixture):

    def test_REGRESSION_an_unreachable_mongodb_is_logged_and_nothing_is_sent(self):
        store = UnreachableMongo()
        with self.assertLogs(COMMAND, level='ERROR') as logged:
            outbox, printed = self.drain(store)

        self.assertTrue(
            any(QUERY_FAILED in line for line in logged.output), logged.output)
        self.assert_drained_nothing(outbox, printed)
        self.assertEqual(
            store.calls, [('notifications', 'find')],
            'one query was attempted and nothing was marked as sent',
        )

    def test_REGRESSION_the_real_driver_against_an_unreachable_server(self):
        """The same, through the app's own client and the real driver: the
        selection timeout arrives at the iteration, after about two seconds."""
        with unreachable_real_mongo() as module, \
                self.assertLogs(COMMAND, level='ERROR') as logged:
            outbox, printed = self.drain(module.MONGO_DB)

        self.assertTrue(
            any(QUERY_FAILED in line for line in logged.output), logged.output)
        self.assert_drained_nothing(outbox, printed)

    def test_CONTROL_an_unconfigured_mongodb_was_already_handled(self):
        """The proxy refuses at ``MONGO_DB[...]``, inside the old guard, so this
        passed before the change and must keep passing."""
        store = UnconfiguredMongo()
        with self.assertLogs(COMMAND, level='ERROR') as logged:
            outbox, printed = self.drain(store)

        self.assertTrue(
            any(QUERY_FAILED in line for line in logged.output), logged.output)
        self.assert_drained_nothing(outbox, printed)


class AReachableStoreIsDrainedTests(DrainFixture):

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            first_name='Drain', last_name='Recipient', email='drain@notify.test',
            phone_number='256700009201', username='256700009201',
            country='Uganda', password='password', roles=[],
        )
        self.store = InMemoryMongo()
        producer_outbox = Outbox()
        with mock.patch('misc_app.controllers.save_to_mongo.MONGO_DB', self.store), \
                mock.patch('misc_app.controllers.notifications.notification.Messenger',
                           producer_outbox.messenger()):
            Notification(msg_data={
                'msg_type': 'password-change',
                'user_id': str(self.user.pk),
            }).create_notification()
        [self.pending] = self.store['notifications'].documents

    def test_CONTROL_a_pending_notification_is_sent_once_and_marked(self):
        self.assertNotIn('sent', self.pending)

        outbox, printed = self.drain(self.store)

        self.assertIn('=== Sending emails ===', printed)
        self.assertEqual(outbox.emails, [{
            'to': 'drain@notify.test', 'cc': [], 'subject': 'Dinify Password Changed',
        }])
        self.assertEqual(outbox.sms, [], 'only a credentials notification is texted here')
        [stored] = self.store['notifications'].documents
        self.assertIs(stored.get('sent'), True)

        again, _ = self.drain(self.store)
        self.assertEqual(again.emails, [], 'a sent notification is not sent again')
