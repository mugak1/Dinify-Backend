"""
endpoints for restaurant configurations
"""
from rest_framework.response import Response
from rest_framework.views import APIView
from restaurants_app.controllers.create_restaurant import create_restaurant
from misc_app.controllers.decode_auth_token import decode_jwt_token


class RestaurantSetupEndpoint(APIView):
    """
    the endpoint for restaurant setups
    """
    def post(self, request, config_detail):
        """
        handle the POST method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        if config_detail == 'restaurants':
            # TODO if the user is not a Dinify admin,.
            # then set the owner value from the auth details
            data = request.data.copy()
            data['owner'] = auth['user_id']
            response = create_restaurant(
                data,
                auth
            )

        return Response(
            response,
            status=response['status']
        )
