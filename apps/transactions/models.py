"""Deposit and withdrawal requests, their attachments, and their message thread.

The app is named ``transactions`` rather than ``requests`` so it can never
shadow the third-party ``requests`` package on the import path.
"""

import secrets
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.choices import ActorRole
from apps.core.models import TimeStampedModel
from apps.core.validators import validate_upload_content, validate_upload_size

PUBLIC_REF_PREFIX = "MP"
PUBLIC_REF_DIGITS = 5


class RequestType(models.TextChoices):
    DEPOSIT = "deposit", _("إيداع")
    WITHDRAWAL = "withdrawal", _("سحب")


class RequestStatus(models.TextChoices):
    """The union of both lifecycles in spec §6.

    Deposit:    submitted → under_review → assigned → merchant_confirmed
                → credited → closed
    Withdrawal: submitted → under_review → assigned → merchant_paid → closed

    Three ways out that are not the happy path, and the Finance review of
    24 Aug 2026 was explicit that they are three different things rather than
    shades of one:

    ``pending``
        Parked. Waiting on something, and nobody has given up on it. A merchant
        hands a request back into this state; Finance may park one here too.
        It is the only non-terminal member of this group — a parked request is
        routed onward or ended later, it does not sit here for ever.
    ``rejected``
        Failed. Somebody looked at it and said no.
    ``cancelled``
        Abandoned. Nothing failed; it stopped mattering. Kept apart from
        ``rejected`` because a desk reconciling a month needs "how many did we
        turn away" and "how many went away" to be different numbers.
    """

    SUBMITTED = "submitted", _("مُقدَّم")
    UNDER_REVIEW = "under_review", _("قيد المراجعة")
    ASSIGNED = "assigned", _("مُسند إلى تاجر")
    PENDING = "pending", _("مُعلَّق بانتظار المالية")
    MERCHANT_CONFIRMED = "merchant_confirmed", _("أكّد التاجر الاستلام")
    MERCHANT_PAID = "merchant_paid", _("دفع التاجر")
    CREDITED = "credited", _("قُيِّد في B2CORE")
    CLOSED = "closed", _("مغلق")
    REJECTED = "rejected", _("مرفوض")
    CANCELLED = "cancelled", _("مُلغى")


#: Statuses a deposit may legitimately hold.
DEPOSIT_STATUSES = frozenset({
    RequestStatus.SUBMITTED,
    RequestStatus.UNDER_REVIEW,
    RequestStatus.ASSIGNED,
    RequestStatus.PENDING,
    RequestStatus.MERCHANT_CONFIRMED,
    RequestStatus.CREDITED,
    RequestStatus.CLOSED,
    RequestStatus.REJECTED,
    RequestStatus.CANCELLED,
})

#: Statuses a withdrawal may legitimately hold.
WITHDRAWAL_STATUSES = frozenset({
    RequestStatus.SUBMITTED,
    RequestStatus.UNDER_REVIEW,
    RequestStatus.ASSIGNED,
    RequestStatus.PENDING,
    RequestStatus.MERCHANT_PAID,
    RequestStatus.CLOSED,
    RequestStatus.REJECTED,
    RequestStatus.CANCELLED,
})

#: Nothing moves out of these. ``pending`` is deliberately absent: parked is not
#: finished, and a request nobody can route onward is a request that has been
#: abandoned without anybody saying so.
TERMINAL_STATUSES = frozenset({
    RequestStatus.CLOSED,
    RequestStatus.REJECTED,
    RequestStatus.CANCELLED,
})


def generate_public_ref() -> str:
    """A short human-readable reference, e.g. ``MP-24817`` (spec §5).

    Random rather than sequential so the reference leaks no volume information
    to merchants, who see it as the request's only identifier.
    """
    lower = 10 ** (PUBLIC_REF_DIGITS - 1)
    upper = 10**PUBLIC_REF_DIGITS - 1
    return f"{PUBLIC_REF_PREFIX}-{secrets.randbelow(upper - lower + 1) + lower}"


