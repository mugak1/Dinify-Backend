"""
Read-side controller for restaurant subscription details.

The WRITE verb (``update``) was REMOVED with ambient administrator authority:
it was reachable on the strength of a ``dinify_admin`` string in the caller's
``User.roles`` and could grant any restaurant an indefinite free subscription.
Phase 1: setting subscription validity/expiry is admin-plane functionality,
built natively on /api/admin/v1, where it gets elevation and an audit row. Do
not re-add a write path here.
"""
from django.core.exceptions import ValidationError
from rest_framework.response import Response

from restaurants_app.models import Restaurant


class RestaurantSubscription:
    def __init__(self, ):
        pass

    def get_details(self, request):
        # Read-authorization is enforced upstream in the endpoint's GET handler
        # (settings-module gate, 404 on denial); here we only harden input so a
        # missing/unknown restaurant returns 400/404 instead of a 500.
        restaurant_id = request.GET.get('restaurant')
        if not restaurant_id:
            return Response(
                {'status': 400, 'message': 'restaurant is required.'},
                status=400,
            )

        try:
            restaurant = Restaurant.objects.values(
                'subscription_validity',
                'subscription_expiry_date',
            ).get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError, ValidationError):
            # DoesNotExist, or a malformed (non-UUID) id. Treat both as not found
            # — defensive, so a bad value can never surface as a 500.
            return Response(
                {'status': 404, 'message': 'Restaurant not found.'},
                status=404,
            )

        data = {
            'subscription_validity': restaurant['subscription_validity'],
            'subscription_expiry_date': restaurant['subscription_expiry_date'],
        }

        response = {
            'status': 200,
            'message': 'Successfully retrieved the restaurant subscription information',
            'data': data
        }
        return Response(response, status=200)
