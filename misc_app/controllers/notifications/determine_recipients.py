from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RESTAURANT_MANAGER, DINIFY_ADMIN
)
from restaurants_app.models import RestaurantEmployee
from users_app.models import User


def determine_receipients(message_type: str, restaurant_id: str,):
    tos = []
    ccs = []

    if restaurant_id is not None:
        if message_type in [
            'admin-new-restaurant',
            'new-menu-section',
            'new-restaurant'
        ]:
            employees = RestaurantEmployee.objects.filter(restaurant_id=restaurant_id)
            owners = [employee.user.email for employee in employees if RESTAURANT_OWNER in employee.roles]  # noqa
            managers = [employee.user.email for employee in employees if RESTAURANT_MANAGER in employee.roles]  # noqa
            tos = owners + managers

    if message_type in ['admin-new-restaurant', 'new-restaurant']:
        dinify_admins = User.objects.filter(roles__contains=[DINIFY_ADMIN])
        ccs += [admin.email for admin in dinify_admins]

    return {
        'tos': tos,
        'ccs': ccs
    }
