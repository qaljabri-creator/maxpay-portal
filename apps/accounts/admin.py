from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.core.choices import AuditAction
from apps.core.services import record_audit, snapshot

from .forms import InternalUserChangeForm, InternalUserCreationForm
from .models import Client, User


def _is_admin_role(user) -> bool:
    """Spec §3 and §9: user and role management is finance_admin only."""
    return bool(user and user.is_authenticated and (user.is_superuser or getattr(user, "is_finance_admin", False)))


@admin.register(User)
class InternalUserAdmin(DjangoUserAdmin):
    add_form = InternalUserCreationForm
    form = InternalUserChangeForm
    model = User

    list_display = ("email", "full_name", "role", "is_active", "two_factor_state", "last_login")
    list_filter = ("role", "is_active", "is_superuser", "groups")
    search_fields = ("email", "full_name", "phone")
    ordering = ("full_name", "email")
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("last_login", "date_joined", "last_login_ip", "created_by", "two_factor_state")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("البيانات الشخصية"), {"fields": ("full_name", "phone")}),
        (_("الدور والصلاحيات"), {
            "fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
            "description": _(
                "الدور يمنح مجموعة الصلاحيات الأساسية تلقائيًا. الصلاحيات الإضافية هنا تُضاف فوقها."
            ),
        }),
        (_("المصادقة الثنائية"), {"fields": ("two_factor_state",)}),
        (_("سجلات"), {"fields": ("last_login", "last_login_ip", "date_joined", "created_by")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "full_name", "role", "phone", "password1", "password2", "is_active"),
        }),
    )

    actions = ["reset_two_factor"]

    @admin.display(description=_("المصادقة الثنائية"), boolean=True)
    def two_factor_state(self, obj):
        return obj.has_verified_two_factor

    # -- permissions -------------------------------------------------------

    def has_add_permission(self, request):
        return _is_admin_role(request.user) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return _is_admin_role(request.user) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        # Accounts are deactivated, never deleted, so the audit trail keeps its
        # actor references (spec §5).
        return False

    def has_view_permission(self, request, obj=None):
        return _is_admin_role(request.user) and super().has_view_permission(request, obj)

    # -- auditing ----------------------------------------------------------

    AUDITED_FIELDS = ["email", "full_name", "role", "is_active", "is_staff", "is_superuser"]

    def save_model(self, request, obj, form, change):
        before = None
        if change and obj.pk:
            before = snapshot(User.objects.get(pk=obj.pk), self.AUDITED_FIELDS)
        elif not change:
            obj.created_by = request.user

        super().save_model(request, obj, form, change)

        record_audit(
            action=AuditAction.USER_CHANGE,
            target=obj,
            request=request,
            before=before,
            after=snapshot(obj, self.AUDITED_FIELDS),
        )

    def save_related(self, request, form, formsets, change):
        """Group and permission edits are logged separately (spec §5)."""
        obj = form.instance
        before = None
        if change:
            before = {
                "groups": sorted(g.name for g in obj.groups.all()),
                "user_permissions": sorted(
                    f"{p.content_type.app_label}.{p.codename}" for p in obj.user_permissions.all()
                ),
            }

        super().save_related(request, form, formsets, change)

        after = {
            "groups": sorted(g.name for g in obj.groups.all()),
            "user_permissions": sorted(
                f"{p.content_type.app_label}.{p.codename}" for p in obj.user_permissions.all()
            ),
        }
        if before != after:
            record_audit(
                action=AuditAction.PERMISSION_CHANGE,
                target=obj,
                request=request,
                before=before,
                after=after,
            )

    @admin.action(description=_("إعادة تعيين المصادقة الثنائية للمستخدمين المحددين"))
    def reset_two_factor(self, request, queryset):
        """Wipe a locked-out user's devices so they re-enrol on next login."""
        from django_otp import devices_for_user

        if not request.user.has_perm("accounts.reset_user_two_factor"):
            self.message_user(
                request, _("لا تملك صلاحية إعادة تعيين المصادقة الثنائية."), messages.ERROR
            )
            return

        affected = 0
        for user in queryset:
            devices = list(devices_for_user(user, confirmed=None))
            if not devices:
                continue
            for device in devices:
                device.delete()
            affected += 1
            record_audit(
                action=AuditAction.TWO_FACTOR_CHANGE,
                target=user,
                request=request,
                before={"devices": len(devices)},
                after={"devices": 0, "reason": "admin_reset"},
            )

        self.message_user(
            request,
            ngettext(
                "أُعيد تعيين المصادقة الثنائية لمستخدم واحد.",
                "أُعيد تعيين المصادقة الثنائية لـ %(count)d مستخدمين.",
                affected,
            ) % {"count": affected},
            messages.SUCCESS,
        )


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    """Finance-only. Merchants have no admin access to this model at all (spec §2)."""

    list_display = ("b2core_id", "display_name", "email", "is_active", "last_seen_at")
    list_filter = ("is_active", "preferred_language")
    search_fields = ("b2core_id", "display_name", "email", "account_number")
    readonly_fields = ("b2core_id", "created_at", "updated_at", "last_seen_at")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        # Client records are created on first verified B2CORE token (spec §4),
        # never by hand — a hand-made record would not correspond to a real
        # verified subject.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        user = request.user
        return bool(
            user.is_authenticated
            and getattr(user, "can_see_client_identity", False)
            and super().has_view_permission(request, obj)
        )

    def has_change_permission(self, request, obj=None):
        return bool(
            _is_admin_role(request.user) and super().has_change_permission(request, obj)
        )
