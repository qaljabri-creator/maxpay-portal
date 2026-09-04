"""One client's history, and finding a request by what it is worth.

Finance review of 24 Aug 2026, phase 4. Three things, and the same permission
runs through all of them: ``accounts.view_client_identity`` is what separates
"Finance" from "Finance, allowed to know who this is".

* **4.1** every request by one client, on one page, reachable from any of them;
* **4.2** search by reference, amount, email and account number;
* **4.3** that history exported through the report's existing gates rather than
  through an export of its own.

The amount half of 4.2 is tested twice over: once as a pure function, because
"is this string an amount" is the whole of it and a view test would only prove
the wiring, and once through the queue, because the wiring is the other half.
"""

from decimal import Decimal
from io import BytesIO

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.finance.search import as_amount, query
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import Request, RequestStatus, RequestType


class HistoryTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="تاجر أ", user=self.merchant_user)
        link = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )
        Wallet.objects.create(merchant_method=link, number="07700000001")

        self.zainab = PortalClient.objects.create(
            b2core_id="sub-1",
            display_name="زينب الجبوري",
            email="zainab@example.com",
            account_number="MX-90210",
        )
        self.omar = PortalClient.objects.create(
            b2core_id="sub-2",
            display_name="عمر السامرائي",
            email="omar@example.com",
            account_number="MX-90211",
        )

        self.first = self.make_request(amount_usd=Decimal("100.00"))
        self.second = self.make_request(
            amount_usd=Decimal("250.00"),
            amount_iqd=Decimal("363250.00"),
            status=RequestStatus.CLOSED,
        )
        self.omars = self.make_request(client=self.omar, amount_usd=Decimal("40.00"))

    def make_request(self, **overrides) -> Request:
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.zainab,
            payment_method=self.method,
            merchant_selected=self.merchant,
            merchant_assigned=self.merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.SUBMITTED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def login(self, user):
        verify_otp(self.client, user)
        return user

    @staticmethod
    def revoke(role, codename):
        Group.objects.get(name=role).permissions.remove(
            Permission.objects.get(codename=codename)
        )

    def history_url(self, client_record=None):
        return reverse("finance:client_history", args=[(client_record or self.zainab).pk])


# ---------------------------------------------------------------------------
# 4.1 — the history page
# ---------------------------------------------------------------------------


class ClientHistoryTests(HistoryTestCase):
    def test_it_lists_every_request_by_that_client(self):
        self.login(self.admin)
        body = self.client.get(self.history_url()).content.decode()

        self.assertIn(self.first.public_ref, body)
        self.assertIn(self.second.public_ref, body)

    def test_it_lists_nobody_else_s(self):
        self.login(self.admin)
        body = self.client.get(self.history_url()).content.decode()
        self.assertNotIn(self.omars.public_ref, body)

    def test_the_totals_are_over_the_whole_history_not_the_page(self):
        self.login(self.admin)
        summary = self.client.get(self.history_url()).context["summary"]

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["total_usd"], Decimal("350.00"))

    def test_a_rejected_request_is_counted_but_not_added_up(self):
        self.make_request(
            amount_usd=Decimal("500.00"), status=RequestStatus.REJECTED
        )
        self.login(self.admin)
        summary = self.client.get(self.history_url()).context["summary"]

        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["total_usd"], Decimal("350.00"))

    def test_it_is_reachable_from_any_of_that_client_s_requests(self):
        """4.1 in as many words: *reachable from any of their requests*."""
        self.login(self.admin)
        for request_obj in (self.first, self.second):
            body = self.client.get(
                reverse("finance:request_detail", args=[request_obj.public_ref])
            ).content.decode()
            self.assertIn(self.history_url(), body)

    def test_the_queue_links_a_client_to_their_history(self):
        self.login(self.admin)
        body = self.client.get(reverse("finance:request_list")).content.decode()
        self.assertIn(self.history_url(), body)

    def test_it_does_not_repeat_the_client_in_every_row(self):
        """Every row is the same person; the column carries nothing.

        Counted before and after a third request rather than against a fixed
        number: the name legitimately appears in the heading and in the detail
        block, and what is being asserted is that it does not appear *per row*.
        """
        self.login(self.admin)
        before = self.client.get(self.history_url()).content.decode()
        self.make_request(amount_usd=Decimal("15.00"))
        after = self.client.get(self.history_url()).content.decode()

        name = self.zainab.display_name
        self.assertEqual(before.count(name), after.count(name))

    def test_an_unknown_client_is_a_404_not_a_blank_page(self):
        self.login(self.admin)
        self.assertEqual(
            self.client.get(reverse("finance:client_history", args=[999999])).status_code,
            404,
        )


