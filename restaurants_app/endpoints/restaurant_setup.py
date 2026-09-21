"""
endpoints for restaurant configurations.
Refactoring needed to make it more maintainable.
"""
import ast
import logging
from django.db import transaction
from restaurants_app.controllers.catalogue_admission import (
    lock_catalogue_for_write,
)
from django.db.models import Max
from rest_framework.response import Response

logger = logging.getLogger(__name__)
from rest_framework.views import APIView
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
from restaurants_app.controllers.qr_disclosure import qr_disclosure_policy
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
from restaurants_app.controllers.employee_membership_lock import (
    lock_restaurant_for_membership_mutation,
)
from dinify_backend.configss.string_definitions import (
    RESTAURANT_OWNER,
    MODULE_SETTINGS,
    MODULE_TEAM,
    MODULE_MENU,
    MODULE_TABLES,
)

from users_app.controllers.permissions_check import (
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


#: The records whose WRITES an order admission reads, and which therefore take
#: the exclusive admission barrier. `restaurants` is the pause writer
#: (`accepting_orders`); the three menu records are what `purchase_integrity`
#: re-reads at acceptance. CREATE is deliberately absent: a row that does not
#: exist yet cannot be named by a saved quote line, so creating one changes no
#: verdict — and it is where `Secretary.create()`'s one SYNCHRONOUS MongoDB
#: notification lives, which must never sit inside this lock.
_ADMISSION_BARRIER_RECORDS = frozenset({
    'restaurants', 'menusections', 'sectiongroups', 'menuitems',
})

#: The catalogue records whose DELETE runs its blocker and its soft-delete under
#: one transaction and one barrier.
_CATALOGUE_DELETE_BLOCKERS = frozenset({
    'menusections', 'sectiongroups', 'menuitems',
})


def check_permission(user, record: str, action: str, request_data) -> bool:
    """
    Authorize a write against a restaurant-scoped resource via MODULE access.

    Returns True iff:
      - the user is authenticated and active, AND
      - the user may access the record's permission module (``_RECORD_MODULE``)
        at the *target* restaurant (``can_user_access_module``).

    There is no bypass. Every principal — including a delegated administrator —
    is evaluated through the same module gate at the same server-resolved
    restaurant.

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
    mapping or no known ownership path). A caller without the module ends up with
    an empty ``__in`` list (no rows). No principal is exempt from the binding —
    the resolver always returns a set.
    """
    module = _RECORD_MODULE.get(config_detail)
    if module is None:
        return orm_filter, False
    allowed = get_module_restaurant_ids(user, module)
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
    module gate + ownership path as the GET list scoping: only rows whose owning
    restaurant grants the caller the resource's module. A resource with no module
    or no known ownership path yields ``none()`` (fail closed). This — not the
    request body — is the object universe the mutation may touch, so a spoofed
    ``restaurant`` / foreign id can neither widen scope nor move a row.

    There is no ``model.objects.all()`` branch. The unrestricted queryset that used
    to serve a dinify admin was the single widest write surface on this plane; it
    went with the role predicates and must not come back.
    """
    module = _RECORD_MODULE.get(config_detail)
    if module is None:
        return model.objects.none()
    allowed = get_module_restaurant_ids(user, module)
    path = LIST_RESTAURANT_PATH.get(config_detail)
    if path is None:
        return model.objects.none()
    return model.objects.filter(**{f'{path}__in': list(allowed)})


class RestaurantSetupEndpoint(APIView):
    """
    the endpoint for restaurant setups
    """
    def handle_create_employee(self, request):
        # Authorization is `check_permission` below, which resolves the target
        # restaurant server-side and gates it on the owner-only `team` module.
        # (The TODO that stood here proposed deriving the owner from the auth
        # details "if the user is not a Dinify admin" — a distinction that no
        # longer exists on this plane.)
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

        # Phase 1: restaurant onboarding is admin-plane functionality, built
        # natively on /api/admin/v1. The `admin-register-restaurant` branch that
        # used to live here minted User accounts, dispatched credential SMS/email
        # (self_register, skip_otp=True) and created the Restaurant + owner
        # membership — all on the authority of a `dinify_admin` string in the
        # caller's User.roles. That is exactly the ambient authority this plane no
        # longer recognises, and the capability needs elevation + audit, which only
        # the admin plane provides. It is REMOVED here, not ported: an unknown
        # config_detail now falls through to the generic handling below.

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
        # A membership INSERT runs under the parent-Restaurant barrier, same as every
        # other membership writer. The parent is the one `check_permission` has
        # already authorized for this create (`_resolve_employees` reads
        # `data['restaurant']` on the create action), so locking it neither widens nor
        # narrows what this caller may reach.
        if config_detail == 'employees':
            with transaction.atomic():
                lock_restaurant_for_membership_mutation(post_data.get('restaurant'))
                response = Secretary(secretary_args).create()
        else:
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
            # module at the requested restaurant. 404, not 403, so we don't
            # confirm whether another tenant's restaurant exists. There is no
            # principal that reads any restaurant — the dinify-admin bypass that
            # used to short-circuit this gate is gone — so an absent or foreign
            # `?restaurant=` fails closed here and never reaches the controller.
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
                # The QR credential is bearer authority, not an ordinary field:
                # it is emitted only where ordinary, non-delegated `tables`
                # authority has been positively established. Resolved ONCE here,
                # from the request, AFTER the module gate above — never inside the
                # builder, and never from anything the caller supplied.
                response = get_tables_by_area(
                    restaurant_id=request.GET.get('restaurant'),
                    qr_policy=qr_disclosure_policy(request),
                )
                return Response(response, status=200)

        # Authoritatively bind the read to the caller's restaurants: the caller is
        # scoped to the restaurants whose role grid grants this record's module,
        # and the client ?restaurant= can only narrow within that set, never widen.
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

    def _update_employee(
        self, request, auth, put_data, serializer, edit_information,
        success_message, error_message,
    ):
        """
        PUT employees, under the parent-Restaurant serialization barrier.

        A membership PUT rewrites ``roles`` and/or ``active`` — the two facts
        ``platform_admin_app.onboarding.assert_owner_consistency`` reads. Being an
        UPDATE that never touches the FK, PostgreSQL's referential integrity does not
        block it against a held parent lock (measured; see
        ``employee_membership_lock``), so without this barrier it could commit in the
        window between an onboarding writer's consistency assertion and the
        credential or provenance row that assertion was guarding.

        THE PARENT IS RESOLVED SERVER-SIDE, from the membership's own FK via
        ``_RESTAURANT_RESOLVERS`` — never from a client-supplied ``restaurant``. That
        is the same resolution ``check_permission`` has already authorized against,
        and it is what makes ``{id: <victim's membership>, restaurant: <mine>}``
        unable to move the lock somewhere harmless. The FK itself is ``read_only`` on
        ``SerializerPutRestaurantEmployee``, so the parent cannot shift underneath the
        lock either.

        ORDER: Restaurant -> (last-owner guard) -> RestaurantEmployee. The guard's
        CHECK and the WRITE it guards are now inside the same lock; leaving the check
        outside would have swapped one check-then-act race for another.

        An unresolvable or vanished membership takes the lock helper's ``None`` path
        and falls through to Secretary's existing scoped lookup, which answers the
        ordinary non-enumerating 404. No existence or tenancy oracle widens.
        """
        with transaction.atomic():
            lock_restaurant_for_membership_mutation(
                _resolve_target_restaurant_id('employees', 'update', put_data)
            )

            # A restaurant must always keep at least one ACTIVE owner. The live
            # deactivation path is PUT {active:'false'} (the old DELETE-employees
            # branch was dead), so the last-owner guard lives here. Resolve the
            # target through the SAME server-scoped queryset Secretary uses — an
            # out-of-scope id then gets the ordinary not-found posture instead of a
            # raw lookup that leaks existence. Returns 409 (never 403 — a 403
            # force-logs-out the client), matching the deletion-integrity guards.
            if _is_false_flag(put_data.get('active')):
                target = build_scoped_instance_queryset(
                    request.user, 'employees',
                    SerializerPutRestaurantEmployee.Meta.model,
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

            response = Secretary({
                'serializer': serializer,
                'data': put_data,
                'edit_considerations': edit_information,
                'user_id': auth['id'],
                'username': auth['username'],
                'success_message': success_message,
                'error_message': error_message,
                'user': request.user,
                'instance_queryset': build_scoped_instance_queryset(
                    request.user, 'employees', serializer.Meta.model,
                ),
            }).update()

        return Response(response, status=response['status'])

    def _delete_employee(self, request, auth, data, serializer):
        """
        DELETE employees, under the parent-Restaurant serialization barrier.

        The soft-delete writes ``deleted=True``, which removes the row from exactly
        the set ``assert_owner_consistency`` counts — so it changes the invariant's
        answer just as surely as a role change does, and a child DELETE/UPDATE takes
        no parent lock of its own. The route is LIVE: only the special-cased branch
        that used to hold the last-owner guard was dead (DC-BE-011); ``employees`` is
        still in the ``delete()`` dispatch and is covered by a cross-tenant test.

        LOCK ORDER NOTE. ``Secretary.delete()`` runs ``ConVacuumDeletedRecords()``
        inline inside its transaction, and ``VACUUM_MODELS`` leads with
        ``Restaurant`` — so the sweep can in principle UPDATE ``restaurants`` rows
        while this transaction already holds one, which is the ``RestaurantEmployee
        -> Restaurant`` inversion to watch for. It is not reachable: no production
        path soft-deletes a ``Restaurant`` (the ``delete()`` dispatch has no
        ``restaurants`` key), so that sweep's queryset is empty by construction. The
        sweep's child models are safe on their own terms — an UPDATE that leaves the
        FK unchanged takes no lock on the parent row (measured on PostgreSQL 16).
        """
        with transaction.atomic():
            lock_restaurant_for_membership_mutation(
                _resolve_target_restaurant_id('employees', 'delete', data)
            )
            response = Secretary({
                'serializer': serializer,
                'data': data,
                'user_id': auth['id'],
                'username': auth['username'],
                'user': request.user,
                'instance_queryset': build_scoped_instance_queryset(
                    request.user, 'employees', serializer.Meta.model,
                ),
            }).delete()

        return Response(response, status=response['status'])

    def _delete_table(self, request, auth, data, serializer):
        """
        DELETE tables, with the deletion blocker decided UNDER the row lock.

        WHAT THIS CLOSES (D06). The blocker used to be evaluated in autocommit and
        Secretary then opened its own transaction to write the soft-delete, so the
        two decisions were made against different snapshots. A diner's order
        accepted in that gap was invisible to the check that had just said the
        table was free, and the table was soft-deleted with a live order on it —
        an order whose own boundary had, a moment earlier, correctly found the
        table usable.

        Holding the row across BOTH is what makes them one decision. Both order
        boundaries take this exact row ``FOR UPDATE``: an acceptance already in
        flight either commits first, in which case ``has_unsettled_orders`` sees
        its order and this refuses, or it waits behind this transaction and finds
        the table soft-deleted through its own checks. There is no interleaving
        where both succeed.

        LOCK ORDER: ``Table -> (read orders)``. The same direction the order path
        takes, and this branch never reaches for the admission advisory lock, so
        it can block an order but cannot cycle against one — nor against the
        lifecycle transition, which never waits on a ``Table``.

        The refusal stays a 409 with the model's own sentence. It is NEVER a 403:
        a 403 force-logs-out the client, and being told to remove an order first
        is not an authorization failure.
        """
        with transaction.atomic():
            table = (
                Table.objects.select_for_update()
                .filter(id=data.get('id'))
                .first()
            )
            blocker = table.deletion_blockers() if table else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)

            # A table this actor may not reach, or one that does not exist, falls
            # through to Secretary's scoped lookup and its existing
            # non-enumerating 404 — the lock above deliberately does NOT decide
            # authority, and locking a row is not permission to read it.
            response = Secretary({
                'serializer': serializer,
                'data': data,
                'user_id': auth['id'],
                'username': auth['username'],
                'user': request.user,
                'instance_queryset': build_scoped_instance_queryset(
                    request.user, 'tables', serializer.Meta.model,
                ),
            }).delete()

        return Response(response, status=response['status'])

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
        # reorder / sort-mode controllers take user= and check internally.
        #
        # Phase 1: setting a restaurant's subscription validity/expiry is
        # admin-plane functionality, built natively on /api/admin/v1. The write
        # verb here was reachable on a `dinify_admin` role string alone and could
        # grant any restaurant an indefinite free subscription; it is REMOVED, not
        # ported. 405 (not 403) because the path itself is still live — the
        # settings-gated subscription READ is served by get(). Mirrors the retired
        # DELETE on upsell-config/items/reorder/ (DC-BE-004).
        if config_detail == 'subscription-details':
            return Response(
                {
                    'status': 405,
                    'message': 'This action is no longer available.',
                },
                status=405,
            )

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

        # An employee PUT changes `roles` or `active` — the exact two facts
        # `assert_owner_consistency` reads — so it runs under the parent-Restaurant
        # serialization barrier, in its own branch. Nothing between here and the
        # Secretary call below applies to employees (the platform-field strip is
        # `restaurants`; the parsing and image sentinels are `menuitems` /
        # `menusections`), so splitting it out costs no behaviour and keeps the
        # last-owner guard and the write inside one lock.
        if config_detail == 'employees':
            return self._update_employee(
                request, auth, put_data, serializer, edit_information,
                success_message, error_message,
            )

        # `flat_fee` (the subscription PRICE Dinify charges the restaurant) and
        # `preferred_subscription_method` (the BILLING METHOD deciding whether a
        # subscription charge may be raised at all) are the two halves of Dinify's
        # side of the commercial relationship. Both are platform-owned state no
        # principal on THIS plane may write, so both are stripped UNCONDITIONALLY.
        #
        # `flat_fee` used to be stripped only for non-admins, leaving a
        # `dinify_admin` role-holder able to zero a subscription price through the
        # tenant portal; with ambient admin authority gone there is no such
        # principal. Delegation cannot reach this route in either case —
        # restaurant-setup is GET-only on the delegated allowlist.
        #
        # `preferred_subscription_method` stayed writable when `flat_fee` was
        # closed, which was an oversight rather than a decision. It is read by
        # `finance_app.tx_subscription.initiate`, whose ONLY gate is
        # `preferred_subscription_method == 'per_order'` -> refuse: an owner could
        # set `monthly` here and then POST api/v1/finances/transactions/ (which
        # authorises on `can_manage_restaurant`, and an owner passes) to have Dinify
        # record a subscription charge against terms it never chose. Closing the
        # write closes that path at its source; the transaction controller is
        # unchanged.
        #
        # Phase 1: both are admin-plane functionality, built natively on
        # /api/admin/v1 — the keys deliberately STAY in EDIT_INFORMATION so that
        # writer can still go through Secretary. This is a post-gate payload strip,
        # NOT an EDIT_INFORMATION removal; contrast `status` / `is_test`, which have
        # a dedicated single writer assigning the model field directly and are
        # therefore absent from EDIT_INFORMATION entirely. Stripping (not 403)
        # matches how Secretary already ignores non-applicable fields; the tenant
        # portal never sends either field, so nothing legitimate breaks.
        #
        # `status` USED TO BE STRIPPED HERE TOO. It no longer needs to be, and the
        # strip would now be misleading: PR-5 made the lifecycle a constrained axis
        # owned by ONE writer (restaurants_app.controllers.lifecycle). `status` left
        # EDIT_INFORMATION and is read_only on SerializerPutRestaurant, so NO caller
        # reaches it through this path. The legacy admin changeApprovalStatus PUT is
        # therefore retired; lifecycle changes happen only through
        # POST admin/v1/restaurants/<id>/transition/.
        if config_detail == 'restaurants':
            platform_only_fields = [
                key for key in ('flat_fee', 'preferred_subscription_method')
                if key in put_data
            ]
            if platform_only_fields:
                # request.data is uncopied on this path and may be an immutable
                # QueryDict (form/multipart) — copy before mutating.
                put_data = put_data.copy()
                for key in platform_only_fields:
                    put_data.pop(key, None)
                logger.warning(
                    'Stripped platform-owned restaurant field(s) %s. '
                    'user=%s restaurant=%s',
                    platform_only_fields, auth.get('id'), put_data.get('id'),
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

        # A RESTAURANT WRITE PARTICIPATES IN ORDER ADMISSION (D06). This is the
        # PAUSE WRITER — `accepting_orders` is edited here and nowhere else — and
        # since D06 both order boundaries enforce it, at creation and at first
        # acceptance, from a value re-read inside the order transaction.
        #
        # THE ADVISORY LOCK IS WHAT MAKES THAT ENFORCEMENT REAL; a `Restaurant`
        # row lock would not. `order_admission.admit` reads `accepting_orders`
        # with a plain `values_list().get()`, and under MVCC that read does not
        # block on a row held FOR UPDATE — so an owner pausing mid-service could
        # commit while an admission that had already read `True` was still
        # waiting on the table lock, and the order went through after the pause.
        # That is exactly the reasoning `mark_restaurant_test` records for the
        # `is_test` flag, which rides the same protected read.
        #
        # EXCLUSIVE, and FIRST — before Secretary takes the row. It is the same
        # order the lifecycle transition uses (`advisory EXCLUSIVE -> Restaurant`),
        # so this writer joins an ordering already proven acyclic rather than
        # adding a level. Taking it AFTER the row lock would invert that ordering
        # and reintroduce the cycle it exists to prevent.
        #
        # THE MENU RECORDS JOINED IT IN THE D06 COMPLETION (G1a), AND THE
        # SENTENCE THAT USED TO SIT HERE IS WHY THEY HAD TO. It read: "taking a
        # per-restaurant exclusive lock for a menu-item rename would queue every
        # diner order at the restaurant behind an edit that no admission reads."
        # True when admission read three restaurant columns; FALSE the moment
        # D06's own `purchase_integrity` made acceptance re-read the catalogue,
        # which happened in the same change. An edit committing after that read
        # and before the transition put an order on a kitchen board against a
        # dish that had just been withdrawn.
        #
        # `tables` and `employees` stay out, and still for their own reasons:
        # both order boundaries take the `Table` row `FOR UPDATE`, and a
        # membership write takes the parent-`Restaurant` barrier. Neither needs
        # this lock, and a record that changes nothing an admission reads must
        # not take it — see `catalogue_admission` for the field inventory.
        if config_detail in _ADMISSION_BARRIER_RECORDS:
            admission_target = _resolve_target_restaurant_id(
                config_detail, 'update', put_data)
            with transaction.atomic():
                # `admission_target` is the SERVER-resolved id `check_permission`
                # authorized against, not a client-supplied one. A `None` here
                # means the resolver could not identify a target, in which case
                # the permission gate above has already refused — but the lock is
                # skipped rather than guessed at, and Secretary's scoped queryset
                # is what answers.
                lock_catalogue_for_write(admission_target)
                response = Secretary(secretary_args).update()
        else:
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
            # THE TABLE BLOCKER IS DECIDED UNDER THE ROW LOCK, AND THE DELETE
            # HAPPENS IN THE SAME TRANSACTION (D06).
            #
            # It used to read the table and its orders in autocommit and then
            # hand off to Secretary, which opened its OWN transaction — so a
            # diner's order could be accepted in the gap and the table was
            # soft-deleted out from under it. Both order boundaries take this
            # exact row `FOR UPDATE`, so holding it here is what makes the two
            # serialize: an acceptance in flight either commits first (and the
            # blocker then sees its order and refuses) or waits (and finds the
            # table gone through its own checks). `has_unsettled_orders` is a
            # read over `orders`, so the direction is `Table -> Order`, matching
            # the order path exactly; this branch never reaches for the admission
            # advisory lock, so it can block an order but cannot cycle with one.
            #
            # `_delete_within` runs Secretary inside the block it opened.
            return self._delete_table(
                request, auth, data, serializer[config_detail],
            )
        # A CATALOGUE SOFT-DELETE IS AN ELIGIBILITY WRITE, so it takes the
        # admission barrier and decides its blocker under it (D06 G1a) — the
        # same treatment `_delete_table` already gives the tables branch, and
        # for the same reason: reading the blocker in autocommit and then
        # handing off to a transaction Secretary opens leaves a window an
        # acceptance can commit in.
        if config_detail in _CATALOGUE_DELETE_BLOCKERS:
            return self._delete_catalogue_record(
                request, auth, data, serializer[config_detail], config_detail,
            )

        # A membership soft-delete changes the owner-consistency predicate, so it
        # runs under the parent-Restaurant barrier in its own branch.
        if config_detail == 'employees':
            return self._delete_employee(
                request, auth, data, serializer[config_detail],
            )

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
        response = Secretary(secretary_args).delete()

        return Response(
            response,
            status=response['status']
        )

    def _delete_catalogue_record(self, request, auth, data, write_serializer,
                                 config_detail):
        """Soft-delete a menu record under the admission barrier.

        THE BARRIER IS THE FIRST STATEMENT of the transaction, before the
        blocker read and before Secretary takes its row — the documented
        ``advisory -> rows`` order. Taking it after a row lock would invert that
        ordering; taking it after the blocker read would leave the very window
        this branch exists to close.

        The blocker itself now runs INSIDE the transaction. It used to be read
        in autocommit while Secretary opened its own, so a delete could be
        allowed on evidence that had already changed — the defect D06 fixed for
        tables and did not carry across to the menu.

        The menu-item soft-delete additionally runs the referenced-extra
        lifecycle guard inside ``SerializerPutMenuItem.validate()``, which takes
        a ``select_for_update`` row lock and so must execute in a transaction;
        that requirement is unchanged and is now satisfied for all three
        records rather than one.
        """
        model = {
            'menuitems': MenuItem,
            'menusections': MenuSection,
            'sectiongroups': SectionGroup,
        }[config_detail]

        with transaction.atomic():
            lock_catalogue_for_write(
                _resolve_target_restaurant_id(config_detail, 'delete', data))

            record = model.objects.filter(id=data.get('id')).first()
            blocker = record.deletion_blockers() if record else None
            if blocker:
                return Response({'status': 409, 'message': blocker}, status=409)

            secretary_args = {
                'serializer': write_serializer,
                'data': data,
                'user_id': auth['id'],
                'username': auth['username'],
                'user': request.user,
                'instance_queryset': build_scoped_instance_queryset(
                    request.user, config_detail, write_serializer.Meta.model,
                ),
            }
            response = Secretary(secretary_args).delete()

        return Response(response, status=response['status'])

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
