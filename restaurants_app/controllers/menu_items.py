import logging

from django.db import transaction

from restaurants_app.models import MenuItem, MenuSection
from users_app.controllers.permissions_check import (
    is_dinify_admin,
    get_user_restaurant_roles,
)
from users_app.models import User
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER,
)


logger = logging.getLogger(__name__)


class ConMenuItem:
    def __init__(self):
        pass

    def reorder_listing(self, section_id: str, ordered_ids: list, user: User) -> dict:
        """
        Reorder menu items within a section by writing a fresh listing_position
        to each item in the list. The first id in `ordered_ids` becomes
        position 0, the second becomes 1, and so on.

        Contract:
        - section_id: UUID of the section whose items are being reordered.
        - ordered_ids: full ordered list of UUID strings for every non-deleted
          item in the section. Partial reorders are rejected — the caller is
          asserting a total ordering for the section.
        - user: must be a Dinify admin or owner/manager of the restaurant the
          section belongs to.
        """
        if not section_id:
            return {
                'status': 400,
                'message': 'section_id is required'
            }

        if not ordered_ids or not isinstance(ordered_ids, list):
            return {
                'status': 400,
                'message': 'ordered_ids must be a non-empty list of item ids'
            }

        # Reject duplicate ids - the caller is asserting a total ordering, so
        # the same id appearing twice is a malformed request.
        if len(set(str(x) for x in ordered_ids)) != len(ordered_ids):
            return {
                'status': 400,
                'message': 'ordered_ids must not contain duplicate ids'
            }

        # Verify the section exists and resolve the restaurant.
        try:
            section = MenuSection.objects.get(id=section_id, deleted=False)
        except MenuSection.DoesNotExist:
            return {
                'status': 404,
                'message': 'Section not found'
            }

        restaurant_id = str(section.restaurant_id)

        if not self._user_can_manage_restaurant(user, restaurant_id):
            return {
                'status': 403,
                'message': 'You do not have permission to reorder items in this section'
            }

        # Fetch all non-deleted items currently in the section.
        items = list(MenuItem.objects.filter(section_id=section_id, deleted=False))
        section_item_ids = {str(item.id) for item in items}
        ordered_ids_set = {str(x) for x in ordered_ids}

        # Require the request to cover every item in the section, no more,
        # no less. Partial sets create ambiguous position assignments.
        if section_item_ids != ordered_ids_set:
            missing = sorted(section_item_ids - ordered_ids_set)
            extra = sorted(ordered_ids_set - section_item_ids)
            return {
                'status': 400,
                'message': (
                    f'ordered_ids must contain exactly the non-deleted items in '
                    f'the section. Missing: {missing}. Extra (not in section or '
                    f'deleted): {extra}.'
                )
            }

        by_id = {str(item.id): item for item in items}

        with transaction.atomic():
            for index, item_id in enumerate(ordered_ids):
                item = by_id[str(item_id)]
                if item.listing_position != index:
                    item.listing_position = index
                    item.save(update_fields=['listing_position'])

        return {
            'status': 200,
            'message': 'Menu items reordered successfully'
        }

    @staticmethod
    def _user_can_manage_restaurant(user: User, restaurant_id: str) -> bool:
        if is_dinify_admin(user):
            return True
        roles = get_user_restaurant_roles(
            user_id=str(user.id),
            restaurant_id=restaurant_id,
        )
        return any(role in (RESTAURANT_OWNER, RESTAURANT_MANAGER) for role in roles)
