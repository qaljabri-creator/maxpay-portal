"""Enumerations shared across apps.

They live here rather than in the app that "owns" them because messages,
attachments and audit entries all need to name an actor that may be an internal
user, a merchant, a B2CORE client, or the system itself.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class ActorRole(models.TextChoices):
    """Who performed an action (spec §3, extended with the system actor)."""

    CLIENT = "client", _("العميل")
    FINANCE_ADMIN = "finance_admin", _("مدير مالي")
    FINANCE_STAFF = "finance_staff", _("موظف مالي")
    MERCHANT = "merchant", _("تاجر")
    SYSTEM = "system", _("النظام")


class AuditAction(models.TextChoices):
    """Actions that spec §5 requires to be written to the audit log."""

    STATUS_CHANGE = "status_change", _("تغيير الحالة")
    #: Correcting a request to the amount that actually arrived (Finance
    #: review, 24 Aug 2026). Separate from a status change because it is a
    #: different question of the log — "who moved this along" and "who changed
    #: what it is worth" are not answered by the same filter.
    AMOUNT_CHANGE = "amount_change", _("تعديل المبلغ")
    WALLET_CHANGE = "wallet_change", _("تغيير محفظة")
    RATE_CHANGE = "rate_change", _("تغيير سعر الصرف")
    PERMISSION_CHANGE = "permission_change", _("تغيير الصلاحيات")
    USER_CHANGE = "user_change", _("تغيير مستخدم")
    MERCHANT_CHANGE = "merchant_change", _("تغيير تاجر")
    SETTINGS_CHANGE = "settings_change", _("تغيير إعدادات النظام")
    LOGIN = "login", _("تسجيل دخول")
    LOGIN_FAILED = "login_failed", _("محاولة دخول فاشلة")
    TWO_FACTOR_CHANGE = "two_factor_change", _("تغيير المصادقة الثنائية")
