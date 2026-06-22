import logging

from rest_framework.response import Response
from rest_framework.views import APIView

from misc_app.controllers.decode_auth_token import decode_jwt_token
from users_app.controllers.permissions_check import can_user_access_module
from dinify_backend.configss.string_definitions import MODULE_MENU
from restaurants_app.models import Restaurant

logger = logging.getLogger(__name__)

REQUIRED_TAG_FIELDS = {'id', 'name', 'icon', 'color', 'filterable'}


class PresetTagsEndpoint(APIView):
    """GET and PUT for a restaurant's preset tag configuration."""

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

        if not can_user_access_module(request.user, restaurant_id, MODULE_MENU):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except Restaurant.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404
            )

        return Response({
            'status': 200,
            'message': 'Preset tags retrieved successfully',
            'data': restaurant.preset_tags
        }, status=200)

    def put(self, request):
        try:
            decode_jwt_token(request)
        except Exception:
            return Response({'status': 401, 'message': 'Unauthorized'}, status=401)

        restaurant_id = request.data.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant field is required'},
                status=400
            )

        if not can_user_access_module(request.user, restaurant_id, MODULE_MENU):
            return Response({'status': 403, 'message': 'Forbidden'}, status=403)

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except Restaurant.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'Restaurant not found'}, status=404
            )

        tags = request.data.get('tags')
        if tags is None or not isinstance(tags, list):
            return Response(
                {'status': 400, 'message': 'tags (list) is required'},
                status=400
            )

        # Validate each tag
        for i, tag in enumerate(tags):
            if not isinstance(tag, dict):
                return Response(
                    {'status': 400, 'message': f'Tag at index {i} must be an object'},
                    status=400
                )
            missing = REQUIRED_TAG_FIELDS - set(tag.keys())
            if missing:
                return Response(
                    {'status': 400, 'message': f'Tag at index {i} is missing fields: {", ".join(sorted(missing))}'},
                    status=400
                )

        # Validate no duplicate names
        names = [tag['name'] for tag in tags]
        if len(names) != len(set(names)):
            return Response(
                {'status': 400, 'message': 'Duplicate tag names are not allowed'},
                status=400
            )

        restaurant.preset_tags = tags
        restaurant.save(update_fields=['preset_tags'])

        return Response({
            'status': 200,
            'message': 'Preset tags updated successfully',
            'data': restaurant.preset_tags
        }, status=200)
