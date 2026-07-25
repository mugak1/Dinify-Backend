"""
endpoints for restaurant configurations.
Refactoring needed to make it more maintainable.
"""
import ast
import logging
from django.db import transaction
from django.db.models import Max
from rest_framework.response import Response

logger = logging.getLogger(__name__)
from rest_framework.views import APIView
from restaurants_app.controllers.create_restaurant import (
    admin_register_restaurant
)
from misc_app.controllers.decode_auth_token import decode_jwt_token
from misc_app.controllers.define_filter_params import define_filter_params
from misc_app.controllers.secretary import Secretary
from restaurants_app.serializers import (
    SerializerPutRestaurant, SerializerPublicGetRestaurant, SerializerGetRestaurantDetail,
    SerializerPutRestaurantEmployee, SerializerGetRestaurantEmployee,

    SerializerPutMenuSection, SerializerPublicGetMenuSection,
    SerializerPutMenuItem, SerializerPublicGetMenuItem,
    SerializerPutTable, SerializerPublicGetTable,

    SerializerPutSectionGroup, SerializerPublicGetSectionGroup,

    SerializerPutDiningArea, SerializerGetDiningArea
)
from restaurants_app.models import Restaurant, MenuSection, SectionGroup, MenuItem
from restaurants_app.controllers.tables import (
    get_tables_by_area
)
from restaurants_app.controllers.dining_areas import create_dining_area
from restaurants_app.controllers.menu_sections import ConMenuSection
from restaurants_app.controllers.menu_items import ConMenuItem
from restaurants_app.controllers.menu_item_sort_mode import ConMenuItemSortMode
from restaurants_app.controllers.lifecycle_policy import portal_access_states
from dinify_backend.configss.required_information import (
    REQUIRED_INFORMATION,
    RI_RESTAURANT_EMPLOYEES,
    RI_SECTION_GROUP,
    RI_DINING_AREA
)
from dinify_backend.configss.edit_information import EDIT_INFORMATION, EI_DINING_AREA, EI_SECTION_GROUP
from dinify_backend.configss.messages import (
    OK_GET_RECORD_DETAIL, ERR_GENERAL,
    ERR_UNSPECIFIED_RECORD_DETAILS,
    OK_ADDED_SECTION_GROUP, ERR_ADDED_SECTION_GROUP,
    OK_RETRIEVED_SECTION_GROUP, ERR_RETRIEVED_SECTION_GROUP,
    OK_UPDATED_SECTION_GROUP, ERR_UPDATED_SECTION_GROUP  # noqa
)
from restaurants_app.controllers.create_employee import create_employee
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    MODULE_SETTINGS,
    MODULE_TEAM,
    MODULE_MENU,
    MODULE_TABLES,
)

from users_app.controllers.permissions_check import (
    is_dinify_admin,
    can_user_access_module,
    get_module_restaurant_ids,
)

from restaurants_app.models import RestaurantEmployee, DiningArea, Table
from restaurants_app.controllers.subscriptions import RestaurantSubscription
from restaurants_app.configs.non_unique_combination import RECORDS_NON_UNIQUE_COMBINATIONS
from restaurants_app.controllers.con_cla_employees import ConRestaurantEmployee



def normalize_ordered_section_ids(put_data) -> list:
    """
    Resolve the section-reorder payload into a flat list of section ids.

    New contract: ``ordered_ids`` is a flat list of UUID strings.
    Legacy contract: ``ordering`` was a list of ``{id, listing_position}`` dicts.
    Accepts either so frontend builds shipped before/after the rename keep working.
    Returns None if neither key is present so the controller can return a 400.
    """
    ordered_ids = put_data.get('ordered_ids')
    if ordered_ids is not None:
        return ordered_ids
    legacy = put_data.get('ordering') or []
    if legacy and isinstance(legacy[0], dict):
        return [item.get('id') for item in legacy]
    if legacy and isinstance(legacy[0], str):
        return legacy
    return None


def _as_str_id(value):
    """Normalize a UUID/str/None FK value to a string id, or None."""
    return str(value) if value else None


def _resolve_restaurants(action, data):
    # The resource IS the restaurant; the id (or POST owner) IS the target.
    return _as_str_id(data.get('id') or data.get('restaurant'))


def _resolve_employees(action, data):
    if action == 'create':
        return _as_str_id(data.get('restaurant'))
    try:
        return _as_str_id(
            RestaurantEmployee.objects
            .values_list('restaurant_id', flat=True)
            .get(id=data.get('id'))
        )
    except (RestaurantEmployee.DoesNotExist, ValueError, TypeError):
        return None


def _resolve_menusections(action, data):
    if action == 'create':
        return _as_str_id(data.get('restaurant'))
    try:
        return _as_str_id(
            MenuSection.objects
            .values_list('restaurant_id', flat=True)
            .get(id=data.get('id'))
        )
    except (MenuSection.DoesNotExist, ValueError, TypeError):
        return None


def _resolve_sectiongroups(action, data):
    if action == 'create':
        try:
            return _as_str_id(
                MenuSection.objects
                .values_list('restaurant_id', flat=True)
                .get(id=data.get('section'))
            )
        except (MenuSection.DoesNotExist, ValueError, TypeError):
            return None
    try:
        return _as_str_id(
            SectionGroup.objects
            .values_list('section__restaurant_id', flat=True)
            .get(id=data.get('id'))
        )
    except (SectionGroup.DoesNotExist, ValueError, TypeError):
        return None


