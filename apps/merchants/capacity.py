"""How much a wallet has taken in today, and how much it still may (spec §5).

``Wallet.daily_cap`` is **in Iraqi dinars** and is measured against
``Request.amount_iqd`` — the total the client actually transfers into the wallet,
commission included, which is the figure that lands in the account the cap is
protecting.

A request does not point at a ``Wallet``: it snapshots the *number* it was shown
(``wallet_number_snapshot``), so a later wallet edit cannot rewrite history
(spec §5). Consumption is therefore matched the same way the money was — the
merchant the client chose, the method they chose, and the number they were given
— rather than through a foreign key that does not exist.

The day is the local calendar day in the configured business timezone, not UTC,
because a cap is a thing Finance and the merchant reconcile against a working
day. Rejected requests are excluded: nothing was ever credited against them.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone


def day_bounds(at=None) -> tuple:
    """The half-open ``[start, end)`` of the local day containing ``at``.

    Expressed as a range rather than ``__date`` so the query stays sargable
    against the ``submitted_at`` index and correct under ``USE_TZ``.
    """
    moment = at or timezone.now()
    local_day = timezone.localtime(moment).date()
    start = timezone.make_aware(
        datetime.combine(local_day, time.min), timezone.get_current_timezone()
    )
    return start, start + timedelta(days=1)


def consumed_iqd(wallet, at=None) -> Decimal:
    """Total IQD already submitted into ``wallet`` during its local day."""
    from apps.transactions.models import Request, RequestStatus, RequestType

    start, end = day_bounds(at)
    total = (
        Request.objects.filter(
            type=RequestType.DEPOSIT,
            merchant_selected_id=wallet.merchant_method.merchant_id,
            payment_method_id=wallet.merchant_method.payment_method_id,
            wallet_number_snapshot=wallet.number,
            submitted_at__gte=start,
            submitted_at__lt=end,
        )
        .exclude(status=RequestStatus.REJECTED)
        .aggregate(total=Sum("amount_iqd"))["total"]
    )
    return total or Decimal("0.00")


def headroom_iqd(wallet, at=None):
    """What ``wallet`` may still take today, or ``None`` when it is uncapped."""
    if wallet.daily_cap is None:
        return None
    remaining = Decimal(wallet.daily_cap) - consumed_iqd(wallet, at=at)
    return remaining if remaining > 0 else Decimal("0.00")


def accepts(wallet, amount_iqd, at=None) -> bool:
    """Whether one more deposit of ``amount_iqd`` fits under the cap."""
    remaining = headroom_iqd(wallet, at=at)
    return remaining is None or Decimal(amount_iqd) <= remaining


def usage(wallet, at=None) -> dict:
    """Everything a Finance screen needs to render the cap, in one call."""
    consumed = consumed_iqd(wallet, at=at)
    cap = Decimal(wallet.daily_cap) if wallet.daily_cap is not None else None
    if cap is None:
        return {"capped": False, "consumed": consumed, "cap": None,
                "remaining": None, "percent": 0, "is_full": False}
    remaining = cap - consumed
    return {
        "capped": True,
        "consumed": consumed,
        "cap": cap,
        "remaining": remaining if remaining > 0 else Decimal("0.00"),
        # Clamped: a cap lowered below what today already took would otherwise
        # render a bar wider than its track.
        "percent": min(100, int(consumed / cap * 100)) if cap > 0 else 100,
        "is_full": remaining <= 0,
    }
