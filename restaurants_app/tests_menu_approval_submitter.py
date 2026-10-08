"""
The first-time menu approval's separation of duties: whoever submitted a menu
for approval may not approve it, unless they are the restaurant owner.

THE CHECK NEVER FIRED. It was written in November 2024 (``8b69a45``) to read the
submitter back out of the MongoDB action log, and the reader never matched the
writer, ``misc_app.controllers.save_action_log.save_action``:

* it filtered on ``affected_model`` / ``affected_record``, while the writer
  stores ``model`` / ``record``;
* it pinned ``action`` to the approval's own decision (``approve``) and then
  looked for ``action == 'submit'`` among the results, which no document can
  satisfy;
* it read ``user_id``, while the writer stores ``user.id``.

So the submitter was always ``None`` and the refusal was unreachable.

THE KEYS WERE NOT THE ONLY PROBLEM. ``save_action`` writes from a daemon thread
and swallows a failure, and MongoDB Atlas is recorded as unreachable from the
EC2 host. A submitter kept only in MongoDB is lost whenever MongoDB is, and a
check reading it would fail open exactly then. The lookup also iterated its
cursor outside its try/except, so a server the client could name but not reach
raised ``ServerSelectionTimeoutError`` out of the approval
(``dinify_backend/tests_mongo_unavailability.py`` establishes why).

THE SUBMITTER IS NOW RECORDED IN POSTGRESQL. A successful submit writes
``Restaurant.first_time_menu_submitted_by`` in the same ``save(update_fields=...)``
as the decision, and the approval reads it off the restaurant row it already
loads. The decision no longer reads MongoDB at all, so an unavailable MongoDB
changes nothing about it. ``save_action`` still writes the activity log,
unchanged.

A SUBMISSION WITH NO RECORDED SUBMITTER FAILS CLOSED. A menu awaiting approval
that names no submitter (submitted before this change, or whose submitter's
account was deleted) cannot be shown to have been submitted by someone else, so
only the owner may approve it. The owner exemption is unchanged.

Every request here goes through the real endpoint with a real customer JWT, and
the MongoDB layer is replaced by the stand-ins from
``dinify_backend.tests_mongo_unavailability`` rather than the test settings'
``MagicMock``, which iterates as empty and could not show any of this.
"""
import ast
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.test import TestCase

from dinify_backend.configss.edit_information import EDIT_INFORMATION
from dinify_backend.configss.string_definitions import (
    RESTAURANT_MANAGER, RESTAURANT_OWNER, RestaurantStatus_Live,
)
from dinify_backend.tests_mongo_unavailability import (
    InMemoryMongo, UnconfiguredMongo, UnreachableMongo, action_log_store,
)
from restaurants_app.models import (
    MenuItem, MenuSection, Restaurant, RestaurantEmployee, SerArcRestaurant,
)
from restaurants_app.serializers import (
    SerializerGetRestaurantDetail, SerializerPutRestaurant,
)
from users_app.customer_access import issue_customer_tokens
from users_app.models import User

CONTROLLER = 'restaurants_app.controllers.first_time_batch_approval'
CONTROLLER_SOURCE = (
    Path(__file__).resolve().parent / 'controllers' / 'first_time_batch_approval.py'
)
REVIEW_URL = '/api/v1/restaurant-setup/manager-actions/first-time-menu-review/'
FIELD = 'first_time_menu_submitted_by'

# Independent oracles, deliberately not imported from the controller. The first
# is the message the check has always carried, kept byte for byte.
SUBMITTED_IT_YOURSELF = 'Sorry, you cannot approve a menu that you submitted.'
SUBMITTER_NOT_ON_RECORD = (
    'Sorry, only the restaurant owner can approve this menu, '
    'because the person who submitted it is not on record.'
)


def _person(phone, first, last):
    return User.objects.create_user(
        first_name=first, last_name=last, email=f'{phone}@approval.test',
        phone_number=phone, username=phone, country='Uganda',
        password='password', roles=[],
    )


