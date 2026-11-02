from django.apps import AppConfig


class OrdersAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orders_app'

    def ready(self):
        # Import for the side effect of registering the system check in
        # orders_app/checks.py (the dining-model writer switch warning).
        from orders_app import checks  # noqa: F401
