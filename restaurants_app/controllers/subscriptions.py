"""
endpoints for restaurant subscriptions
"""
from rest_framework.response import Response
from restaurants_app.models import Restaurant


class RestaurantSubscription:
    def __init__(self, ):
        pass

    def get_details(self, request):
        restaurant = Restaurant.objects.values(
            'subscription_validity',
            'subscription_expiry_date',
        ).get(id=request.GET.get('restaurant'))

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
        restaurant = Restaurant.objects.get(id=request.data['restaurant'])

        restaurant.subscription_validity = request.data['subscription_validity']
        restaurant.subscription_expiry_date = request.data['subscription_expiry_date']

        restaurant.save()

        # TODO save the subscription change in the logs

        response = {
            'status': 200,
            'message': 'Successfully updated the restaurant subscription information'
        }

        return Response(response, status=200)
