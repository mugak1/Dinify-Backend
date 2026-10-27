from django.apps import AppConfig


class UsersAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'users_app'

    def ready(self):
        # Registers the D11 abuse-limit configuration check.
        from users_app import checks  # noqa: F401