class ApprovalFixture(TestCase):
    """A restaurant whose menu is still waiting to be submitted (the shape every
    restaurant created between migrations 0014 and 0036 was given), an owner and
    two managers. The menu section was created by the OWNER, so the separate
    created-it-yourself check can never be what refuses anybody here."""

    def setUp(self):
        super().setUp()
        self.owner = _person('256700009101', 'Approval', 'Owner')
        self.manager_a = _person('256700009102', 'Manager', 'Alpha')
        self.manager_b = _person('256700009103', 'Manager', 'Bravo')
        self.restaurant = Restaurant.objects.create(
            name='Approval Kitchen', location='Kampala', owner=self.owner,
            status=RestaurantStatus_Live,
            first_time_menu_approval=False,
            first_time_menu_approval_decision='pending',
        )
        for person, role in ((self.owner, RESTAURANT_OWNER),
                             (self.manager_a, RESTAURANT_MANAGER),
                             (self.manager_b, RESTAURANT_MANAGER)):
            RestaurantEmployee.objects.create(
                user=person, restaurant=self.restaurant, roles=[role], active=True,
            )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, created_by=self.owner,
            approved=False, enabled=False, available=True,
        )
        self.item = MenuItem.objects.create(
            name='Rolex', section=self.section, primary_price='10000',
            approved=False, enabled=False, available=True, in_stock=True,
        )

    def review(self, person, decision, reason=None):
        body = {'restaurant': str(self.restaurant.id), 'decision': decision}
        if reason is not None:
            body['reason'] = reason
        token = issue_customer_tokens(person).access_token
        return self.client.post(
            REVIEW_URL, data=body, content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    @contextmanager
    def mongo(self, store):
        """Install ``store`` as MongoDB for the whole request, everywhere a
        reader could reach it from.

        The test settings put a ``MagicMock`` at ``dinify_backend.mongo_db``. It
        iterates as empty and never raises, so a MongoDB reader that reached it
        would go unseen. Three names are therefore patched:

        * ``save_action``'s module, where the activity log is written;
        * the controller's own ``MONGO_DB``, which a module-level import would
          bind. ``create=True`` because the controller no longer has one. It is
          also what lets this file run against the pre-fix controller and fail;
        * ``MONGO_DB`` on the mocked ``dinify_backend.mongo_db`` module itself,
          which an import inside a function would read at call time.
        """
        with action_log_store(store), \
                mock.patch(f'{CONTROLLER}.MONGO_DB', store, create=True), \
                mock.patch('dinify_backend.mongo_db.MONGO_DB', store):
            yield store

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json().get('message'), message)

    def assert_not_approved(self):
        self.restaurant.refresh_from_db()
        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'submit')
        self.assertFalse(self.restaurant.first_time_menu_approval)
        self.assertFalse(self.section.approved or self.section.enabled)
        self.assertFalse(self.item.approved or self.item.enabled)

    def assert_approved(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.restaurant.refresh_from_db()
        self.section.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'approve')
        self.assertTrue(self.restaurant.first_time_menu_approval)
        self.assertTrue(self.section.approved and self.section.enabled)
        self.assertTrue(self.item.approved and self.item.enabled)

    def submit_without_a_recorded_submitter(self):
        """The state a menu submitted before this change is in: awaiting
        approval, with nobody on record as its submitter."""
        Restaurant.objects.filter(pk=self.restaurant.pk).update(
            first_time_menu_approval_decision='submit')


class TheSubmitterCannotApproveTests(ApprovalFixture):

    def test_REGRESSION_the_submitter_cannot_approve_their_own_submission(self):
        store = InMemoryMongo()
        with self.mongo(store):
            submitted = self.review(self.manager_a, 'submit')
            self.assertEqual(submitted.status_code, 200, submitted.content)

            # What a reader needed was in the log all along, in the writer's
            # shape. The old reader asked for different keys and never found it.
            [entry] = store['action_logs'].documents
            self.assertEqual(entry['model'], 'restaurant-menu-approval')
            self.assertEqual(entry['record'], str(self.restaurant.id))
            self.assertEqual(entry['action'], 'submit')
            self.assertEqual(entry['user']['id'], str(self.manager_a.id))
            self.assertNotIn('affected_model', entry)
            self.assertNotIn('user_id', entry)

            response = self.review(self.manager_a, 'approve')

        self.assert_refused(response, SUBMITTED_IT_YOURSELF)
        self.assert_not_approved()

    def test_CONTROL_a_different_member_approves_the_submission(self):
        with self.mongo(InMemoryMongo()):
            self.review(self.manager_a, 'submit')
            response = self.review(self.manager_b, 'approve')
        self.assert_approved(response)

    def test_CONTROL_the_owner_approves_a_managers_submission(self):
        with self.mongo(InMemoryMongo()):
            self.review(self.manager_a, 'submit')
            response = self.review(self.owner, 'approve')
        self.assert_approved(response)

    def test_CONTROL_the_owner_may_approve_a_submission_they_made(self):
        """The owner exemption: it is their restaurant, and separation of duties
        within a tenant cannot bind the tenant's own principal."""
        with self.mongo(InMemoryMongo()):
            self.review(self.owner, 'submit')
            response = self.review(self.owner, 'approve')
        self.assert_approved(response)

    def test_a_rejection_leaves_the_submission_and_its_submitter_standing(self):
        """Rejection writes nothing to the restaurant row, as before: the menu
        stays submitted, so its submitter must stay on record."""
        with self.mongo(InMemoryMongo()):
            self.review(self.manager_a, 'submit')
            rejected = self.review(self.owner, 'reject', reason='Prices missing')
            self.assertEqual(rejected.status_code, 200, rejected.content)
            response = self.review(self.manager_a, 'approve')
        self.assert_refused(response, SUBMITTED_IT_YOURSELF)
        self.restaurant.refresh_from_db()
        self.assertEqual(getattr(self.restaurant, f'{FIELD}_id'), self.manager_a.id)