class ClientHistoryAccessTests(HistoryTestCase):
    def test_finance_without_the_identity_permission_is_refused(self):
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        self.assertEqual(self.client.get(self.history_url()).status_code, 403)

    def test_there_is_no_masked_version_of_it(self):
        """The refusal is the whole answer: a page whose subject is the client
        has nothing left once the client is withheld."""
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        body = self.client.get(self.history_url()).content.decode()
        self.assertNotIn(self.zainab.email, body)
        self.assertNotIn(self.first.public_ref, body)

    def test_a_merchant_cannot_reach_it(self):
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(self.history_url()).status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.history_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:login"), response["Location"])

    def test_a_staff_account_that_still_holds_it_may_read(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(self.history_url()).status_code, 200)

    def test_the_link_is_absent_for_someone_who_may_not_follow_it(self):
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        body = self.client.get(
            reverse("finance:request_detail", args=[self.first.public_ref])
        ).content.decode()
        self.assertNotIn(self.history_url(), body)


# ---------------------------------------------------------------------------
# 4.2 — search
# ---------------------------------------------------------------------------


class AmountParsingTests(TestCase):
    def test_a_plain_figure(self):
        self.assertEqual(as_amount("100"), Decimal("100"))

    def test_a_figure_with_a_decimal_part(self):
        self.assertEqual(as_amount("99.50"), Decimal("99.50"))

    def test_thousands_separators_are_ignored(self):
        self.assertEqual(as_amount("145,500"), Decimal("145500"))

    def test_arabic_thousands_separators_too(self):
        self.assertEqual(as_amount("145٬500"), Decimal("145500"))

    def test_a_reference_is_not_an_amount(self):
        self.assertIsNone(as_amount("MX-0042"))

    def test_an_email_is_not_an_amount(self):
        self.assertIsNone(as_amount("zainab@example.com"))

    def test_infinity_is_not_an_amount(self):
        """Decimal parses it quite happily; a numeric column does not."""
        self.assertIsNone(as_amount("Infinity"))
        self.assertIsNone(as_amount("nan"))

    def test_a_figure_wider_than_the_column_is_refused(self):
        self.assertIsNone(as_amount("1e40"))
        self.assertIsNone(as_amount("99999999999999999999"))

    def test_three_decimal_places_is_not_a_stored_amount(self):
        self.assertIsNone(as_amount("100.005"))

    def test_a_negative_figure_is_refused(self):
        self.assertIsNone(as_amount("-100"))

    def test_an_empty_term_is_not_an_amount(self):
        self.assertIsNone(as_amount("   "))


class SearchQueryTests(TestCase):
    def test_identity_fields_are_absent_without_the_permission(self):
        rendered = str(query("zainab", with_identity=False))
        self.assertNotIn("client__email", rendered)

    def test_identity_fields_are_present_with_it(self):
        rendered = str(query("zainab", with_identity=True))
        self.assertIn("client__email", rendered)

    def test_a_reference_search_touches_no_amount_column(self):
        rendered = str(query("MX-0042", with_identity=True))
        self.assertNotIn("amount_usd", rendered)


