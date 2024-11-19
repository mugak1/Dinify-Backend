from django.test import TestCase
from dinify_backend.configss.messages import MESSAGES
from users_app.controllers.self_register import self_register
from users_app.controllers.login import login
from users_app.controllers.change_password import change_password
from users_app.controllers.reset_password import reset_password
from users_app.models import User, UserOtp
from users_app.controllers.otp_manager import OtpManager


TEST_PHONE = '1234567890'
TEST_EMAIL = 'test@user.com'


def seed_user():
    """
    seed a user to work with for tests
    """
    User.objects.create_user(
        first_name='Test',
        last_name='User',
        email=TEST_EMAIL,
        phone_number=TEST_PHONE,
        username=TEST_PHONE,
        country='Uganda',
        password='password'
    )

# Create your tests here.
class UsersAppTestFunctions(TestCase):
    """
    the test functions for the Users app
    """
    def setUp(self):
        """
        set up for the tests
        """
        seed_user()

    def test_self_registration(self):
        """
        test self_registration
        """
        data = {
            'first_name': 'John',
            'last_name': 'Doe',
            'email': 'john@doe.com',
            'phone_number': '256712345678',
            'password': 'password',
            'country': 'Uganda'
        }

        def test_success():
            """ when the registration is successful """
            response = self_register(data)
            self.assertEqual(response.get('status'), 200)
            self.assertEqual(
                response.get('message'),
                MESSAGES.get('OK_SELF_REGISTER')
            )

        def test_duplicate_phone_number():
            response = self_register(data)
            self.assertEqual(response.get('status'), 400)
            self.assertEqual(
                response.get('message'),
                MESSAGES.get('PHONE_NUMBER_EXISTS')
            )

        def test_duplicate_email():
            data['phone_number'] = '256712345679'
            response = self_register(data)            
            self.assertEqual(response.get('status'), 400)
            self.assertEqual(
                response.get('message'),
                MESSAGES.get('EMAIL_EXISTS')
            )

        test_success()
        test_duplicate_phone_number()
        test_duplicate_email()

    def test_login(self):
        """
        test login
        """
        def test_success():
            """ when the login is successful """
            response = login('1234567890', 'password')
            self.assertEqual(response.get('status'), 200)
            self.assertEqual(response.get('message'), MESSAGES.get('OK_LOGIN'))

        def test_wrong_password():
            """ when the password is wrong """
            response = login('1234567890', 'wrong_password')
            self.assertEqual(response.get('status'), 401)
            self.assertEqual(response.get('message'), MESSAGES.get('WRONG_PASSWORD'))

        def test_no_username():
            """ when the username does not exist """
            response = login('123456780', 'password')
            self.assertEqual(response.get('status'), 401)
            self.assertEqual(response.get('message'), MESSAGES.get('NO_USERNAME'))

        test_success()
        test_wrong_password()
        test_no_username()

    def test_change_password(self):
        """
        test change_password
        """
        user = User.objects.get(phone_number=TEST_PHONE)

        def test_success():
            """ when the password change is successful """
            response = change_password(str(user.id), 'password', 'new_password')
            self.assertEqual(response.get('status'), 200)
            self.assertEqual(response.get('message'), MESSAGES.get('OK_PASSWORD_CHANGE'))

        def test_wrong_password():
            """ when the old password is wrong """
            response = change_password(str(user.id), 'wrong_password', 'new_password')
            self.assertEqual(response.get('status'), 400)
            self.assertEqual(response.get('message'), MESSAGES.get('WRONG_PASSWORD'))

        test_success()
        test_wrong_password()

    def test_reset_password(self):
        """
        test reset_password
        """
        def test_success():
            """ when the password reset is successful """
            response = reset_password(TEST_PHONE)
            self.assertEqual(response.get('status'), 200)
            self.assertEqual(response.get('message'), MESSAGES.get('OK_PASSWORD_RESET'))

        def test_no_phone_number():
            """ when the phone number does not exist """
            response = reset_password('123456780')
            self.assertEqual(response.get('status'), 400)
            self.assertEqual(response.get('message'), MESSAGES.get('NO_PHONE_NUMBER'))

        test_success()
        test_no_phone_number()

    def test_otp_manager(self):
        """
        test otp_manager
        """
        user = User.objects.get(phone_number=TEST_PHONE)
        otp_manager = OtpManager()

        def test_make_otp():
            """ test make_otp """
            otp_manager.make_otp(user)
            user_otp = UserOtp.objects.get(user_id=user.id)
            self.assertTrue(user_otp)

        def test_verify_otp():
            """ test verify_otp """
            self.assertTrue(otp_manager.verify_otp(user.id, '1234'))
            self.assertFalse(otp_manager.verify_otp(user.id, '1111'))

        def test_resend_otp():
            """ test reset_otp """
            result = otp_manager.resend_otp('msisdn', TEST_PHONE)
            self.assertEqual(result.get('status'), 200)

        test_make_otp()
        test_verify_otp()
