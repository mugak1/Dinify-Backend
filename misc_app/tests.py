from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from misc_app.controllers.check_required_information import check_required_information
from misc_app.controllers.secretary import (
    Secretary, make_notification_for_new_entry,
)
from misc_app.controllers.determine_changes import determine_changes
from restaurants_app.serializers import SerializerPutRestaurantEmployee, SerializerPutRestaurant
from restaurants_app.tests import seed_restaurant, TEST_RESTAURANT_NAME
from restaurants_app.models import Restaurant
from dinify_backend.configs import ROLES
from dinify_backend.configss.edit_information import EDIT_INFORMATION
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User
from misc_app.controllers.notifications.notification import Notification


# Create your tests here.
class MiscAppTestFunctions(TestCase):
    """
    the test functions for the Misc app
    """
    def setUp(self):
        """
        setup the test
        """
        seed_user()
        seed_restaurant(seed_owner=False)

    def test_check_required_information(self):
        """
        test the function to check for required information
        """
        required_information = [
            {
                "key": "key1",
                "label": "Key 1",
                "min_length": 5
            },
            {
                "key": "key2",
                "label": "Key 2",
                "min_length": 5
            }
        ]

        # missing key2
        provided_information1 = {'key1': 'value1'}
        result = check_required_information(
            required_information,
            provided_information1
        )
        self.assertEqual(result['status'], False)

        # all requirements met
        provided_information2 = {
            'key1': 'value1',
            'key2': 'value2'
        }
        result = check_required_information(
            required_information,
            provided_information2
        )
        self.assertEqual(result['status'], True)

        # key2 is not long enough
        provided_information3 = {
            'key1': 'value1',
            'key2': 'valu'
        }
        result = check_required_information(
            required_information,
            provided_information3
        )
        self.assertEqual(result['status'], False)

    def test_secretary(self):
        """
        test the Secretary
        """
        user = User.objects.get(username=TEST_PHONE)
        restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

        def test_create():
            """
            testing secretary create
            """
            data = {
                'request': None,
                'serializer': SerializerPutRestaurantEmployee,
                'required_information': [],
                'data': {
                    'user': str(user.id),
                    'restaurant': str(restaurant.id),
                    'roles': [ROLES.get('RESTAURANT_OWNER')]
                },
                'user_id': str(User.objects.get(username=TEST_PHONE).id),
                'username': TEST_PHONE,
                'user': user,
                'msg_type': 'new-restaurant-employee',
                'success_message': 'The restaurant employee has been added successfully.',
                'error_message': 'An error occurred while adding the restaurant employee.'
            }
            result = Secretary(data).create()
            self.assertEqual(result.get('status'), 200)

        def test_read():
            """
            testing secretary read
            """
            # test without pagination
            data = {
                'request': None,
                'serializer': SerializerPutRestaurantEmployee,
                'filter': {'deleted': False},
                'paginate': False,
                'user_id': str(user.id),
                'username': TEST_PHONE,
                'success_message': 'Successfully retrieved the restaurant employees.',
                'error_message': 'Error while retrieving the list of restaurant employees.'
            }
            result = Secretary(data).read()
            self.assertEqual(result['status'], 200)

            # test with pagination
            request = RequestFactory().get('/?page=1&page_size=10')
            data = {
                'request': request,
                'serializer': SerializerPutRestaurantEmployee,
                'filter': {'deleted': False},
                'paginate': True,
                'user_id': TEST_PHONE,
                'username': TEST_PHONE,
                'success_message': 'Successfully retrieved the restaurant employees.',
                'error_message': 'Error while retrieving the list of restaurant employees.'
            }
            result = Secretary(data).read()
            self.assertEqual(result.get('status'), 200)
            data = result.get('data')
            self.assertEqual(data.get('pagination').get('page_size'), 10)

        def test_update():
            """
            test record update
            """
            data = {
                'request': None,
                'serializer': SerializerPutRestaurant,
                'data': {
                    'id': str(restaurant.id),
                    'name': 'new Restaurant name',
                },
                'edit_considerations': EDIT_INFORMATION.get('restaurants'),
                'user_id': str(user.id),
                'username': TEST_PHONE,
                'success_message': 'The details of the restaurant have been updated successfully.',
                'error_message': 'An error occurred while updating the details of the restaurant.'
            }
            result = Secretary(data).update()
            self.assertEqual(result.get('status'), 200)
            restaurant.refresh_from_db()
            self.assertEqual(restaurant.name, 'New Restaurant Name')

        def test_delete():
            """
            test record deletion
            """
            data = {
                'request': None,
                'serializer': SerializerPutRestaurant,
                'data': {
                    'id': str(restaurant.id),
                    'deletion_reason': 'Test deletion'
                },
                'user_id': str(user.id),
                'username': TEST_PHONE,
            }
            result = Secretary(data).delete()
            self.assertEqual(result.get('status'), 200)
            restaurant.refresh_from_db()
            self.assertEqual(restaurant.deleted, True)

        test_create()
        test_read()
        test_update()
        test_delete()

    def test_determine_changes(self):
        """
        test the function to determine changes
        """
        old_data = {
            'name': 'old name',
            'location': 'old location',
        }
        new_data = {
            'name': 'new name',
            'location': 'old location',
        }
        consider = ['name', 'location']
        result = determine_changes({
            'old_info': old_data,
            'new_info': new_data,
            'consider': consider
        })
        self.assertEqual(len(result), 1)

    def test_notification(self):
        """
        test the notification class
        """
        data = {
            'msg_type': 'new-restaurant',
            'first_name': 'John',
            'restaurant_name': 'Test Restaurant'
        }
        notification = Notification(msg_data=data)
        notification.create_notification()
        self.assertIsNotNone(notification)


