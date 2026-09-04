"""Reporting and export (spec §8, §9) — build-order step 16.

The centre of gravity here is deliberate. Most of these cases open the exported
workbook and read its cells, rather than asserting on a response: a status code
and a content type say a file was produced, and say nothing at all about what is
inside it. Spec §2's guarantee is about what a merchant *receives*, and for an
export what they receive is the bytes.

So :class:`MerchantExportContentTests` downloads the file, parses it with
openpyxl, and walks every cell of every sheet looking for identity markers that
appear nowhere else in the fixtures. If any of them is in the workbook, the
guarantee is broken however the response was shaped.
"""

import io
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.reports import aggregates
from apps.reports.filters import ReportFilter
from apps.transactions.models import Request, RequestStatus, RequestType

#: Deliberately unmistakable, and spelled nowhere else in the fixtures — no
#: status label, no reference, no wallet number, no merchant name. A sweep for
#: these strings is therefore a genuine test rather than a coincidence.
IDENTITY_MARKERS = {
    "display_name": "زينب-الجبوري-QQZZ",
    "email": "zainab-qqzz@example.com",
    "account_number": "MX-QQZZ-90210",
    "b2core_id": "sub-qqzz-77771",
}

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def cells_of(payload: bytes) -> list[str]:
    """Every non-empty cell of every sheet, as text.

    Reads the actual file rather than the response it arrived in. That is the
    whole point of the sweep below: a workbook is what leaves the building.
    """
    book = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    values: list[str] = []
    for sheet in book.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for value in row:
                if value is not None and str(value).strip():
                    values.append(str(value))
    book.close()
    return values


class ReportTestCase(TestCase):
    """One desk, two merchants, and a spread of requests to report on."""

    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.other_user = make_user("other@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.other_method = PaymentMethod.objects.create(
            code="fastpay", caption_ar="فاست باي", caption_en="FastPay"
        )
        self.merchant = self.make_merchant("تاجر أ", self.method, self.merchant_user)
        self.other = self.make_merchant("تاجر ب", self.other_method, self.other_user)

        self.client_record = PortalClient.objects.create(**IDENTITY_MARKERS)

        now = timezone.now()
        self.deposit = self.make_request(
            status=RequestStatus.ASSIGNED, amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
        )
        self.confirmed = self.make_request(
            status=RequestStatus.MERCHANT_CONFIRMED, amount_usd=Decimal("250.00"),
            amount_iqd=Decimal("363750.00"),
        )
        self.rejected = self.make_request(
            status=RequestStatus.REJECTED, amount_usd=Decimal("999.00"),
            amount_iqd=Decimal("1453545.00"), rejection_reason="الإيصال غير مقروء",
        )
        self.withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL, status=RequestStatus.MERCHANT_PAID,
            amount_usd=Decimal("400.00"), amount_iqd=Decimal("582000.00"),
            destination_account="4257880011937742", wallet_number_snapshot="",
        )
        self.elsewhere = self.make_request(
            status=RequestStatus.ASSIGNED, amount_usd=Decimal("70.00"),
            amount_iqd=Decimal("101850.00"), merchant=self.other,
            method=self.other_method,
        )
        self.old = self.make_request(
            status=RequestStatus.CLOSED, amount_usd=Decimal("55.00"),
            amount_iqd=Decimal("80025.00"),
        )
        Request.objects.filter(pk=self.old.pk).update(
            submitted_at=now - timedelta(days=90)
        )
        self.old.refresh_from_db()

    def make_merchant(self, name, method, user) -> Merchant:
        merchant = Merchant.objects.create(name=name, user=user)
        link = MerchantMethod.objects.create(merchant=merchant, payment_method=method)
        Wallet.objects.create(merchant_method=link, number="07700000001")
        return merchant

    def make_request(self, *, merchant=None, method=None, **overrides) -> Request:
        merchant = merchant or self.merchant
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=method or self.method,
            merchant_selected=merchant,
            merchant_assigned=merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.ASSIGNED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def login(self, user):
        verify_otp(self.client, user)
        return user

    @staticmethod
    def grant(role, codename):
        Group.objects.get(name=role).permissions.add(
            Permission.objects.get(codename=codename)
        )

    @staticmethod
    def revoke(role, codename):
        Group.objects.get(name=role).permissions.remove(
            Permission.objects.get(codename=codename)
        )


