"""Turning a USD figure into what the client actually pays, or receives
(spec §5, §7).

The client enters **USD** in both directions — the amount that moves in their
trading account — and is shown **IQD**. What differs is which way the
commission points::

    deposit     total_iqd = amount_usd × iqd_per_usd  +  commission
    withdrawal  total_iqd = amount_usd × iqd_per_usd  −  commission

with the commission prorated from the rate's ``commission_iqd_per_100usd``.
The sign is the whole difference and it is not cosmetic: on a deposit the
client hands over the money, so the desk's fee is added to what they transfer;
on a withdrawal the desk pays out, so the fee is deducted from what they
receive. Added in both directions would charge the client on the way in and
hand the fee back on the way out.

``Request.amount_iqd`` stores that *total* either way, because it is the number
that actually moves between the client and the merchant, and therefore the only
number a receipt can be checked against. ``rate_applied`` and
``commission_applied`` keep the two components, so the total stays
reconstructible after any later rate change (spec §5, §9).

A withdrawal whose commission would swallow the whole payout is refused rather
than clamped: ``amount_iqd`` may not be zero or negative, and quietly paying
out nothing is worse than saying the amount is too small.

Everything here is pure: it takes a rate and a string, and returns numbers or
raises. The view layer decides what to do about a failure, and the same
functions back both the quote shown on screen and the figures written at
submission — which is what stops the two from ever drifting apart.
"""

import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from apps.rates.models import ExchangeRate, RateType
from apps.transactions.models import RequestType

#: USD is quantised to the cent. A cent is real money in the currency the
#: client's trading balance is denominated in, and rounding it would silently
#: change the amount they asked for.
CENT = Decimal("0.01")

#: IQD is quantised to the whole dinar. The fils is long out of circulation, so
#: a dinar figure carrying two decimal places is showing a denomination that
#: does not exist — which is what Finance asked to have removed. Rounding here,
#: in the one module both the on-screen quote and the stored figures come from,
#: is what stops the two from disagreeing about it.
DINAR = Decimal("1")

#: Which rate governs which direction (spec §5).
RATE_TYPE_FOR_REQUEST = {
    RequestType.DEPOSIT: RateType.DEPOSIT,
    RequestType.WITHDRAWAL: RateType.WITHDRAWAL,
}


class PricingError(Exception):
    """A quote that cannot be produced, carrying a code the embed branches on."""

    def __init__(self, code: str, message):
        super().__init__(code)
        self.code = code
        self.message = message


#: Characters that carry no numeric meaning but ride along with a pasted or
#: keyboard-typed figure: the Arabic thousands separator, ordinary and
#: non-breaking spaces, and the bidi marks an RTL paragraph wraps numbers in.
_NOISE = frozenset("٬    ‎‏⁦⁧⁨⁩؜_,'")

#: Both decimal separators a client can produce: ASCII and Arabic (U+066B).
_DECIMAL_SEPARATORS = frozenset(".٫")


def normalise_number(raw) -> str:
    """Reduce a human-typed figure to something :class:`Decimal` accepts.

    An Arabic keyboard produces Arabic-Indic digits (``١٢٣``) and an Arabic
    decimal separator (``٫``); phone keypads and copy-paste add thousands
    separators, spaces, and the bidi marks that surround a number inside an RTL
    paragraph. None of that is a malformed amount, so none of it is rejected as
    one. Anything else is left untouched for :func:`parse_amount_usd` to refuse.
    """
    if raw is None:
        return ""

    out = []
    for char in str(raw).strip():
        if char in _NOISE:
            continue
        if char in _DECIMAL_SEPARATORS:
            out.append(".")
            continue
        digit = unicodedata.digit(char, None)
        # Covers ٠-٩ (U+0660) and ۰-۹ (U+06F0) alongside ASCII.
        out.append(str(digit) if digit is not None else char)
    return "".join(out)