class SecretaryAbsentVsNullSemanticTests(TestCase):
    """
    Locks in the absent-vs-None contract for Secretary.update():

      - key absent from payload → field is left untouched on the model
      - key present with explicit None → field is cleared on the model

    Pre-fix, secretary.py:316 used `if self.data.get(key) is not None`
    which collapsed both cases into "skip", forcing the Bug 6
    `clear_<field>: true` sentinel workaround. The fix replaces it with
    `if key in self.data` and adds a None-guard around the
    `text_presentation` char block so a null payload to a name/status
    field doesn't blow up `str.title(None)`.
    """

    def setUp(self):
        from decimal import Decimal
        from restaurants_app.models import (
            Restaurant, MenuSection, MenuItem,
        )
        from dinify_backend.configss.string_definitions import (
            RestaurantStatus_Active,
        )
        self.owner = User.objects.create_user(
            first_name='Secretary', last_name='Tester',
            email='secretary_tester@test.com', phone_number='256700000070',
            username='256700000070', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Secretary Test Restaurant', location='loc',
            status=RestaurantStatus_Active, owner=self.owner,
        )
        self.section = MenuSection.objects.create(
            name='Mains', restaurant=self.restaurant, listing_position=0,
        )
        self.item = MenuItem.objects.create(
            name='Burger', section=self.section,
            primary_price=Decimal('10.00'),
            calories=200,
            discounted_price=Decimal('5.00'),
            listing_position=0,
        )

    def _menu_item_args(self, data):
        from restaurants_app.serializers import SerializerPutMenuItem
        return {
            'serializer': SerializerPutMenuItem,
            'data': data,
            'edit_considerations': EDIT_INFORMATION.get('menu_item'),
            'user_id': str(self.owner.id),
            'username': self.owner.username,
            'success_message': 'ok',
            'error_message': 'err',
        }

    def test_explicit_null_clears_field(self):
        """Explicit None in payload clears the field (was a no-op pre-fix)."""
        result = Secretary(self._menu_item_args({
            'id': str(self.item.id),
            'calories': None,
        })).update()
        self.assertEqual(result.get('status'), 200)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.calories)

    def test_absent_field_skipped(self):
        """Keys not present in payload leave the model field untouched."""
        result = Secretary(self._menu_item_args({
            'id': str(self.item.id),
            'name': 'Burger Renamed',
        })).update()
        self.assertEqual(result.get('status'), 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.calories, 200)

    def test_text_presentation_guarded_against_null(self):
        """
        Regression guard for the char-block landmine: a null payload to a
        text_presentation-bearing field (e.g. name → str.title) must not
        raise TypeError. Whether the underlying serializer accepts None on
        a non-nullable field is a separate concern; what matters here is
        that Secretary itself doesn't crash before the serializer runs.
        """
        try:
            result = Secretary(self._menu_item_args({
                'id': str(self.item.id),
                'name': None,
            })).update()
        except TypeError as exc:
            self.fail(f"Secretary raised TypeError on null name: {exc}")
        # Whatever the serializer decides (likely 400 validation), the
        # important thing is no exception escaped Secretary.
        self.assertIn(result.get('status'), (200, 400))

    def test_determine_changes_records_null_transitions(self):
        """A null transition is now an audit-log change row."""
        from misc_app.controllers.determine_changes import determine_changes
        old_info = {'name': 'Burger', 'calories': 200}
        new_info = {'name': 'Burger Renamed', 'calories': None}
        changes = determine_changes({
            'old_info': old_info,
            'new_info': new_info,
            'consider': ['name', 'calories'],
        })
        self.assertEqual(len(changes), 2)
        calories_change = next(c for c in changes if c['field'] == 'calories')
        self.assertEqual(calories_change['old_value'], 200)
        self.assertIsNone(calories_change['new_value'])


