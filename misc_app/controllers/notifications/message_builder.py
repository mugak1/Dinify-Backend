from misc_app.controllers.notifications.msg_builder_menu import make_menu_messages
from misc_app.controllers.notifications.msg_builder_menu_groups import make_menu_group_messages
from misc_app.controllers.notifications.msg_builder_menu_items import make_menu_item_messages
from misc_app.controllers.notifications.msg_builder_restaurant import make_restaurant_messages
from misc_app.controllers.notifications.msg_builder_user import make_user_messages

footer = """
    <p>&nbsp;</p>
    <p><span style="font-weight:400;">Sincerely,&nbsp;</span></p>
    <p><span style="font-weight:400;">Dinify</span></p>
    <p><span style="font-weight:400;">Various Links to Dinify Social Media Accounts</span></p>
"""


def build_messages(msg_data: dict) -> dict:
    """
    Build the messages to be sent to the user
    """
    message = {
        'email': None,
        'sms': None
    }

    if msg_data.get('msg_type') in [
        'password-change',
        'forgot-password',
        'otp',
        'new-user',
        'new-user-credentials'
    ]:
        return make_user_messages(msg_data, footer)

    elif msg_data.get('msg_type') in [
        'first-time-batch-approval',
        'new-menu-section',
        'menu-section-updated',
        'menu-section-disabled',
        'menu-section-enabled',
        'menu-section-deleted'
    ]:
        return make_menu_messages(msg_data, footer)

    elif msg_data.get('msg_type') in [
        'new-restaurant',
        'admin-new-restaurant',
        'new-restaurant-employee',
        'restaurant-activated',
        'restaurant-rejected',
    ]:
        return make_restaurant_messages(msg_data, footer)

    elif msg_data.get('msg_type') in [
        'new-menu-group',
        'menu-group-updated',
        'menu-group-disabled',
        'menu-group-enabled',
        'menu-group-deleted',
    ]:
        return make_menu_group_messages(msg_data, footer)

    elif msg_data.get('msg_type') in [
        'new-menu-item',
        'menu-item-updated',
        'menu-item-disabled',
        'menu-item-enabled',
        'menu-item-deleted',
    ]:
        return make_menu_item_messages(msg_data, footer)

    return message
