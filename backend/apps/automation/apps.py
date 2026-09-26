from django.apps import AppConfig


class AutomationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.automation"

    def ready(self):
        from . import actions  # noqa: F401 - registers approvable actions
