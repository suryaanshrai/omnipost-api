from django.apps import AppConfig


class OmnipostApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'omnipost_api'

    def ready(self):
        from . import signals  # noqa: F401
