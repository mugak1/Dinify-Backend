from django.test import TestCase, RequestFactory
from misc_app.controllers.check_required_information import check_required_information
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import SerializerPutRestaurantEmployee, SerializerPutRestaurant
from restaurants_app.tests import seed_restaurant, TEST_RESTAURANT_NAME
from restaurants_app.models import Restaurant
from dinify_backend.configs import (
    REQUIRED_INFORMATION,
    ROLES,
    EDIT_INFORMATION
)
from users_app.tests import seed_user, TEST_PHONE
from users_app.models import User


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
            self.assertEqual(data.get('pagination').get('page_size'), str(10))

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
                'edit_considerations': EDIT_INFORMATION.get('restaurant_registration'),
                'user_id': str(user.id),
                'username': TEST_PHONE,
                'success_message': 'The details of the restaurant have been updated successfully.',
                'error_message': 'An error occurred while updating the details of the restaurant.'
            }
            result = Secretary(data).update()
            self.assertEqual(result.get('status'), 200)
            restaurant.refresh_from_db()
            self.assertEqual(restaurant.name, 'New Restaurant Name')

        test_create()
        test_read()
        test_update()
