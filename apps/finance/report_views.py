"""The Finance report and its export (spec §9) — build-order step 16.

Two views over one query. The screen and the workbook are built from the same
filtered queryset and the same column list, so a figure that appears on both
cannot differ between them — the export is a rendering of the report, not a
second implementation of it.

**Two gates, not one.** Reading a report needs what the queue needs; taking a
copy of it out of the building needs ``transactions.export_reports``. They are
genuinely different powers: a spreadsheet on a laptop is outside every control
this system has, and spec §9 gives ``finance_admin`` the say over who may make
one.

**Client identity is a third gate on top of both.** The four identity columns
are appended only for a user holding ``accounts.view_client_identity``, exactly
as the queue's client column is. A Finance user without it exports what a
merchant would see, and the export is audited either way.
"""

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from apps.core.choices import AuditAction
from apps.core.services import record_audit
from apps.reports import aggregates
from apps.reports.filters import ReportFilter
from apps.reports.workbook import (
    IQD_FORMAT,
    RATE_FORMAT,
    USD_FORMAT,
    Column,
    build,
    cells_for,
)
from apps.transactions.models import Request

from .mixins import FinancePanelMixin
from .queue_views import PERM_VIEW_IDENTITY
from .report_forms import FinanceReportFilterForm

#: Spec §9, and the reason this is its own permission: a report on a screen is
#: inside the system, and a workbook on somebody's laptop is not.
PERM_EXPORT = "transactions.export_reports"

#: What every Finance report carries, in reading order. Identity columns are
#: appended to this, never interleaved, so a file exported without them has the
#: same shape as one exported with them minus a suffix.
BASE_COLUMNS = [
    Column("reference", _("المرجع"), width=14),
    Column("title", _("العنوان"), width=22),
    Column("type", _("النوع"), width=10),
    Column("status", _("الحالة"), width=18),
    Column("submitted_at", _("وقت التقديم"), width=18),
    Column("method", _("طريقة الدفع"), width=16),
    Column("merchant_selected", _("التاجر المختار"), width=18),
    Column("merchant_assigned", _("التاجر المُسند"), width=18),
    Column("amount_usd", _("المبلغ بالدولار"), width=15, number_format=USD_FORMAT),
    Column("amount_iqd", _("المبلغ بالدينار"), width=17, number_format=IQD_FORMAT, decimals=0),
    Column("rate_applied", _("السعر المُلتقط"), width=14, number_format=RATE_FORMAT, decimals=0),
    Column("commission_applied", _("العمولة"), width=13, number_format=IQD_FORMAT, decimals=0),
    Column("wallet_number", _("المحفظة"), width=18),
    Column("destination_account", _("حساب الوجهة"), width=20),
    Column("assigned_at", _("وقت الإسناد"), width=18),
    Column("merchant_actioned_at", _("وقت تنفيذ التاجر"), width=18),
    Column("closed_at", _("وقت الإغلاق"), width=18),
    # Finance review 3.2 and 3.3: the two questions a monthly review is
    # actually made of — how long, and who.
    Column("resolved_at", _("وقت الحسم"), width=18),
    Column("elapsed", _("المدة"), width=12),
    Column("handled_by", _("آخر من عالجه"), width=20),
    Column("submitted_amount_usd", _("المطلوب أصلًا"), width=14, number_format=USD_FORMAT),
]

IDENTITY_COLUMNS = [
    Column("client_name", _("اسم العميل"), width=22),
    Column("client_email", _("بريد العميل"), width=24),
    Column("client_account", _("رقم حساب العميل"), width=18),
    Column("client_b2core", _("معرّف B2CORE"), width=20),
]


def columns_for(user) -> list[Column]:
    if user.has_perm(PERM_VIEW_IDENTITY):
        return [*BASE_COLUMNS, *IDENTITY_COLUMNS]
    return list(BASE_COLUMNS)


def row_for(request_obj: Request, *, with_identity: bool) -> dict:
    """One request as a flat dict keyed the way the columns read it."""
    row = {
        "reference": request_obj.public_ref,
        "type": str(request_obj.get_type_display()),
        "status": str(request_obj.get_status_display()),
        "submitted_at": request_obj.submitted_at,
        "method": str(request_obj.payment_method),
        "merchant_selected": str(request_obj.merchant_selected) if request_obj.merchant_selected_id else "",
        "merchant_assigned": str(request_obj.merchant_assigned) if request_obj.merchant_assigned_id else "",
        "amount_usd": request_obj.amount_usd,
        "amount_iqd": request_obj.amount_iqd,
        "rate_applied": request_obj.rate_applied,
        "commission_applied": request_obj.commission_applied,
        "wallet_number": request_obj.wallet_number_snapshot,
        "destination_account": request_obj.destination_account,
        "assigned_at": request_obj.assigned_at,
        "merchant_actioned_at": request_obj.merchant_actioned_at,
        "closed_at": request_obj.closed_at,
        "title": request_obj.display_title,
        "resolved_at": request_obj.resolved_at,
        "elapsed": request_obj.elapsed_display,
        "handled_by": str(request_obj.handled_by) if request_obj.handled_by_id else "",
        "submitted_amount_usd": request_obj.submitted_amount_usd,
    }
    if with_identity:
        client = request_obj.client
        row.update({
            "client_name": client.display_name if client else "",
            "client_email": client.email if client else "",
            "client_account": client.account_number if client else "",
            "client_b2core": client.b2core_id if client else "",
        })
    return row


