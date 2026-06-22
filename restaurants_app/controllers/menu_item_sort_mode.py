import logging

from django.core.exceptions import ValidationError

from restaurants_app.models import Restaurant
from users_app.controllers.permissions_check import can_user_access_module
from users_app.models import User
from dinify_backend.configss.string_definitions import MODULE_MENU


logger = logging.getLogger(__name__)


class ConMenuItemSortMode:
    """
    Read/write the per-restaurant diner-facing menu item sort mode.

    The backend only stores the mode and gates who can change it; the actual
    item ordering stays in `listing_position` and the frontend applies the
    sort (shared comparator for portal/diner parity).
    """

    def __init__(self):
        pass

    def get_mode(self, restaurant_id: str, user: User) -> dict:
        """
        Return the restaurant's current sort mode.

        restaurant_id is required and the user must have owner/manager
        permission on that restaurant (or be a Dinify admin).
        """
        if not restaurant_id:
            return {
                'status': 400,
                'message': 'restaurant is required'
            }

        restaurant = self._get_restaurant(restaurant_id)
        if restaurant is None:
            return {
                'status': 404,
                'message': 'Restaurant not found'
            }

        if not can_user_access_module(user, str(restaurant.id), MODULE_MENU):
            return {
                'status': 403,
                'message': 'You do not have permission to view the sort mode for this restaurant'
            }

        return {
            'status': 200,
            'item_sort_mode': restaurant.menu_item_sort_mode
        }

    def set_mode(self, restaurant_id: str, mode: str, user: User) -> dict:
        """
        Persist a new sort mode on the restaurant.

        restaurant_id is required, the user must have owner/manager permission
        on that restaurant (or be a Dinify admin), and `mode` must be one of
        the values declared on the model field.
        """
        if not restaurant_id:
            return {
                'status': 400,
                'message': 'restaurant is required'
            }

        restaurant = self._get_restaurant(restaurant_id)
        if restaurant is None:
            return {
                'status': 404,
                'message': 'Restaurant not found'
            }

        if not can_user_access_module(user, str(restaurant.id), MODULE_MENU):
            return {
                'status': 403,
                'message': 'You do not have permission to change the sort mode for this restaurant'
            }

        if mode not in self._allowed_modes():
            return {
                'status': 400,
                'message': 'Invalid sort mode'
            }

        if restaurant.menu_item_sort_mode != mode:
            restaurant.menu_item_sort_mode = mode
            restaurant.save(update_fields=['menu_item_sort_mode'])

        return {
            'status': 200,
            'item_sort_mode': mode
        }

    @staticmethod
    def _allowed_modes() -> set:
        # Derive valid modes from the model field so they can never drift from
        # the choices the migration/frontend round-trip against.
        return {
            choice[0]
            for choice in Restaurant._meta.get_field('menu_item_sort_mode').choices
        }

    @staticmethod
    def _get_restaurant(restaurant_id: str):
        # Guard against malformed UUIDs so bad input is a 404, never a 500.
        try:
            return Restaurant.objects.filter(id=restaurant_id).first()
        except (ValidationError, ValueError, TypeError):
            return None
