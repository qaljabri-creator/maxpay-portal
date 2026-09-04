"""The report's filters (spec §9).

Deliberately not the queue's :class:`~apps.finance.queue_forms.RequestFilterForm`
even though four of the fields are identical. Two differences make sharing it
the wrong economy: a report has no free-text search — searching for one
reference is what the queue is for — and its status control has no default,
because a report that quietly excluded closed requests would answer a question
nobody asked. Inheriting and then subtracting both would leave a form whose
behaviour is only legible by reading two files.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Client
from apps.merchants.models import Merchant, PaymentMethod
from apps.transactions.models import RequestStatus, RequestType

STATUS_CHOICES = [
    ("", _("كل الحالات")),
    ("open", _("الجارية فقط")),
    ("awaiting_finance", _("بانتظار المالية")),
    ("awaiting_merchant", _("لدى التجار")),
] + list(RequestStatus.choices)


class ReportFilterFormBase(forms.Form):
    """Everything both panels filter on."""

    date_from = forms.DateField(
        label=_("من تاريخ"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("إلى تاريخ"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    status = forms.ChoiceField(label=_("الحالة"), required=False, choices=STATUS_CHOICES)
    type = forms.ChoiceField(
        label=_("النوع"),
        required=False,
        choices=[("", _("النوعان"))] + list(RequestType.choices),
    )
    method = forms.ModelChoiceField(
        label=_("طريقة الدفع"),
        required=False,
        queryset=PaymentMethod.objects.all().order_by("sort_order", "caption_ar"),
        empty_label=_("كل الطرق"),
    )

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            # Swapped rather than refused: the intent is unambiguous, and making
            # somebody retype two dates to be told so helps nobody.
            cleaned["date_from"], cleaned["date_to"] = end, start
        return cleaned


class FinanceReportFilterForm(ReportFilterFormBase):
    """Spec §9 adds the merchant dimension, which only Finance has.

    Finance review 4.3 adds a second one: a report narrowed to a single client.
    It is **hidden** rather than a dropdown, and that is a decision rather than
    a shortcut. A select listing every client would be a roster of everyone who
    has ever used the portal, rendered on a screen whose own permission is
    about reports; the way into this dimension is from that client's history
    page, where the identity gate has already been answered.

    The field is removed outright for a user without
    ``accounts.view_client_identity``, so an unpermitted ``?client=`` is not
    quietly honoured and does not silently widen the file either — the report
    views refuse the request instead.
    """

    merchant = forms.ModelChoiceField(
        label=_("التاجر"),
        required=False,
        queryset=Merchant.objects.filter(archived_at__isnull=True).order_by("name"),
        empty_label=_("كل التجار"),
        help_text=_("يشمل التاجر الذي اختاره العميل والتاجر الذي أُسند إليه الطلب."),
    )
    client = forms.ModelChoiceField(
        label=_("العميل"),
        required=False,
        queryset=Client.objects.all(),
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, can_see_identity: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_see_identity:
            del self.fields["client"]