def base_queryset():
    return Request.objects.select_related(
        "client", "payment_method", "merchant_selected", "merchant_assigned", "handled_by"
    ).order_by("-submitted_at", "-id")


class FinanceReportMixin(FinancePanelMixin):
    nav_section = "reports"

    def get_filter_form(self) -> FinanceReportFilterForm:
        if not hasattr(self, "_form"):
            can_see_identity = self.request.user.has_perm(PERM_VIEW_IDENTITY)
            # Finance review 4.3: asking for one client's report is an identity
            # operation. Refused outright rather than dropped, because silently
            # returning *every* client's rows to someone who asked for one
            # client's is a worse answer than saying no.
            if self.request.GET.get("client") and not can_see_identity:
                raise PermissionDenied(_("لا تملك صلاحية الاطلاع على هوية العميل."))
            form = FinanceReportFilterForm(
                self.request.GET or None, can_see_identity=can_see_identity
            )
            form.is_valid()  # every field is optional; this populates cleaned_data
            self._form = form
        return self._form

    def get_report_filter(self) -> ReportFilter:
        return ReportFilter.from_form(self.get_filter_form())

    def get_rows_queryset(self):
        return self.get_report_filter().apply(base_queryset())


class FinanceReportView(FinanceReportMixin, TemplateView):
    """The report on screen. Same numbers the workbook carries."""

    template_name = "finance/report.html"

    #: A report is a summary; the row table under it is a sample, not the whole
    #: history. Anyone who wants every row wants the file.
    preview_limit = 100

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        queryset = self.get_rows_queryset()
        report_filter = self.get_report_filter()

        context["filter_form"] = self.get_filter_form()
        context["report_filter"] = report_filter
        context["filter_pairs"] = report_filter.describe()
        context["summary"] = aggregates.summarise(queryset)
        context["by_merchant"] = aggregates.by_merchant(queryset)
        context["by_method"] = aggregates.by_method(queryset)
        context["can_export"] = user.has_perm(PERM_EXPORT)
        context["can_see_identity"] = user.has_perm(PERM_VIEW_IDENTITY)
        context["columns"] = columns_for(user)
        context["preview_limit"] = self.preview_limit
        rows = [
            row_for(obj, with_identity=context["can_see_identity"])
            for obj in queryset[: self.preview_limit]
        ]
        context["rows"] = rows
        context["preview"] = cells_for(rows, context["columns"])
        context["querystring"] = self.request.GET.urlencode()
        return context


class FinanceReportExportView(FinanceReportMixin, TemplateView):
    """The same report as a file, behind its own permission."""

    def get(self, request, *args, **kwargs):
        if not request.user.has_perm(PERM_EXPORT):
            raise PermissionDenied(_("لا تملك صلاحية تصدير التقارير."))

        queryset = self.get_rows_queryset()
        report_filter = self.get_report_filter()
        with_identity = request.user.has_perm(PERM_VIEW_IDENTITY)
        summary = aggregates.summarise(queryset)

        payload = build(
            title=str(_("تقرير الطلبات — لوحة المالية")),
            filters=report_filter.describe(),
            summary_rows=[
                (str(_("عدد الطلبات")), summary["count"]),
                (str(_("منها مرفوضة")), summary["rejected"]),
                (str(_("إجمالي الدولار")), summary["total_usd"]),
                (str(_("إجمالي الدينار")), summary["total_iqd"]),
                (str(_("إجمالي العمولة")), summary["total_commission_iqd"]),
            ],
            tables=[
                (
                    str(_("حسب الحالة")),
                    [str(_("الحالة")), str(_("العدد")), str(_("دولار")), str(_("دينار"))],
                    [[r["label"], r["count"], r["usd"], r["iqd"]] for r in summary["by_status"]],
                ),
                (
                    str(_("حسب التاجر")),
                    [str(_("التاجر")), str(_("العدد")), str(_("دولار")), str(_("دينار"))],
                    [
                        [r["merchant"] or str(_("لم يُسند")), r["count"], r["usd"], r["iqd"]]
                        for r in aggregates.by_merchant(queryset)
                    ],
                ),
                (
                    str(_("حسب طريقة الدفع")),
                    [str(_("الطريقة")), str(_("العدد")), str(_("دولار")), str(_("دينار"))],
                    [[r["method"], r["count"], r["usd"], r["iqd"]] for r in aggregates.by_method(queryset)],
                ),
            ],
            columns=columns_for(request.user),
            rows=[row_for(obj, with_identity=with_identity) for obj in queryset],
            # Finance is the identity-aware surface; the anonymity sweep would
            # refuse the very columns this report exists to carry.
            masked=False,
        )

        # Spec §11 wants the trail intact, and who took a copy of the client
        # list out of the building is exactly the kind of thing it is for.
        record_audit(
            action=AuditAction.STATUS_CHANGE,
            target_type="transactions.Request",
            target_id="report",
            actor=request.user,
            request=request,
            after={
                "event": "report_exported",
                "surface": "finance",
                "rows": summary["count"],
                "with_client_identity": with_identity,
                "filters": dict(report_filter.describe()),
            },
        )
        return _xlsx_response(payload, "maxpay-finance-report")


def _xlsx_response(payload: bytes, stem: str) -> HttpResponse:
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M")
    response = HttpResponse(
        payload,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # ASCII filename only: a Content-Disposition carrying Arabic is a header
    # every proxy on the path gets to mangle differently.
    response["Content-Disposition"] = f'attachment; filename="{stem}-{stamp}.xlsx"'
    response["Cache-Control"] = "no-store"
    return response
