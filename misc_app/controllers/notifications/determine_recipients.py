from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER, RESTAURANT_MANAGER
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

    # A `roles__contains=[DINIFY_ADMIN]` CC list used to sit here for the
    # new-restaurant notifications. It had already stopped matching anything —
    # platform staff are created with `roles=[]` and are identified by
    # `account_type` — and it was the last place on this plane that read
    # `User.roles` to mean "a platform administrator". Removed with the rest.

    if message_type in [
        'forgot-password',
        'password-change',
        'new-restaurant-employee',
        'new-user',
        'new-user-credentials'
    ]:
        user = User.objects.values('phone_number', 'email').get(id=user_id)
        # A self-registered user may have email=None/''. Never persist a falsy
        # scalar "to": an empty string still lingers as a stored empty recipient
        # that an emailless caller's own empty identity could equality-match on
        # the read/mark-read sink. Emit the real email as the scalar recipient
        # (unchanged — a downstream caller consumes it as a scalar), else an empty
        # list: no recipient, consistent with the array shape the multi-recipient
        # branches above use.
        email_value = user['email']
        tos = email_value if (email_value and email_value.strip()) else []
        msisdn = user['phone_number']

    return {
        'tos': tos,
        'ccs': ccs,
        'msisdn': msisdn
    }
