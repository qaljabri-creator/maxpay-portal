"""Internal users, their roles, and the B2CORE-authenticated client record.

Two distinct notions of "user" live here, and keeping them apart is deliberate:

* :class:`User` is an *internal* account — finance_admin, finance_staff or
  merchant. It logs in with a Django session and a mandatory second factor
  (spec §4, §11).
* :class:`Client` is the fourth role in spec §3. Clients never get a Django
  account: their identity is derived solely from a B2CORE JWT verified against
  the JWKS endpoint (spec §4), so there is no password to steal and no way to
  authenticate as a client without a valid upstream token.
"""

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

from .managers import UserManager


class Role(models.TextChoices):
    """Internal roles (spec §3).

    The ``client`` role from the spec is not listed here on purpose — it is not
    an internal account. See :class:`Client`.
    """

    FINANCE_ADMIN = "finance_admin", _("مدير مالي")
    FINANCE_STAFF = "finance_staff", _("موظف مالي")
    MERCHANT = "merchant", _("تاجر")


#: Roles that may never see client identifying data (spec §2).
IDENTITY_BLIND_ROLES = frozenset({Role.MERCHANT})

#: Roles with full visibility of client identity (spec §9).
IDENTITY_AWARE_ROLES = frozenset({Role.FINANCE_ADMIN, Role.FINANCE_STAFF})


