from django.test import TestCase
from misc_app.controllers.check_required_information import check_required_information
from controller_classes.misc_controller import MiscController

# Create your tests here.
class MiscAppTestFunctions(TestCase):
    """
    the test functions for the Misc app
    """
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
        provided_information1 = {
            'key1': 'value1',
        }
        result = MiscController(
            {
                "required_information": required_information,
                "provided_information": provided_information1
            }
        ).check_required_information()
        self.assertEqual(result['status'], False)

        # all requirements met
        provided_information2 = {
            'key1': 'value1',
            'key2': 'value2'
        }
        result = MiscController(
            {
                "required_information": required_information,
                "provided_information": provided_information2
            }
        ).check_required_information()
        self.assertEqual(result['status'], True)

        # key2 is not long enough
        provided_information3 = {
            'key1': 'value1',
            'key2': 'valu'
        }
        result = MiscController(
            {
                "required_information": required_information,
                "provided_information": provided_information3
            }
        ).check_required_information()
        self.assertEqual(result['status'], False)
