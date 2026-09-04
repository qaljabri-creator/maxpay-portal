"""Filtering the audit log — build-order step 12.

The filters answer the three questions an audit log is actually opened with:
*who* did it, *what* did they do, and *what did they do it to*. Everything is
optional and nothing here writes, so an invalid value narrows nothing rather
than refusing the page.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.choices import AuditAction
from apps.core.models import AuditLog

from . import audit


class AuditFilterForm(forms.Form):
    """Spec §9 — the audit log viewer's controls."""

    q = forms.CharField(
        label=_("بحث"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("المنفّذ أو معرّف الهدف أو عنوان IP")}
        ),
    )
    action = forms.ChoiceField(
        label=_("الإجراء"),
        required=False,
        choices=[("", _("كل الإجراءات"))] + list(AuditAction.choices),
    )
    target_type = forms.ChoiceField(label=_("نوع الهدف"), required=False, choices=[])
    actor = forms.ModelChoiceField(
        label=_("المنفّذ"),
        required=False,
        queryset=None,  # set in __init__, so importing this module needs no DB
        empty_label=_("كل المنفّذين"),
    )
    date_from = forms.DateField(
        label=_("من تاريخ"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("إلى تاريخ"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User

        # Built from what the log actually holds rather than from a hard-coded
        # list: the set of things that get audited grows with the system, and a
        # dropdown that has to be edited alongside it would drift.
        present = (
            AuditLog.objects.order_by()
            .values_list("target_type", flat=True)
            .distinct()
            .order_by("target_type")
        )
        self.fields["target_type"].choices = [("", _("كل الأنواع"))] + [
            (value, audit.target_label(value)) for value in present
        ]
        # Only accounts that have actually written an entry. Listing every
        # internal user would offer filters that can only ever return nothing.
        self.fields["actor"].queryset = User.objects.filter(
            pk__in=AuditLog.objects.exclude(actor=None).values("actor")
        ).order_by("full_name", "email")

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            # Swapped rather than refused, as in the request queue: the intent
            # is unambiguous and re-typing two dates helps nobody.
            cleaned["date_from"], cleaned["date_to"] = end, start
        return cleaned