class TheSubmitterIsRecordedTests(ApprovalFixture):

    def test_a_submit_records_its_submitter_with_the_decision(self):
        captured = []
        original = Restaurant.save

        def capture(instance, *args, **kwargs):
            captured.append(kwargs.get('update_fields'))
            return original(instance, *args, **kwargs)

        with self.mongo(InMemoryMongo()), mock.patch.object(Restaurant, 'save', capture):
            response = self.review(self.manager_a, 'submit')

        self.assertEqual(response.status_code, 200, response.content)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.first_time_menu_approval_decision, 'submit')
        self.assertEqual(getattr(self.restaurant, f'{FIELD}_id'), self.manager_a.id)
        self.assertEqual(len(captured), 1, captured)
        self.assertEqual(
            set(captured[0] or []), {'first_time_menu_approval_decision', FIELD},
            'the submitter is written in the same narrow save as the decision, '
            'and nothing else on the row is',
        )

    def test_a_refused_second_submit_records_nothing(self):
        with self.mongo(InMemoryMongo()):
            self.review(self.manager_a, 'submit')
            again = self.review(self.manager_b, 'submit')
        self.assert_refused(again, 'Sorry, the restaurant menu has already been submitted.')
        self.restaurant.refresh_from_db()
        self.assertEqual(getattr(self.restaurant, f'{FIELD}_id'), self.manager_a.id)


class AnUnrecordedSubmitterFailsClosedTests(ApprovalFixture):

    def test_REGRESSION_only_the_owner_may_approve_a_submission_with_no_recorded_submitter(self):
        self.submit_without_a_recorded_submitter()
        with self.mongo(InMemoryMongo()):
            response = self.review(self.manager_b, 'approve')
        self.assert_refused(response, SUBMITTER_NOT_ON_RECORD)
        self.assert_not_approved()

    def test_CONTROL_the_owner_approves_a_submission_with_no_recorded_submitter(self):
        self.submit_without_a_recorded_submitter()
        with self.mongo(InMemoryMongo()):
            response = self.review(self.owner, 'approve')
        self.assert_approved(response)

    def test_a_deleted_submitter_leaves_a_submission_only_the_owner_can_approve(self):
        """The attribution is ``SET_NULL``, like every ``created_by``: deleting
        the account leaves the submission unattributed, never attributed to
        somebody else."""
        with self.mongo(InMemoryMongo()):
            self.review(self.manager_a, 'submit')
            self.manager_a.delete()
            response = self.review(self.manager_b, 'approve')
        self.restaurant.refresh_from_db()
        self.assertIsNone(getattr(self.restaurant, f'{FIELD}_id'))
        self.assert_refused(response, SUBMITTER_NOT_ON_RECORD)

    def test_SCOPE_a_menu_that_was_never_submitted_is_not_governed_by_this_rule(self):
        """The rule is about a SUBMISSION. Approval has never required one: a
        member with the menu module can approve a menu that is still ``pending``,
        and this change keeps that. Whether approval should require a prior
        submission is a separate decision, reported with this change rather than
        made by it; this test is here so that decision is taken on purpose."""
        with self.mongo(InMemoryMongo()):
            response = self.review(self.manager_b, 'approve')
        self.assertEqual(response.status_code, 200, response.content)


