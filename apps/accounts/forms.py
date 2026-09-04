"""Admin forms for internal accounts.

There is no self-registration (spec §3), so these forms are only ever reached
from the admin by a ``finance_admin``.
"""

from django import forms
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.translation import gettext_lazy as _

from .models import Role, User


class InternalUserCreationForm(UserCreationForm):
    """Create an internal account and pick its role."""

    class Meta:
        model = User
        fields = ("email", "full_name", "role", "phone", "is_active")
        labels = {"email": _("البريد الإلكتروني")}

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("يوجد حساب بهذا البريد الإلكتروني."))
        return email


class InternalUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = (
            "email",
            "full_name",
            "role",
            "phone",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
        )

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        clash = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(_("يوجد حساب آخر بهذا البريد الإلكتروني."))
        return email

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        if cleaned.get("is_superuser") and role != Role.FINANCE_ADMIN:
            raise forms.ValidationError(
                _("صلاحية المستخدم الأعلى متاحة لدور «مدير مالي» فقط.")
            )
        return cleaned
