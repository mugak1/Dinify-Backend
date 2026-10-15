from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from misc_app.controllers.msisdn import normalise_msisdn, MsisdnError
from users_app.models import User
from users_app.controllers.permissions_check import (
    get_module_restaurant_ids,
    MODULE_TEAM,
)


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

        # check if the identity includes @
        if '@' in contact:
            lookup = {'email': contact}
        else:
            # Compare in the stored, canonical form, as `self_register`'s
            # duplicate check does (create-employee delegates to it), so "found"
            # here means registration would refuse the number. A value that
            # cannot be canonicalised gets the 400 registration gives it.
            try:
                lookup = {'phone_number': normalise_msisdn(contact)}
            except MsisdnError:
                return Response(
                    {'status': 400,
                     'message': 'Please provide a valid Ugandan phone number.'},
                    status=400,
                )

        try:
            found = User.objects.values(
                'id', 'first_name', 'last_name'
            ).get(**lookup)
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
        # The public sign-up page reads 404 as "no account" and sends an OTP, so
        # that answer is given only when the lookup ran and found nobody
        # (tests_msisdn_lookup.py). Nothing around the query is caught: a
        # database failure propagates and is a 500, never "User not found".
        msisdn = request.GET.get('msisdn')
        if not msisdn or not msisdn.strip():
            # Never query with a missing number. `phone_number=None` is
            # `IS NULL`, which matches the phone-less platform-staff accounts.
            return Response(
                {'status': 400,
                 'message': 'A msisdn query parameter is required.'},
                status=400,
            )

        # Compare in the stored, canonical form, as `self_register`'s duplicate
        # check does, so "found" here means registration would refuse the number.
        # A value that cannot be canonicalised gets the 400 registration gives it.
        try:
            msisdn = normalise_msisdn(msisdn)
        except MsisdnError:
            return Response(
                {'status': 400,
                 'message': 'Please provide a valid Ugandan phone number.'},
                status=400,
            )

        if User.objects.filter(phone_number=msisdn).exists():
            response = {
                'status': 200,
                'message': 'User found',
                'data': {
                    'found': True
                }
            }
        else:
            response = {
                'status': 404,
                'message': 'User not found',
                'data': {
                    'found': False
                }
            }

        return Response(response, status=response['status'])
