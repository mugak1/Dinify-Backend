from django.urls import path
from restaurants_app.endpoints.restaurant_setup import RestaurantSetupEndpoint
from restaurants_app.endpoints.misc_public import MiscPublicEndpoint
from restaurants_app.endpoints.manager_actions import RestaurantManagerActionsEndpoint
from restaurants_app.endpoints.upsell_config import UpsellConfigEndpoint, UpsellItemsEndpoint
from restaurants_app.endpoints.preset_tags import PresetTagsEndpoint
from restaurants_app.endpoints.reservations import ReservationsEndpoint
from restaurants_app.endpoints.waitlist import WaitlistEndpoint
from restaurants_app.endpoints.table_actions import TableActionsEndpoint


urlpatterns = [
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
    # Existing patterns
    path('<str:config_detail>/', RestaurantSetupEndpoint.as_view()),
    path('manager-actions/<str:action>/', RestaurantManagerActionsEndpoint.as_view()),
    path('misc-public/<str:config_detail>/', MiscPublicEndpoint.as_view()),
]
