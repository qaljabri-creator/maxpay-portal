from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MerchantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.merchants"
    label = "merchants"
    verbose_name = _("التجار وطرق الدفع")