class TheDecisionDoesNotDependOnMongoTests(ApprovalFixture):
    """MongoDB is the activity log, not the record the decision rests on."""

    def test_REGRESSION_an_unreachable_mongodb_raises_nothing_and_changes_nothing(self):
        """The server can be named but not reached, which is when the old
        reader raised out of the approval from its unguarded iteration."""
        store = UnreachableMongo()
        with self.mongo(store), \
                self.assertLogs('misc_app.controllers.save_action_log', level='ERROR') as logged:
            submitted = self.review(self.manager_a, 'submit')
            own = self.review(self.manager_a, 'approve')
            other = self.review(self.manager_b, 'approve')

        self.assertEqual(submitted.status_code, 200, submitted.content)
        self.assert_refused(own, SUBMITTED_IT_YOURSELF)
        self.assert_approved(other)
        self.assertNotIn(('action_logs', 'find'), store.calls)
        self.assertTrue(
            any('Failed to write action log to MongoDB' in line for line in logged.output),
            'the activity-log write still fails visibly, inside its own guard',
        )

    def test_REGRESSION_the_owner_approves_while_mongodb_is_unreachable(self):
        """The old reader iterated its cursor for EVERY approval, the owner's
        included, so an unreachable server failed the owner's approval too."""
        store = UnreachableMongo()
        with self.mongo(store), \
                self.assertLogs('misc_app.controllers.save_action_log', level='ERROR'):
            self.review(self.manager_a, 'submit')
            approved = self.review(self.owner, 'approve')
        self.assert_approved(approved)
        self.assertNotIn(('action_logs', 'find'), store.calls)

    def test_REGRESSION_an_unconfigured_mongodb_does_not_open_the_rule(self):
        """The client cannot be built, so the proxy refuses at subscription. The
        old reader caught that and proceeded with no submitter, which approved
        the submitter's own menu."""
        store = UnconfiguredMongo()
        with self.mongo(store), \
                self.assertLogs('misc_app.controllers.save_action_log', level='ERROR'):
            self.review(self.manager_a, 'submit')
            own = self.review(self.manager_a, 'approve')
        self.assert_refused(own, SUBMITTED_IT_YOURSELF)
        self.assert_not_approved()

    def test_the_decision_never_reads_the_action_log(self):
        store = InMemoryMongo()
        with self.mongo(store):
            self.review(self.manager_a, 'submit')
            self.review(self.manager_a, 'approve')
            self.review(self.manager_b, 'approve')
        self.assertNotIn(('action_logs', 'find'), store.calls)
        self.assertEqual(
            [entry['action'] for entry in store['action_logs'].documents],
            ['submit', 'approve'],
            'the log still records each decision that was made, and only those',
        )

    def test_the_controller_imports_nothing_from_the_mongodb_module(self):
        tree = ast.parse(CONTROLLER_SOURCE.read_text())
        modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn('dinify_backend.mongo_db', modules)
        self.assertNotIn('pymongo', modules)


class TheSubmitterIsServerOwnedTests(ApprovalFixture):
    """Who submitted is written by the submit decision and nowhere else."""

    def test_it_is_not_editable_through_secretary(self):
        keys = {entry['key'] for entry in EDIT_INFORMATION['restaurants']}
        self.assertNotIn(FIELD, keys)

    def test_the_restaurant_write_serializer_cannot_write_it(self):
        field = SerializerPutRestaurant().fields.get(FIELD)
        self.assertTrue(field is None or field.read_only)

    def test_the_all_fields_read_serializers_expose_it_read_only(self):
        """``editable=False`` on the model is what keeps the two ``__all__``
        serializers from growing a writable user relation, which the tenancy
        ratchet would rightly refuse."""
        self.assertFalse(Restaurant._meta.get_field(FIELD).editable)
        for serializer in (SerializerGetRestaurantDetail(), SerArcRestaurant()):
            self.assertTrue(serializer.fields[FIELD].read_only, type(serializer).__name__)

    def test_a_restaurants_put_cannot_set_it(self):
        token = issue_customer_tokens(self.owner).access_token
        response = self.client.put(
            '/api/v1/restaurant-setup/restaurants/',
            data={'id': str(self.restaurant.id), 'name': 'Approval Kitchen Two',
                  FIELD: str(self.manager_a.id)},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.restaurant.refresh_from_db()
        self.assertEqual(self.restaurant.name, 'Approval Kitchen Two')
        self.assertIsNone(getattr(self.restaurant, f'{FIELD}_id'))