class User(AbstractBaseUser, PermissionsMixin):
    """An internal account. Created by a ``finance_admin``, never self-registered."""

    email = models.EmailField(_("البريد الإلكتروني"), unique=True, max_length=254)
    full_name = models.CharField(_("الاسم الكامل"), max_length=150)
    role = models.CharField(
        _("الدور"),
        max_length=20,
        choices=Role.choices,
        db_index=True,
        help_text=_("يحدد مجموعة الصلاحيات الممنوحة تلقائيًا."),
    )
    phone = models.CharField(_("رقم الهاتف"), max_length=32, blank=True)

    is_active = models.BooleanField(
        _("نشط"),
        default=True,
        help_text=_("ألغِ التفعيل بدل الحذف حتى تبقى سجلات التدقيق مرتبطة."),
    )
    is_staff = models.BooleanField(
        _("يدخل لوحة الإدارة"),
        default=True,
        help_text=_("كل الحسابات الداخلية تستخدم لوحة الإدارة."),
    )

    #: Spec §3 gives a ``finance_admin`` the power to grant *and revoke*
    #: permissions. Granting is what ``user_permissions`` already does; revoking
    #: is what it cannot, because Django ORs the user's own permissions with
    #: every group's and a role group re-grants on the next login. So a refusal
    #: is stored explicitly and subtracted by the authentication backend. See
    #: :class:`apps.accounts.throttling.ThrottledModelBackend`.
    denied_permissions = models.ManyToManyField(
        "auth.Permission",
        verbose_name=_("صلاحيات ممنوعة"),
        blank=True,
        related_name="denied_for",
        help_text=_("تُطرح من صلاحيات الدور، حتى لو منحتها المجموعة."),
    )

    must_change_password = models.BooleanField(
        _("يجب تغيير كلمة المرور"),
        default=False,
        help_text=_(
            "يُفرض على المستخدم اختيار كلمة مرور جديدة قبل استخدام النظام. "
            "يُضبط تلقائيًا عند إنشاء الحساب أو إعادة تعيين كلمة المرور."
        ),
    )

    two_factor_reset_at = models.DateTimeField(
        _("آخر إعادة تعيين للمصادقة الثنائية"), null=True, blank=True
    )

    created_by = models.ForeignKey(
        "self",
        verbose_name=_("أنشأه"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_users",
    )
    date_joined = models.DateTimeField(_("تاريخ الإنشاء"), default=timezone.now)
    last_login_ip = models.GenericIPAddressField(_("آخر IP للدخول"), null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["full_name", "role"]

    class Meta:
        verbose_name = _("مستخدم داخلي")
        verbose_name_plural = _("المستخدمون الداخليون")
        ordering = ["full_name", "email"]
        permissions = [
            # Granted only to finance_admin (spec §3, §9).
            ("manage_internal_users", _("إدارة المستخدمين الداخليين وأدوارهم")),
            ("manage_permissions", _("منح وسحب الصلاحيات")),
            ("reset_user_two_factor", _("إعادة تعيين المصادقة الثنائية لمستخدم")),
            # Guards every serializer and view that can expose client identity
            # (spec §2). A merchant must never hold this.
            ("view_client_identity", _("الاطلاع على هوية العميل")),
            ("view_audit_log", _("عرض سجل التدقيق")),
        ]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    def clean(self):
        super().clean()
        if self.email:
            self.email = self.email.strip().lower()
        if self.role not in Role.values:
            raise ValidationError({"role": _("دور غير معروف.")})

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        return super().save(*args, **kwargs)

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.email

    # -- role predicates ---------------------------------------------------

    @property
    def is_finance_admin(self) -> bool:
        return self.role == Role.FINANCE_ADMIN

    @property
    def is_finance_staff(self) -> bool:
        return self.role == Role.FINANCE_STAFF

    @property
    def is_finance(self) -> bool:
        return self.role in IDENTITY_AWARE_ROLES

    @property
    def is_merchant(self) -> bool:
        return self.role == Role.MERCHANT

    @property
    def can_see_client_identity(self) -> bool:
        """Spec §2 — merchants never see who the client is."""
        return self.role in IDENTITY_AWARE_ROLES

    @property
    def requires_two_factor(self) -> bool:
        """Spec §11 — every internal account requires 2FA."""
        return self.role in set(getattr(settings, "TWO_FACTOR_REQUIRED_ROLES", Role.values))

    @property
    def has_verified_two_factor(self) -> bool:
        """True once the user has at least one confirmed OTP device."""
        from django_otp import devices_for_user

        return any(True for _device in devices_for_user(self, confirmed=True))

    # -- permissions -------------------------------------------------------

    def denied_permission_labels(self) -> set[str]:
        """``app_label.codename`` for every permission explicitly refused."""
        return {
            f"{perm.content_type.app_label}.{perm.codename}"
            for perm in self.denied_permissions.select_related("content_type")
        }


class Client(TimeStampedModel):
    """A B2CORE end user (spec §3, §4).

    ``b2core_id`` is the verified ``sub`` claim of the B2CORE JWT and is the only
    trusted identifier. Spec §4: never trust a client-supplied account number —
    ``account_number`` here is informational, populated from verified claims for
    Finance's convenience, and is never accepted from request input.
    """

    b2core_id = models.CharField(
        _("معرّف B2CORE"),
        max_length=128,
        unique=True,
        db_index=True,
        help_text=_("مطالبة sub الموثّقة من رمز B2CORE."),
    )
    display_name = models.CharField(_("الاسم"), max_length=200, blank=True)
    email = models.EmailField(_("البريد الإلكتروني"), max_length=254, blank=True)
    account_number = models.CharField(
        _("رقم الحساب"),
        max_length=64,
        blank=True,
        help_text=_("للعرض لدى المالية فقط، ولا يُقبل أبدًا من إدخال العميل."),
    )
    preferred_language = models.CharField(
        _("اللغة المفضلة"), max_length=8, default="ar"
    )
    is_active = models.BooleanField(_("نشط"), default=True)
    last_seen_at = models.DateTimeField(_("آخر ظهور"), null=True, blank=True)

    class Meta:
        verbose_name = _("عميل")
        verbose_name_plural = _("العملاء")
        ordering = ["-created_at"]
        permissions = [
            ("view_client_pii", _("الاطلاع على البيانات الشخصية للعميل")),
        ]

    def __str__(self):
        # Deliberately identifying: this model is only ever rendered in
        # finance-facing surfaces. Merchant-scoped serializers must not touch it.
        return self.display_name or self.email or f"B2CORE:{self.b2core_id}"

    @property
    def masked_label(self) -> str:
        """What a merchant is allowed to see instead of a name (spec §5)."""
        return str(_("العميل"))

    def touch(self, *, save: bool = True):
        self.last_seen_at = timezone.now()
        if save:
            self.save(update_fields=["last_seen_at", "updated_at"])
