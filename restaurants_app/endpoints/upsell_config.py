import logging

from django.db import models
from rest_framework.response import Response
from rest_framework.views import APIView

from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.permissions_check import (
    get_user_restaurant_roles,
    is_dinify_admin
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    RESTAURANT_MANAGER
)
from restaurants_app.models import (
    Restaurant, MenuItem, UpsellConfig, UpsellItem
)
from restaurants_app.serializers import (
    UpsellConfigSerializer, UpsellConfigUpdateSerializer
)

logger = logging.getLogger(__name__)


def check_restaurant_permission(user, restaurant_id):
    """Check if user is a dinify admin or owner/manager of the given restaurant."""
    if is_dinify_admin(user):
        return True
    roles = get_user_restaurant_roles(
        user_id=str(user.id),
        restaurant_id=str(restaurant_id)
    )
    return any(role in [RESTAURANT_OWNER, RESTAURANT_MANAGER] for role in roles)


def get_config_response(config):
    """Serialize and return an upsell config."""
    return {
        'status': 200,
        'message': 'Upsell config retrieved successfully',
        'data': UpsellConfigSerializer(config).data
    }


class UpsellConfigEndpoint(APIView):
    """Endpoint for upsell config GET and PUT."""

    def get(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant query param is required'},
                status=400
            )

        if not check_restaurant_permission(request.user, restaurant_id):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except Restaurant.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404
            )

        config, _ = UpsellConfig.objects.get_or_create(restaurant=restaurant)
        response = get_config_response(config)
        return Response(response, status=200)

    def put(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        config_id = request.data.get('id')
        if not config_id:
            return Response(
                {'status': 400, 'message': 'id field is required'},
                status=400
            )

        try:
            config = UpsellConfig.objects.get(id=config_id)
        except UpsellConfig.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Upsell config not found'},
                status=404
            )

        restaurant_id = str(config.restaurant_id)
        if not check_restaurant_permission(request.user, restaurant_id):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        serializer = UpsellConfigUpdateSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            response = get_config_response(config)
            return Response(response, status=200)

        return Response(
            {'status': 400, 'message': 'Validation error', 'errors': serializer.errors},
            status=400
        )


class UpsellItemsEndpoint(APIView):
    """Endpoint for upsell item management (add, remove, reorder)."""

    def post(self, request, action=None, **kwargs):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        config_id = request.data.get('config')
        if not config_id:
            return Response(
                {'status': 400, 'message': 'config field is required'},
                status=400
            )

        try:
            config = UpsellConfig.objects.get(id=config_id)
        except UpsellConfig.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Upsell config not found'},
                status=404
            )

        if not check_restaurant_permission(request.user, str(config.restaurant_id)):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        if action == 'reorder':
            return self._reorder_items(request, config)
        return self._add_items(request, config)

    def _add_items(self, request, config):
        item_ids = request.data.get('item_ids', [])
        if not item_ids or not isinstance(item_ids, list):
            return Response(
                {'status': 400, 'message': 'item_ids (list of menu item UUIDs) is required'},
                status=400
            )

        # Validate all items belong to the restaurant
        restaurant = config.restaurant
        valid_items = MenuItem.objects.filter(
            id__in=item_ids,
            section__restaurant=restaurant,
            deleted=False
        )
        valid_item_ids = set(str(item.id) for item in valid_items)
        invalid_ids = [iid for iid in item_ids if str(iid) not in valid_item_ids]
        if invalid_ids:
            return Response(
                {'status': 400, 'message': f'Items not found in this restaurant: {invalid_ids}'},
                status=400
            )

        # Get current max position
        max_pos = config.upsell_items.aggregate(
            max_pos=models.Max('listing_position')
        )['max_pos'] or 0

        created_count = 0
        for item_id in item_ids:
            _, created = UpsellItem.objects.get_or_create(
                config=config,
                menu_item_id=item_id,
                defaults={'listing_position': max_pos + 1}
            )
            if created:
                max_pos += 1
                created_count += 1

        config.refresh_from_db()
        response = get_config_response(config)
        response['message'] = f'{created_count} item(s) added to upsell list'
        return Response(response, status=200)

    def _reorder_items(self, request, config):
        item_ids = request.data.get('item_ids', [])
        if not item_ids or not isinstance(item_ids, list):
            return Response(
                {'status': 400, 'message': 'item_ids (ordered list of menu item UUIDs) is required'},
                status=400
            )

        for position, item_id in enumerate(item_ids):
            UpsellItem.objects.filter(
                config=config,
                id=item_id
            ).update(listing_position=position)

        config.refresh_from_db()
        response = get_config_response(config)
        response['message'] = 'Upsell items reordered successfully'
        return Response(response, status=200)

    def delete(self, request, **kwargs):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        item_id = request.data.get('id')
        if not item_id:
            return Response(
                {'status': 400, 'message': 'id field is required'},
                status=400
            )

        try:
            item = UpsellItem.objects.select_related('config').get(id=item_id)
        except UpsellItem.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Upsell item not found'},
                status=404
            )

        if not check_restaurant_permission(request.user, str(item.config.restaurant_id)):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        config = item.config
        item.delete()
        config.refresh_from_db()
        response = get_config_response(config)
        response['message'] = 'Upsell item removed successfully'
        return Response(response, status=200)
