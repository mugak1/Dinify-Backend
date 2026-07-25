"""
URL configuration for dinify_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
# from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('api/v1/health/', include('misc_app.urls')),
    path('api/v1/users/', include('users_app.urls')),
    path('api/v1/restaurant-setup/', include('restaurants_app.urls')),
    path('api/v1/orders/', include('orders_app.urls')),
    path('api/v2/orders/', include('orders_app.v2_urls')),
    path('api/v1/finances/', include('finance_app.urls')),
    path('api/v1/reports/', include('reports_app.urls')),
    path('api/v1/notifications/', include('notifications_app.urls')),
    path('api/v1/kitchen/', include('orders_app.urls_kitchen')),
    path('api/v1/support/', include('support_app.urls')),
    path('api/v1/reviews/', include('reviews_app.urls')),
    # Delegated administrator access — the customer-plane half of delegation:
    # redeem a one-time code for a session, inspect it, end it. The admin control
    # plane (minting, listing, revoking grants) is a separate urlconf entirely.
    path('api/v1/delegation/', include('platform_admin_app.urls_delegation')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