#: Per-direction floor and ceiling settings, and how the refusal is worded.
#: Deposits and withdrawals are not the same trade and Finance may not want the
#: same limits on both, so neither borrows the other's figures.
BOUNDS_SETTINGS = {
    RequestType.DEPOSIT: ("PORTAL_DEPOSIT_MIN_USD", "PORTAL_DEPOSIT_MAX_USD"),
    RequestType.WITHDRAWAL: ("PORTAL_WITHDRAWAL_MIN_USD", "PORTAL_WITHDRAWAL_MAX_USD"),
}

_BOUND_MESSAGES = {
    RequestType.DEPOSIT: (
        _("أقل مبلغ للإيداع هو %(limit)s دولار."),
        _("أعلى مبلغ للإيداع هو %(limit)s دولار."),
    ),
    RequestType.WITHDRAWAL: (
        _("أقل مبلغ للسحب هو %(limit)s دولار."),
        _("أعلى مبلغ للسحب هو %(limit)s دولار."),
    ),
}


def bounds_for(request_type: str) -> tuple[Decimal, Decimal]:
    """The configured per-request floor and ceiling for a direction, in USD."""
    try:
        low, high = BOUNDS_SETTINGS[request_type]
    except KeyError as exc:
        raise PricingError("unknown_type", _("نوع طلب غير معروف.")) from exc
    minimum = Decimal(str(getattr(settings, low))).quantize(CENT)
    maximum = Decimal(str(getattr(settings, high))).quantize(CENT)
    return minimum, maximum


def parse_amount_usd(raw, request_type: str) -> Decimal:
    """Validate a client-supplied USD amount, or raise :class:`PricingError`.

    ``request_type`` is required rather than defaulted: the bounds differ by
    direction, and a default would let a withdrawal be silently measured
    against the deposit ceiling.
    """
    text = normalise_number(raw)
    if not text:
        raise PricingError("amount_missing", _("أدخل المبلغ بالدولار."))

    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise PricingError("amount_invalid", _("المبلغ غير صالح.")) from exc

    if not amount.is_finite():
        raise PricingError("amount_invalid", _("المبلغ غير صالح."))

    # Refused rather than rounded. Rounding 0.999 up to 1.00 would charge for
    # a figure the client never typed, and silently changing an amount is a
    # worse answer than asking them to type it again.
    if amount.as_tuple().exponent < -2:
        raise PricingError(
            "amount_precision",
            _("المبلغ يقبل منزلتين عشريتين على الأكثر."),
        )

    minimum, maximum = bounds_for(request_type)
    below, above = _BOUND_MESSAGES[request_type]
    if amount < minimum:
        raise PricingError("amount_below_min", below % {"limit": f"{minimum:.2f}"})
    if amount > maximum:
        raise PricingError("amount_above_max", above % {"limit": f"{maximum:.2f}"})

    # Safe now: the value is within bounds and already no finer than a cent.
    return amount.quantize(CENT)


def current_rate(request_type: str, at=None) -> ExchangeRate:
    """The rate in force for this direction, or raise.

    A missing rate is a configuration failure, not a client error: Finance has
    not set one yet (spec §9). The client is told the service is unavailable
    rather than being shown a zero.
    """
    rate_type = RATE_TYPE_FOR_REQUEST.get(request_type)
    if rate_type is None:
        raise PricingError("unknown_type", _("نوع طلب غير معروف."))

    rate = ExchangeRate.current(rate_type, at=at)
    if rate is None:
        raise PricingError(
            "no_rate",
            _("لم يُحدَّد سعر الصرف بعد. تواصل مع الدعم."),
        )
    return rate


@dataclass(frozen=True)
class Quote:
    """What a given amount costs, or yields, at a given rate.

    ``rate`` is carried along because the submission has to prove it used the
    very revision the client was quoted on — see
    :func:`apps.portal.submissions.create_request`.

    ``total_iqd`` is the figure that moves: what a deposit client transfers to
    the merchant, and what a withdrawal client receives from one. ``direction``
    says which, so a caller never has to infer it from the arithmetic.
    """

    rate: ExchangeRate
    amount_usd: Decimal
    converted_iqd: Decimal
    commission_iqd: Decimal
    total_iqd: Decimal
    direction: str = RequestType.DEPOSIT

    @property
    def is_withdrawal(self) -> bool:
        return self.direction == RequestType.WITHDRAWAL

    def as_dict(self) -> dict:
        return {
            "type": self.direction,
            "amount_usd": f"{self.amount_usd:.2f}",
            "converted_iqd": f"{self.converted_iqd:.2f}",
            "commission_iqd": f"{self.commission_iqd:.2f}",
            "total_iqd": f"{self.total_iqd:.2f}",
        }


