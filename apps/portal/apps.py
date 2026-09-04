from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"
    label = "portal"
    verbose_name = _("بوابة العميل")

    def ready(self):
        # Registers the start-up checks that refuse a misconfigured embed.
        from . import checks  # noqa: F401
