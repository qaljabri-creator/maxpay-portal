"""Exchange rates, kept as an immutable history (spec §5)."""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import AppendOnlyModel


class RateType(models.TextChoices):
    DEPOSIT = "deposit", _("إيداع")
    WITHDRAWAL = "withdrawal", _("سحب")


class ExchangeRate(AppendOnlyModel):
    """One rate revision.

    Spec §5: never updated in place — each change creates a new row, preserving
    history. Spec §9: rate changes apply to new requests only, which is why
    :class:`apps.transactions.models.Request` snapshots the numbers rather than
    keeping a foreign key to the rate in force.
    """

    rate_type = models.CharField(
        _("النوع"), max_length=20, choices=RateType.choices, db_index=True
    )
    iqd_per_usd = models.DecimalField(
        _("دينار لكل دولار"),
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    commission_iqd_per_100usd = models.DecimalField(
        _("العمولة بالدينار لكل 100 دولار"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    effective_from = models.DateTimeField(
        _("سارٍ من"), default=timezone.now, db_index=True
    )
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("حدّده"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="exchange_rates_set",
    )
    note = models.CharField(_("ملاحظة"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("أُنشئ في"), auto_now_add=True, db_index=True)

    class Meta(AppendOnlyModel.Meta):
        abstract = False
        verbose_name = _("سعر صرف")
        verbose_name_plural = _("أسعار الصرف")
        ordering = ["-effective_from", "-id"]
        indexes = [
            models.Index(fields=["rate_type", "-effective_from"], name="rate_type_effective_idx"),
        ]

    def __str__(self):
        return f"{self.get_rate_type_display()} · {self.iqd_per_usd} IQD/USD"

    @classmethod
    def current(cls, rate_type: str, at=None):
        """The revision in force at ``at`` (default: now), or ``None``."""
        moment = at or timezone.now()
        return (
            cls.objects.filter(rate_type=rate_type, effective_from__lte=moment)
            .order_by("-effective_from", "-id")
            .first()
        )

    def commission_for(self, amount_usd: Decimal) -> Decimal:
        """Commission in IQD for ``amount_usd``, prorated per 100 USD."""
        amount = Decimal(amount_usd)
        return (amount / Decimal("100")) * self.commission_iqd_per_100usd

    def convert_usd_to_iqd(self, amount_usd: Decimal) -> Decimal:
        return Decimal(amount_usd) * self.iqd_per_usd
