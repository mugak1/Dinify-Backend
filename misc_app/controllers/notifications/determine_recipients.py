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
            employees = RestaurantEmployee.objects.filter(
                restaurant_id=restaurant_id
            ).exclude(user__email__isnull=True).exclude(user__email='')
            owners = [employee.user.email for employee in employees if RESTAURANT_OWNER in employee.roles and employee.user.email]  # noqa
            managers = [employee.user.email for employee in employees if RESTAURANT_MANAGER in employee.roles and employee.user.email]  # noqa
            tos = owners + managers

    if message_type in ['admin-new-restaurant', 'new-restaurant']:
        dinify_admins = User.objects.filter(
            roles__contains=[DINIFY_ADMIN], email__isnull=False
        ).exclude(email='')
        ccs += [admin.email for admin in dinify_admins if admin.email]

    if message_type in [
        'forgot-password',
        'password-change',
        'new-restaurant-employee',
        'new-user',
        'new-user-credentials'
    ]:
        user = User.objects.values('phone_number', 'email').get(id=user_id)
        # A self-registered user may have email=None; never pass a null/falsy
        # value downstream as a "to" (it can degenerate a recipient predicate
        # into email IS NULL). Empty string keeps the scalar shape and stays falsy.
        tos = user['email'] or ''
        msisdn = user['phone_number']

    return {
        'tos': tos,
        'ccs': ccs,
        'msisdn': msisdn
    }
