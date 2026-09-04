"""Forms for merchant, method, wallet and exchange-rate management."""

from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.core.models import SystemSettings
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.rates.models import ExchangeRate, RateType


class MerchantForm(forms.ModelForm):
    class Meta:
        model = Merchant
        fields = ["name", "b2core_id", "user", "is_active", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            # An opaque Latin identifier on an RTL page: without `dir` the bidi
            # algorithm reorders anything in it that looks like a number, and
            # the operator proof-reads a string that is not the one being saved.
            "b2core_id": forms.TextInput(
                attrs={"class": "num", "dir": "ltr", "autocomplete": "off"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only merchant-role accounts, and only ones not already taken by
        # another merchant, so the OneToOne cannot fail at save time.
        taken = Merchant.objects.exclude(pk=self.instance.pk or 0).values_list("user_id", flat=True)
        self.fields["user"].queryset = User.objects.filter(
            role=Role.MERCHANT, is_active=True
        ).exclude(pk__in=[pk for pk in taken if pk])
        self.fields["user"].required = False
        self.fields["user"].empty_label = _("بلا حساب دخول")
        self.fields["user"].help_text = _(
            "التاجر بلا حساب دخول يمكن توجيه الطلبات إليه، لكنه لا يستطيع الدخول إلى النظام."
        )
        # `blank=True` on the model already implies this; stated so the screen
        # cannot start demanding an identifier because a later edit to the
        # model's `blank` flag went unnoticed. Empty is a legitimate answer.
        self.fields["b2core_id"].required = False

    def clean_b2core_id(self):
        """Refuse a duplicate by name, not by number.

        The model's own `validate_unique` already catches this and would say so
        in Django's generic wording. What that wording cannot say is *where the
        identifier already is*, which is the only fact that lets the operator
        act: the fix is nearly always that the value belongs to the other
        merchant and was pasted onto this one.
        """
        value = Merchant.normalise_b2core_id(self.cleaned_data.get("b2core_id"))
        if value is None:
            return None

        holder = (
            Merchant.objects.filter(b2core_id=value)
            .exclude(pk=self.instance.pk or 0)
            .first()
        )
        if holder is not None:
            raise forms.ValidationError(
                _("هذا المعرّف مسجَّل بالفعل للتاجر «%(name)s».") % {"name": holder.name}
            )
        return value


class MerchantMethodForm(forms.ModelForm):
    """Assigns a payment method to a merchant (spec §5: unique together)."""

    class Meta:
        model = MerchantMethod
        fields = ["payment_method", "is_active"]

    def __init__(self, *args, merchant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.merchant = merchant or getattr(self.instance, "merchant", None)
        already = MerchantMethod.objects.filter(merchant=self.merchant).values_list(
            "payment_method_id", flat=True
        )
        self.fields["payment_method"].queryset = PaymentMethod.objects.filter(
            is_active=True
        ).exclude(pk__in=already)
        self.fields["payment_method"].empty_label = None

    def clean_payment_method(self):
        method = self.cleaned_data["payment_method"]
        if MerchantMethod.objects.filter(merchant=self.merchant, payment_method=method).exists():
            raise forms.ValidationError(_("هذه الطريقة مُسندة إلى هذا التاجر بالفعل."))
        return method

    def save(self, commit=True):
        self.instance.merchant = self.merchant
        return super().save(commit=commit)


class WalletForm(forms.ModelForm):
    """Add or edit a wallet.

    ``is_active`` here is the whole point of the screen: ticking it makes this
    the one wallet clients are shown, and the model stands down whichever one
    held that slot before (spec §5).
    """

    class Meta:
        model = Wallet
        fields = ["number", "qr_image", "label", "daily_cap", "is_active"]
        widgets = {
            "number": forms.TextInput(attrs={"class": "num", "inputmode": "numeric",
                                             "autocomplete": "off", "dir": "ltr"}),
            # Dinars, so the step is a dinar. A cap typed to the fils is a cap
            # measured against amounts that no longer carry any.
            "daily_cap": forms.NumberInput(attrs={"class": "num", "step": "1", "min": "0"}),
        }
        labels = {"is_active": _("اجعلها المحفظة النشطة")}

    def __init__(self, *args, merchant_method=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.merchant_method = merchant_method or getattr(self.instance, "merchant_method", None)
        self.fields["label"].required = False
        # Both may be blank *here*; which combinations are actually legal is
        # `Wallet.clean`'s decision, because it is the one that knows what the
        # payment method is paid by. Marking either required on the form would
        # be a second, quieter copy of that rule.
        self.fields["number"].required = False
        method = getattr(self.merchant_method, "payment_method", None)
        if method is not None and not method.requires_wallet_number:
            self.fields["number"].help_text = _(
                "اختياري لهذه الطريقة: تُدفع بمسح صورة أو رمز QR."
            )
        self.fields["is_active"].help_text = _(
            "محفظة واحدة فقط لكل طريقة دفع لدى التاجر تكون نشطة. تفعيل هذه يُلغي تفعيل الحالية."
        )

    @property
    def current_active(self):
        """The wallet that would be stood down, if any — shown as a warning."""
        if not self.merchant_method:
            return None
        current = self.merchant_method.active_wallet
        if current and current.pk == self.instance.pk:
            return None
        return current

    def clean_number(self):
        number = (self.cleaned_data["number"] or "").strip()
        clash = Wallet.objects.filter(
            merchant_method=self.merchant_method, number=number
        ).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(_("هذا الرقم مسجّل بالفعل لهذه الطريقة لدى هذا التاجر."))
        return number

    def save(self, commit=True):
        self.instance.merchant_method = self.merchant_method
        return super().save(commit=commit)


class PaymentMethodForm(forms.ModelForm):
    class Meta:
        model = PaymentMethod
        fields = [
            "code", "caption_ar", "caption_en", "icon",
            "supports_deposit", "supports_withdrawal", "requires_wallet_number",
            "is_active", "sort_order",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # The code is referenced by data and reports; freeze it once set.
            self.fields["code"].disabled = True
            self.fields["code"].help_text = _("لا يمكن تغيير الرمز بعد الإنشاء.")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("supports_deposit") and not cleaned.get("supports_withdrawal"):
            raise forms.ValidationError(
                _("يجب أن تدعم الطريقة الإيداع أو السحب على الأقل.")
            )
        return cleaned


class ExchangeRateForm(forms.ModelForm):
    """Set a new rate.

    Never an edit: spec §5 requires each change to create a new row so the
    history is preserved, and the model refuses to update a saved one.
    """

    class Meta:
        model = ExchangeRate
        fields = ["rate_type", "iqd_per_usd", "commission_iqd_per_100usd", "effective_from", "note"]
        widgets = {
            "iqd_per_usd": forms.NumberInput(attrs={"class": "num", "step": "0.01", "min": "0.01"}),
            "commission_iqd_per_100usd": forms.NumberInput(attrs={"class": "num", "step": "0.01", "min": "0"}),
            "effective_from": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["effective_from"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        self.fields["effective_from"].initial = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        self.fields["effective_from"].help_text = _(
            "السعر يسري من هذا الوقت على الطلبات الجديدة فقط. الطلبات القائمة تحتفظ بلقطة سعرها."
        )
        self.fields["note"].required = False

    def clean_iqd_per_usd(self):
        value = self.cleaned_data["iqd_per_usd"]
        if value <= Decimal("0"):
            raise forms.ValidationError(_("السعر يجب أن يكون أكبر من صفر."))
        return value

    @property
    def superseded(self):
        """The revision this one will replace, for the confirmation notice."""
        rate_type = self.data.get("rate_type") or self.initial.get("rate_type")
        return ExchangeRate.current(rate_type) if rate_type else None


class RateHistoryFilterForm(forms.Form):
    rate_type = forms.ChoiceField(
        label=_("النوع"),
        required=False,
        choices=[("", _("الكل"))] + list(RateType.choices),
    )


class BusinessHoursForm(forms.ModelForm):
    """Business hours and the closed notice (spec §5, §9) — build-order step 11.

    ``SystemSettings.is_open_override`` is a three-state field — follow the
    schedule, force open, force closed — and Django renders a nullable boolean
    as "Unknown / Yes / No". "Unknown" is not what an empty override means, and
    a Finance user reading it as "the system does not know" would be reading it
    exactly backwards. So the three states get their own named choices and the
    boolean is set from them on save.
    """

    FOLLOW = ""
    FORCE_OPEN = "open"
    FORCE_CLOSED = "closed"

    OVERRIDE_CHOICES = [
        (FOLLOW, _("اتبع المواعيد أدناه")),
        (FORCE_OPEN, _("مفتوح الآن، بتجاوز المواعيد")),
        (FORCE_CLOSED, _("مغلق الآن، بتجاوز المواعيد")),
    ]

    #: ``is_open_override`` value for each choice.
    OVERRIDE_VALUES = {FOLLOW: None, FORCE_OPEN: True, FORCE_CLOSED: False}

    override = forms.ChoiceField(
        label=_("الحالة"),
        choices=OVERRIDE_CHOICES,
        required=False,
        help_text=_(
            "التجاوز يبقى ساريًا حتى تُعيده إلى «اتبع المواعيد» بنفسك؛ لا ينتهي وحده."
        ),
    )

    class Meta:
        model = SystemSettings
        fields = ["open_time", "close_time", "timezone", "closed_message_ar"]
        widgets = {
            "open_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "close_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "closed_message_ar": forms.Textarea(attrs={"rows": 3}),
        }

    field_order = ["override", "open_time", "close_time", "timezone", "closed_message_ar"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("open_time", "close_time"):
            self.fields[name].input_formats = ["%H:%M", "%H:%M:%S"]
        self.fields["open_time"].help_text = _(
            "وقت الفتح بتوقيت المنطقة الزمنية أدناه."
        )
        self.fields["close_time"].help_text = _(
            "وقت الإغلاق. إذا كان أبكر من وقت الفتح فالدوام يمتد إلى ما بعد منتصف الليل."
        )
        self.fields["timezone"].help_text = _(
            "اسم منطقة زمنية من قاعدة IANA، مثل Asia/Baghdad."
        )
        self.fields["closed_message_ar"].help_text = _(
            "يقرؤه العميل كما هو على شاشة الإغلاق، فوق العدّاد التنازلي."
        )
        self.fields["closed_message_ar"].required = True

        current = self.instance.is_open_override
        self.fields["override"].initial = next(
            (key for key, value in self.OVERRIDE_VALUES.items() if value is current),
            self.FOLLOW,
        )

    def clean_closed_message_ar(self):
        message = (self.cleaned_data["closed_message_ar"] or "").strip()
        if not message:
            raise forms.ValidationError(_("اكتب نص رسالة الإغلاق التي يراها العميل."))
        return message

    def save(self, commit=True):
        self.instance.is_open_override = self.OVERRIDE_VALUES[
            self.cleaned_data.get("override", self.FOLLOW)
        ]
        return super().save(commit=commit)