class BackendTechDebtBundleTests(TestCase):
    """Regression guards for the DinifyPaginator robustness fixes:
    string-vs-int page coercion and out-of-range graceful handling."""

    def setUp(self):
        self.factory = RequestFactory()

    def _paginate(self, query_string, records):
        from misc_app.controllers.paginator import DinifyPaginator
        request = self.factory.get(f'/?{query_string}')
        return DinifyPaginator({
            'request': request,
            'records': records,
        }).paginate()

    def test_paginator_handles_string_page_param(self):
        # request.GET values are always strings; the paginator must coerce
        # rather than passing the raw string through to Paginator.page().
        records = list(range(1, 11))
        response = self._paginate('page=1&page_size=5', records)
        pagination = response['pagination']
        self.assertEqual(list(response['records']), [1, 2, 3, 4, 5])
        self.assertEqual(pagination['current_page'], 1)
        self.assertEqual(pagination['page_size'], 5)
        self.assertEqual(pagination['number_of_pages'], 2)
        self.assertTrue(pagination['has_next'])
        self.assertFalse(pagination['has_previous'])

    def test_paginator_handles_invalid_page_param(self):
        # ?page=abc previously bubbled a ValueError out of Paginator.page().
        # Should fall back to page 1 instead of 500-ing.
        records = list(range(1, 6))
        response = self._paginate('page=abc&page_size=5', records)
        pagination = response['pagination']
        self.assertEqual(list(response['records']), [1, 2, 3, 4, 5])
        self.assertEqual(pagination['current_page'], 1)

    def test_paginator_handles_out_of_range_page(self):
        # ?page=999 against a 1-page dataset previously raised EmptyPage.
        # Should return an empty records list with the same pagination shape.
        records = list(range(1, 6))
        response = self._paginate('page=999&page_size=5', records)
        pagination = response['pagination']
        self.assertEqual(list(response['records']), [])
        self.assertEqual(pagination['current_page'], 999)
        self.assertEqual(pagination['number_of_pages'], 1)
        self.assertFalse(pagination['has_next'])
        self.assertFalse(pagination['has_previous'])
        self.assertTrue(pagination['paginated'])
        self.assertEqual(pagination['total_records'], 5)

    def test_paginator_handles_zero_page_size(self):
        # ?page_size=0 previously reached Paginator(per_page=0), whose
        # num_pages does ceil(hits / 0) -> ZeroDivisionError. That's not an
        # EmptyPage/InvalidPage, so it escaped the catch and 500'd. The
        # max(1, ...) floor coerces it to 1 instead of crashing.
        records = list(range(1, 6))
        response = self._paginate('page=1&page_size=0', records)
        pagination = response['pagination']
        self.assertEqual(list(response['records']), [1])
        self.assertEqual(pagination['page_size'], 1)
        self.assertEqual(pagination['number_of_pages'], 5)
        self.assertTrue(pagination['has_next'])

    def test_paginator_floors_negative_page_size(self):
        # A negative page_size used to produce odd negative-stride slices.
        # It is floored to 1 like the zero case.
        records = list(range(1, 6))
        response = self._paginate('page=1&page_size=-5', records)
        pagination = response['pagination']
        self.assertEqual(list(response['records']), [1])
        self.assertEqual(pagination['page_size'], 1)

    def test_paginator_floors_zero_and_negative_page(self):
        # ?page=0 / negative page are nonsensical; floor them to page 1 rather
        # than returning an empty out-of-range page.
        records = list(range(1, 6))
        for query in ('page=0&page_size=5', 'page=-3&page_size=5'):
            response = self._paginate(query, records)
            pagination = response['pagination']
            self.assertEqual(pagination['current_page'], 1, query)
            self.assertEqual(list(response['records']), [1, 2, 3, 4, 5], query)


