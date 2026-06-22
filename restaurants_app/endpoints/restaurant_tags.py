"""
CRUD endpoints for the per-restaurant tag catalog (RestaurantTag).

Tenant isolation: every read and write resolves the target restaurant
from the authenticated user's caller-supplied restaurant id (for list
and create) or from the tag row's FK (for patch and delete), then gates
on the `menu` module at that restaurant via can_user_access_module —
cross-restaurant / insufficient-module access is rejected.

Catch-all placement note: this endpoint MUST be registered before the
restaurant-setup catch-all (`<str:config_detail>/`) in
restaurants_app/urls.py, otherwise the catch-all swallows the route.
"""
import logging

from rest_framework.response import Response
from rest_framework.views import APIView

from dinify_backend.configss.edit_information import EI_RESTAURANT_TAG
from dinify_backend.configss.string_definitions import MODULE_MENU
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.secretary import Secretary
from restaurants_app.models import Restaurant, RestaurantTag
from restaurants_app.serializers import SerializerRestaurantTag
from users_app.controllers.permissions_check import can_user_access_module


logger = logging.getLogger(__name__)


def _serialize_tags(restaurant):
    qs = RestaurantTag.objects.filter(
        restaurant=restaurant, deleted=False,
    ).order_by('display_order', 'name')
    return SerializerRestaurantTag(qs, many=True).data


class RestaurantTagsEndpoint(APIView):
    """List and create restaurant tags."""

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

        if not can_user_access_module(request.user, restaurant_id, MODULE_MENU):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError):
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404,
            )

        return Response({
            'status': 200,
            'message': 'Restaurant tags retrieved successfully',
            'data': _serialize_tags(restaurant),
        }, status=200)

    def post(self, request):
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

        if not can_user_access_module(request.user, restaurant_id, MODULE_MENU):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        try:
            Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError):
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404,
            )

        # Caller-supplied is_system_preset is ignored (read-only on the serializer).
        data['is_system_preset'] = False

        serializer = SerializerRestaurantTag(data=data)
        if not serializer.is_valid():
            return Response({
                'status': 400,
                'message': 'Validation error',
                'errors': serializer.errors,
            }, status=400)

        serializer.save(created_by=request.user)
        return Response({
            'status': 201,
            'message': 'Restaurant tag created successfully',
            'data': serializer.data,
        }, status=201)


class RestaurantTagDetailEndpoint(APIView):
    """Patch and delete a single restaurant tag."""

    def _get_tag(self, tag_id):
        try:
            return RestaurantTag.objects.select_related('restaurant').get(
                id=tag_id, deleted=False,
            )
        except (RestaurantTag.DoesNotExist, ValueError):
            return None

    def patch(self, request, tag_id):
        try:
            auth = decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        tag = self._get_tag(tag_id)
        if tag is None:
            return Response(
                {'status': 404, 'message': 'Restaurant tag not found'}, status=404,
            )

        if not can_user_access_module(
            request.user, str(tag.restaurant_id), MODULE_MENU,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        data = request.data
        try:
            data = data.dict()
        except Exception:
            data = dict(data) if data else {}
        data['id'] = str(tag.id)

        secretary_args = {
            'serializer': SerializerRestaurantTag,
            'data': data,
            'edit_considerations': EI_RESTAURANT_TAG,
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': 'Restaurant tag updated successfully',
            'error_message': 'Failed to update restaurant tag',
        }
        response = Secretary(secretary_args).update()

        if response.get('status') == 200:
            tag.refresh_from_db()
            response['data'] = SerializerRestaurantTag(tag).data
        return Response(response, status=response['status'])

    def delete(self, request, tag_id):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        tag = self._get_tag(tag_id)
        if tag is None:
            return Response(
                {'status': 404, 'message': 'Restaurant tag not found'}, status=404,
            )

        if not can_user_access_module(
            request.user, str(tag.restaurant_id), MODULE_MENU,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        # Hard delete so dependent MenuItemTag rows cascade away. The
        # legacy soft-delete pattern is intentionally not used here:
        # frontend filters expect the catalog row to disappear.
        tag.delete()
        return Response({
            'status': 200,
            'message': 'Restaurant tag deleted successfully',
        }, status=200)
