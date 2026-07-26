import logging

from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from users_app.models import User
from users_app.controllers.permissions_check import (
    get_module_restaurant_ids,
    MODULE_TEAM,
)

logger = logging.getLogger(__name__)


class UserLookupEndpoint(APIView):
    def get(self, request):
        # Gate: this resolves a person's identity from a raw contact, so it is
        # restricted to its one legitimate caller — the restaurant team staff-add
        # lookup. `team` is owner-only, so this admits restaurant owners and denies
        # everyone else (managers, kitchen, staff, diners).
        #
        # The Dinify-admin owner-creation lookup that used to share this gate is
        # gone with the capability it served (see the retired
        # admin-register-restaurant branch). `get_module_restaurant_ids` now always
        # returns a set, so `len()` is total.
        user = request.user
        if not (
            user
            and user.is_authenticated
            and user.is_active
            and len(get_module_restaurant_ids(user, MODULE_TEAM)) > 0
        ):
            return Response(
                {'status': 403, 'message': 'Not authorised.'},
                status=403,
            )

        contact = request.GET.get('contact')
        if not contact:
            return Response(
                {'status': 400,
                 'message': 'A contact query parameter is required.'},
                status=400,
            )

        try:
            # check if the identity includes @
            if '@' in contact:
                found = User.objects.values(
                    'id', 'first_name', 'last_name'
                ).get(email=contact)
            else:
                # TODO internationalise the phone number
                found = User.objects.values(
                    'id', 'first_name', 'last_name'
                ).get(phone_number=contact)
        except User.DoesNotExist:
            return Response(
                {'status': 404, 'message': 'User not found'},
                status=404,
            )

        # Minimal disclosure: existence + id (to link) + name (to confirm).
        # phone_number / email are deliberately NOT returned — searching by one
        # contact must not reveal the complementary contact (BUG-P1-3).
        response = {
            'status': 200,
            'message': 'User found',
            'data': {
                'id': str(found.get('id')),
                'first_name': found.get('first_name'),
                'last_name': found.get('last_name'),
            }
        }
        return Response(response, status=200)


class MsisdnLookupEndpoint(APIView):
    permission_classes = (AllowAny,)

    def get(self, request):
        try:
            User.objects.values('id').get(
                phone_number=request.GET.get('msisdn')
            )
            response = {
                'status': 200,
                'message': 'User found',
                'data': {
                    'found': True
                }
            }
        except Exception as error:
            logger.error("Error while looking up user: %s", error)
            response = {
                'status': 404,
                'message': 'User not found',
                'data': {
                    'found': False
                }
            }

        return Response(response, status=response['status'])