def attachment_upload_path(instance, filename: str) -> str:
    """Group uploads under the request they belong to.

    The path contains ``public_ref``, never a client identifier, because the
    stored path can surface in logs and signed URLs.
    """
    return f"attachments/{instance.request.public_ref}/{filename}"


class Request(TimeStampedModel):
    """A deposit or withdrawal request (spec §5, §6)."""

    public_ref = models.CharField(
        _("المرجع"),
        max_length=16,
        unique=True,
        editable=False,
        db_index=True,
        help_text=_("المعرّف الوحيد الذي يراه التاجر."),
    )
    type = models.CharField(
        _("النوع"), max_length=20, choices=RequestType.choices, db_index=True
    )

    # Spec §2: never exposed to a merchant. Merchant-scoped serializers must
    # whitelist fields explicitly and this must never be among them.
    client = models.ForeignKey(
        "accounts.Client",
        verbose_name=_("العميل"),
        on_delete=models.PROTECT,
        related_name="requests",
    )

    payment_method = models.ForeignKey(
        "merchants.PaymentMethod",
        verbose_name=_("طريقة الدفع"),
        on_delete=models.PROTECT,
        related_name="requests",
    )
    merchant_selected = models.ForeignKey(
        "merchants.Merchant",
        verbose_name=_("التاجر المختار من العميل"),
        on_delete=models.PROTECT,
        related_name="requests_selected",
    )
    merchant_assigned = models.ForeignKey(
        "merchants.Merchant",
        verbose_name=_("التاجر المُسند من المالية"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="requests_assigned",
        help_text=_("قد يختلف عن التاجر الذي اختاره العميل."),
    )

    #: Which wallet the client was actually shown. The snapshot below stays
    #: the historical truth — what the screen said, frozen (spec §5) — and this
    #: is provenance: it answers "was anything ever submitted against this
    #: wallet", which is the question that decides whether retiring one may
    #: delete it or must archive it. ``PROTECT`` so the database refuses the
    #: delete even if the application ever forgets to ask.
    wallet = models.ForeignKey(
        "merchants.Wallet",
        verbose_name=_("المحفظة"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="requests",
    )

    # Snapshotted at submission (spec §5): later wallet or rate changes never
    # alter an existing request.
    wallet_number_snapshot = models.CharField(
        _("رقم المحفظة وقت التقديم"), max_length=64, blank=True
    )
    destination_account = models.CharField(
        _("حساب الوجهة"),
        max_length=64,
        blank=True,
        help_text=_("للسحب فقط: بطاقة العميل أو رقم محفظته."),
    )

    amount_usd = models.DecimalField(
        _("المبلغ بالدولار"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    amount_iqd = models.DecimalField(
        _("المبلغ بالدينار"),
        max_digits=16,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    #: What the client asked for, kept whatever anybody corrects the request to
    #: afterwards (Finance review, 24 Aug 2026). The real case is a client who
    #: requests $100 and transfers $60: the request becomes $60 because that is
    #: what arrived, and the $100 stays because "what was asked for" and "what
    #: turned up" are two different questions and a desk needs both.
    submitted_amount_usd = models.DecimalField(
        _("المبلغ المطلوب أصلًا بالدولار"), max_digits=12, decimal_places=2,
        null=True, blank=True,
    )
    submitted_amount_iqd = models.DecimalField(
        _("المبلغ المطلوب أصلًا بالدينار"), max_digits=16, decimal_places=2,
        null=True, blank=True,
    )

    rate_applied = models.DecimalField(
        _("السعر المطبَّق"), max_digits=12, decimal_places=2
    )
    commission_applied = models.DecimalField(
        _("العمولة المطبَّقة"), max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    #: The commission *rate* the request was quoted on, beside the amount it
    #: produced. Both are snapshots (spec §5) and the pair is what makes a
    #: correction possible: re-prorating a fee for a new amount needs the rule,
    #: and `commission_applied` alone is only ever its answer for the old one.
    commission_rate_applied = models.DecimalField(
        _("نسبة العمولة المطبَّقة لكل 100 دولار"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    status = models.CharField(
        _("الحالة"),
        max_length=25,
        choices=RequestStatus.choices,
        default=RequestStatus.SUBMITTED,
        db_index=True,
    )
    rejection_reason = models.TextField(_("سبب الرفض"), blank=True)

    #: Read by Finance in the queue rather than composed by them. Blank means
    #: "use the generated one" — see :attr:`display_title` — so a title follows
    #: a corrected amount by default and stops following it the moment somebody
    #: writes their own. Storing only the override is what makes both true
    #: without a second "is this custom?" flag to keep in step.
    title = models.CharField(
        _("عنوان الطلب"),
        max_length=120,
        blank=True,
        help_text=_("اتركه فارغًا ليُولَّد من النوع والمبلغ ويتبعهما عند التعديل."),
    )

    #: Who last moved this request, for the performance review Finance asked
    #: for. The audit log holds every step; this holds the latest, because a
    #: queue cannot be filtered or a report grouped on a log.
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("آخر من عالجه"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="handled_requests",
    )
    handled_at = models.DateTimeField(_("وقت آخر معالجة"), null=True, blank=True)

    submitted_at = models.DateTimeField(_("وقت التقديم"), auto_now_add=True, db_index=True)
    assigned_at = models.DateTimeField(_("وقت الإسناد"), null=True, blank=True)
    merchant_actioned_at = models.DateTimeField(_("وقت تنفيذ التاجر"), null=True, blank=True)
    closed_at = models.DateTimeField(_("وقت الإغلاق"), null=True, blank=True)

    class Meta:
        verbose_name = _("طلب")
        verbose_name_plural = _("الطلبات")
        ordering = ["-submitted_at", "-id"]
        indexes = [
            models.Index(fields=["status", "-submitted_at"], name="request_status_time_idx"),
            models.Index(fields=["merchant_assigned", "status"], name="request_merchant_idx"),
            models.Index(fields=["client", "-submitted_at"], name="request_client_time_idx"),
        ]
        permissions = [
            # Finance (spec §9)
            ("route_request", _("إسناد الطلب إلى تاجر")),
            ("approve_request", _("اعتماد الطلب")),
            ("credit_request", _("تسجيل الإيداع في B2CORE")),
            ("close_request", _("إغلاق الطلب")),
            # Merchant (spec §8)
            ("confirm_request", _("تأكيد تنفيذ الطلب")),
            # Both (spec §6)
            ("reject_request", _("رفض الطلب")),
            ("view_all_requests", _("عرض جميع الطلبات وليس المُسندة فقط")),
            # Merchant return and cancellation (Finance review, 24 Aug 2026).
            ("return_request", _("إعادة الطلب إلى المالية")),
            ("cancel_request", _("إلغاء الطلب")),
            # Correcting a request to the amount that actually arrived. Held by
            # both sides on purpose: the merchant is who sees the money land,
            # and Finance is who answers for the figure afterwards.
            ("change_request_amount", _("تعديل مبلغ الطلب")),
            # Reporting (spec §9) — build-order step 16. Separate from reading
            # a report on screen: a workbook on somebody's laptop is outside
            # every control this system has.
            ("export_reports", _("تصدير التقارير إلى ملف")),
        ]

    def __str__(self):
        return f"{self.public_ref} · {self.get_type_display()} · {self.amount_usd} USD"

    def save(self, *args, **kwargs):
        if not self.public_ref:
            self.public_ref = self._allocate_public_ref()
        return super().save(*args, **kwargs)

    @classmethod
    def _allocate_public_ref(cls, attempts: int = 20) -> str:
        """Draw a reference that is not already taken.

        The unique constraint remains the real guarantee; this just keeps the
        collision rate at insert time negligible.
        """
        for _attempt in range(attempts):
            candidate = generate_public_ref()
            if not cls.objects.filter(public_ref=candidate).exists():
                return candidate
        raise RuntimeError(
            "Could not allocate a unique public_ref; widen PUBLIC_REF_DIGITS."
        )

    def clean(self):
        super().clean()
        if self.type == RequestType.WITHDRAWAL and not self.destination_account:
            raise ValidationError(
                {"destination_account": _("حساب الوجهة مطلوب لطلبات السحب.")}
            )
        if self.type == RequestType.DEPOSIT and self.destination_account:
            raise ValidationError(
                {"destination_account": _("حساب الوجهة لا يُستخدم في طلبات الإيداع.")}
            )
        allowed = self.allowed_statuses(self.type)
        if allowed and self.status not in allowed:
            raise ValidationError(
                {"status": _("هذه الحالة لا تنتمي إلى مسار هذا النوع من الطلبات.")}
            )

    @staticmethod
    def allowed_statuses(request_type: str) -> frozenset:
        if request_type == RequestType.DEPOSIT:
            return DEPOSIT_STATUSES
        if request_type == RequestType.WITHDRAWAL:
            return WITHDRAWAL_STATUSES
        return frozenset()

    @property
    def is_deposit(self) -> bool:
        return self.type == RequestType.DEPOSIT

    @property
    def is_withdrawal(self) -> bool:
        return self.type == RequestType.WITHDRAWAL

    @property
    def is_closed(self) -> bool:
        return self.status in TERMINAL_STATUSES

    # -- timing (Finance review, 24 Aug 2026) ------------------------------

    @property
    def resolved_at(self):
        """When the request stopped being live, or ``None`` while it still is.

        The same instant ``closed_at`` records, named for the question Finance
        actually asks it: not "when was it closed" but "when did this stop
        being my problem". A parked request has not resolved, which is why
        ``pending`` is not a terminal status.
        """
        return self.closed_at if self.is_closed else None

    @property
    def elapsed(self):
        """How long it has taken, or is taking.

        Finance's example was a client claiming they waited thirty minutes. A
        duration that stops at resolution answers that; one that keeps running
        on an open request answers the more useful version of it, which is how
        long the ones still open have been waiting.
        """
        from django.utils import timezone

        if self.submitted_at is None:
            return None
        return (self.resolved_at or timezone.now()) - self.submitted_at

    @property
    def elapsed_display(self) -> str:
        """The duration as a desk reads it, not as a ``timedelta`` prints it.

        Coarse on purpose: Finance's question is "did this take half an hour or
        two days", and seconds in a reconciliation column are noise.
        """
        span = self.elapsed
        if span is None:
            return ""
        minutes = int(span.total_seconds() // 60)
        if minutes < 1:
            return str(_("أقل من دقيقة"))
        if minutes < 60:
            return str(_("%(n)s دقيقة")) % {"n": minutes}
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return str(_("%(h)s س %(m)s د")) % {"h": hours, "m": minutes}
        days, hours = divmod(hours, 24)
        return str(_("%(d)s ي %(h)s س")) % {"d": days, "h": hours}

    @property
    def was_amount_corrected(self) -> bool:
        return (
            self.submitted_amount_usd is not None
            and self.submitted_amount_usd != self.amount_usd
        )

    @property
    def display_title(self) -> str:
        """What the queue is read by.

        Generated unless somebody wrote their own. Finance reconciles by
        reading titles down a column, so a request still called "إيداع 100.00 $"
        after $60 arrived is a request being reconciled against the wrong
        figure — which is the whole reason this exists.
        """
        if self.title:
            return self.title
        return f"{self.get_type_display()} {self.amount_usd:,.2f} $"

    @property
    def effective_merchant(self):
        """Who is actually responsible: the routed merchant, else the chosen one."""
        return self.merchant_assigned or self.merchant_selected


class Attachment(TimeStampedModel):
    """Proof of payment or transfer (spec §5).

    Stored under ``MEDIA_ROOT``, which sits outside the web root and is never
    mapped to a URL prefix; files are served through a signed time-limited view
    (spec §11).
    """

    request = models.ForeignKey(
        Request,
        verbose_name=_("الطلب"),
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(
        _("الملف"),
        upload_to=attachment_upload_path,
        validators=[validate_upload_size, validate_upload_content],
    )
    original_name = models.CharField(_("اسم الملف الأصلي"), max_length=255, blank=True)
    content_type = models.CharField(_("نوع المحتوى"), max_length=100, blank=True)
    size_bytes = models.PositiveBigIntegerField(_("الحجم"), default=0)
    uploaded_by_role = models.CharField(
        _("رفعه"), max_length=20, choices=ActorRole.choices
    )
    uploaded_by_id = models.PositiveBigIntegerField(
        _("معرّف الرافع"),
        null=True,
        blank=True,
        help_text=_("مفتاح الحساب الداخلي أو العميل حسب الدور."),
    )
    uploaded_at = models.DateTimeField(_("وقت الرفع"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("مرفق")
        verbose_name_plural = _("المرفقات")
        ordering = ["uploaded_at"]

    def __str__(self):
        return self.original_name or self.file.name

    def save(self, *args, **kwargs):
        if self.file and not self.original_name:
            self.original_name = self.file.name.rsplit("/", 1)[-1][:255]
        if self.file and not self.size_bytes:
            try:
                self.size_bytes = self.file.size
            except (OSError, ValueError):
                self.size_bytes = 0
        return super().save(*args, **kwargs)


class Message(TimeStampedModel):
    """One entry in a request's thread (spec §5, §9).

    ``sender_role`` plus ``sender_id`` rather than a foreign key because a
    sender may be an internal user, a client, or the system. Merchant-scoped
    serialisation labels any client message as "العميل" and omits ``sender_id``.
    """

    request = models.ForeignKey(
        Request,
        verbose_name=_("الطلب"),
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender_role = models.CharField(
        _("دور المُرسل"), max_length=20, choices=ActorRole.choices, db_index=True
    )
    sender_id = models.PositiveBigIntegerField(
        _("معرّف المُرسل"), null=True, blank=True
    )
    body = models.TextField(_("النص"), blank=True)
    attachment = models.ForeignKey(
        Attachment,
        verbose_name=_("المرفق"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    is_internal_note = models.BooleanField(
        _("ملاحظة داخلية"),
        default=False,
        help_text=_("تظهر للمالية فقط، ولا تُرسل للعميل أو التاجر."),
    )
    created_at = models.DateTimeField(_("وقت الإرسال"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("رسالة")
        verbose_name_plural = _("الرسائل")
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["request", "created_at"], name="message_request_time_idx"),
        ]

    def __str__(self):
        return f"{self.get_sender_role_display()}: {self.body[:40]}"

    def clean(self):
        super().clean()
        if not self.body.strip() and not self.attachment_id:
            raise ValidationError(_("الرسالة يجب أن تحتوي نصًا أو مرفقًا."))

    @property
    def display_sender(self) -> str:
        """Sender label safe to show anyone, including a merchant (spec §5)."""
        return str(self.get_sender_role_display())


class RequestRead(models.Model):
    """When an internal user last opened a request (spec §10) — step 13.

    The badge on both panels is "has anything happened since I looked at this",
    and this row is the "since I looked" half of it. The rule that turns it into
    an unread count lives in :mod:`apps.transactions.reads`.

    **Per user, not per role.** Two Finance staff work the same queue; a marker
    shared across the desk would let whoever opened a request first clear the
    badge for everyone else.

    Nothing here is client data, and a merchant's own marker names only their
    own user id — which is theirs. It is still not serialised to them: what a
    merchant is told is whether a row is unread, never who read it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("المستخدم"),
        on_delete=models.CASCADE,
        related_name="request_reads",
    )
    request = models.ForeignKey(
        Request,
        verbose_name=_("الطلب"),
        on_delete=models.CASCADE,
        related_name="reads",
    )
    seen_at = models.DateTimeField(_("وقت الاطلاع"), auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("اطلاع على طلب")
        verbose_name_plural = _("الاطلاعات على الطلبات")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "request"], name="requestread_unique_user_request"
            )
        ]
        indexes = [
            models.Index(fields=["user", "request"], name="requestread_user_req_idx"),
        ]
        # Nobody grants or holds these: the row is written by the act of opening
        # a screen, and there is no surface on which it is managed by hand.
        default_permissions = ()

    def __str__(self):
        return f"{self.user_id} saw {self.request_id} at {self.seen_at:%Y-%m-%d %H:%M}"
