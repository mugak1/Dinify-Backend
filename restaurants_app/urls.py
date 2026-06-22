from django.urls import path
from restaurants_app.endpoints.restaurant_setup import RestaurantSetupEndpoint
from restaurants_app.endpoints.misc_public import MiscPublicEndpoint
from restaurants_app.endpoints.manager_actions import RestaurantManagerActionsEndpoint
from restaurants_app.endpoints.upsell_config import UpsellConfigEndpoint, UpsellItemsEndpoint
from restaurants_app.endpoints.preset_tags import PresetTagsEndpoint
from restaurants_app.endpoints.restaurant_tags import (
    RestaurantTagsEndpoint, RestaurantTagDetailEndpoint,
)
from restaurants_app.endpoints.reservations import ReservationsEndpoint
from restaurants_app.endpoints.waitlist import WaitlistEndpoint
from restaurants_app.endpoints.table_actions import TableActionsEndpoint
from restaurants_app.endpoints.role_permissions import RolePermissionsEndpoint


urlpatterns = [
    # Restaurant tags catalog (must be before the catch-all)
    path('restaurant-tags/', RestaurantTagsEndpoint.as_view()),
    path('restaurant-tags/<uuid:tag_id>/', RestaurantTagDetailEndpoint.as_view()),
    # Preset tags endpoint (must be before the catch-all)
    path('preset-tags/', PresetTagsEndpoint.as_view()),
    # Upsell config endpoints (must be before the catch-all)
    path('upsell-config/', UpsellConfigEndpoint.as_view()),
    path('upsell-config/items/reorder/', UpsellItemsEndpoint.as_view(), {'action': 'reorder'}),
    path('upsell-config/items/', UpsellItemsEndpoint.as_view()),
    # Reservations & waitlist endpoints (must be before the catch-all)
    path('reservations/', ReservationsEndpoint.as_view()),
    path('waitlist/', WaitlistEndpoint.as_view()),
    # Table lifecycle actions (must be before the catch-all)
    path('table-actions/<str:action>/', TableActionsEndpoint.as_view()),
    # Role-permissions management (must be before the catch-all)
    path('role-permissions/', RolePermissionsEndpoint.as_view()),
    # Existing patterns
    path('<str:config_detail>/', RestaurantSetupEndpoint.as_view()),
    path('manager-actions/<str:action>/', RestaurantManagerActionsEndpoint.as_view()),
    path('misc-public/<str:config_detail>/', MiscPublicEndpoint.as_view()),
]
