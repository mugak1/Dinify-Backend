"""
endpoints for restaurant subscriptions
"""
from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.response import Response

from restaurants_app.models import Restaurant
from users_app.controllers.permissions_check import is_dinify_admin


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
            # DoesNotExist, or a malformed (non-UUID) id — the latter reachable
            # only on the admin path, which bypasses the endpoint's settings
            # gate. Treat both as not found.
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

    def update(self, request):
        # WRITE is system/billing state: Dinify-admin ONLY. No restaurant user
        # (the owner included) may self-set their subscription. The gate lives
        # here in the controller so the capability is safe regardless of caller.
        user = getattr(request, 'user', None)
        if not (
            user is not None
            and getattr(user, 'is_authenticated', False)
            and user.is_active
            and is_dinify_admin(user)
        ):
            return Response(
                {'status': 403, 'message': 'Not authorised.'},
                status=403,
            )

        restaurant_id = request.data.get('restaurant')
        subscription_validity = request.data.get('subscription_validity')
        subscription_expiry_date = request.data.get('subscription_expiry_date')

        if (
            restaurant_id is None
            or subscription_validity is None
            or subscription_expiry_date is None
        ):
            return Response(
                {
                    'status': 400,
                    'message': (
                        'restaurant, subscription_validity and '
                        'subscription_expiry_date are required.'
                    ),
                },
                status=400,
            )

        if not isinstance(subscription_validity, bool):
            return Response(
                {'status': 400, 'message': 'subscription_validity must be a boolean.'},
                status=400,
            )

        raw_expiry = str(subscription_expiry_date)
        if parse_datetime(raw_expiry) is None and parse_date(raw_expiry) is None:
            return Response(
                {'status': 400, 'message': 'subscription_expiry_date must be a valid date.'},
                status=400,
            )

        try:
            restaurant = Restaurant.objects.get(id=restaurant_id)
        except (Restaurant.DoesNotExist, ValueError, ValidationError):
            return Response(
                {'status': 404, 'message': 'Restaurant not found.'},
                status=404,
            )

        restaurant.subscription_validity = subscription_validity
        restaurant.subscription_expiry_date = subscription_expiry_date

        restaurant.save()

        # TODO save the subscription change in the logs

        response = {
            'status': 200,
            'message': 'Successfully updated the restaurant subscription information'
        }

        return Response(response, status=200)
