"""The merchant's own report and its export (spec §8, §9) — build-order step 16.

A merchant reconciles: "what did I take last month, on which method, and how
much of it is still open." That question needs no client in it, which is why
this exists at all rather than merchants being sent a file by Finance.

**Nothing here builds a row by hand.** Every row comes out of
:class:`MerchantReportRowSerializer`, which derives from ``MerchantSafeSerializer``
and therefore cannot be *defined* naming an identifying field, cannot *run*
producing one, and is swept once more by the workbook writer before a cell is
written. Four layers on the one artefact that leaves the building — a screen
closes, a JSON response dies with the tab, and a spreadsheet gets forwarded.

The scope is :func:`apps.merchant_panel.scoping.requests_for`, the same queryset
every other merchant screen narrows through, so a report cannot see a request a
queue could not.
"""

from django.utils.translation import gettext as _
from django.views.generic import TemplateView
from rest_framework import serializers

from apps.core.choices import AuditAction
from apps.core.services import record_audit
from apps.finance.report_views import _xlsx_response
from apps.reports import aggregates
from apps.reports.filters import ReportFilter
from apps.reports.workbook import IQD_FORMAT, USD_FORMAT, Column, build, cells_for

from .report_forms import MerchantReportFilterForm
from .serializers import MerchantRequestSummarySerializer, _iso
from .views import MerchantPanelMixin

#: The report's columns. Every key is one the masked serializer produces, and
#: `build(masked=True)` refuses any key the anonymity rule forbids — so this
#: list cannot quietly acquire a client column.
COLUMNS = [
    Column("reference", _("المرجع"), width=14),
    Column("type_label", _("النوع"), width=10),
    Column("status_label", _("الحالة"), width=20),
    Column("method", _("طريقة الدفع"), width=16),
    Column("wallet_number", _("المحفظة"), width=18),
    Column("destination_account", _("حساب الوجهة"), width=20),
    Column("amount_usd", _("المبلغ بالدولار"), width=15, number_format=USD_FORMAT),
    Column("amount_iqd", _("المبلغ بالدينار"), width=17, number_format=IQD_FORMAT, decimals=0),
    Column("submitted_at", _("وقت التقديم"), width=18),
    Column("assigned_at", _("وقت الإسناد"), width=18),
    Column("merchant_actioned_at", _("وقت تنفيذك"), width=18),
    Column("closed_at", _("وقت الإغلاق"), width=18),
]


class MerchantReportRowSerializer(MerchantRequestSummarySerializer):
    """The queue row plus the two timestamps a reconciliation needs.

    Subclassed rather than assembled here so the guard applies: the parent's
    ``__init_subclass__`` re-checks this class's own field names and sources at
    import time, and the parent's ``to_representation`` re-checks the output of
    every one of them at serialisation time.
    """

    merchant_actioned_at = serializers.SerializerMethodField()
    closed_at = serializers.SerializerMethodField()

    def get_merchant_actioned_at(self, request_obj):
        return _iso(request_obj.merchant_actioned_at)

    def get_closed_at(self, request_obj):
        return _iso(request_obj.closed_at)


class MerchantReportMixin(MerchantPanelMixin):
    nav_section = "reports"

    def get_filter_form(self) -> MerchantReportFilterForm:
        if not hasattr(self, "_form"):
            form = MerchantReportFilterForm(self.request.GET or None)
            form.is_valid()  # every field is optional
            self._form = form
        return self._form

    def get_report_filter(self) -> ReportFilter:
        # `match_merchant_either_side` is moot here — the scope is already one
        # merchant's assigned requests and the form has no merchant field.
        return ReportFilter.from_form(self.get_filter_form())

    def get_rows_queryset(self):
        from .scoping import requests_for

        return self.get_report_filter().apply(requests_for(self.merchant))

    def serialise(self, queryset) -> list[dict]:
        return [dict(row) for row in MerchantReportRowSerializer(queryset, many=True).data]


class MerchantReportView(MerchantReportMixin, TemplateView):
    template_name = "merchant/report.html"

    preview_limit = 100

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_rows_queryset()
        report_filter = self.get_report_filter()

        context["filter_form"] = self.get_filter_form()
        context["filter_pairs"] = report_filter.describe()
        context["summary"] = aggregates.summarise(queryset)
        context["by_method"] = aggregates.by_method(queryset)
        context["columns"] = COLUMNS
        context["preview_limit"] = self.preview_limit
        rows = self.serialise(queryset[: self.preview_limit])
        context["rows"] = rows
        context["preview"] = cells_for(rows, COLUMNS)
        context["querystring"] = self.request.GET.urlencode()
        return context


class MerchantReportExportView(MerchantReportMixin, TemplateView):
    """The same report as a file. No extra permission: a merchant exporting
    their own worklist is the feature, and the scope is what protects it."""

    def get(self, request, *args, **kwargs):
        queryset = self.get_rows_queryset()
        report_filter = self.get_report_filter()
        summary = aggregates.summarise(queryset)

        payload = build(
            title=str(_("تقرير الطلبات المُسندة إليّ")),
            filters=report_filter.describe(),
            summary_rows=[
                (str(_("عدد الطلبات")), summary["count"]),
                (str(_("منها مرفوضة")), summary["rejected"]),
                (str(_("إجمالي الدولار")), summary["total_usd"]),
                (str(_("إجمالي الدينار")), summary["total_iqd"]),
            ],
            tables=[
                (
                    str(_("حسب الحالة")),
                    [str(_("الحالة")), str(_("العدد")), str(_("دولار")), str(_("دينار"))],
                    [[r["label"], r["count"], r["usd"], r["iqd"]] for r in summary["by_status"]],
                ),
                (
                    str(_("حسب طريقة الدفع")),
                    [str(_("الطريقة")), str(_("العدد")), str(_("دولار")), str(_("دينار"))],
                    [[r["method"], r["count"], r["usd"], r["iqd"]] for r in aggregates.by_method(queryset)],
                ),
            ],
            columns=COLUMNS,
            rows=self.serialise(queryset),
            # The whole reason this argument exists.
            masked=True,
        )

        record_audit(
            action=AuditAction.STATUS_CHANGE,
            target_type="transactions.Request",
            target_id="report",
            actor=request.user,
            request=request,
            after={
                "event": "report_exported",
                "surface": "merchant",
                "merchant": str(self.merchant),
                "rows": summary["count"],
            },
        )
        return _xlsx_response(payload, "maxpay-merchant-report")


__all__ = [
    "COLUMNS",
    "MerchantReportExportView",
    "MerchantReportRowSerializer",
    "MerchantReportView",
]