def _resolve_menuitems(action, data):
    if action == 'create':
        try:
            return _as_str_id(
                MenuSection.objects
                .values_list('restaurant_id', flat=True)
                .get(id=data.get('section'))
            )
        except (MenuSection.DoesNotExist, ValueError, TypeError):
            return None
    try:
        return _as_str_id(
            MenuItem.objects
            .values_list('section__restaurant_id', flat=True)
            .get(id=data.get('id'))
        )
    except (MenuItem.DoesNotExist, ValueError, TypeError):
        return None


def _resolve_tables(action, data):
    if action == 'create':
        return _as_str_id(data.get('restaurant'))
    try:
        return _as_str_id(
            Table.objects
            .values_list('restaurant_id', flat=True)
            .get(id=data.get('id'))
        )
    except (Table.DoesNotExist, ValueError, TypeError):
        return None


def _resolve_diningareas(action, data):
    if action == 'create':
        return _as_str_id(data.get('restaurant'))
    try:
        return _as_str_id(
            DiningArea.objects
            .values_list('restaurant_id', flat=True)
            .get(id=data.get('id'))
        )
    except (DiningArea.DoesNotExist, ValueError, TypeError):
        return None


# Dispatch table: maps URL `config_detail` segment → restaurant resolver.
# Update/delete resolvers walk FK chains server-side from the record's id;
# they intentionally ignore any client-supplied `restaurant` field so a
# crafted payload like {id: <victim's record>, restaurant: <attacker's own>}
# cannot smuggle authorization.
_RESTAURANT_RESOLVERS = {
    'restaurants':   _resolve_restaurants,
    'employee':      _resolve_employees,   # alias used by handle_create_employee
    'employees':     _resolve_employees,
    'menusections':  _resolve_menusections,
    'sectiongroups': _resolve_sectiongroups,
    'menuitems':     _resolve_menuitems,
    'tables':        _resolve_tables,
    'diningareas':   _resolve_diningareas,
}


def _resolve_target_restaurant_id(record, action, data):
    resolver = _RESTAURANT_RESOLVERS.get(record)
    if resolver is None:
        return None
    try:
        return resolver(action, data or {})
    except Exception:
        logger.exception("Resolver %s failed; denying.", record)
        return None


# record / config_detail (URL segment) -> the permission MODULE that gates it.
# ONE mapping drives the catch-all write gate (check_permission), the GET list
# scoping (scope_list_filter) and the single-record detail read (get_detail).
# Per Decision 1, employees -> team (owner-only). A record absent here fails
# closed.
_RECORD_MODULE = {
    'restaurants':   MODULE_SETTINGS,
    'employee':      MODULE_TEAM,   # alias used by handle_create_employee
    'employees':     MODULE_TEAM,
    'menusections':  MODULE_MENU,
    'sectiongroups': MODULE_MENU,
    'menuitems':     MODULE_MENU,
    'tables':        MODULE_TABLES,
    'diningareas':   MODULE_TABLES,
}


