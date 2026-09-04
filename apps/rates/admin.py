from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.core.choices import AuditAction
from apps.core.services import record_audit, snapshot

from .models import ExchangeRate, RateType


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    """Add-and-view only.

    Spec §5: a rate is never updated in place — setting a new rate means adding
    a row. The model has no ``change`` or ``delete`` permission at all, so the
    admin cannot offer either.
    """

    list_display = ("rate_type", "iqd_per_usd", "commission_iqd_per_100usd", "effective_from", "set_by", "is_current")
    list_filter = ("rate_type", "effective_from")
    search_fields = ("note",)
    date_hierarchy = "effective_from"
    ordering = ("-effective_from",)
    readonly_fields = ("set_by", "created_at")
    list_select_related = ("set_by",)

    @admin.display(description=_("سارٍ الآن"), boolean=True)
    def is_current(self, obj):
        current = ExchangeRate.current(obj.rate_type)
        return bool(current and current.pk == obj.pk)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        user = request.user
        return bool(
            user.is_authenticated
            and (user.is_superuser or getattr(user, "is_finance_admin", False))
            and super().has_add_permission(request)
        )

    def save_model(self, request, obj, form, change):
        obj.set_by = request.user
        previous = ExchangeRate.current(obj.rate_type)
        super().save_model(request, obj, form, change)
        record_audit(
            action=AuditAction.RATE_CHANGE,
            target=obj,
            request=request,
            before=snapshot(previous) if previous else None,
            after=snapshot(obj),
        )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["current_rates"] = {
            rate_type.label: ExchangeRate.current(rate_type.value)
            for rate_type in RateType
        }
        return super().changelist_view(request, extra_context)
