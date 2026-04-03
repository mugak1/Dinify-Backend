import logging
from typing import Optional
from users_app.models import User
from dinify_backend.mongo_db import (
    MONGO_DB,
    COL_PROFILE_UPDATE_APPROVALS
)

logger = logging.getLogger(__name__)
from restaurants_app.models import RestaurantEmployee
from users_app.controllers.permissions_check import (
    dinify_roles,
    get_user_restaurant_roles
)

from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER
)


def get_pending_profile_updates(
    user: User,
    restaurant: Optional[str]
) -> dict:

    grant_access = False
    employee_ids = []

    filter_query = {"approval_status": {"$exists": False}}
    projection = {
        "_id": 0,
        "user_id": 1,
        "detail": 1,
        "old": 1,
        "new": 1
    }

    if any(role in dinify_roles for role in user.roles):
        grant_access = True

    if restaurant is not None:
        employee_roles = get_user_restaurant_roles(str(user.id), restaurant)
        if len(employee_roles) > 0:
            if any(role in [RESTAURANT_OWNER, RESTAURANT_MANAGER] for role in employee_roles):
                grant_access = True
                employee_ids = RestaurantEmployee.objects.filter(
                    user=user.id,
                    restaurant=restaurant,
                    deleted=False
                ).values_list('id', flat=True)
                # print(employee_ids)
                filter_query["user_id"] = {"$in": list(employee_ids)}

    if not grant_access:
        return {
            "status": 401,
            "message": "You do not have permission to perform this action"
        }
    try:
        pending_updates = MONGO_DB[COL_PROFILE_UPDATE_APPROVALS].find(
            filter_query,
            projection=projection
        )
        return list(pending_updates)
    except Exception as e:
        logger.error("Failed to query pending profile updates from MongoDB: %s", e)
        return []
