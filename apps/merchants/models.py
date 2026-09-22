"""Payment methods, merchants, the methods each merchant covers, and wallets."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.core.validators import validate_upload_content, validate_upload_size


class PaymentMethod(TimeStampedModel):
    """A local payment rail, e.g. ZainCash or a bank card (spec §5)."""

    code = models.SlugField(
        _("الرمز"),
        max_length=50,
        unique=True,
        help_text=_("معرّف ثابت يُستخدم في الشيفرة والتقارير، ولا يُغيَّر بعد الإنشاء."),
    )
    caption_ar = models.CharField(_("الاسم بالعربية"), max_length=100)
    caption_en = models.CharField(_("الاسم بالإنجليزية"), max_length=100)
    #: The brand mark beside a method's name, and nothing more. Rendered at
    #: 1.9rem on the client's screen 3 and in no other place; a wallet's
    #: ``qr_image`` is the picture a client actually points a camera at, and the
    #: two are deliberately worlds apart in size so they cannot be confused.
    icon = models.ImageField(
        _("أيقونة الطريقة"),
        upload_to="payment_methods/",
        blank=True,
        null=True,
        help_text=_(
            "علامة صغيرة تظهر بجانب اسم الطريقة في قائمة العميل، للتعريف فقط. "
            "ليست رمزًا يُمسح — رمز QR يُرفع على المحفظة نفسها في صفحة التاجر."
        ),
    )
    supports_deposit = models.BooleanField(_("يدعم الإيداع"), default=True)
    supports_withdrawal = models.BooleanField(_("يدعم السحب"), default=True)
    #: Some rails are paid by scanning a code rather than by typing an account
    #: into a banking app — Super QI, and any wallet that issues a static QR.
    #: For those a wallet's *number* is not what the client needs and may not
    #: exist at all, so the method says so and its wallets are validated
    #: against it. See :meth:`Wallet.clean`.
    requires_wallet_number = models.BooleanField(
        _("يُدفع برقم محفظة"),
        default=True,
        help_text=_(
            "أوقفه للطرق التي تُدفع بمسح رمز QR أو بصورة تعليمات بدل كتابة رقم. "
            "عندها يمكن أن تكون المحفظة صورة بلا رقم."
        ),
    )
    is_active = models.BooleanField(_("نشط"), default=True, db_index=True)
    sort_order = models.PositiveIntegerField(
        _("ترتيب العرض"), default=0, help_text=_("الأصغر يظهر أولًا.")
    )

    class Meta:
        verbose_name = _("طريقة دفع")
        verbose_name_plural = _("طرق الدفع")
        ordering = ["sort_order", "caption_ar"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(supports_deposit=True) | models.Q(supports_withdrawal=True),
                name="paymentmethod_supports_at_least_one_direction",
            ),
        ]

    def __str__(self):
        return self.caption_ar or self.caption_en or self.code

    def supports(self, request_type: str) -> bool:
        from apps.transactions.models import RequestType

        if request_type == RequestType.DEPOSIT:
            return self.supports_deposit
        if request_type == RequestType.WITHDRAWAL:
            return self.supports_withdrawal
        return False


class Merchant(TimeStampedModel):
    """A third party that executes deposits and withdrawals (spec §5).

    ``user`` is the merchant's internal login. It is optional so Finance can
    register a merchant before their account exists, but a merchant with no user
    can never sign in.
    """

    name = models.CharField(_("الاسم"), max_length=150, unique=True)
    #: The merchant's own account identifier in B2CORE.
    #:
    #: **This field authorises.** It began as reference data — typed by Finance
    #: so a payout here could be matched by hand against a movement over there,
    #: and documented as something never to be promoted into an authorisation
    #: decision. It has been promoted. When B2CORE gained a merchant-panel menu
    #: item, the token it mints for the person opening it carries no client
    #: type, so the *only* thing separating an ordinary client from a merchant's
    #: queue is whether the verified ``sub`` claim equals this string. See
    #: :mod:`apps.merchant_panel.session`.
    #:
    #: Two consequences worth stating where the field is, rather than leaving
    #: them to be discovered:
    #:
    #: * A typo is no longer a reconciliation nuisance. It is a merchant who
    #:   cannot sign in — or, if it happens to match somebody else's subject,
    #:   that person holding this merchant's queue.
    #: * Editing it is an access-control change. It belongs to whoever may
    #:   manage merchants, and it should be read back from B2CORE rather than
    #:   copied from an email.
    #:
    #: It still shares a name with :attr:`apps.accounts.models.Client.b2core_id`
    #: and is still a different thing: that one is written *from* a verified
    #: token, this one is compared *against* one.
    #:
    #: Optional, and unique when present — two merchants pointing at one B2CORE
    #: account is a reconciliation error waiting to happen. Which is why it is
    #: ``null`` rather than ``""`` when empty: SQL counts NULLs as distinct, so
    #: any number of merchants may have no identifier while no two may share
    #: one. Blank input is folded to NULL in :meth:`clean` and :meth:`save`, and
    #: the constraint below stops an empty string reaching the table by any
    #: other road.
    b2core_id = models.CharField(
        _("معرّف B2CORE"),
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        help_text=_(
            "معرّف التاجر في B2CORE. هو ما يُفتح به دخول التاجر إلى لوحته من داخل "
            "B2CORE: يُقارَن بالمُعرّف الموقّع في الرمز، فإن تطابقا فُتحت الجلسة. "
            "خطأ في كتابته يمنع التاجر من الدخول. اتركه فارغًا إن لم يكن له حساب هناك."
        ),
    )
    is_active = models.BooleanField(_("نشط"), default=True, db_index=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("حساب الدخول"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="merchant_profile",
        limit_choices_to={"role": "merchant"},
    )
    notes = models.TextField(_("ملاحظات"), blank=True)
    #: Retired. Not deleted — spec §11 needs every reference in the audit log
    #: and on every past request to keep resolving, and a merchant's name on a
    #: closed request is part of that request's history.
    #:
    #: A timestamp rather than a boolean, because "when" is the question asked
    #: of an archive and a boolean cannot answer it. ``is_archived`` reads it
    #: back for the places that only want the yes or no.
    archived_at = models.DateTimeField(_("أُرشف في"), null=True, blank=True, db_index=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("أرشفه"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_merchants",
    )

    class Meta:
        verbose_name = _("تاجر")
        verbose_name_plural = _("التجار")
        ordering = ["name"]
        constraints = [
            # The unique index treats '' as a value like any other, so a single
            # blank slipping past the Python layer would take the one free slot
            # and every later merchant without an identifier would fail to save.
            # Absent is NULL here, and only NULL.
            models.CheckConstraint(
                condition=models.Q(b2core_id__isnull=True) | ~models.Q(b2core_id=""),
                name="merchant_b2core_id_null_not_blank",
            ),
        ]
        permissions = [
            ("manage_merchants", _("إدارة التجار وطرقهم ومحافظهم")),
            # Deliberately not part of ``manage_merchants``. Adding a wallet
            # and retiring a merchant are not the same size of act, and the
            # first is delegable to staff in a way the second is not (spec §3).
            ("archive_merchants", _("أرشفة التجار وحذف المحافظ")),
        ]

    def __str__(self):
        return self.name

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @staticmethod
    def normalise_b2core_id(value):
        """Blank in any of its forms — ``""``, spaces, ``None`` — becomes NULL."""
        return (value or "").strip() or None

    def clean(self):
        super().clean()
        # Before `validate_unique`, which `full_clean` runs after this: it is
        # what would otherwise compare one blank against another.
        self.b2core_id = self.normalise_b2core_id(self.b2core_id)
        if self.user_id and self.user.role != "merchant":
            raise ValidationError(
                {"user": _("حساب الدخول يجب أن يكون بدور «تاجر».")}
            )

    def save(self, *args, **kwargs):
        # Again here, because `clean` only runs for form-driven saves and
        # `Merchant.objects.create(b2core_id="")` must not reach the constraint.
        self.b2core_id = self.normalise_b2core_id(self.b2core_id)
        return super().save(*args, **kwargs)

    def active_methods(self):
        return self.methods.filter(
            is_active=True, payment_method__is_active=True
        ).select_related("payment_method")


class MerchantMethod(TimeStampedModel):
    """Which payment methods a merchant covers (spec §5)."""

    merchant = models.ForeignKey(
        Merchant,
        verbose_name=_("التاجر"),
        on_delete=models.CASCADE,
        related_name="methods",
    )
    payment_method = models.ForeignKey(
        PaymentMethod,
        verbose_name=_("طريقة الدفع"),
        on_delete=models.PROTECT,
        related_name="merchant_methods",
    )
    is_active = models.BooleanField(_("نشط"), default=True, db_index=True)

    class Meta:
        verbose_name = _("طريقة دفع لتاجر")
        verbose_name_plural = _("طرق الدفع للتجار")
        ordering = ["merchant__name", "payment_method__sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "payment_method"],
                name="unique_merchant_payment_method",
            ),
        ]

    def __str__(self):
        return f"{self.merchant} · {self.payment_method}"

    @property
    def active_wallet(self):
        """The single wallet currently accepting funds, or ``None``."""
        return self.wallets.filter(is_active=True).first()


class Wallet(TimeStampedModel):
    """A destination account a client pays into for deposits (spec §5).

    Only one wallet per ``merchant_method`` may be active at a time. That is
    enforced twice: in :meth:`save`, which deactivates the previous holder, and
    by a partial unique constraint so no concurrent write can create a second.
    """

    merchant_method = models.ForeignKey(
        MerchantMethod,
        verbose_name=_("طريقة الدفع للتاجر"),
        on_delete=models.CASCADE,
        related_name="wallets",
    )
    number = models.CharField(
        _("رقم المحفظة"),
        max_length=64,
        blank=True,
        help_text=_("يُترك فارغًا فقط للطرق التي تُدفع برمز QR."),
        validators=[
            RegexValidator(
                r"^[0-9+\-\s]{4,64}$",
                message=_("رقم المحفظة يقبل الأرقام والمسافات وعلامتي + و - فقط."),
            )
        ],
    )
    #: The code the client points a camera at. Stored alongside the number
    #: rather than instead of it: a rail can perfectly well issue a QR *and* an
    #: account, and a merchant reconciling wants both.
    #:
    #: Named for what it is. ``image`` was the old name and it sat one letter
    #: away from ``PaymentMethod.icon`` — two picture fields with nothing in
    #: their names to say that one is a brand mark the size of a favicon and
    #: the other is a scannable code that fills a phone screen. Confusing them
    #: in the panel is how a client ends up scanning a logo.
    qr_image = models.ImageField(
        _("رمز QR للدفع"),
        upload_to="wallets/%Y/%m/",
        blank=True,
        validators=[validate_upload_size, validate_upload_content],
        help_text=_(
            "الرمز الذي يمسحه العميل بتطبيق الدفع. يظهر كبيرًا في شاشة تفاصيل "
            "الطلب تحت عنوان صريح، لا كشعار. ارفع صورة الرمز نفسه لا شعار "
            "الطريقة — شعار الطريقة يُرفع في «طرق الدفع»."
        ),
    )
    label = models.CharField(_("التسمية"), max_length=100, blank=True)
    is_active = models.BooleanField(_("نشطة"), default=True, db_index=True)
    daily_cap = models.DecimalField(
        # The unit is in the label, not only in the docs: this is the field an
        # operator types a bare number into, and a cap entered in the wrong
        # currency turns clients away by a factor of a thousand.
        _("السقف اليومي (دينار عراقي)"),
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text=_(
            "إجمالي ما تستقبله هذه المحفظة في اليوم بالدينار العراقي، شاملًا العمولة. "
            "فارغ يعني بلا سقف."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("أنشأها"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wallets",
    )
    deactivated_at = models.DateTimeField(_("أُلغي تفعيلها في"), null=True, blank=True)
    #: Retired rather than removed, and only when removing it would cost
    #: something: a wallet no request was ever submitted against is deleted
    #: outright, because there is no history to keep and a list of dead rows
    #: nobody can act on is its own kind of mess. See
    #: :mod:`apps.merchants.lifecycle`.
    archived_at = models.DateTimeField(_("أُرشفت في"), null=True, blank=True, db_index=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("أرشفها"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_wallets",
    )

    class Meta:
        verbose_name = _("محفظة")
        verbose_name_plural = _("المحافظ")
        ordering = ["-is_active", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["merchant_method"],
                condition=models.Q(is_active=True),
                name="one_active_wallet_per_merchant_method",
            ),
        ]

    def __str__(self):
        name = self.number or self.label or str(_("محفظة برمز QR"))
        return f"{self.number} ({self.label})" if self.number and self.label else name

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def was_used(self) -> bool:
        """Has any request ever been submitted against this wallet?

        Two questions, because there are two eras of data. ``Request.wallet``
        is the direct answer and exists from the moment that field was added;
        for anything filed before it, the snapshot is matched back — same
        number, same merchant, same method. Fail-closed by construction: an
        ambiguous match archives rather than deletes, which is the harmless
        way round.
        """
        from apps.transactions.models import Request

        if self.requests.exists():
            return True
        if not self.number or not self.merchant_method_id:
            return False
        return Request.objects.filter(
            wallet_number_snapshot=self.number,
            payment_method_id=self.merchant_method.payment_method_id,
            merchant_selected_id=self.merchant_method.merchant_id,
        ).exists()

    @property
    def is_scan_only(self) -> bool:
        """Paid by scanning rather than by typing."""
        return not self.number and bool(self.qr_image)

    def clean(self):
        """A wallet has to give the client *something* to pay into.

        Which of the two it may be is the payment method's decision, not the
        wallet's: a rail that is paid by typing an account must not be able to
        offer a picture instead, and a rail paid by scanning must not be forced
        to invent a number. The method says which it is; this refuses anything
        the method did not allow for.
        """
        super().clean()
        if self.number:
            self.number = self.number.strip()

        method = None
        if self.merchant_method_id:
            method = self.merchant_method.payment_method

        if not self.number and not self.qr_image:
            raise ValidationError({
                "number": _("أدخل رقم المحفظة، أو أرفق رمز QR بدلًا منه."),
            })

        if not self.number and method is not None and method.requires_wallet_number:
            raise ValidationError({
                "number": _(
                    "طريقة الدفع «%(method)s» تُدفع برقم محفظة. أدخل الرقم، أو "
                    "أوقف خيار «يُدفع برقم محفظة» على الطريقة أولًا."
                ) % {"method": method},
            })

    def save(self, *args, **kwargs):
        if self.number:
            self.number = self.number.strip()

        # Track the deactivation moment without needing the caller to remember.
        if not self.is_active and self.deactivated_at is None:
            self.deactivated_at = timezone.now()
        elif self.is_active:
            self.deactivated_at = None

        update_fields = kwargs.get("update_fields")
        with transaction.atomic():
            if self.is_active and self.merchant_method_id:
                # Stand down whichever wallet currently holds the active slot,
                # so the partial unique constraint is never violated.
                siblings = Wallet.objects.filter(
                    merchant_method_id=self.merchant_method_id, is_active=True
                )
                if self.pk:
                    siblings = siblings.exclude(pk=self.pk)
                siblings.update(is_active=False, deactivated_at=timezone.now())

            if update_fields is not None:
                fields = set(update_fields) | {"deactivated_at", "updated_at"}
                kwargs["update_fields"] = sorted(fields)
            return super().save(*args, **kwargs)

    def deactivate(self, *, by=None):
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.save(update_fields=["is_active", "deactivated_at"])
        return self