class MsgBuilderContractTests(TestCase):
    """
    Locks in the dual-contract msg_data shape produced by
    `make_notification_for_new_entry`:

      - **Recipient-greeted** (msg_type='new-restaurant-employee'):
        the new entity's owner is the recipient; msg_data carries
        first_name + user_id derived from record.instance.user.
      - **Restaurant-greeted** (default, e.g. 'new-menu-section'):
        the restaurant is the greetee; msg_data carries the actor's
        full name as `user` plus item_name.

    Pre-fix, the helper produced only the restaurant-greeted shape,
    so the restaurant builder's `msg_data['first_name']` lookup raised
    KeyError (silently swallowed) for 'new-restaurant-employee' — and
    the recipient lookup downstream broke too because user_id wasn't
    set. Also pins the msg_builder_menu_groups.py alignment to read
    `item_name` instead of the outlier `group_name`.
    """

    def setUp(self):
        self.actor = User.objects.create_user(
            first_name='Bob', last_name='Actor',
            email='bob_actor@test.com', phone_number='256700000080',
            username='256700000080', country='Uganda', password='password',
            roles=[],
        )
        self.recipient = User.objects.create_user(
            first_name='Alice', last_name='Recipient',
            email='alice_recipient@test.com', phone_number='256700000081',
            username='256700000081', country='Uganda', password='password',
            roles=[],
        )
        self.restaurant = Restaurant.objects.create(
            name='Contract Test Restaurant', location='loc',
            owner=self.actor,
        )

    def _patch_notification(self):
        return patch('misc_app.controllers.secretary.Notification')

    def test_recipient_greeted_contract_for_new_employee(self):
        record = MagicMock()
        record.instance.user = self.recipient
        with self._patch_notification() as mock_notification:
            make_notification_for_new_entry(
                restaurant_id=str(self.restaurant.id),
                user=self.actor,
                item_name=None,
                msg_type='new-restaurant-employee',
                record=record,
            )
        mock_notification.assert_called_once()
        msg_data = mock_notification.call_args.args[0]
        self.assertEqual(msg_data['msg_type'], 'new-restaurant-employee')
        self.assertEqual(msg_data['first_name'], 'Alice')
        self.assertEqual(msg_data['user_id'], str(self.recipient.id))
        self.assertEqual(msg_data['restaurant_name'], 'Contract Test Restaurant')
        self.assertNotIn('user', msg_data)
        self.assertNotIn('item_name', msg_data)

    def test_restaurant_greeted_contract_for_menu_section(self):
        with self._patch_notification() as mock_notification:
            make_notification_for_new_entry(
                restaurant_id=str(self.restaurant.id),
                user=self.actor,
                item_name='Starters',
                msg_type='new-menu-section',
                record=None,
            )
        mock_notification.assert_called_once()
        msg_data = mock_notification.call_args.args[0]
        self.assertEqual(msg_data['msg_type'], 'new-menu-section')
        self.assertEqual(msg_data['user'], 'Bob Actor')
        self.assertEqual(msg_data['item_name'], 'Starters')
        self.assertEqual(msg_data['restaurant_name'], 'Contract Test Restaurant')
        self.assertNotIn('first_name', msg_data)
        self.assertNotIn('user_id', msg_data)

    def test_recipient_greeted_skips_when_record_missing(self):
        with self._patch_notification() as mock_notification, \
                self.assertLogs('misc_app.controllers.secretary', level='WARNING') as logs:
            make_notification_for_new_entry(
                restaurant_id=str(self.restaurant.id),
                user=self.actor,
                item_name=None,
                msg_type='new-restaurant-employee',
                record=None,
            )
        mock_notification.assert_not_called()
        self.assertTrue(any(
            'recipient-greeted contract requires record.instance' in msg
            for msg in logs.output
        ), f"Expected warning not found in: {logs.output}")

    def test_menu_group_uses_item_name(self):
        from misc_app.controllers.notifications.msg_builder_menu_groups import (
            make_menu_group_messages,
        )
        msg_data = {
            'msg_type': 'new-menu-group',
            'restaurant_name': 'Contract Test Restaurant',
            'user': 'Bob Actor',
            'item_name': 'Spicy Sides',
        }
        # Pre-fix this raised KeyError on 'group_name'. Post-fix the
        # builder reads item_name and embeds it in the rendered email.
        message = make_menu_group_messages(msg_data, footer='')
        self.assertIn('Spicy Sides', message['email'])
        self.assertEqual(message['subject'], 'Menu Group Created')

    def test_create_employee_notification_succeeds_end_to_end(self):
        """
        Reproduces the catch-all endpoint flow at
        restaurants_app/endpoints/restaurant_setup.py:596 — POST
        /restaurant-setup/employees/ — which is the user-facing path
        that sets msg_type='new-restaurant-employee' on Secretary args
        and triggers `make_notification_for_new_entry`. Pre-fix this
        raised KeyError('first_name') in
        msg_builder_restaurant.py:65 (silently swallowed by the
        Secretary try/except as ERROR-log noise).
        """
        secretary_args = {
            'serializer': SerializerPutRestaurantEmployee,
            'data': {
                'user': str(self.recipient.id),
                'restaurant': str(self.restaurant.id),
                'roles': [ROLES.get('RESTAURANT_KITCHEN')],
            },
            'required_information': [],
            'user_id': str(self.actor.id),
            'username': self.actor.username,
            'user': self.actor,
            'msg_type': 'new-restaurant-employee',
            'success_message': 'ok',
            'error_message': 'err',
        }
        secretary_logger = 'misc_app.controllers.secretary'
        with self.assertLogs(secretary_logger, level='DEBUG') as captured:
            # Drop a benign DEBUG to satisfy assertLogs (it requires
            # at least one record). Then assert no ERROR records fire
            # — pre-fix the KeyError swallow logged ERROR here.
            import logging
            logging.getLogger(secretary_logger).debug('test marker')
            result = Secretary(secretary_args).create()
        self.assertEqual(result.get('status'), 200)
        error_records = [
            line for line in captured.output
            if line.startswith(f'ERROR:{secretary_logger}')
        ]
        self.assertEqual(
            error_records, [],
            f"Unexpected ERROR log on {secretary_logger}: {error_records}",
        )
