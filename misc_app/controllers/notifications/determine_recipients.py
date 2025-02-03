from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RESTAURANT_MANAGER, DINIFY_ADMIN
)
from restaurants_app.models import RestaurantEmployee
from users_app.models import User


def determine_receipients(
    message_type: str,
    restaurant_id: str,
    user_id: str
):
    tos = []
    ccs = []
    msisdn = None

    if restaurant_id is not None:
        if message_type in [
            'admin-new-restaurant',
            'new-menu-section',
            'new-restaurant',
            'restaurant-activated',
            'restaurant-rejected',
        ]:
            employees = RestaurantEmployee.objects.filter(restaurant_id=restaurant_id)
            owners = [employee.user.email for employee in employees if RESTAURANT_OWNER in employee.roles]  # noqa
            managers = [employee.user.email for employee in employees if RESTAURANT_MANAGER in employee.roles]  # noqa
            tos = owners + managers

    if message_type in ['admin-new-restaurant', 'new-restaurant']:
        dinify_admins = User.objects.filter(roles__contains=[DINIFY_ADMIN])
        ccs += [admin.email for admin in dinify_admins]

    if message_type in [
        'forgot-password',
        'password-change',
        'new-restaurant-employee',
        'new-user',
        'new-user-credentials'
    ]:
        user = User.objects.values('phone_number', 'email').get(id=user_id)
        tos = user['email']
        msisdn = user['phone_number']

    return {
        'tos': tos,
        'ccs': ccs,
        'msisdn': msisdn
    }
