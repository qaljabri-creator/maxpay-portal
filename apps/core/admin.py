from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .choices import AuditAction
from .models import AuditLog, SystemSettings
from .services import record_audit, snapshot


class ReadOnlyAdminMixin:
    """Registers a model for viewing only — no add, change or delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Spec §11: the audit log has no delete or update path anywhere."""

    list_display = ("created_at", "action", "actor_display", "target_type", "target_id", "ip")
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("actor_label", "target_type", "target_id", "ip")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("actor",)

    @admin.display(description=_("المنفّذ"), ordering="actor_label")
    def actor_display(self, obj):
        return obj.actor_label or (str(obj.actor) if obj.actor_id else _("النظام"))


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    """Singleton editor. Only finance_admin may change business hours (spec §9)."""

    fieldsets = (
        (_("مواعيد العمل"), {"fields": ("open_time", "close_time", "timezone", "is_open_override")}),
        (_("رسالة الإغلاق"), {"fields": ("closed_message_ar",)}),
        (_("تواريخ"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    readonly_fields = ("created_at", "updated_at")

    def has_add_permission(self, request):
        # The singleton is created on first access; never add a second row.
        return not SystemSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        SystemSettings.load()
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        before = snapshot(SystemSettings.objects.get(pk=obj.pk)) if change and obj.pk else None
        super().save_model(request, obj, form, change)
        record_audit(
            action=AuditAction.SETTINGS_CHANGE,
            target=obj,
            request=request,
            before=before,
            after=snapshot(obj),
        )


admin.site.site_header = _("بوابة MaxPay")
admin.site.site_title = _("بوابة MaxPay")
admin.site.index_title = _("لوحة الإدارة")