# ---------------------------------------------------------------------------
# The filter and the arithmetic
# ---------------------------------------------------------------------------


class FilterTests(ReportTestCase):
    def test_a_blank_filter_narrows_nothing(self):
        self.assertEqual(
            ReportFilter().apply(Request.objects.all()).count(), Request.objects.count()
        )

    def test_the_date_range_is_inclusive_at_both_ends(self):
        today = timezone.localtime(self.deposit.submitted_at).date()
        narrowed = ReportFilter(date_from=today, date_to=today).apply(Request.objects.all())

        self.assertIn(self.deposit, narrowed)
        self.assertNotIn(self.old, narrowed)

    def test_a_backwards_range_is_swapped_rather_than_refused(self):
        from apps.finance.report_forms import FinanceReportFilterForm

        form = FinanceReportFilterForm({"date_from": "2026-08-20", "date_to": "2026-08-01"})
        form.is_valid()

        self.assertEqual(str(form.cleaned_data["date_from"]), "2026-08-01")
        self.assertEqual(str(form.cleaned_data["date_to"]), "2026-08-20")

    def test_a_status_group_stands_for_its_whole_set(self):
        narrowed = ReportFilter(status="awaiting_merchant").apply(Request.objects.all())
        self.assertEqual(
            set(narrowed.values_list("status", flat=True)), {RequestStatus.ASSIGNED}
        )

    def test_filtering_by_merchant_matches_both_roles_for_finance(self):
        self.deposit.merchant_assigned = self.other
        self.deposit.save(update_fields=["merchant_assigned"])

        narrowed = ReportFilter(merchant=self.other, match_merchant_either_side=True).apply(
            Request.objects.all()
        )

        self.assertIn(self.deposit, narrowed)
        self.assertIn(self.elsewhere, narrowed)

    def test_filtering_by_type_and_method_compose(self):
        narrowed = ReportFilter(type=RequestType.DEPOSIT, method=self.other_method).apply(
            Request.objects.all()
        )
        self.assertEqual(list(narrowed), [self.elsewhere])


class AggregateTests(ReportTestCase):
    def test_rejected_requests_are_counted_but_not_added_up(self):
        """No money moved on a rejection, and a total that includes one
        overstates every figure a desk reconciles against."""
        summary = aggregates.summarise(Request.objects.all())

        self.assertEqual(summary["count"], 6)
        self.assertEqual(summary["rejected"], 1)
        self.assertNotIn(self.rejected.amount_usd, [summary["total_usd"]])
        self.assertEqual(
            summary["total_usd"],
            Decimal("100.00") + Decimal("250.00") + Decimal("400.00")
            + Decimal("70.00") + Decimal("55.00"),
        )

    def test_the_rejected_status_still_appears_in_the_breakdown(self):
        summary = aggregates.summarise(Request.objects.all())
        keys = {row["key"] for row in summary["by_status"]}
        self.assertIn(RequestStatus.REJECTED, keys)

    def test_the_totals_follow_the_filter(self):
        narrowed = ReportFilter(type=RequestType.WITHDRAWAL).apply(Request.objects.all())
        summary = aggregates.summarise(narrowed)

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["total_usd"], Decimal("400.00"))

    def test_unrouted_requests_are_grouped_rather_than_dropped(self):
        """"How much is sitting unrouted" is exactly what this table is asked
        at month end."""
        self.make_request(merchant_assigned=None, status=RequestStatus.SUBMITTED)

        rows = aggregates.by_merchant(Request.objects.all())

        self.assertIn("", [row["merchant"] for row in rows])

    def test_an_empty_report_totals_zero_rather_than_none(self):
        summary = aggregates.summarise(Request.objects.none())
        self.assertEqual(summary["total_usd"], Decimal("0.00"))
        self.assertEqual(summary["total_iqd"], Decimal("0.00"))


