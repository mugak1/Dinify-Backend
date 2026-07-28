from django.apps import AppConfig


class RestaurantsAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'restaurants_app'

    def ready(self):
        # Import for the side effect of registering the deploy checks in
        # restaurants_app/checks.py. This app owns them because it owns the advisory
        # lock they exist to protect (controllers/admission_lock.py).
        from restaurants_app import checks  # noqa: F401
