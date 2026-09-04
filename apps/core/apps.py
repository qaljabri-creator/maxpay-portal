from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = _("النظام")

    def ready(self):
        # Registers the deployment checks for the security posture spec §11
        # asks for — build-order step 14.
        from . import checks  # noqa: F401