class SecretaryNotificationDispatchTests(TestCase):
    """Locks in the actor-required contract for Secretary's create-time
    notification dispatch:

      - msg_type absent  -> notification skipped, no log noise
      - msg_type set, user missing -> WARNING log, no AttributeError swallow
      - msg_type set, user set -> dispatch reaches Notification.create_notification

    Plus an end-to-end regression on create_employee, which previously
    triggered the 'NoneType' object has no attribute 'first_name' swallow.
    """

    SECRETARY_LOGGER = 'misc_app.controllers.secretary'

    def setUp(self):
        seed_user()
        seed_restaurant()
        self.user = User.objects.get(username=TEST_PHONE)
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

    def _employee_args(self, *, include_user, include_msg_type):
        # Builds the same kind of args dict the production callers pass.
        # The 'data' payload's 'user' is the FK on the new RestaurantEmployee
        # row and is unrelated to Secretary's top-level 'user' actor kwarg.
        new_user = User.objects.create_user(
            first_name='Notif', last_name='Target',
            email='notif_target@test.com', phone_number='256700000099',
            username='256700000099', country='Uganda', password='password',
            roles=[],
        )
        args = {
            'request': None,
            'serializer': SerializerPutRestaurantEmployee,
            'required_information': [],
            'data': {
                'user': str(new_user.id),
                'restaurant': str(self.restaurant.id),
                'roles': [ROLES.get('RESTAURANT_KITCHEN')],
            },
            'user_id': str(self.user.id),
            'username': TEST_PHONE,
            'success_message': 'ok',
            'error_message': 'err',
        }
        if include_user:
            args['user'] = self.user
        if include_msg_type:
            args['msg_type'] = 'new-restaurant-employee'
        return args

    def test_notification_skipped_when_msg_type_absent(self):
        # Without msg_type the dispatch is a no-op: no ERROR log, record saved.
        import logging
        with self.assertLogs(self.SECRETARY_LOGGER, level=logging.WARNING) as cm:
            # assertLogs requires at least one log record at the given level;
            # emit a sentinel so the context manager doesn't fail when the
            # production code is correctly silent.
            logging.getLogger(self.SECRETARY_LOGGER).warning('sentinel')
            result = Secretary(self._employee_args(
                include_user=False, include_msg_type=False,
            )).create()
        self.assertEqual(result.get('status'), 200)
        # Only the sentinel is allowed; nothing from secretary.py.
        secretary_records = [
            r for r in cm.records if r.getMessage() != 'sentinel'
        ]
        self.assertEqual(secretary_records, [])

    def test_notification_warns_when_user_missing(self):
        # msg_type set, user missing -> WARNING log, no swallowed ERROR.
        import logging
        with self.assertLogs(self.SECRETARY_LOGGER, level=logging.WARNING) as cm:
            result = Secretary(self._employee_args(
                include_user=False, include_msg_type=True,
            )).create()
        self.assertEqual(result.get('status'), 200)
        warnings = [r for r in cm.records if r.levelno == logging.WARNING]
        errors = [r for r in cm.records if r.levelno >= logging.ERROR]
        self.assertTrue(
            any('no actor user supplied' in r.getMessage() for r in warnings),
            f'expected WARNING about missing actor, got: {[r.getMessage() for r in warnings]}',
        )
        self.assertEqual(
            errors, [],
            f'unexpected ERROR logs from secretary: {[r.getMessage() for r in errors]}',
        )

    def test_notification_dispatches_when_both_present(self):
        # Both kwargs set -> Notification.create_notification is invoked once,
        # and no swallowed ERROR fires from the secretary notification block.
        from unittest.mock import patch
        import logging
        target = 'misc_app.controllers.secretary.Notification'
        with patch(target) as MockNotification:
            MockNotification.return_value.create_notification.return_value = None
            with self.assertLogs(self.SECRETARY_LOGGER, level=logging.WARNING) as cm:
                logging.getLogger(self.SECRETARY_LOGGER).warning('sentinel')
                result = Secretary(self._employee_args(
                    include_user=True, include_msg_type=True,
                )).create()
        self.assertEqual(result.get('status'), 200)
        self.assertEqual(MockNotification.return_value.create_notification.call_count, 1)
        secretary_errors = [
            r for r in cm.records
            if r.levelno >= logging.ERROR and r.name == self.SECRETARY_LOGGER
        ]
        self.assertEqual(secretary_errors, [])

    def test_create_employee_does_not_log_secretary_error(self):
        # End-to-end regression for the upstream fix only: create_employee
        # previously triggered 'NoneType' object has no attribute 'first_name'
        # inside Secretary because it forgot to pass 'user' and 'msg_type'.
        # The downstream Notification stack has separate fragility for the
        # 'new-restaurant-employee' msg_type (msg builder expects a
        # 'first_name' key that make_notification_for_new_entry doesn't
        # populate); that's flagged as out-of-scope follow-up. Mock the
        # Notification class so this test isolates the Secretary layer.
        from restaurants_app.controllers.create_employee import create_employee
        from unittest.mock import patch
        import logging
        target = 'misc_app.controllers.secretary.Notification'
        with patch(target) as MockNotification:
            MockNotification.return_value.create_notification.return_value = None
            with self.assertLogs(self.SECRETARY_LOGGER, level=logging.WARNING) as cm:
                logging.getLogger(self.SECRETARY_LOGGER).warning('sentinel')
                result = create_employee(
                    first_name='New',
                    last_name='Hire',
                    email='new_hire@test.com',
                    phone_number='256700000088',
                    restaurant=self.restaurant,
                    roles=[ROLES.get('RESTAURANT_KITCHEN')],
                    creator=self.restaurant.owner,
                    skip_otp=True,
                )
        self.assertEqual(result.get('status'), 200)
        # The specific pre-fix error must be gone.
        nonetype_errors = [
            r for r in cm.records
            if r.name == self.SECRETARY_LOGGER
            and r.levelno >= logging.ERROR
            and 'NoneType' in r.getMessage()
        ]
        self.assertEqual(
            nonetype_errors, [],
            f'pre-fix NoneType error still leaks: '
            f'{[r.getMessage() for r in nonetype_errors]}',
        )
        # And the missing-actor warning must NOT fire — create_employee now
        # passes the actor through.
        actor_warnings = [
            r for r in cm.records
            if r.name == self.SECRETARY_LOGGER
            and 'no actor user supplied' in r.getMessage()
        ]
        self.assertEqual(actor_warnings, [])