class QueueSearchTests(HistoryTestCase):
    def refs_for(self, term):
        response = self.client.get(reverse("finance:request_list"), {"q": term, "status": ""})
        return {obj.public_ref for obj in response.context["requests"]}

    def test_by_reference(self):
        self.login(self.admin)
        self.assertEqual(self.refs_for(self.first.public_ref), {self.first.public_ref})

    def test_by_amount_in_dollars(self):
        self.login(self.admin)
        self.assertEqual(self.refs_for("250"), {self.second.public_ref})

    def test_by_amount_in_dinars(self):
        self.login(self.admin)
        self.assertEqual(self.refs_for("363250"), {self.second.public_ref})

    def test_by_amount_with_a_thousands_separator(self):
        self.login(self.admin)
        self.assertEqual(self.refs_for("363,250"), {self.second.public_ref})

    def test_by_the_amount_originally_asked_for(self):
        """After a correction the client rings up quoting the figure they
        typed, not the one that arrived (Finance review 3.1)."""
        corrected = self.make_request(
            amount_usd=Decimal("60.00"),
            submitted_amount_usd=Decimal("777.00"),
            submitted_amount_iqd=Decimal("1131390.00"),
        )
        self.login(self.admin)
        self.assertEqual(self.refs_for("777"), {corrected.public_ref})

    def test_by_client_email(self):
        self.login(self.admin)
        self.assertEqual(
            self.refs_for("zainab@example.com"),
            {self.first.public_ref, self.second.public_ref},
        )

    def test_by_account_number(self):
        self.login(self.admin)
        self.assertEqual(
            self.refs_for("MX-90211"), {self.omars.public_ref}
        )

    def test_email_search_returns_nothing_without_the_identity_permission(self):
        """Spec §2: matching on an email is a way of confirming one without
        ever displaying it."""
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        self.assertEqual(self.refs_for("zainab@example.com"), set())

    def test_amount_search_still_works_without_it(self):
        """An amount is not identity, and the desk still has to find the
        request."""
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        self.assertEqual(self.refs_for("250"), {self.second.public_ref})

    def test_a_term_that_is_neither_finds_nothing_rather_than_everything(self):
        self.login(self.admin)
        self.assertEqual(self.refs_for("لا يوجد"), set())


# ---------------------------------------------------------------------------
# 4.3 — the history in a report, through the export gates that already exist
# ---------------------------------------------------------------------------


class ClientReportTests(HistoryTestCase):
    def report(self, **params):
        return self.client.get(reverse("finance:report"), params)

    def export(self, **params):
        return self.client.get(reverse("finance:report_export"), params)

    def test_the_report_can_be_narrowed_to_one_client(self):
        self.login(self.admin)
        summary = self.report(client=self.zainab.pk).context["summary"]
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["total_usd"], Decimal("350.00"))

    def test_the_history_page_offers_that_report(self):
        self.login(self.admin)
        context = self.client.get(self.history_url()).context
        self.assertIn(f"client={self.zainab.pk}", context["report_url"])
        self.assertIn(f"client={self.zainab.pk}", context["export_url"])

    def test_the_filter_sheet_names_the_client(self):
        self.login(self.admin)
        book = load_workbook(BytesIO(self.export(client=self.zainab.pk).content))
        first = book[book.sheetnames[0]]
        text = "\n".join(
            str(cell.value) for row in first.iter_rows() for cell in row if cell.value
        )
        self.assertIn(self.zainab.display_name, text)

    def test_the_exported_rows_are_that_client_s_only(self):
        self.login(self.admin)
        book = load_workbook(BytesIO(self.export(client=self.zainab.pk).content))
        sheet = book[book.sheetnames[-1]]
        refs = {
            row[0]
            for row in sheet.iter_rows(min_row=2, max_col=1, values_only=True)
            if row[0]
        }
        self.assertEqual(refs, {self.first.public_ref, self.second.public_ref})

    def test_export_still_needs_its_own_permission(self):
        """4.3 asks for the *existing* gates, not a way around them."""
        self.revoke(Role.FINANCE_STAFF, "export_reports")
        self.login(self.staff)
        self.assertEqual(self.export(client=self.zainab.pk).status_code, 403)

    def test_asking_for_one_client_without_the_identity_permission_is_refused(self):
        """Refused rather than silently widened: returning every client's rows
        to somebody who asked for one client's is the worse answer."""
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        self.assertEqual(self.report(client=self.zainab.pk).status_code, 403)
        self.assertEqual(self.export(client=self.zainab.pk).status_code, 403)

    def test_an_unnarrowed_report_is_untouched_by_any_of_this(self):
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        self.assertEqual(self.report().status_code, 200)

    def test_the_hidden_field_is_absent_for_a_user_who_may_not_use_it(self):
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        self.assertNotIn("client", self.report().context["filter_form"].fields)

    def test_the_merchant_report_has_no_client_dimension_at_all(self):
        from apps.merchant_panel.report_forms import MerchantReportFilterForm

        self.assertNotIn("client", MerchantReportFilterForm().fields)

    def test_a_merchant_passing_client_in_the_query_is_simply_ignored(self):
        """Not an error, because the field does not exist on that surface —
        and narrowing is all it could ever have done anyway."""
        from apps.merchant_panel.report_forms import MerchantReportFilterForm
        from apps.reports.filters import ReportFilter

        form = MerchantReportFilterForm({"client": str(self.zainab.pk)})
        form.is_valid()
        self.assertIsNone(ReportFilter.from_form(form).client)
