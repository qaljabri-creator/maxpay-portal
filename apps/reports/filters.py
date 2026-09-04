"""The filters spec §9 asks a report to carry, applied to a queryset.

One implementation, used by both panels. What each panel supplies is the
queryset it is allowed to report on — Finance's is everything, a merchant's is
:func:`apps.merchant_panel.scoping.requests_for` — so the narrowing here never
has to know whose report it is building, and cannot widen anybody's scope by
getting it wrong.

The date range is on ``submitted_at`` rather than on whichever timestamp each
status happens to stamp. A report answers "what came in between these dates and
what happened to it", and anchoring the range to anything later would drop the
requests that are still in flight — the ones most worth looking at.
"""

from dataclasses import dataclass, field
from datetime import date

from django.db.models import Q

from apps.merchants.models import Merchant, PaymentMethod
from apps.transactions.models import RequestStatus, RequestType
from apps.transactions.services import (
    AWAITING_FINANCE,
    AWAITING_MERCHANT,
    OPEN_STATUSES,
)

#: Status values that stand for a set rather than one. Shared with the queue's
#: own control so a report and a worklist filtered "the same way" really are.
STATUS_GROUPS: dict[str, frozenset] = {
    "awaiting_finance": AWAITING_FINANCE,
    "awaiting_merchant": AWAITING_MERCHANT,
    "open": OPEN_STATUSES,
}


@dataclass(frozen=True)
class ReportFilter:
    """What the operator asked for, already validated.

    Built from a form rather than from raw query parameters, so nothing here
    has to re-parse a date or second-guess a merchant id.
    """

    date_from: date | None = None
    date_to: date | None = None
    status: str = ""
    type: str = ""
    method: PaymentMethod | None = None
    merchant: Merchant | None = None
    #: Finance review 4.3. Never settable from a merchant's report — the form
    #: that carries it is Finance's, and it is dropped from even that one for a
    #: user without ``accounts.view_client_identity``. A report scoped to one
    #: client is an identity operation whichever way its columns are gated.
    client: object | None = None
    #: Set by the surface, not by the operator: a merchant filtering by merchant
    #: is meaningless, and Finance filtering by one means both roles it can play.
    match_merchant_either_side: bool = True
    labels: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_form(cls, form, *, match_merchant_either_side: bool = True) -> "ReportFilter":
        cleaned = getattr(form, "cleaned_data", None) or {}
        return cls(
            date_from=cleaned.get("date_from"),
            date_to=cleaned.get("date_to"),
            status=cleaned.get("status") or "",
            type=cleaned.get("type") or "",
            method=cleaned.get("method"),
            merchant=cleaned.get("merchant"),
            # ``.get`` rather than ``[]``: a merchant's form has no such field,
            # and the absence is how that surface is kept out of this dimension
            # rather than by a check somewhere else that could be forgotten.
            client=cleaned.get("client"),
            match_merchant_either_side=match_merchant_either_side,
        )

    # -- applying ----------------------------------------------------------

    def apply(self, queryset):
        """Narrow ``queryset``. Never widens it, whatever is set."""
        if self.date_from:
            queryset = queryset.filter(submitted_at__date__gte=self.date_from)
        if self.date_to:
            queryset = queryset.filter(submitted_at__date__lte=self.date_to)

        if self.status in STATUS_GROUPS:
            queryset = queryset.filter(status__in=STATUS_GROUPS[self.status])
        elif self.status in RequestStatus.values:
            queryset = queryset.filter(status=self.status)

        if self.type in RequestType.values:
            queryset = queryset.filter(type=self.type)
        if self.method is not None:
            queryset = queryset.filter(payment_method=self.method)
        if self.merchant is not None:
            if self.match_merchant_either_side:
                # "Requests involving this merchant": the one the client chose
                # and the one Finance routed to are both meant, and spec §5
                # allows them to differ.
                queryset = queryset.filter(
                    Q(merchant_selected=self.merchant)
                    | Q(merchant_assigned=self.merchant)
                )
            else:
                queryset = queryset.filter(merchant_assigned=self.merchant)
        if self.client is not None:
            queryset = queryset.filter(client=self.client)
        return queryset

    # -- describing --------------------------------------------------------

    @property
    def is_narrowed(self) -> bool:
        return any(
            (
                self.date_from,
                self.date_to,
                self.status,
                self.type,
                self.method,
                self.merchant,
                self.client,
            )
        )

    def describe(self) -> list[tuple[str, str]]:
        """The filter as label/value pairs, for the screen and the workbook.

        An exported file outlives the screen it was taken from, so it carries
        the question it answers on its own first sheet. A spreadsheet of numbers
        with no statement of what was filtered is a spreadsheet somebody will
        read the wrong way in three months.
        """
        from django.utils.translation import gettext as _

        pairs: list[tuple[str, str]] = []
        pairs.append((str(_("من تاريخ")), self.date_from.isoformat() if self.date_from else str(_("بلا حد"))))
        pairs.append((str(_("إلى تاريخ")), self.date_to.isoformat() if self.date_to else str(_("بلا حد"))))
        pairs.append((str(_("الحالة")), self._status_label()))
        pairs.append((str(_("النوع")), self._type_label()))
        pairs.append((str(_("طريقة الدفع")), str(self.method) if self.method else str(_("كل الطرق"))))
        if self.merchant is not None:
            pairs.append((str(_("التاجر")), str(self.merchant)))
        if self.client is not None:
            # Naming the client on the workbook's first sheet is safe for the
            # same reason the identity columns are: nothing sets this field
            # except a form that ``view_client_identity`` gates.
            pairs.append((str(_("العميل")), str(self.client)))
        return pairs

    def _status_label(self) -> str:
        from django.utils.translation import gettext as _

        group_labels = {
            "open": _("كل الطلبات الجارية"),
            "awaiting_finance": _("بانتظار المالية"),
            "awaiting_merchant": _("لدى التجار"),
        }
        if self.status in group_labels:
            return str(group_labels[self.status])
        if self.status in RequestStatus.values:
            return str(RequestStatus(self.status).label)
        return str(_("كل الحالات"))

    def _type_label(self) -> str:
        from django.utils.translation import gettext as _

        if self.type in RequestType.values:
            return str(RequestType(self.type).label)
        return str(_("النوعان"))
