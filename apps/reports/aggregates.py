"""What a filtered set of requests adds up to.

Deliberately small. A report's job is to answer "how much, of what, and where
did it get to", and every number below is one of those three. Anything richer —
per-day series, merchant league tables, reconciliation against statements — is
a different feature with different questions behind it, and spec §13 puts the
last of those out of scope for phase 1.

**Rejected requests are counted but never added up.** They appear in the status
breakdown because "how many did we turn away" is a real question; they are left
out of the money totals because no money moved, and a total that includes them
overstates every figure a desk would reconcile against.
"""

from decimal import Decimal

from django.db.models import Count, Sum

from apps.transactions.models import RequestStatus, RequestType

#: Statuses whose amounts are real. Everything else in flight is money that is
#: *expected* to move; rejected is money that never will, and cancelled is money
#: that stopped being asked for. ``pending`` stays counted — a parked request is
#: still live, and dropping it would understate what the desk is carrying.
UNCOUNTED_STATUSES = frozenset({RequestStatus.REJECTED, RequestStatus.CANCELLED})

COUNTED_STATUSES = frozenset(
    status for status in RequestStatus.values if status not in UNCOUNTED_STATUSES
)

ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return value if value is not None else ZERO


def summarise(queryset) -> dict:
    """Headline figures, plus a breakdown by status and by direction.

    One aggregate query per breakdown rather than a loop of counts: a report
    over a year of requests should not be N queries deep in statuses.
    """
    counted = queryset.filter(status__in=COUNTED_STATUSES)

    totals = counted.aggregate(
        usd=Sum("amount_usd"), iqd=Sum("amount_iqd"), commission=Sum("commission_applied")
    )

    by_status = {
        row["status"]: {"count": row["n"], "usd": _money(row["usd"]), "iqd": _money(row["iqd"])}
        for row in queryset.values("status").annotate(
            n=Count("id"), usd=Sum("amount_usd"), iqd=Sum("amount_iqd")
        )
    }

    by_type = {
        row["type"]: {"count": row["n"], "usd": _money(row["usd"]), "iqd": _money(row["iqd"])}
        for row in counted.values("type").annotate(
            n=Count("id"), usd=Sum("amount_usd"), iqd=Sum("amount_iqd")
        )
    }

    return {
        "count": queryset.count(),
        "counted": counted.count(),
        "rejected": queryset.filter(status=RequestStatus.REJECTED).count(),
        "cancelled": queryset.filter(status=RequestStatus.CANCELLED).count(),
        "total_usd": _money(totals["usd"]),
        "total_iqd": _money(totals["iqd"]),
        "total_commission_iqd": _money(totals["commission"]),
        "by_status": [
            {
                "key": status,
                "label": str(RequestStatus(status).label),
                **by_status.get(status, {"count": 0, "usd": ZERO, "iqd": ZERO}),
            }
            for status in RequestStatus.values
            if status in by_status
        ],
        "by_type": [
            {
                "key": kind,
                "label": str(RequestType(kind).label),
                **by_type.get(kind, {"count": 0, "usd": ZERO, "iqd": ZERO}),
            }
            for kind in RequestType.values
            if kind in by_type
        ],
    }


def by_merchant(queryset) -> list[dict]:
    """Per-merchant totals, for the Finance report only.

    Grouped on ``merchant_assigned`` — who actually executed it — because that
    is what a merchant is reconciled against. A request nobody was routed to
    yet is grouped under a null name rather than dropped, since "how much is
    sitting unrouted" is exactly what this table is asked at month end.
    """
    rows = (
        queryset.filter(status__in=COUNTED_STATUSES)
        .values("merchant_assigned__name")
        .annotate(n=Count("id"), usd=Sum("amount_usd"), iqd=Sum("amount_iqd"))
        .order_by("-iqd")
    )
    return [
        {
            "merchant": row["merchant_assigned__name"] or "",
            "count": row["n"],
            "usd": _money(row["usd"]),
            "iqd": _money(row["iqd"]),
        }
        for row in rows
    ]


def by_method(queryset) -> list[dict]:
    """Per-payment-method totals. Both panels can see this one: a payment
    method is not client identity and a merchant already knows which of them
    they cover."""
    rows = (
        queryset.filter(status__in=COUNTED_STATUSES)
        .values("payment_method__caption_ar", "payment_method__code")
        .annotate(n=Count("id"), usd=Sum("amount_usd"), iqd=Sum("amount_iqd"))
        .order_by("-iqd")
    )
    return [
        {
            "method": row["payment_method__caption_ar"] or row["payment_method__code"],
            "count": row["n"],
            "usd": _money(row["usd"]),
            "iqd": _money(row["iqd"]),
        }
        for row in rows
    ]
