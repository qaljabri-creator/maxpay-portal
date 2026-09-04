from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.core.choices import AuditAction
from apps.core.services import record_audit, snapshot

from .models import Merchant, MerchantMethod, PaymentMethod, Wallet


def _can_manage(request) -> bool:
    """Spec §3: merchants, wallets and payment methods are managed by finance_admin."""
    user = request.user
    return bool(user.is_authenticated and (user.is_superuser or getattr(user, "is_finance_admin", False)))


class ManagedByFinanceAdminMixin:
    def has_add_permission(self, request):
        return _can_manage(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return _can_manage(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        # Deactivate rather than delete, so existing requests keep their
        # foreign keys and their history stays readable.
        return False


@admin.register(PaymentMethod)
class PaymentMethodAdmin(ManagedByFinanceAdminMixin, admin.ModelAdmin):
    list_display = ("caption_ar", "code", "supports_deposit", "supports_withdrawal", "is_active", "sort_order")
    list_filter = ("is_active", "supports_deposit", "supports_withdrawal")
    search_fields = ("code", "caption_ar", "caption_en")
    ordering = ("sort_order", "caption_ar")
    list_editable = ("is_active", "sort_order")

    def get_readonly_fields(self, request, obj=None):
        # The code is referenced by data and reports; freeze it after creation.
        return ("code",) if obj else ()


class MerchantMethodInline(admin.TabularInline):
    model = MerchantMethod
    extra = 0
    autocomplete_fields = ("payment_method",)


@admin.register(Merchant)
class MerchantAdmin(ManagedByFinanceAdminMixin, admin.ModelAdmin):
    list_display = ("name", "is_active", "user", "method_count")
    list_filter = ("is_active",)
    search_fields = ("name", "user__email", "user__full_name")
    inlines = [MerchantMethodInline]
    autocomplete_fields = ("user",)

    @admin.display(description=_("عدد الطرق"))
    def method_count(self, obj):
        return obj.methods.count()

    def save_model(self, request, obj, form, change):
        before = snapshot(Merchant.objects.get(pk=obj.pk)) if change and obj.pk else None
        super().save_model(request, obj, form, change)
        record_audit(
            action=AuditAction.MERCHANT_CHANGE,
            target=obj,
            request=request,
            before=before,
            after=snapshot(obj),
        )


@admin.register(MerchantMethod)
class MerchantMethodAdmin(ManagedByFinanceAdminMixin, admin.ModelAdmin):
    list_display = ("merchant", "payment_method", "is_active", "active_wallet")
    list_filter = ("is_active", "payment_method")
    search_fields = ("merchant__name", "payment_method__caption_ar")
    autocomplete_fields = ("merchant", "payment_method")
    list_select_related = ("merchant", "payment_method")


@admin.register(Wallet)
class WalletAdmin(ManagedByFinanceAdminMixin, admin.ModelAdmin):
    """Only one wallet per merchant_method stays active; saving here enforces it."""

    list_display = ("number", "label", "merchant_method", "is_active", "daily_cap", "deactivated_at")
    list_filter = ("is_active", "merchant_method__merchant")
    search_fields = ("number", "label", "merchant_method__merchant__name")
    autocomplete_fields = ("merchant_method",)
    readonly_fields = ("created_by", "deactivated_at", "created_at", "updated_at")
    list_select_related = ("merchant_method", "merchant_method__merchant")

    def save_model(self, request, obj, form, change):
        before = snapshot(Wallet.objects.get(pk=obj.pk)) if change and obj.pk else None
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=obj,
            request=request,
            before=before,
            after=snapshot(obj),
        )
