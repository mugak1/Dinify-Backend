import logging

from rest_framework.response import Response
from rest_framework.views import APIView
from users_app.controllers.update_user_profile import (
    self_update_user_profile,
)

logger = logging.getLogger(__name__)


class UserProfileEndpoint(APIView):
    def put(self, request):
        try:
            response = self_update_user_profile(
                user_id=request.user.id,
                country=request.data.get('country'),
                first_name=request.data.get('first_name'),
                last_name=request.data.get('last_name'),
                other_names=request.data.get('other_names'),
                email=request.data.get('email'),
                phone_number=request.data.get('phone_number')
            )
            return Response(response, status=response.get('status', 200))
        except Exception as error:
            logger.error("Error while updating profile: %s", error)
            response = {
                'status': 400,
                'message': 'Sorry, an error occurred.'
            }
            return Response(response, status=400)