def price(
    iqd_per_usd: Decimal,
    commission_per_100: Decimal,
    amount_usd: Decimal,
    request_type: str,
) -> tuple[Decimal, Decimal, Decimal]:
    """The three dinar figures, from the two numbers a rate is made of.

    Split out of :func:`quote` so a *correction* can reprice a request without
    an ``ExchangeRate`` row to hand: Finance asked that an edited request keep
    the rate it was quoted on, and what a request stores is the pair of numbers,
    not a pointer to the row they came from (spec §5). One implementation, so a
    corrected request and a fresh one cannot be priced by two different rules.
    """
    if request_type not in RATE_TYPE_FOR_REQUEST:
        raise PricingError("unknown_type", _("نوع طلب غير معروف."))

    amount = Decimal(amount_usd)
    converted = (amount * Decimal(iqd_per_usd)).quantize(DINAR, rounding=ROUND_HALF_UP)
    commission = ((amount / Decimal("100")) * Decimal(commission_per_100)).quantize(
        DINAR, rounding=ROUND_HALF_UP
    )

    if request_type == RequestType.WITHDRAWAL:
        total = converted - commission
        if total < DINAR:
            raise PricingError(
                "amount_below_commission",
                _("العمولة (%(fee)s دينار) تستهلك المبلغ بالكامل. اسحب مبلغًا أكبر.")
                % {"fee": f"{commission:,.0f}"},
            )
    else:
        total = converted + commission

    return converted, commission, total


def quote(rate: ExchangeRate, amount_usd: Decimal, request_type: str) -> Quote:
    """Price ``amount_usd`` at ``rate``, in the direction ``request_type`` names.

    Each component is rounded once, on its own, to the whole dinar, and the
    total is built from the rounded pair — rather than rounding a single long
    expression — so the three figures on screen always add up to the one the
    client sees at the bottom. Rounding the total instead would leave a line
    that does not add up, which on a receipt is worse than a lost dinar.

    On a withdrawal the commission comes *off* the payout, and a commission that
    would leave nothing (or less than nothing) is a refusal rather than a clamp:
    a payout of zero is not a smaller withdrawal, it is a request the client
    would never have made.
    """
    if request_type not in RATE_TYPE_FOR_REQUEST:
        raise PricingError("unknown_type", _("نوع طلب غير معروف."))

    converted, commission, total = price(
        rate.iqd_per_usd, rate.commission_iqd_per_100usd, amount_usd, request_type
    )

    return Quote(
        rate=rate,
        amount_usd=amount_usd,
        converted_iqd=converted,
        commission_iqd=commission,
        total_iqd=total,
        direction=request_type,
    )


def rate_payload(rate: ExchangeRate, request_type: str) -> dict:
    """The rate as the embed needs it, to compute the live figure locally.

    ``id`` goes back with the submission and is checked against the rate then in
    force: a client must never be charged at a revision they were not shown.
    ``commission_sign`` saves the script from carrying a second copy of the rule
    above — the direction is decided here, in the module that owns it.
    """
    minimum, maximum = bounds_for(request_type)
    return {
        "id": rate.pk,
        "iqd_per_usd": f"{rate.iqd_per_usd:.2f}",
        "commission_iqd_per_100usd": f"{rate.commission_iqd_per_100usd:.2f}",
        "commission_sign": -1 if request_type == RequestType.WITHDRAWAL else 1,
        "effective_from": rate.effective_from.isoformat(),
        "min_usd": f"{minimum:.2f}",
        "max_usd": f"{maximum:.2f}",
    }
