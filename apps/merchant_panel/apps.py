from django.apps import AppConfig


class MerchantPanelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.merchant_panel"
    label = "merchant_panel"
    verbose_name = "لوحة التاجر"

    def ready(self):
        # Registers the start-up checks that refuse a misconfigured embed.
        from . import checks  # noqa: F401
