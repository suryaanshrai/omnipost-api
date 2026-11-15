from django.apps import AppConfig


class OmnipostApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'omnipost_api'

    def ready(self):
        from . import schema_auth, signals  # noqa: F401