def check_permission(user, record: str, action: str, request_data) -> bool:
    """
    Authorize a write against a restaurant-scoped resource via MODULE access.

    Returns True iff:
      - the user is authenticated and active, AND
      - the user is a dinify admin (bypass), OR may access the record's
        permission module (``_RECORD_MODULE``) at the *target* restaurant
        (``can_user_access_module``).

    Resolution of the target restaurant is server-side (see
    _RESTAURANT_RESOLVERS): for create it reads the payload, for
    update/delete it walks FK chains from the record's id. This blocks the
    spoof payload `{id: <victim's record>, restaurant: <attacker's own>}` —
    the module check runs against the SERVER-resolved restaurant, never a
    client-supplied one.

    Fails closed: returns False if the target restaurant cannot be resolved
    (e.g. a nonexistent id) or the record type has no module mapping.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if not user.is_active:
        return False
    if is_dinify_admin(user):
        return True

    target_restaurant_id = _resolve_target_restaurant_id(record, action, request_data)
    if not target_restaurant_id:
        logger.warning(
            "check_permission denied: unresolved restaurant. user=%s record=%s action=%s",
            getattr(user, 'id', None), record, action,
        )
        return False

    module = _RECORD_MODULE.get(record)
    if module is None:
        logger.warning(
            "check_permission denied: unmapped record. user=%s record=%s action=%s",
            getattr(user, 'id', None), record, action,
        )
        return False

    if can_user_access_module(user, target_restaurant_id, module):
        return True

    logger.warning(
        "check_permission denied: no module access. user=%s record=%s action=%s "
        "restaurant=%s module=%s",
        getattr(user, 'id', None), record, action, target_restaurant_id, module,
    )
    return False


# config_detail (URL segment) -> ORM lookup path from the served model to the
# owning restaurant's id. Used to authoritatively bind the GET read queryset to
# the caller's restaurants. Mirrors the restaurant paths in FILTER_DEFINITIONS.
LIST_RESTAURANT_PATH = {
    'restaurants':      'id',
    'employees':        'restaurant_id',
    'menusections':     'restaurant_id',
    'sectiongroups':    'section__restaurant_id',
    'menuitems':        'section__restaurant_id',
    'tables':           'restaurant_id',
    'diningareas':      'restaurant_id',
}


def scope_list_filter(user, config_detail, orm_filter):
    """
    Authoritatively bind a GET list queryset filter to the restaurants where the
    caller may access the resource's permission MODULE (``_RECORD_MODULE``). The
    added ``<path>__in`` clause ANDs with any client-supplied ``restaurant`` param
    on the same column, so the client can only narrow within the allowed set,
    never widen it (fail closed) — and a request that omits ``restaurant`` no
    longer leaks every tenant's records.

    Returns ``(orm_filter, ok)``. ``ok=False`` => deny (resource has no module
    mapping or no known ownership path). A dinify admin is unrestricted and the
    filter is left untouched; a caller without the module ends up with an empty
    ``__in`` list (no rows).
    """
    module = _RECORD_MODULE.get(config_detail)
    if module is None:
        return orm_filter, False
    allowed = get_module_restaurant_ids(user, module)
    if allowed is None:
        return orm_filter, True
    path = LIST_RESTAURANT_PATH.get(config_detail)
    if path is None:
        return orm_filter, False
    orm_filter[f'{path}__in'] = list(allowed)
    requested = orm_filter.get('restaurant')
    if requested is not None and str(requested) not in allowed:
        orm_filter['restaurant'] = None
    return orm_filter, True


def _is_false_flag(value):
    """
    Interpret a PUT boolean-ish flag as False.

    ``bool('false')`` is ``True`` in Python and the frontend sends the STRING
    ``'false'`` for a deactivation, so an explicit check is required. Recognises
    real ``False``, the strings ``'false'``/``'False'``/``'0'`` and integer ``0``;
    an absent value (or any other value) is treated as 'not false'.
    """
    if isinstance(value, str):
        return value.strip().lower() in ('false', '0')
    return value is False or value == 0


def build_scoped_instance_queryset(user, config_detail, model):
    """
    Build the authoritative, server-scoped queryset that Secretary.update /
    Secretary.delete resolve the target row through (locked). Reuses the same
    module gate + ownership path as the GET list scoping: a dinify admin is
    unrestricted (all rows — an explicit decision); otherwise only rows whose
    owning restaurant grants the caller the resource's module. A resource with no
    module or no known ownership path yields ``none()`` (fail closed). This — not
    the request body — is the object universe the mutation may touch, so a spoofed
    ``restaurant`` / foreign id can neither widen scope nor move a row.
    """
    module = _RECORD_MODULE.get(config_detail)
    if module is None:
        return model.objects.none()
    allowed = get_module_restaurant_ids(user, module)
    if allowed is None:  # dinify admin — unrestricted (explicit)
        return model.objects.all()
    path = LIST_RESTAURANT_PATH.get(config_detail)
    if path is None:
        return model.objects.none()
    return model.objects.filter(**{f'{path}__in': list(allowed)})


class RestaurantSetupEndpoint(APIView):
    """
    the endpoint for restaurant setups
    """
    def handle_create_employee(self, request):
        # TODO if the user is not a Dinify admin,.
        # then set the owner value from the auth details
        data = request.data
        try:
            data = data.dict()
        except Exception as error:
            logger.debug("Error converting data to dict: %s", error)

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record='employee',
            action='create',
            request_data=data,
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=403)

        try:
            response = create_employee(
                first_name=data.get('first_name'),
                last_name=data.get('last_name'),
                email=data.get('email'),
                phone_number=data.get('phone_number'),
                restaurant=Restaurant.objects.get(id=data.get('restaurant')),
                roles=data.get('roles'),
                creator=request.user,
                otp=data.get('otp'),
                skip_otp=True
            )
        except Exception as error:
            logger.error("Error while creating employee: %s", error)
            response = {
                'status': 500,
                'message': "An error occurred while creating the employee. Please check that you have provided all the details."
            }
        return Response(
            response,
            status=response['status']
        )

    def post(self, request, config_detail):
        """
        handle the POST method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        if config_detail == 'admin-register-restaurant':
            # Admin-only trust boundary: this branch mints User accounts and
            # dispatches credential SMS/email (self_register, skip_otp=True), so
            # it must be gated before any data processing. request.user is only
            # available here — the controller receives an auth_info dict.
            if not (
                request.user
                and request.user.is_authenticated
                and request.user.is_active
                and is_dinify_admin(request.user)
            ):
                return Response(
                    {'status': 403, 'message': 'Not authorised.'},
                    status=403,
                )

            post_data = request.data
            try:
                post_data = post_data.dict()
            except Exception as error:
                logger.debug("Error converting data to dict: %s", error)

            data = post_data.copy()
            response = admin_register_restaurant(
                data=data,
                auth_info={
                    'id': str(request.user.id),
                    'user_id': str(request.user.id),
                    'username': request.user.username,
                    'first_name': request.user.first_name,
                    'email': request.user.email
                }
            )
            return Response(response, status=response['status'])

        if config_detail == 'create-employee':
            return self.handle_create_employee(request)

        if config_detail == 'tables':
            tables_count = Table.objects.filter(
                restaurant=request.data.get('restaurant'),
                number=request.data.get('number'),
                deleted=False
            ).count()
            if tables_count > 0:
                response = {
                    'status': 400,
                    'message': f"Table number {request.data.get('number')} is already in use."
                }
                return Response(response, status=400)

        if config_detail == 'employees':
            # Gate the shortcut path explicitly: it returns 200 on success and
            # mutates the database, so it cannot rely on the check below.
            if not check_permission(
                user=request.user,
                record='employees',
                action='create',
                request_data=request.data,
            ):
                response = {
                    'status': 401,
                    'message': 'You do not have permission to perform this action.'
                }
                return Response(response, status=403)

            shortcut_employee_creation = ConRestaurantEmployee.create_employee_from_existing_user(
                user_id=request.data.get('user'),
                restaurant_id=request.data.get('restaurant'),
                roles=request.data.get('roles')
            )

            if shortcut_employee_creation['status'] == 200:
                return Response(
                    shortcut_employee_creation,
                    status=shortcut_employee_creation['status']
                )

        serializers = {
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable,
            'diningareas': SerializerPutDiningArea
        }

        required_information = {
            'employees': RI_RESTAURANT_EMPLOYEES,
            'menusections': REQUIRED_INFORMATION.get('menu_section'),
            'sectiongroups': RI_SECTION_GROUP,
            'menuitems': REQUIRED_INFORMATION.get('menu_item'),
            'tables': REQUIRED_INFORMATION.get('table'),
            'diningareas': RI_DINING_AREA
        }

        success_messages = {
            'employees': 'The employee has been added successfully.',
            'menusections': 'The menu section has been added successfully.',
            'sectiongroups': OK_ADDED_SECTION_GROUP,
            'menuitems': 'The menu item has been added successfully.',
            'tables': 'The table has been added successfully',
            'diningareas': 'The dining area has been added successfully'
        }

        error_messages = {
            'employees': 'An error occurred while adding the employee.',
            'menusections': 'An error occurred while adding the menu section.',
            'sectiongroups': ERR_ADDED_SECTION_GROUP,
            'menuitems': 'An error occurred while adding the menu item.',
            'tables': 'An error occurred while adding the table',
            'diningareas': 'An error occurred while adding the dining area'
        }

        msg_types = {
            'employees': 'new-restaurant-employee',
            'menusections': 'new-menu-section',
            'sectiongroups': 'new-menu-group',
            'menuitems': 'new-menu-item',
            'tables': 'new-table',
            'diningareas': 'new-dining-area'
        }

        post_data = request.data

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record=config_detail,
            action='create',
            request_data=post_data,
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=403)

        try:
            post_data = post_data.dict()
        except Exception as error:
            logger.debug("Error converting data to dict: %s", error)

        # Server-owned create values travel the TRUSTED Secretary server_values
        # channel (never the request payload): the auto-publication defaults
        # (approval UI removed) and the parent restaurant resolved from the
        # authorized resource — all read_only on the write serializers.
        server_values = {}
        if config_detail in ['menusections', 'sectiongroups', 'menuitems']:
            restaurant_id = None
            if config_detail == 'menusections':
                restaurant_id = post_data.get('restaurant')
            if config_detail in ['sectiongroups', 'menuitems']:
                restaurant_id = MenuSection.objects.get(
                    id=post_data['section']
                ).restaurant.pk
                restaurant_id = str(restaurant_id)

            if restaurant_id is not None:
                server_values['approved'] = True   # Always approve — approval UI removed
                server_values['enabled'] = True    # Always enable — approval UI removed
                # MenuSection.restaurant is server-derived — bind it from the
                # authorized resource, never the client payload (inventory #8).
                if config_detail == 'menusections':
                    server_values['restaurant_id'] = restaurant_id

            # Default new sections to the end of the rail so they don't
            # collide with existing sections at listing_position=0.
            if (
                config_detail == 'menusections'
                and restaurant_id is not None
                and 'listing_position' not in post_data
            ):
                max_pos = MenuSection.objects.filter(
                    restaurant_id=restaurant_id,
                    deleted=False,
                ).aggregate(max_pos=Max('listing_position'))['max_pos']
                post_data['listing_position'] = (max_pos + 1) if max_pos is not None else 0

            # Default new menu items to the end of their section so they don't
            # collide with existing items at listing_position=0. Items are scoped
            # per-section (not per-restaurant) because reordering happens within
            # a section, not across them.
            if (
                config_detail == 'menuitems'
                and 'listing_position' not in post_data
                and post_data.get('section') is not None
            ):
                max_pos = MenuItem.objects.filter(
                    section_id=post_data['section'],
                    deleted=False,
                ).aggregate(max_pos=Max('listing_position'))['max_pos']
                post_data['listing_position'] = (max_pos + 1) if max_pos is not None else 0

        if config_detail == 'tables':
            try:
                post_data['number'] = str(post_data.get('number'))
                post_data['str_number'] = str(post_data.get('number'))
            except Exception as error:
                logger.error("Error converting table number to string: %s", error)

        if config_detail == 'diningareas':
            response = create_dining_area(
                restaurant_id=post_data.get('restaurant'),
                dining_area_name=post_data.get('name'),
                smoking_zone=post_data.get('smoking_zone'),
                outdoor_seating=post_data.get('outdoor_seating'),
                user=request.user,
                create_tables=post_data.get('create_tables', False),
                consideration=post_data.get('consideration', 'count'),
                description=post_data.get('description', None),
                no_tables=post_data.get('no_tables', 0),
                range_from=int(post_data.get('start', 0)),
                range_to=int(post_data.get('end', 0))
            )
            return Response(response, status=response['status'])

        serializer = serializers.get(config_detail)
        required_information = required_information.get(config_detail)
        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        secretary_args = {
            'serializer': serializer,
            'data': post_data,
            'required_information': required_information,
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': success_message,
            'error_message': error_message,
            'user': request.user,
            'msg_type': msg_types.get(config_detail),
            'non_unique_handling': RECORDS_NON_UNIQUE_COMBINATIONS.get(config_detail),
            'server_values': server_values,
        }
        response = Secretary(secretary_args).create()

        # if the config_detail is menusections,
        # check if the groups were posted so as to create them
        if config_detail == 'menusections':
            if response['status'] != 200:
                return Response(
                    response,
                    status=response['status']
                )

            try:
                # check if the groups were posted
                section_groups = post_data.get('groups')
                if section_groups is None:
                    return Response(
                        response,
                        status=response['status']
                    )
                section = MenuSection.objects.get(id=response['data']['id'])
                section_groups = ast.literal_eval(section_groups)
                group_records = []
                for record in section_groups:
                    group_records.append(
                        SectionGroup(
                            name=record,
                            section=section
                        )
                    )
                SectionGroup.objects.bulk_create(group_records)
            except Exception as error:
                logger.error("BulkCreateSectionGroupsError: %s", error)
                response['message'] = f"{response['message']}. However, an error while defining the section groups." # noqa

        return Response(
            response,
            status=response['status']
        )

    def get(self, request, config_detail):
        """
        handle the GET method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        # auth = decode_jwt_token(request)

        if config_detail == 'details':
            return self.get_detail(request)

        if config_detail == 'subscription-details':
            # Tenant isolation: gate the subscription read on the `settings`
            # module at the requested restaurant (a dinify admin reads any).
            # 404, not 403, so we don't confirm whether another tenant's
            # restaurant exists.
            if not can_user_access_module(
                request.user, request.GET.get('restaurant'), MODULE_SETTINGS,
            ):
                return Response(
                    {'status': 404, 'message': 'Not found'}, status=404
                )
            return RestaurantSubscription().get_details(request)

        # Restaurant-scoped read of the diner-facing item sort mode. The
        # controller owns the permission check (mirrors the reorder dispatch).
        if config_detail == 'menu-item-sort-mode':
            response = ConMenuItemSortMode().get_mode(
                restaurant_id=request.GET.get('restaurant'),
                user=request.user,
            )
            return Response(response, status=response['status'])

        filter_params = request.GET.copy()
        orm_filter = define_filter_params(filter_params, config_detail)

        if config_detail == 'restaurants':
            if 'status' not in request.GET:
                # Default the list to the states a portal user can actually work in
                # (onboarding + live) — the successor to the old ['active','pending'].
                orm_filter['status__in'] = portal_access_states()

        if 'deleted' not in request.GET:
            orm_filter['deleted'] = False
        logger.debug("The ORM filter is: %s", orm_filter)
        logger.debug("The GET params are: %s", request.GET)

        if config_detail == 'menuitems':
            # orm_filter['section_group__deleted'] = False
            # orm_filter['section_group__available'] = True
            # if 'available' not in request.GET:
            #     orm_filter['available'] = True
            if 'is_extra' in request.GET:
                orm_filter['is_extra'] = True if request.GET.get('is_extra') == 'true' else False

        if config_detail == 'sectiongroups':
            if 'available' not in request.GET:
                orm_filter['available'] = True

        if config_detail == 'tables':
            if request.GET.get('grouping') is not None:
                # Tenant isolation: this branch builds its own queryset from the
                # client-supplied ?restaurant=, bypassing the list scoping below.
                # Gate on the `tables` module at that restaurant.
                if not can_user_access_module(
                    request.user, request.GET.get('restaurant'), MODULE_TABLES,
                ):
                    return Response(
                        {'status': 404, 'message': 'Not found'}, status=404
                    )
                response = get_tables_by_area(
                    restaurant_id=request.GET.get('restaurant')
                )
                return Response(response, status=200)

        # Authoritatively bind the read to the caller's restaurants. A dinify
        # admin is unrestricted; everyone else is scoped to their owner/manager
        # restaurants and the client ?restaurant= can only narrow within that set.
        orm_filter, scope_ok = scope_list_filter(request.user, config_detail, orm_filter)
        if not scope_ok:
            return Response(
                {
                    'status': 403,
                    'message': 'You do not have permission to read this resource.'
                },
                status=403,
            )

        serializers = {
            'restaurants': SerializerPublicGetRestaurant,
            'employees': SerializerGetRestaurantEmployee,
            'menusections': SerializerPublicGetMenuSection,
            'sectiongroups': SerializerPublicGetSectionGroup,
            'menuitems': SerializerPublicGetMenuItem,
            'tables': SerializerPublicGetTable,
            'diningareas': SerializerGetDiningArea
        }

        success_messages = {
            'restaurants': 'Successfully retrieved the restaurants',
            'employees': 'Successfully retrieved the employees',
            'menusections': 'Successfully retrieved the menu sections',
            'sectiongroups': OK_RETRIEVED_SECTION_GROUP,
            'menuitems': 'Successfully retrieved the menu items',
            'tables': 'Successfully retrieved the tables',
            'diningareas': 'Successfully retrieved the dining areas'
        }

        error_messages = {
            'restaurants': 'Error while retrieving restaurants',
            'employees': 'Error while retrieving employees',
            'menusections': 'Error while retrieving menu sections',
            'sectiongroups': ERR_RETRIEVED_SECTION_GROUP,
            'menuitems': 'Error while retrieving menu items',
            'tables': 'Error while retrieving the tables',
            'diningareas': 'Error while retrieving the dining areas'
        }

        serializer = serializers.get(config_detail)
        # TODO determine the correct serializer to use depending on the role

        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        secretary_args = {
            'request': request,
            'serializer': serializer,
            'filter': orm_filter,
            'paginate': True,
            'user_id': request.user.id,
            'username': request.user.username,
            'success_message': success_message,
            'error_message': error_message
        }

        response = Secretary(secretary_args).read()

        return Response(
            response,
            status=response['status']
        )

    def put(self, request, config_detail):
        """
        handle the PUT method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        serializers = {
            'restaurants': SerializerPutRestaurant,
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable,
            'diningareas': SerializerPutDiningArea
        }

        edit_information = {
            'restaurants': EDIT_INFORMATION.get('restaurants'),
            'employees': EDIT_INFORMATION.get('restaurant_employee'),
            'menusections': EDIT_INFORMATION.get('menu_section'),
            'sectiongroups': EI_SECTION_GROUP,
            'menuitems': EDIT_INFORMATION.get('menu_item'),
            'tables': EDIT_INFORMATION.get('table'),
            'diningareas': EI_DINING_AREA
        }

        success_messages = {
            'restaurants': 'The details of the restaurant have been updated successfully.',
            'employees': 'The details of the employee have been updated successfully',
            'menusections': 'The details of the menu section have been updated successfully.',
            'sectiongroups': OK_UPDATED_SECTION_GROUP,
            'menuitems': 'The details of the menu item have been updated successfully.',
            'tables': 'The details of the table have been updated successfully.',
            'diningareas': 'The details of the dining area have been updated successfully.'
        }

        error_messages = {
            'restaurants': 'An error occurred while updating the details of the restaurant.',
            'employees': 'An error occurred while updating the details of the employee.',
            'menusections': 'An error occurred while updating the details of the menu section.',
            'sectiongroups': ERR_UPDATED_SECTION_GROUP,
            'menuitems': 'An error occurred while updating the details of the menu item.',
            'tables': 'An error occurred while updating the details of the table.',
            'diningareas': 'An error occurred while updating the details of the dining area.'
        }

        # TODO check if the user has permissions to edit the details

        serializer = serializers.get(config_detail)
        edit_information = edit_information.get(config_detail)
        success_message = success_messages.get(config_detail)
        error_message = error_messages.get(config_detail)

        put_data = request.data

        # Non-CRUD config_detail values (reorder + subscription) don't match
        # the resolver dispatch (which is keyed on CRUD resource names), so
        # they are handled above the generic gate. Each self-guards: the
        # reorder / sort-mode controllers take user= and check internally, and
        # RestaurantSubscription.update enforces a Dinify-admin-only gate on the
        # subscription write. (The subscription READ is gated separately in
        # get() on the settings module.)
        if config_detail == 'subscription-details':
            return RestaurantSubscription().update(request)

        if config_detail in ('reorder-menu-sections', 'reorder-menu-items'):
            if config_detail == 'reorder-menu-items':
                logger.warning(
                    'Deprecated endpoint reorder-menu-items called; use reorder-menu-sections.'
                )
            ordered_ids = normalize_ordered_section_ids(put_data)
            response = ConMenuSection().reorder_listing(
                ordered_ids=ordered_ids,
                user=request.user,
            )
            return Response(response, status=response['status'])

        if config_detail == 'reorder-section-items':
            response = ConMenuItem().reorder_listing(
                section_id=put_data.get('section_id'),
                ordered_ids=put_data.get('ordered_ids'),
                user=request.user,
            )
            return Response(response, status=response['status'])

        if config_detail == 'menu-item-sort-mode':
            response = ConMenuItemSortMode().set_mode(
                restaurant_id=put_data.get('restaurant'),
                mode=put_data.get('mode'),
                user=request.user,
            )
            return Response(response, status=response['status'])

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record=config_detail,
            action='update',
            request_data=put_data,
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=403)

        # A restaurant must always keep at least one ACTIVE owner. The live
        # deactivation path is PUT {active:'false'} (the old DELETE-employees
        # branch was dead), so the last-owner guard lives here. Resolve the
        # target through the SAME server-scoped queryset Secretary uses — an
        # out-of-scope id then gets the ordinary not-found posture instead of a
        # raw lookup that leaks existence. Returns 409 (never 403 — a 403
        # force-logs-out the client), matching the deletion-integrity guards.
        if config_detail == 'employees' and _is_false_flag(put_data.get('active')):
            target = build_scoped_instance_queryset(
                request.user, config_detail, SerializerPutRestaurantEmployee.Meta.model,
            ).filter(id=put_data.get('id')).values('roles', 'restaurant').first()
            if (
                target
                and RESTAURANT_OWNER in target['roles']
                and not RestaurantEmployee.objects.filter(
                    restaurant_id=target['restaurant'],
                    roles__contains=[RESTAURANT_OWNER],
                    active=True,
                    deleted=False,
                ).exclude(id=put_data.get('id')).exists()
            ):
                return Response(
                    {'status': 409,
                     'message': 'You need to assign another restaurant owner '
                                'before you can deactivate this one.'},
                    status=409,
                )

        # `flat_fee` is the Dinify subscription price charged to the restaurant
        # (finance_app tx_subscription bills restaurant.flat_fee) — platform-owned
        # state a tenant must never write, so a non-admin's value is silently
        # stripped here while Dinify admins keep write access. Stripping (not 403)
        # matches how Secretary already ignores non-applicable fields; the tenant
        # portal never sends the field, so nothing legitimate breaks.
        #
        # `status` USED TO BE STRIPPED HERE TOO. It no longer needs to be, and the
        # strip would now be misleading: PR-5 made the lifecycle a constrained axis
        # owned by ONE writer (restaurants_app.controllers.lifecycle). `status` left
        # EDIT_INFORMATION and is read_only on SerializerPutRestaurant, so NO caller
        # reaches it through this path — not a tenant, and not a Dinify admin. The
        # legacy admin changeApprovalStatus PUT is therefore retired; lifecycle
        # changes happen only through POST admin/v1/restaurants/<id>/transition/.
        if config_detail == 'restaurants' and not is_dinify_admin(request.user):
            admin_only_fields = [
                key for key in ('flat_fee',) if key in put_data
            ]
            if admin_only_fields:
                # request.data is uncopied on this path and may be an immutable
                # QueryDict (form/multipart) — copy before mutating.
                put_data = put_data.copy()
                for key in admin_only_fields:
                    put_data.pop(key, None)
                logger.warning(
                    'Stripped tenant-supplied admin-only restaurant field(s) %s. '
                    'user=%s restaurant=%s',
                    admin_only_fields, auth.get('id'), put_data.get('id'),
                )

        # if editing a menu item,
        # convert the options and extras_applicable to a list
        if config_detail == 'menuitems':
            try:
                if type(put_data) is not dict:
                    put_data = put_data.dict()
            except Exception as error:
                logger.error("Error parsing data to dict: %s", error)

            options = put_data.get('options')
            if options is not None:
                # convert the options to dict
                if type(put_data) is not dict:
                    put_data['options'] = ast.literal_eval(options)

            # extras_applicable is now a typed list field (JSONStringCompatListField
            # + UUID child) that decodes a multipart stringified array itself and
            # rejects non-JSON — the old ast.literal_eval-on-request-input path is
            # both dead (put_data is already a dict here) and a footgun, so it is gone.

            # A multipart clear of the nullable section_group arrives as '' — map it
            # to an explicit null so it reads as "clear this group" (not an invalid
            # pk 400) and the cohesion validator can tell it apart from an omission.
            if put_data.get('section_group') == '':
                put_data['section_group'] = None

        # Handle explicit image-clearing sentinels.
        # These must be processed before Secretary.update() runs because Secretary
        # treats None/missing values as "no change" — which is correct in general
        # but means there's no way to clear a file field through the normal path.
        if config_detail == 'menusections':
            try:
                if type(put_data) is not dict:
                    put_data = put_data.dict()
            except Exception as error:
                logger.error("Error parsing data to dict: %s", error)

        if config_detail == 'menuitems' and put_data.get('clear_image'):
            record_id = put_data.get('id')
            if record_id:
                try:
                    target = MenuItem.objects.get(id=record_id)
                    if target.image:
                        try:
                            target.image.delete(save=False)
                        except Exception as file_err:
                            logger.error("Failed to delete menu item image file: %s", file_err)
                    target.image = None
                    target.save(update_fields=['image'])
                except MenuItem.DoesNotExist:
                    pass
            put_data.pop('clear_image', None)

        if config_detail == 'menusections' and put_data.get('clear_section_banner_image'):
            record_id = put_data.get('id')
            if record_id:
                try:
                    target = MenuSection.objects.get(id=record_id)
                    if target.section_banner_image:
                        try:
                            target.section_banner_image.delete(save=False)
                        except Exception as file_err:
                            logger.error("Failed to delete menu section banner file: %s", file_err)
                    target.section_banner_image = None
                    target.save(update_fields=['section_banner_image'])
                except MenuSection.DoesNotExist:
                    pass
            put_data.pop('clear_section_banner_image', None)

        secretary_args = {
            'serializer': serializer,
            'data': put_data,
            'edit_considerations': edit_information,
            'user_id': auth['id'],
            'username': auth['username'],
            'success_message': success_message,
            'error_message': error_message,
            'user': request.user,
            # Authoritative server-built scope: Secretary resolves + locks the row
            # ONLY within the restaurants where this actor may access the resource's
            # module. A spoofed restaurant / foreign id cannot widen or move it.
            'instance_queryset': build_scoped_instance_queryset(
                request.user, config_detail, serializer.Meta.model,
            ),
        }

        response = Secretary(secretary_args).update()

        return Response(
            response,
            status=response['status']
        )

    def delete(self, request, config_detail):
        """
        handle the DELETE method
        """
        response = {'status': 500, 'message': "Invalid request"}
        # decode the token
        auth = decode_jwt_token(request)

        # check if the actor has rights to perform the action
        if not check_permission(
            user=request.user,
            record=config_detail,
            action='delete',
            request_data=request.data,
        ):
            response = {
                'status': 401,
                'message': 'You do not have permission to perform this action.'
            }
            return Response(response, status=403)

        data = request.data

        serializer = {
            'employees': SerializerPutRestaurantEmployee,
            'menusections': SerializerPutMenuSection,
            'sectiongroups': SerializerPutSectionGroup,
            'menuitems': SerializerPutMenuItem,
            'tables': SerializerPutTable,
            'diningareas': SerializerPutDiningArea
        }

        # --- deletion-integrity guards (the rule lives on the model) ---
        # Block the soft-delete up front when the entity still has dependents
        # that must be dealt with first. Scoped to the relevant resource types
        # and kept here, NOT inside the generic Secretary, so it survives the
        # substrate migration. Returns 409 (never 403 — a 403 force-logs-out
        # the client).
        if config_detail == 'diningareas':
            area = DiningArea.objects.filter(id=data.get('id')).first()
            blocker = area.deletion_blockers() if area else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)
        elif config_detail == 'tables':
            table = Table.objects.filter(id=data.get('id')).first()
            blocker = table.deletion_blockers() if table else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)
        elif config_detail == 'menuitems':
            item = MenuItem.objects.filter(id=data.get('id')).first()
            blocker = item.deletion_blockers() if item else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)
        elif config_detail == 'menusections':
            section = MenuSection.objects.filter(id=data.get('id')).first()
            blocker = section.deletion_blockers() if section else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)
        elif config_detail == 'sectiongroups':
            group = SectionGroup.objects.filter(id=data.get('id')).first()
            blocker = group.deletion_blockers() if group else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)

        secretary_args = {
            'serializer': serializer[config_detail],
            'data': data,
            'user_id': auth['id'],
            'username': auth['username'],
            'user': request.user,
            # Authoritative server-built scope (see the update path): Secretary
            # resolves + locks the row only within the actor's permitted universe.
            'instance_queryset': build_scoped_instance_queryset(
                request.user, config_detail, serializer[config_detail].Meta.model,
            ),
        }
        # The menu-item soft-delete runs the referenced-extra lifecycle guard inside
        # SerializerPutMenuItem.validate(), which takes a select_for_update row lock —
        # so it MUST execute in a transaction (the generic Secretary.delete() is not
        # transactional). Scoping one here also serialises a concurrent delete-extra
        # vs assign-extra on the extra's own row, closing that race.
        if config_detail == 'menuitems':
            with transaction.atomic():
                response = Secretary(secretary_args).delete()
        else:
            response = Secretary(secretary_args).delete()

        return Response(
            response,
            status=response['status']
        )

    def get_detail(self, request):
        try:
            serializers = {
                'restaurants': SerializerGetRestaurantDetail,
                'employees': SerializerGetRestaurantEmployee,
                'menusections': SerializerPutMenuSection,
                'menuitems': SerializerPutMenuItem,
                'tables': SerializerPutTable,
            }

            record = request.GET.get('record')
            id = request.GET.get('id')

            if record is None or id is None:
                response = {
                    'status': 400,
                    'message': ERR_UNSPECIFIED_RECORD_DETAILS
                }
                return Response(response, status=400)

            # Tenant isolation: resolve the record's owning restaurant via the
            # write-path resolvers (which walk FK chains from the id) and gate
            # the read on the record's permission module. A nonexistent id or
            # unknown record type resolves to None -> 404 BEFORE the module check
            # (never passed into the gate as None). 404, not 403, so we don't
            # confirm the record exists in another tenant.
            owner_restaurant_id = _resolve_target_restaurant_id(record, 'detail', {'id': id})
            module = _RECORD_MODULE.get(record)
            if (
                not owner_restaurant_id
                or module is None
                or not can_user_access_module(
                    request.user, owner_restaurant_id, module,
                )
            ):
                return Response(
                    {'status': 404, 'message': ERR_GENERAL}, status=404
                )

            serializer = serializers.get(record)
            db_record = serializer.Meta.model.objects.get(
                id=id
            )
            response = {
                'status': 200,
                'message': OK_GET_RECORD_DETAIL,
                'data': serializer(db_record, many=False).data
            }
            return Response(response, status=200)
        except Exception as error:
            logger.error("Error while getting record detail: %s", error)
            response = {
                'status': 400,
                'message': ERR_GENERAL
            }
            return Response(response, status=400)
