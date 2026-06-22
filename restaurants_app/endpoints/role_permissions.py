"""
Owner-only role-permission management endpoint.

    GET  /api/v1/restaurant-setup/role-permissions/?restaurant=<id>
    PUT  /api/v1/restaurant-setup/role-permissions/

Reads the four per-role module grids for a restaurant, and writes a non-owner
role's grid. Both verbs gate on the ``team`` module at the target restaurant via
can_user_access_module — which is owner-only (the resolver grants ``team`` only
through the owner/admin short-circuit), so this is the owner-only management
surface that mirrors the create-employee (``employee`` → ``team``) gate from C.

Catch-all placement note: this endpoint MUST be registered before the
restaurant-setup catch-all (``<str:config_detail>/``) in restaurants_app/urls.py,
otherwise the catch-all swallows the route.
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from dinify_backend.configss.string_definitions import MODULE_TEAM
from misc_app.controllers.decode_auth_token import decode_jwt_token
from restaurants_app.controllers.role_permissions import (
    get_role_permissions,
    update_role_permission,
)
from restaurants_app.models import Restaurant
from users_app.controllers.permissions_check import can_user_access_module


class RolePermissionsEndpoint(APIView):
    """Read all role grids; write a non-owner role's grid (owner-only)."""

    def get(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query param is required'},
                status=400,
            )

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError):
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404,
            )

        if not can_user_access_module(request.user, restaurant_id, MODULE_TEAM):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        return Response(get_role_permissions(restaurant), status=200)

    def put(self, request):
        try:
            auth = decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        data = request.data
        try:
            data = data.dict()
        except Exception:
            pass

        restaurant_id = data.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant field is required'},
                status=400,
            )

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError):
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404,
            )

        if not can_user_access_module(request.user, restaurant_id, MODULE_TEAM):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        response = update_role_permission(
            restaurant=restaurant,
            role=data.get('role'),
            modules=data.get('modules'),
            user_id=auth['id'],
        )
        return Response(response, status=response['status'])
