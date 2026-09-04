"""Shared base models, system settings, and the append-only audit log."""

from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from .choices import AuditAction


class TimeStampedModel(models.Model):
    """Adds creation and modification timestamps."""

    created_at = models.DateTimeField(_("أُنشئ في"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("عُدّل في"), auto_now=True)

    class Meta:
        abstract = True


class AppendOnlyError(Exception):
    """Raised when something tries to mutate or delete an append-only row."""


class AppendOnlyModel(models.Model):
    """A row that can be created and read, never changed or removed.

    Spec §5 requires this of ``AuditLog``, and §5 requires ``ExchangeRate`` to
    never be updated in place. ``default_permissions`` deliberately omits
    ``change`` and ``delete`` so those permissions do not exist to be granted —
    spec §11: "no delete or update path exposed anywhere in the application".

    Enforcement is at the Python level and at the permission level. It is still
    bypassable by ``QuerySet.update``/``delete`` or raw SQL, which is a
    deliberate limit for now: a database-level trigger belongs with the security
    hardening in build-order step 14.
    """

    class Meta:
        abstract = True
        default_permissions = ("add", "view")

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise AppendOnlyError(
                f"{type(self).__name__} is append-only; row {self.pk} cannot be "
                f"modified. Create a new row instead."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{type(self).__name__} is append-only; rows cannot be deleted.")


class AuditLog(AppendOnlyModel):
    """Append-only record of every consequential change (spec §5).

    ``target_type`` / ``target_id`` are stored as plain text and integer rather
    than a ``GenericForeignKey`` so an entry survives the deletion of whatever
    it points at.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("المنفّذ"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
        help_text=_("فارغ إذا كان المنفّذ النظام أو عميلاً."),
    )
    actor_label = models.CharField(
        _("وصف المنفّذ"),
        max_length=150,
        blank=True,
        help_text=_("لقطة نصية للمنفّذ وقت الحدث، تبقى بعد حذف حسابه."),
    )
    action = models.CharField(_("الإجراء"), max_length=40, choices=AuditAction.choices, db_index=True)
    target_type = models.CharField(_("نوع الهدف"), max_length=100, db_index=True)
    target_id = models.CharField(_("معرّف الهدف"), max_length=64, db_index=True)
    before = models.JSONField(_("قبل"), null=True, blank=True)
    after = models.JSONField(_("بعد"), null=True, blank=True)
    ip = models.GenericIPAddressField(_("عنوان IP"), null=True, blank=True)
    created_at = models.DateTimeField(_("وقت الحدث"), auto_now_add=True, db_index=True)

    class Meta(AppendOnlyModel.Meta):
        abstract = False
        verbose_name = _("سجل التدقيق")
        verbose_name_plural = _("سجل التدقيق")
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
            models.Index(fields=["actor", "-created_at"], name="audit_actor_time_idx"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} · {self.target_type}#{self.target_id}"


class SystemSettings(TimeStampedModel):
    """Business hours and the closed-portal notice (spec §5, §7).

    A singleton: exactly one row, always ``pk=1``. Fetch it with
    ``SystemSettings.load()``.
    """

    SINGLETON_PK = 1

    open_time = models.TimeField(_("وقت الفتح"), default=time(9, 0))
    close_time = models.TimeField(_("وقت الإغلاق"), default=time(21, 0))
    timezone = models.CharField(_("المنطقة الزمنية"), max_length=64, default="Asia/Baghdad")
    is_open_override = models.BooleanField(
        _("تجاوز حالة الفتح"),
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "فارغ: اتبع مواعيد العمل. نعم: افتح دائمًا. لا: أغلق دائمًا."
        ),
    )
    closed_message_ar = models.TextField(
        _("رسالة الإغلاق"),
        default="النظام مغلق حاليًا. يرجى المحاولة خلال ساعات العمل.",
    )

    class Meta:
        verbose_name = _("إعدادات النظام")
        verbose_name_plural = _("إعدادات النظام")

    def __str__(self):
        return str(_("إعدادات النظام"))

    def clean(self):
        super().clean()
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValidationError({"timezone": _("منطقة زمنية غير صالحة.")}) from exc

    def save(self, *args, **kwargs):
        # Force the singleton identity regardless of how the row was built.
        self.pk = self.SINGLETON_PK

        if self._state.adding:
            # A freshly constructed instance still carries the singleton pk, so
            # Django would take its UPDATE branch and write a null over
            # created_at (auto_now_add only fills on insert). Decide explicitly.
            existing_created_at = (
                type(self).objects.filter(pk=self.pk).values_list("created_at", flat=True).first()
            )
            if existing_created_at is None:
                kwargs.setdefault("force_insert", True)
            else:
                # Overwrite the existing singleton, keeping its creation time.
                self.created_at = existing_created_at
                self._state.adding = False

        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("SystemSettings is a singleton and cannot be deleted.")

    @classmethod
    def load(cls) -> "SystemSettings":
        obj, _created = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj
