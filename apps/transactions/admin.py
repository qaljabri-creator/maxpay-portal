from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Attachment, Message, Request


class FinanceOnlyAdminMixin:
    """Restricts a model's admin to Finance.

    The admin shows client identity, so a merchant must never reach it (spec
    §2). Merchants get their own panel with masked serializers in build-order
    step 8; until then they have no request surface at all, which is the safe
    default rather than a partially-masked one.
    """

    def _is_finance(self, request) -> bool:
        user = request.user
        return bool(
            user.is_authenticated
            and (user.is_superuser or getattr(user, "can_see_client_identity", False))
        )

    def has_module_permission(self, request):
        return self._is_finance(request) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._is_finance(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return self._is_finance(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._is_finance(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ("original_name", "uploaded_by_role", "size_bytes", "uploaded_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("created_at", "sender_role", "body", "is_internal_note")
    readonly_fields = ("created_at", "sender_role", "body", "is_internal_note")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Request)
class RequestAdmin(FinanceOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "public_ref",
        "type",
        "status",
        "amount_usd",
        "amount_iqd",
        "payment_method",
        "merchant_assigned",
        "submitted_at",
    )
    list_filter = ("type", "status", "payment_method", "merchant_assigned", "submitted_at")
    search_fields = ("public_ref", "client__display_name", "client__b2core_id", "destination_account")
    date_hierarchy = "submitted_at"
    ordering = ("-submitted_at",)
    inlines = [AttachmentInline, MessageInline]
    autocomplete_fields = ("client", "payment_method", "merchant_selected", "merchant_assigned")
    list_select_related = ("payment_method", "merchant_assigned", "client")

    readonly_fields = (
        "public_ref",
        "submitted_at",
        "assigned_at",
        "merchant_actioned_at",
        "closed_at",
        "created_at",
        "updated_at",
        # Snapshotted at submission; changing them retroactively would rewrite
        # the terms the client agreed to (spec §5).
        "wallet_number_snapshot",
        "rate_applied",
        "commission_applied",
        "amount_usd",
        "amount_iqd",
    )

    fieldsets = (
        (None, {"fields": ("public_ref", "type", "status", "rejection_reason")}),
        (_("العميل"), {"fields": ("client", "destination_account")}),
        (_("التوجيه"), {"fields": ("payment_method", "merchant_selected", "merchant_assigned")}),
        (_("المبالغ (لقطة وقت التقديم)"), {
            "fields": ("amount_usd", "amount_iqd", "rate_applied", "commission_applied", "wallet_number_snapshot"),
        }),
        (_("التواريخ"), {
            "fields": ("submitted_at", "assigned_at", "merchant_actioned_at", "closed_at"),
            "classes": ("collapse",),
        }),
    )

    def has_add_permission(self, request):
        # Requests originate from the client flow (build-order step 6), never
        # from the admin — a hand-made one would have no verified client behind
        # it and no snapshotted rate.
        return False


@admin.register(Attachment)
class AttachmentAdmin(FinanceOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("original_name", "request", "uploaded_by_role", "size_bytes", "uploaded_at")
    list_filter = ("uploaded_by_role", "uploaded_at")
    search_fields = ("original_name", "request__public_ref")
    readonly_fields = ("request", "file", "original_name", "content_type", "size_bytes",
                       "uploaded_by_role", "uploaded_by_id", "uploaded_at")
    list_select_related = ("request",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Message)
class MessageAdmin(FinanceOnlyAdminMixin, admin.ModelAdmin):
    """Spec §9: Finance has full read access to every message thread."""

    list_display = ("request", "sender_role", "short_body", "is_internal_note", "created_at")
    list_filter = ("sender_role", "is_internal_note", "created_at")
    search_fields = ("body", "request__public_ref")
    readonly_fields = ("request", "sender_role", "sender_id", "body", "attachment", "created_at")
    list_select_related = ("request",)

    @admin.display(description=_("النص"))
    def short_body(self, obj):
        return (obj.body[:60] + "…") if len(obj.body) > 60 else obj.body

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
