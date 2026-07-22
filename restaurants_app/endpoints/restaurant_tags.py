"""
CRUD (+ reorder and usage-count) endpoints for the per-restaurant tag
catalog (RestaurantTag).

Routes:
- GET/POST      restaurant-tags/                     list / create
- PATCH/DELETE  restaurant-tags/<tag_id>/            update / delete
- POST          restaurant-tags/reorder/             batch display_order write
- GET           restaurant-tags/<tag_id>/usage-count/  count of menu items using the tag

Tenant isolation: every read and write resolves the target restaurant
from the authenticated user's caller-supplied restaurant id (for list
and create), from the tag row's FK (for patch, delete and usage-count),
or from the resolved tags themselves (for reorder — every tag in the
payload must belong to ONE restaurant), then gates on the `menu` module
at that restaurant via can_user_access_module — cross-restaurant /
insufficient-module access is rejected. The reorder write is
all-or-nothing: any unknown, soft-deleted, or cross-restaurant id in the
payload rejects the whole request and writes nothing.

Catch-all placement note: these endpoints MUST be registered before the
restaurant-setup catch-all (`<str:config_detail>/`) in
restaurants_app/urls.py, otherwise the catch-all swallows the route.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
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
            decode_jwt_token(request)
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

        # restaurant is server-derived (read_only): bind it from the resolved,
        # gated restaurant id via the trusted save() channel, never the payload.
        serializer.save(created_by=request.user, restaurant_id=restaurant_id)
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
            'user': request.user,
            # The tag was resolved and module-gated above; scope Secretary to that
            # exact row so it can never resolve a foreign tag from a spoofed id.
            'instance_queryset': RestaurantTag.objects.filter(pk=tag.pk),
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


class RestaurantTagReorderEndpoint(APIView):
    """Batch-persist display_order across a restaurant's tags (all-or-nothing)."""

    def post(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        order = request.data.get('order')
        if not order or not isinstance(order, list):
            return Response(
                {'status': 400,
                 'message': 'order (a non-empty list of {id, display_order}) is required'},
                status=400,
            )

        # Validate every entry and build id -> display_order up front.
        order_map = {}
        for entry in order:
            if not isinstance(entry, dict):
                return Response(
                    {'status': 400, 'message': 'each order entry must be an object'},
                    status=400,
                )
            tag_id = entry.get('id')
            display_order = entry.get('display_order')
            # bool is an int subclass — reject it explicitly.
            if (
                not tag_id
                or isinstance(display_order, bool)
                or not isinstance(display_order, int)
            ):
                return Response(
                    {'status': 400,
                     'message': 'each order entry needs a tag id and an integer display_order'},
                    status=400,
                )
            order_map[str(tag_id)] = display_order

        requested_ids = set(order_map)

        # Resolve ALL requested ids in one query.
        try:
            tags = list(
                RestaurantTag.objects.select_related('restaurant').filter(
                    id__in=requested_ids, deleted=False,
                )
            )
        except (ValueError, ValidationError):
            return Response(
                {'status': 400, 'message': 'one or more tag ids are malformed'},
                status=400,
            )

        # Any unknown / soft-deleted id → reject the whole request, write nothing.
        if len(tags) != len(requested_ids):
            return Response(
                {'status': 400, 'message': 'one or more tags do not exist'},
                status=400,
            )

        # Every tag must belong to the SAME restaurant.
        restaurant_ids = {tag.restaurant_id for tag in tags}
        if len(restaurant_ids) != 1:
            return Response(
                {'status': 400,
                 'message': 'all tags must belong to the same restaurant'},
                status=400,
            )

        restaurant = tags[0].restaurant
        if not can_user_access_module(
            request.user, str(restaurant.id), MODULE_MENU,
        ):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        with transaction.atomic():
            for tag in tags:
                tag.display_order = order_map[str(tag.id)]
            RestaurantTag.objects.bulk_update(tags, ['display_order'])

        return Response({
            'status': 200,
            'message': 'Restaurant tags reordered successfully',
            'data': _serialize_tags(restaurant),
        }, status=200)


class RestaurantTagUsageCountEndpoint(APIView):
    """Count the non-deleted menu items that reference a single tag."""

    def _get_tag(self, tag_id):
        try:
            return RestaurantTag.objects.select_related('restaurant').get(
                id=tag_id, deleted=False,
            )
        except (RestaurantTag.DoesNotExist, ValueError):
            return None

    def get(self, request, tag_id):
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

        count = tag.menu_items.filter(deleted=False).distinct().count()
        return Response({
            'status': 200,
            'message': 'Restaurant tag usage count retrieved successfully',
            'data': {'count': count},
        }, status=200)
