"""What a ``finance_admin`` fills in to administer an account (spec §3).

Thin, like every other form in this panel. Nothing here decides whether an
operation is *allowed* — :mod:`apps.accounts.provisioning` does, because it is
also what the audit entry is written from and the two must not be able to
disagree. These forms collect and shape, so a refusal lands next to the field
that caused it instead of as a flash after a round trip.

The password fields you would expect are not here. An administrator never types
a password for somebody else: it is generated, shown once, and forced to be
replaced on first use. See :func:`apps.accounts.provisioning.generate_password`.
"""

from django import forms
from django.contrib.auth.models import Permission
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.accounts.permissions import MANAGED_APP_LABELS, expected_permissions
from apps.merchants.models import Merchant

#: The three states a permission can be in for one account. "Inherit" is not a
#: stored value — it is the absence of both overrides, and it is the default
#: because the role baseline is the thing that should normally be governing.
INHERIT, GRANT, DENY = "", "grant", "deny"

OVERRIDE_CHOICES = [
    (INHERIT, _("من الدور")),
    (GRANT, _("ممنوحة")),
    (DENY, _("ممنوعة")),
]


class InternalUserCreateForm(forms.ModelForm):
    """A new internal account. Role and identity only; the rest is generated."""

    class Meta:
        model = User
        fields = ["full_name", "email", "phone", "role"]
        widgets = {
            "email": forms.EmailInput(attrs={"autocomplete": "off", "dir": "ltr"}),
            "phone": forms.TextInput(attrs={"inputmode": "tel", "dir": "ltr"}),
        }

    merchant = forms.ModelChoiceField(
        label=_("سجل التاجر"),
        queryset=Merchant.objects.filter(
            user__isnull=True, archived_at__isnull=True
        ).order_by("name"),
        required=False,
        empty_label=_("بلا ربط"),
        help_text=_(
            "يظهر هنا التجار غير المربوطين بحساب دخول. يُشترط أن يكون الدور «تاجر»."
        ),
    )

    def clean_email(self):
        email = (self.cleaned_data["email"] or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("يوجد حساب بهذا البريد بالفعل."))
        return email

    def clean(self):
        cleaned = super().clean()
        role, merchant = cleaned.get("role"), cleaned.get("merchant")
        if merchant is not None and role != Role.MERCHANT:
            raise forms.ValidationError(
                _("لا يمكن ربط سجل تاجر بحساب دوره ليس «تاجر».")
            )
        return cleaned


class InternalUserUpdateForm(forms.ModelForm):
    """Name, phone and role. Email is the identifier and does not move.

    Changing an email would silently invalidate every audit entry that recorded
    the old one as a label, and there is no case for it that creating the right
    account does not serve better.
    """

    class Meta:
        model = User
        fields = ["full_name", "phone", "role"]
        widgets = {"phone": forms.TextInput(attrs={"inputmode": "tel", "dir": "ltr"})}


class MerchantLinkForm(forms.Form):
    """Point a merchant record at the account that signs in for it."""

    merchant = forms.ModelChoiceField(
        label=_("سجل التاجر"),
        queryset=Merchant.objects.none(),
        required=False,
        empty_label=_("بلا ربط"),
    )

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        current = Merchant.objects.filter(user=user)
        # Whatever is free, plus whatever this account already holds, so the
        # form can show its own current value without offering somebody else's.
        # Archived merchants are excluded from what is *offerable*, but not
        # from ``current``: an account already linked to one has to be able to
        # see what it is linked to, and hiding it would make the form look like
        # the link had been dropped.
        self.fields["merchant"].queryset = (
            Merchant.objects.filter(user__isnull=True, archived_at__isnull=True)
            | current
        ).distinct().order_by("name")
        self.fields["merchant"].initial = current.first()


class PermissionOverrideForm(forms.Form):
    """One tri-state control per managed permission.

    Built dynamically rather than declared, because the set of permissions is
    whatever the installed apps define and a hand-written list would drift the
    first time a model gained one.

    The field name is the permission label with its dot replaced, so the posted
    data reads back without a lookup table.
    """

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.baseline = expected_permissions(user.role) if user.role in Role.values else set()

        granted = {
            self._label(p)
            for p in user.user_permissions.select_related("content_type")
        }
        denied = user.denied_permission_labels()

        self.rows = []
        for permission in self._catalogue():
            label = self._label(permission)
            name = self._field_name(label)
            initial = GRANT if label in granted else DENY if label in denied else INHERIT
            self.fields[name] = forms.ChoiceField(
                choices=OVERRIDE_CHOICES,
                required=False,
                initial=initial,
                label=str(permission.name),
            )
            self.rows.append(
                {
                    "field": self[name],
                    "label": label,
                    "name": str(permission.name),
                    "app": permission.content_type.app_label,
                    "in_baseline": label in self.baseline,
                }
            )

    @staticmethod
    def _catalogue():
        return (
            Permission.objects.filter(content_type__app_label__in=MANAGED_APP_LABELS)
            .select_related("content_type")
            .order_by("content_type__app_label", "codename")
        )

    @staticmethod
    def _label(permission: Permission) -> str:
        return f"{permission.content_type.app_label}.{permission.codename}"

    @staticmethod
    def _field_name(label: str) -> str:
        return "perm__" + label.replace(".", "__")

    def overrides(self) -> tuple[set[str], set[str]]:
        """The two sets :func:`provisioning.set_permission_overrides` takes."""
        granted, denied = set(), set()
        for row in self.rows:
            choice = self.cleaned_data.get(row["field"].name) or INHERIT
            if choice == GRANT:
                granted.add(row["label"])
            elif choice == DENY:
                denied.add(row["label"])
        return granted, denied


class UserFilterForm(forms.Form):
    """The list's filters. Everything optional; blank means no narrowing."""

    q = forms.CharField(
        label=_("بحث"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("الاسم أو البريد")}
        ),
    )
    role = forms.ChoiceField(
        label=_("الدور"),
        required=False,
        choices=[("", _("كل الأدوار"))] + list(Role.choices),
    )
    status = forms.ChoiceField(
        label=_("الحالة"),
        required=False,
        choices=[("", _("كل الحالات")), ("active", _("نشط")), ("inactive", _("معطّل"))],
    )