# ---------------------------------------------------------------------------
# The Finance surface
# ---------------------------------------------------------------------------


class FinanceReportAccessTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("finance:report")
        self.export_url = reverse("finance:report_export")

    def test_finance_reaches_the_report(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_merchant_never_reaches_the_finance_report(self):
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(self.export_url).status_code, 403)

    def test_export_needs_its_own_permission(self):
        """Reading a report is inside the system; a workbook on a laptop is not."""
        self.login(self.staff)

        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.get(self.export_url).status_code, 403)

    def test_the_permission_can_be_delegated_to_staff(self):
        self.grant(Role.FINANCE_STAFF, "export_reports")
        self.login(self.staff)

        self.assertEqual(self.client.get(self.export_url).status_code, 200)

    def test_an_admin_holds_it_by_default(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(self.export_url).status_code, 200)

    def test_the_export_button_is_hidden_from_someone_who_cannot_use_it(self):
        self.login(self.staff)
        response = self.client.get(self.url)
        self.assertNotContains(response, reverse("finance:report_export"))


class FinanceExportContentTests(ReportTestCase):
    """What is actually in the Finance workbook."""

    def setUp(self):
        super().setUp()
        self.export_url = reverse("finance:report_export")
        self.login(self.admin)

    def download(self, **params) -> bytes:
        response = self.client.get(self.export_url, params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], XLSX_TYPE)
        self.assertIn("attachment;", response["Content-Disposition"])
        return response.content

    def test_the_file_is_a_readable_workbook_with_both_sheets(self):
        book = load_workbook(io.BytesIO(self.download()), read_only=True)
        self.assertEqual(len(book.worksheets), 2)
        book.close()

    def test_every_request_has_a_row(self):
        payload = self.download()
        values = cells_of(payload)

        for request_obj in Request.objects.all():
            self.assertIn(request_obj.public_ref, values)

    def test_the_filter_narrows_the_file_as_well_as_the_screen(self):
        payload = self.download(type=RequestType.WITHDRAWAL)
        values = cells_of(payload)

        self.assertIn(self.withdrawal.public_ref, values)
        self.assertNotIn(self.deposit.public_ref, values)

    def test_the_file_states_the_question_it_answers(self):
        """A spreadsheet with no record of what was filtered is one somebody
        reads the wrong way three months later."""
        payload = self.download(type=RequestType.WITHDRAWAL)
        values = cells_of(payload)

        self.assertIn("سحب", values)

    def test_an_identity_aware_user_gets_the_client_columns(self):
        values = cells_of(self.download())

        for marker in IDENTITY_MARKERS.values():
            self.assertIn(marker, values)

    def test_a_user_without_the_identity_permission_gets_a_file_without_it(self):
        """Spec §2's gate applies to the file exactly as it applies to the queue."""
        self.revoke(Role.FINANCE_ADMIN, "view_client_identity")
        self.client.logout()
        self.login(self.admin)

        values = cells_of(self.download())

        for marker in IDENTITY_MARKERS.values():
            self.assertNotIn(marker, values)
        # Still a real report, not an empty one.
        self.assertIn(self.deposit.public_ref, values)

    def test_the_export_is_audited_with_what_left_the_building(self):
        self.download()

        entry = AuditLog.objects.filter(target_id="report").latest("pk")
        self.assertEqual(entry.after["event"], "report_exported")
        self.assertEqual(entry.after["surface"], "finance")
        self.assertTrue(entry.after["with_client_identity"])
        self.assertEqual(entry.actor_id, self.admin.pk)

    def test_amounts_arrive_as_numbers_rather_than_text(self):
        """A column of strings is a column nobody can sum in Excel."""
        book = load_workbook(io.BytesIO(self.download()), read_only=True, data_only=True)
        sheet = book.worksheets[1]
        header = [cell for cell in next(sheet.iter_rows(values_only=True))]
        index = header.index("المبلغ بالدولار")

        found = []
        for row in sheet.iter_rows(min_row=2, values_only=True):
            found.append(row[index])
        book.close()

        self.assertTrue(found)
        self.assertTrue(all(isinstance(value, (int, float)) for value in found), found)