class SecretaryUpdateNotificationTests(TestCase):
    """
    Locks in Secretary.make_notification's null-safety contract for the
    restaurant-activated / restaurant-rejected status transition path.

    Pre-fix, secretary.py's make_notification dereferenced
    new_record.instance.owner.first_name unconditionally. Restaurant.owner
    is a non-nullable FK (restaurants_app/models.py:Restaurant.owner) but
    User.first_name is nullable (users_app/models.py:User.first_name has
    null=True, blank=True), so production rows do exist with first_name=''
    or None — and any such owner triggered an AttributeError on the
    f-string in msg_builder_restaurant.py rendering "Hello {first_name},".

    The owner-None case is not directly reachable through the schema, so
    we don't test it here; the guard added in secretary.py is
    defence-in-depth for partial fixtures and any future schema change.
    """

    def setUp(self):
        seed_user()
        seed_restaurant()
        self.actor = User.objects.get(username=TEST_PHONE)
        self.restaurant = Restaurant.objects.get(name=TEST_RESTAURANT_NAME)

    def _activate_restaurant(self):
        # Drives the same code path RestaurantSetupEndpoint.put hits when
        # an admin moves a restaurant from pending to active.
        return Secretary({
            'serializer': SerializerPutRestaurant,
            'data': {
                'id': str(self.restaurant.id),
                'status': 'active',
            },
            'edit_considerations': EDIT_INFORMATION.get('restaurants'),
            'user_id': str(self.actor.id),
            'username': TEST_PHONE,
            'success_message': 'ok',
            'error_message': 'err',
        }).update()

    def test_update_notification_with_complete_owner(self):
        """Regression: owner with a populated first_name dispatches normally."""
        # The seed user already has first_name='Test' (users_app.tests.seed_user).
        target = 'misc_app.controllers.secretary.Notification'
        with patch(target) as MockNotification:
            MockNotification.return_value.create_notification.return_value = None
            result = self._activate_restaurant()
        self.assertEqual(result.get('status'), 200)
        MockNotification.assert_called_once()
        msg_data = MockNotification.call_args.kwargs.get(
            'msg_data', MockNotification.call_args.args[0]
            if MockNotification.call_args.args else {}
        )
        self.assertEqual(msg_data['msg_type'], 'restaurant-activated')
        self.assertEqual(msg_data['first_name'], 'Test')
        self.assertEqual(msg_data['user_id'], str(self.actor.id))
        self.assertEqual(msg_data['restaurant_id'], str(self.restaurant.id))

    def test_update_notification_with_missing_first_name(self):
        """Owner with first_name=None must dispatch the fallback greeting."""
        # User.first_name is nullable, so this is the production-reachable case.
        self.actor.first_name = None
        self.actor.save(update_fields=['first_name'])

        target = 'misc_app.controllers.secretary.Notification'
        with patch(target) as MockNotification:
            MockNotification.return_value.create_notification.return_value = None
            try:
                result = self._activate_restaurant()
            except AttributeError as exc:
                self.fail(
                    f"Secretary.make_notification raised AttributeError on "
                    f"null first_name: {exc}"
                )
        self.assertEqual(result.get('status'), 200)
        MockNotification.assert_called_once()
        msg_data = MockNotification.call_args.kwargs.get(
            'msg_data', MockNotification.call_args.args[0]
            if MockNotification.call_args.args else {}
        )
        self.assertEqual(msg_data['msg_type'], 'restaurant-activated')
        # Fallback greeting target so the email reads "Hello there,".
        self.assertEqual(msg_data['first_name'], 'there')
        self.assertEqual(msg_data['user_id'], str(self.actor.id))
