from django.urls import path
from restaurants_app.endpoints.restaurant_setup import RestaurantSetupEndpoint
from restaurants_app.endpoints.misc_public import MiscPublicEndpoint
from restaurants_app.endpoints.manager_actions import RestaurantManagerActionsEndpoint
from restaurants_app.endpoints.upsell_config import UpsellConfigEndpoint, UpsellItemsEndpoint


urlpatterns = [
    # Upsell config endpoints (must be before the catch-all)
    path('upsell-config/', UpsellConfigEndpoint.as_view()),
    path('upsell-config/items/reorder/', UpsellItemsEndpoint.as_view(), {'action': 'reorder'}),
    path('upsell-config/items/<str:item_id>/', UpsellItemsEndpoint.as_view()),
    path('upsell-config/items/', UpsellItemsEndpoint.as_view()),
    # Existing patterns
    path('<str:config_detail>/', RestaurantSetupEndpoint.as_view()),
    path('manager-actions/<str:action>/', RestaurantManagerActionsEndpoint.as_view()),
    path('misc-public/<str:config_detail>/', MiscPublicEndpoint.as_view()),
]