# ---------------------------------------------------------------------------
# The merchant surface — the one that matters most
# ---------------------------------------------------------------------------


class MerchantReportAccessTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("merchant_panel:report")
        self.export_url = reverse("merchant_panel:report_export")

    def test_a_merchant_reaches_their_own_report(self):
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_finance_is_refused_the_merchant_report(self):
        """This panel is one merchant's worklist; there is no Finance version."""
        self.login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(self.export_url).status_code, 403)

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)


class MerchantExportContentTests(ReportTestCase):
    """Spec §2, asserted against the bytes that leave the building.

    Every case here parses the workbook. A 200 with the right content type
    proves a file was produced and proves nothing about what is in it, and for
    an export what a merchant receives *is* the file.
    """

    def setUp(self):
        super().setUp()
        self.export_url = reverse("merchant_panel:report_export")
        self.login(self.merchant_user)

    def download(self, **params) -> bytes:
        response = self.client.get(self.export_url, params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], XLSX_TYPE)
        return response.content

    # -- the guarantee -----------------------------------------------------

    def test_no_cell_of_the_workbook_names_the_client(self):
        values = cells_of(self.download())

        for field, marker in IDENTITY_MARKERS.items():
            with self.subTest(field=field):
                self.assertNotIn(
                    marker,
                    values,
                    f"the merchant's exported workbook leaks the client's {field} (spec §2).",
                )

    def test_no_cell_contains_an_identity_marker_as_a_substring_either(self):
        """A name concatenated into a longer string would pass an equality
        sweep. This is the one that catches it."""
        blob = " ".join(cells_of(self.download()))

        for marker in IDENTITY_MARKERS.values():
            self.assertNotIn(marker, blob)

    def test_no_column_header_names_client_identity(self):
        book = load_workbook(io.BytesIO(self.download()), read_only=True)
        headers = [cell for cell in next(book.worksheets[1].iter_rows(values_only=True))]
        book.close()

        forbidden = ("العميل", "بريد", "حساب العميل", "B2CORE")
        for header in headers:
            for token in forbidden:
                self.assertNotIn(token, str(header))

    def test_the_workbook_still_carries_what_a_merchant_is_entitled_to(self):
        """The guarantee is that identity is absent, not that the file is."""
        values = cells_of(self.download())

        self.assertIn(self.deposit.public_ref, values)
        self.assertIn("07700000001", values)
        self.assertIn("4257880011937742", values)

    # -- the scope ---------------------------------------------------------

    def test_it_carries_only_requests_routed_to_this_merchant(self):
        values = cells_of(self.download())

        self.assertIn(self.deposit.public_ref, values)
        self.assertNotIn(
            self.elsewhere.public_ref,
            values,
            "the export reached a request routed to another merchant (spec §8).",
        )

    def test_a_reroute_takes_the_request_out_of_the_file(self):
        self.deposit.merchant_assigned = self.other
        self.deposit.save(update_fields=["merchant_assigned"])

        values = cells_of(self.download())

        self.assertNotIn(self.deposit.public_ref, values)

    def test_the_other_merchant_sees_their_own_and_only_their_own(self):
        self.client.logout()
        self.login(self.other_user)

        values = cells_of(self.download())

        self.assertIn(self.elsewhere.public_ref, values)
        self.assertNotIn(self.deposit.public_ref, values)

    # -- the filter --------------------------------------------------------

    def test_the_filter_applies_inside_the_scope_rather_than_widening_it(self):
        values = cells_of(self.download(status=""))

        self.assertNotIn(self.elsewhere.public_ref, values)

    def test_filtering_by_status_narrows_the_file(self):
        values = cells_of(self.download(status=RequestStatus.MERCHANT_CONFIRMED))

        self.assertIn(self.confirmed.public_ref, values)
        self.assertNotIn(self.deposit.public_ref, values)

    # -- the file is usable ------------------------------------------------

    def test_timestamps_arrive_as_dates_rather_than_iso_text(self):
        """The merchant's rows come out of the masked serializers, which
        produce ISO strings. A column of text is unsortable in Excel and reads
        as `2026-08-24T10:38:49.407390+00:00` on screen."""
        import datetime as dt

        book = load_workbook(io.BytesIO(self.download()), read_only=True, data_only=True)
        sheet = book.worksheets[1]
        header = list(next(sheet.iter_rows(values_only=True)))
        index = header.index("وقت التقديم")
        found = [row[index] for row in sheet.iter_rows(min_row=2, values_only=True)]
        book.close()

        self.assertTrue(found)
        self.assertTrue(
            all(isinstance(value, dt.datetime) for value in found), found
        )

    def test_amounts_arrive_as_numbers(self):
        book = load_workbook(io.BytesIO(self.download()), read_only=True, data_only=True)
        sheet = book.worksheets[1]
        header = list(next(sheet.iter_rows(values_only=True)))
        index = header.index("المبلغ بالدينار")
        found = [row[index] for row in sheet.iter_rows(min_row=2, values_only=True)]
        book.close()

        self.assertTrue(found)
        self.assertTrue(all(isinstance(value, (int, float)) for value in found), found)

    # -- the audit ---------------------------------------------------------

    def test_the_export_is_audited(self):
        self.download()

        entry = AuditLog.objects.filter(target_id="report").latest("pk")
        self.assertEqual(entry.after["surface"], "merchant")
        self.assertEqual(entry.actor_id, self.merchant_user.pk)


