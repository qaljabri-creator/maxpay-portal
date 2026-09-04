"""What a merchant types before a lifecycle move (spec §8).

Thin, like the Finance panel's equivalents. Nothing here decides whether a move
is *allowed* — :mod:`apps.transactions.services` re-reads and locks the row
before it decides anything, and it is the only writer of ``Request.status``.
These forms collect and shape the input so a validation message about a missing
rejection reason lands next to the textarea rather than as a flash after a round
trip.

A merchant writes exactly one kind of internal note, and only in one place:
the reason they are handing a request back (Finance review, 24 Aug 2026). It is
not a private notebook — the point of it is that Finance reads it — and it is
the only note a merchant can write or read. Finance's own notes remain
invisible here, as does another merchant's handback note on a request that was
re-routed. See :data:`apps.transactions.messaging.NOTE_ROLES`.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.validators import (
    validate_real_content_type,
    validate_upload_content,
    validate_upload_size,
)


class MerchantActionForm(forms.Form):
    """A move that needs nothing typed — confirming a deposit, mostly."""

    def __init__(self, *args, request_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_obj = request_obj

    def transition_kwargs(self) -> dict:
        """What :func:`apps.transactions.services.apply_transition` is called with."""
        return {}

    @property
    def proof(self):
        """The uploaded file, for forms that take one. ``None`` otherwise."""
        return self.cleaned_data.get("proof")


class MerchantRejectForm(MerchantActionForm):
    """Spec §6: the reason is posted into the thread, so it is not optional."""

    reason = forms.CharField(
        label=_("سبب الرفض"),
        max_length=1000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("يقرأ العميل والمالية هذا النص كما هو.")}
        ),
        error_messages={"required": _("اكتب سبب الرفض.")},
        help_text=_("يُنشر في محادثة الطلب وتقرؤه المالية والعميل."),
    )

    def clean_reason(self):
        reason = (self.cleaned_data["reason"] or "").strip()
        if len(reason) < 4:
            raise forms.ValidationError(_("اكتب سببًا مفهومًا."))
        return reason

    def transition_kwargs(self) -> dict:
        return {**super().transition_kwargs(), "reason": self.cleaned_data["reason"]}


class MerchantPayForm(MerchantActionForm):
    """Spec §6, §8: a withdrawal is marked paid *with* proof of transfer.

    Required rather than optional. "Paid" is the point at which the client's
    money has left, and a claim that it did with nothing behind it is exactly
    what Finance would have to chase later.
    """

    proof = forms.FileField(
        label=_("إثبات التحويل"),
        validators=[validate_upload_size, validate_upload_content],
        error_messages={"required": _("أرفق صورة أو ملف PDF يثبت التحويل.")},
        help_text=_("صورة أو PDF لإيصال التحويل."),
    )

    def clean_proof(self):
        upload = self.cleaned_data["proof"]
        # The declared type is the uploader's word for it; this reads the bytes
        # and is what actually decides (spec §11).
        self.proof_content_type = validate_real_content_type(upload)
        return upload


class MerchantHandBackForm(MerchantActionForm):
    """Sending a request back to Finance (Finance review, 24 Aug 2026).

    Not a rejection and not a cancellation: the merchant is saying "not me",
    and somebody else may well execute it. Finance decides which.

    The reason is **mandatory and internal**. Mandatory because a request
    arriving back on the desk with no explanation is a request nobody can act
    on; internal because "no cash today" or "I do not trust this receipt" is
    desk business and not the client's.
    """

    reason = forms.CharField(
        label=_("سبب الإعادة"),
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _("تقرؤه المالية فقط. لا يراه العميل."),
            }
        ),
        error_messages={"required": _("اكتب سبب إعادة الطلب.")},
        help_text=_("يُسجَّل كملاحظة داخلية تقرؤها المالية وتقرؤها أنت، ولا يراها العميل."),
    )

    def clean_reason(self):
        reason = (self.cleaned_data["reason"] or "").strip()
        if len(reason) < 4:
            raise forms.ValidationError(_("اكتب سببًا مفهومًا."))
        return reason

    def transition_kwargs(self) -> dict:
        return {**super().transition_kwargs(), "reason": self.cleaned_data["reason"]}


#: action name → the form that collects its inputs. Anything not listed takes
#: the plain confirm form.
ACTION_FORMS = {
    "reject": MerchantRejectForm,
    "pay": MerchantPayForm,
    "hand_back": MerchantHandBackForm,
}


def form_for(action: str):
    return ACTION_FORMS.get(action, MerchantActionForm)


class MerchantMessageForm(forms.Form):
    """A reply into the request thread (spec §9).

    Not gated on the request's status, and not tied to any lifecycle move: the
    merchant writes to the client whenever they want. Validation of what is
    *in* the message — length, and whether the file is really the type it
    claims — belongs to :mod:`apps.transactions.messaging`, which is the only
    writer of a ``Message``; this form only collects.
    """

    body = forms.CharField(
        label=_("رسالة"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "dir": "auto",
                "placeholder": _("اكتب ردك للعميل…"),
            }
        ),
    )
    attachment = forms.FileField(
        label=_("مرفق"),
        required=False,
        validators=[validate_upload_size, validate_upload_content],
        help_text=_("صورة أو PDF، اختياري."),
    )

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("body") or "").strip() and not cleaned.get("attachment"):
            raise forms.ValidationError(_("اكتب رسالة أو أرفق ملفًا."))
        return cleaned
