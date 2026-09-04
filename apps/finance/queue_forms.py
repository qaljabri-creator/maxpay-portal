"""Filtering the request queue, and the inputs each lifecycle action needs.

The action forms are thin on purpose. Nothing here decides whether a move is
allowed — that is :mod:`apps.transactions.services`, which re-reads and locks
the row before it decides anything. These forms only collect and shape what the
operator typed, so a validation message about an empty rejection reason arrives
next to the textarea rather than as a flash after a round trip.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.validators import validate_upload_content, validate_upload_size
from apps.merchants.models import Merchant, PaymentMethod
from apps.transactions.models import RequestStatus, RequestType
from apps.transactions.services import (
    AWAITING_FINANCE,
    AWAITING_MERCHANT,
    OPEN_STATUSES,
    eligible_merchants,
)

#: Filters that stand for a set of statuses rather than one. They are what a
#: desk actually asks the queue — "what needs me?" — so they sit in the same
#: control as the concrete statuses instead of in a second one beside it.
STATUS_GROUPS = {
    "awaiting_finance": AWAITING_FINANCE,
    "awaiting_merchant": AWAITING_MERCHANT,
    "open": OPEN_STATUSES,
}

STATUS_CHOICES = [
    ("open", _("كل الطلبات الجارية")),
    ("awaiting_finance", _("بانتظار المالية")),
    ("awaiting_merchant", _("لدى التجار")),
    ("", _("الكل، بما فيها المغلقة")),
] + list(RequestStatus.choices)

#: Applied when the operator arrives with no query string at all. A queue that
#: opens on closed history is a queue nobody works from.
DEFAULT_STATUS = "open"


class RequestFilterForm(forms.Form):
    """Spec §9: filters by status, type, method, merchant and date."""

    q = forms.CharField(
        label=_("بحث"),
        required=False,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": _("المرجع أو المبلغ أو اسم العميل أو بريده")}
        ),
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
    merchant = forms.ModelChoiceField(
        label=_("التاجر"),
        required=False,
        queryset=Merchant.objects.filter(archived_at__isnull=True).order_by("name"),
        empty_label=_("كل التجار"),
        help_text=_("يشمل التاجر الذي اختاره العميل والتاجر الذي أُسند إليه الطلب."),
    )
    date_from = forms.DateField(
        label=_("من تاريخ"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("إلى تاريخ"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    #: Spec §9's filters, plus the two the Finance review added: who handled it,
    #: and which way to read the day.
    operator = forms.ModelChoiceField(
        label=_("المُعالِج"),
        required=False,
        queryset=None,  # set in __init__, so the import stays lazy
        empty_label=_("كل المُعالِجين"),
        help_text=_("آخر من حرّك الطلب. سجل التدقيق يحمل كل خطوة."),
    )
    sort = forms.ChoiceField(
        label=_("الترتيب"),
        required=False,
        choices=[
            ("", _("الأحدث تقديمًا")),
            ("resolved_desc", _("الأحدث حسمًا")),
            ("resolved_asc", _("الأقدم حسمًا")),
        ],
        help_text=_("الحسم هو وقت الإغلاق أو الرفض أو الإلغاء."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import IDENTITY_AWARE_ROLES, Role, User

        self.fields["operator"].queryset = User.objects.filter(
            role__in=set(IDENTITY_AWARE_ROLES) | {Role.MERCHANT}
        ).order_by("full_name")

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            # Swapped rather than refused: the intent is unambiguous and making
            # someone retype two dates to be told so helps nobody.
            cleaned["date_from"], cleaned["date_to"] = end, start
        return cleaned


class ActionForm(forms.Form):
    """Base for every lifecycle action a Finance screen posts.

    ``note`` is Finance's own record of why. It is posted to the thread as an
    internal note, which every client-facing serialiser filters out.
    """

    def __init__(self, *args, request_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_obj = request_obj

    note = forms.CharField(
        label=_("ملاحظة داخلية (اختيارية)"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": _("تظهر للمالية فقط، ولا يراها العميل ولا التاجر.")}
        ),
    )

    #: What :func:`apps.transactions.services.apply_transition` is called with.
    def transition_kwargs(self) -> dict:
        return {"note": self.cleaned_data.get("note", "")}


class RouteForm(ActionForm):
    """Choose the merchant a request is routed to (spec §6, §9)."""

    merchant = forms.ModelChoiceField(
        label=_("التاجر"),
        queryset=Merchant.objects.none(),
        empty_label=_("اختر تاجرًا"),
        error_messages={"required": _("اختر التاجر المراد الإسناد إليه.")},
    )

    field_order = ["merchant", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request_obj = self.request_obj
        if request_obj is not None:
            # Only merchants that could actually execute it. A route the
            # merchant cannot carry out is a request that sits in their queue
            # doing nothing until someone notices.
            self.fields["merchant"].queryset = eligible_merchants(request_obj)
            if request_obj.merchant_assigned_id:
                self.fields["merchant"].initial = request_obj.merchant_assigned_id
            elif request_obj.merchant_selected_id:
                self.fields["merchant"].initial = request_obj.merchant_selected_id

    def transition_kwargs(self) -> dict:
        return {**super().transition_kwargs(), "merchant": self.cleaned_data["merchant"]}


class RejectForm(ActionForm):
    """Spec §6: the reason is posted into the thread, so it is not optional."""

    reason = forms.CharField(
        label=_("سبب الرفض"),
        max_length=1000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("يقرأ العميل هذا النص كما هو.")}
        ),
        error_messages={"required": _("اكتب سبب الرفض.")},
        help_text=_("يُنشر في محادثة الطلب ويظهر للعميل وللتاجر."),
    )

    field_order = ["reason", "note"]

    def clean_reason(self):
        reason = (self.cleaned_data["reason"] or "").strip()
        if len(reason) < 4:
            raise forms.ValidationError(_("اكتب سببًا مفهومًا للعميل."))
        return reason

    def transition_kwargs(self) -> dict:
        return {**super().transition_kwargs(), "reason": self.cleaned_data["reason"]}


class AmountCorrectionForm(forms.Form):
    """Correcting a request to the amount that actually arrived.

    Deliberately not a ``ModelForm``: the figure typed here is one of four that
    move together, and the other three are computed by
    :func:`apps.transactions.services.correct_amount` from the rate this
    request was quoted on. A ``ModelForm`` would offer to save one of them on
    its own.
    """

    amount_usd = forms.CharField(
        label=_("المبلغ الذي وصل فعلًا بالدولار"),
        widget=forms.TextInput(
            attrs={"class": "num", "inputmode": "decimal", "dir": "ltr",
                   "autocomplete": "off", "placeholder": "0.00"}
        ),
        error_messages={"required": _("أدخل المبلغ الصحيح.")},
    )
    reason = forms.CharField(
        label=_("سبب التصحيح"),
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": _("اختياري. يُسجَّل في التدقيق وفي المحادثة كملاحظة داخلية.")}
        ),
    )


class CancelForm(RejectForm):
    """Ending a request because it stopped being wanted, not because it failed.

    Same shape as a rejection and deliberately so — both need a reason and both
    post it where the client reads it. What differs is the word the client is
    given for what happened, and a desk reconciling a month needs "turned away"
    and "went away" to be two numbers rather than one.
    """

    reason = forms.CharField(
        label=_("سبب الإلغاء"),
        max_length=1000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("يقرأ العميل هذا النص كما هو.")}
        ),
        error_messages={"required": _("اكتب سبب الإلغاء.")},
        help_text=_("يُنشر في محادثة الطلب ويقرؤه العميل والتاجر."),
    )


class ParkForm(RejectForm):
    """Parking a request that is waiting on something.

    The reason is mandatory and **internal**: parking is a statement about the
    desk's own queue — "waiting on the client's bank", "merchant out of cash
    until Sunday" — and promising the client an explanation the desk did not
    write for them is worse than the status alone.
    """

    reason = forms.CharField(
        label=_("سبب التعليق"),
        max_length=1000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("تقرؤه المالية فقط. لا يراه العميل.")}
        ),
        error_messages={"required": _("اكتب سبب التعليق.")},
        help_text=_("يُسجَّل كملاحظة داخلية، ولا يراه العميل ولا التاجر."),
    )


#: action name → the form that collects its inputs. Anything not listed takes
#: the plain confirm-with-optional-note form.
ACTION_FORMS = {
    "route": RouteForm,
    "reject": RejectForm,
    # Both need a mandatory reason, and both take it in the same field the
    # rejection does. Where the reason *goes* is the transition's decision, not
    # the form's: a cancellation is owed to the client and a park is not.
    # See `Transition.reason_is_note`.
    "cancel": CancelForm,
    "park": ParkForm,
}


def form_for(action: str):
    return ACTION_FORMS.get(action, ActionForm)


class FinanceMessageForm(forms.Form):
    """Finance writing into a request thread (spec §9).

    Two audiences behind one box, and the checkbox is the whole difference: an
    ordinary message is read by the client *and* by whichever merchant holds
    the request, while an internal note stays on the desk. Getting that wrong
    is how a client's name reaches a merchant, so the template puts a standing
    reminder beside this field rather than relying on anyone remembering.
    """

    body = forms.CharField(
        label=_("رسالة"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "dir": "auto",
                "placeholder": _("اكتب ردك…"),
            }
        ),
    )
    attachment = forms.FileField(
        label=_("مرفق"),
        required=False,
        validators=[validate_upload_size, validate_upload_content],
        help_text=_("صورة أو PDF، اختياري."),
    )
    is_internal_note = forms.BooleanField(
        label=_("ملاحظة داخلية"),
        required=False,
        help_text=_("تظهر للمالية فقط، ولا يراها العميل ولا التاجر."),
    )

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("body") or "").strip() and not cleaned.get("attachment"):
            raise forms.ValidationError(_("اكتب رسالة أو أرفق ملفًا."))
        return cleaned