class ExportGuardTests(ReportTestCase):
    """The writer's own refusal, exercised directly.

    The merchant surface cannot produce an identifying row — its serializers
    will not let it — so the guard is provoked here with a row built by hand,
    which is what a future careless caller would do.
    """

    def test_the_writer_refuses_a_masked_workbook_carrying_an_identifying_key(self):
        from apps.merchant_panel.anonymity import AnonymityError
        from apps.reports.workbook import Column, build

        with self.assertRaises(AnonymityError):
            build(
                title="t",
                filters=[],
                summary_rows=[],
                tables=[],
                columns=[Column("reference", "المرجع")],
                rows=[{"reference": "MP-1", "client_email": "zainab@example.com"}],
                masked=True,
            )

    def test_it_refuses_an_identifying_column_even_with_clean_rows(self):
        from apps.merchant_panel.anonymity import AnonymityError
        from apps.reports.workbook import Column, build

        with self.assertRaises(AnonymityError):
            build(
                title="t",
                filters=[],
                summary_rows=[],
                tables=[],
                columns=[Column("client_name", "اسم العميل")],
                rows=[{"reference": "MP-1"}],
                masked=True,
            )

    def test_the_same_rows_are_allowed_through_for_finance(self):
        """The rule is about which surface the file leaves by, not about the
        data existing."""
        from apps.reports.workbook import Column, build

        payload = build(
            title="t",
            filters=[],
            summary_rows=[],
            tables=[],
            columns=[Column("client_email", "بريد العميل")],
            rows=[{"client_email": "zainab@example.com"}],
            masked=False,
        )
        self.assertIn("zainab@example.com", cells_of(payload))

    def test_nothing_is_written_when_the_guard_refuses(self):
        """The check runs before the workbook exists, so there is no
        half-written file to leak."""
        from apps.merchant_panel.anonymity import AnonymityError
        from apps.reports.workbook import Column, build

        try:
            build(
                title="t", filters=[], summary_rows=[], tables=[],
                columns=[Column("reference", "المرجع")],
                rows=[{"reference": "MP-1", "b2core_id": "sub-1"}],
                masked=True,
            )
        except AnonymityError as exc:
            self.assertIn("spec §2", str(exc))
        else:
            self.fail("the guard let an identifying row through")
