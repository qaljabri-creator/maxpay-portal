"""The audit log viewer (spec §9, §11) — build-order step 12.

Three things are being protected, and the file is grouped by them:

* **who may look** — a Finance role plus ``accounts.view_audit_log``, and never
  a merchant (spec §2);
* **that looking is all anyone can do** — spec §11 requires no update or delete
  path anywhere, so every unsafe method has to bounce off both screens;
* **that what is shown is legible** — an entry that says "wallet changed" and
  nothing else is a record nobody can act on.
"""

from decimal import Decimal

from django.urls import reverse

from apps.accounts.permissions import MERCHANT_FORBIDDEN
from apps.core.choices import AuditAction
from apps.core.models import AuditLog
from apps.core.services import record_audit, snapshot
from apps.merchants.models import Wallet
from apps.rates.models import ExchangeRate, RateType
from apps.transactions.models import Request, RequestType

from . import audit
from .tests import FinancePanelTestCase


class AccessTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("finance:audit_list")

    def test_a_merchant_is_turned_away(self):
        self.login(self.merchant_user)

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_permission_is_one_a_merchant_can_never_hold(self):
        # Spec §2, enforced by the matrix rather than by this view.
        self.assertIn("core.view_auditlog", MERCHANT_FORBIDDEN)

    def test_finance_staff_may_read_it(self):
        self.login(self.staff)

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:login"), response["Location"])


class ReadOnlyTests(FinancePanelTestCase):
    """Spec §11: no update or delete path is exposed anywhere."""

    def setUp(self):
        super().setUp()
        self.login(self.admin)
        self.entry = record_audit(
            action=AuditAction.LOGIN, target=self.admin, actor=self.admin
        )

    def test_the_list_refuses_every_unsafe_method(self):
        url = reverse("finance:audit_list")

        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(self.client.put(url, {}).status_code, 405)

    def test_the_detail_refuses_every_unsafe_method(self):
        url = reverse("finance:audit_detail", args=[self.entry.pk])

        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)

    def test_the_model_itself_holds_the_line(self):
        from apps.core.models import AppendOnlyError

        with self.assertRaises(AppendOnlyError):
            self.entry.delete()


class FilterTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)
        self.url = reverse("finance:audit_list")

        self.rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1470.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
            set_by=self.admin,
        )
        self.rate_entry = record_audit(
            action=AuditAction.RATE_CHANGE,
            target=self.rate,
            actor=self.admin,
            after=snapshot(self.rate),
            ip="10.0.0.9",
        )
        self.login_entry = record_audit(
            action=AuditAction.LOGIN, target=self.staff, actor=self.staff, ip="10.0.0.4"
        )

    def entries(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return [row["entry"] for row in response.context["rows"]]

    def test_everything_shows_by_default_newest_first(self):
        found = self.entries()

        self.assertEqual(found[0].pk, self.login_entry.pk)
        self.assertIn(self.rate_entry.pk, [entry.pk for entry in found])

    def test_filtering_by_action(self):
        found = self.entries(action=AuditAction.RATE_CHANGE)

        self.assertEqual([entry.pk for entry in found], [self.rate_entry.pk])

    def test_filtering_by_target_type(self):
        found = self.entries(target_type="rates.ExchangeRate")

        self.assertEqual([entry.pk for entry in found], [self.rate_entry.pk])

    def test_filtering_by_actor(self):
        found = self.entries(actor=self.staff.pk)

        self.assertEqual([entry.pk for entry in found], [self.login_entry.pk])

    def test_searching_by_ip(self):
        found = self.entries(q="10.0.0.9")

        self.assertEqual([entry.pk for entry in found], [self.rate_entry.pk])

    def test_searching_by_actor_name(self):
        found = self.entries(q=self.staff.full_name)

        self.assertEqual([entry.pk for entry in found], [self.login_entry.pk])

    def test_a_date_window_that_excludes_everything_returns_nothing(self):
        self.assertEqual(self.entries(date_to="2020-01-01"), [])

    def test_a_reversed_date_window_is_read_the_way_it_was_meant(self):
        # Swapped rather than refused, as in the request queue: 2099 back to
        # 2020 is unambiguously "everything", not "nothing".
        found = self.entries(date_from="2099-01-01", date_to="2020-01-01")

        self.assertEqual(len(found), AuditLog.objects.count())

    def test_the_target_type_choices_come_from_what_the_log_holds(self):
        response = self.client.get(self.url)
        values = [
            value for value, _label in response.context["filter_form"].fields["target_type"].choices
        ]

        self.assertIn("rates.ExchangeRate", values)
        self.assertIn("accounts.User", values)


class TargetLinkTests(FinancePanelTestCase):
    """An entry that cannot be followed back to its subject is half a record."""

    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_a_request_links_to_its_queue_page_by_reference(self):
        from apps.accounts.models import Client

        client = Client.objects.create(b2core_id="b2c-1", display_name="عميل")
        req = Request.objects.create(
            type=RequestType.DEPOSIT,
            client=client,
            payment_method=self.method,
            merchant_selected=self.merchant,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("147000.00"),
            rate_applied=Decimal("1470.00"),
            commission_applied=Decimal("5000.00"),
        )
        record_audit(action=AuditAction.STATUS_CHANGE, target=req, actor=self.admin)

        row = self.client.get(reverse("finance:audit_list")).context["rows"][0]

        self.assertEqual(row["target"]["label"], req.public_ref)
        self.assertEqual(
            row["target"]["url"], reverse("finance:request_detail", args=[req.public_ref])
        )

    def test_a_wallet_links_to_the_merchant_that_holds_it(self):
        wallet = Wallet.objects.create(
            merchant_method=self.merchant_method, number="07701234567"
        )
        record_audit(action=AuditAction.WALLET_CHANGE, target=wallet, actor=self.admin)

        row = self.client.get(reverse("finance:audit_list")).context["rows"][0]

        self.assertIn("07701234567", row["target"]["label"])
        self.assertEqual(
            row["target"]["url"], reverse("finance:merchant_detail", args=[self.merchant.pk])
        )

    def test_a_deleted_target_still_renders_as_an_entry(self):
        record_audit(
            action=AuditAction.MERCHANT_CHANGE,
            target_type="merchants.Merchant",
            target_id=99999,
            actor=self.admin,
        )

        row = self.client.get(reverse("finance:audit_list")).context["rows"][0]

        self.assertIsNone(row["target"])
        self.assertEqual(row["entry"].target_id, "99999")

    def test_a_client_is_never_resolved_to_a_name(self):
        # The viewer's own permission says nothing about client identity, and
        # spec §2 is not something to leave to a label lookup.
        self.assertNotIn("accounts.Client", audit.RESOLVERS)


class DiffTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)
        self.wallet = Wallet.objects.create(
            merchant_method=self.merchant_method, number="07701234567", is_active=True
        )

    def entry_for(self, before, after):
        return record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=self.wallet,
            actor=self.admin,
            before=before,
            after=after,
        )

    def test_only_the_field_that_moved_is_marked(self):
        entry = self.entry_for(
            {"number": "07701234567", "is_active": True},
            {"number": "07701234567", "is_active": False},
        )

        rows = {row["field"]: row for row in audit.diff_rows(entry)}

        self.assertFalse(rows["number"]["changed"])
        self.assertTrue(rows["is_active"]["changed"])

    def test_fields_are_labelled_with_the_models_own_wording(self):
        entry = self.entry_for(None, {"is_active": False})

        self.assertEqual(audit.diff_rows(entry)[0]["label"], "نشطة")

    def test_booleans_read_as_words_not_as_python(self):
        entry = self.entry_for({"is_active": True}, {"is_active": False})
        rows = audit.diff_rows(entry)

        self.assertEqual(rows[0]["before"], "نعم")
        self.assertEqual(rows[0]["after"], "لا")

    def test_a_creation_marks_nothing_as_changed(self):
        # Every field would otherwise be "changed", which tells a reader
        # nothing they could not see from the action itself.
        entry = self.entry_for(None, {"number": "07701234567", "is_active": True})

        self.assertEqual(audit.changed_fields(entry), [])

    def test_a_key_that_is_not_a_field_still_shows(self):
        # Callers add context like "reason" to say why something happened.
        entry = self.entry_for({"is_active": True}, {"is_active": False, "reason": "manual"})

        rows = {row["field"]: row for row in audit.diff_rows(entry)}
        self.assertEqual(rows["reason"]["after"], "manual")

    def test_an_entry_with_no_snapshot_produces_no_rows(self):
        entry = record_audit(
            action=AuditAction.LOGIN, target=self.admin, actor=self.admin
        )

        self.assertEqual(audit.diff_rows(entry), [])

    def test_a_related_object_is_shown_by_its_own_repr(self):
        entry = self.entry_for(
            None, {"merchant_method": {"model": "merchants.MerchantMethod", "pk": 1, "repr": "تاجر أ · زين كاش"}}
        )

        self.assertEqual(audit.diff_rows(entry)[0]["after"], "تاجر أ · زين كاش")


class DetailPageTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)
        self.wallet = Wallet.objects.create(
            merchant_method=self.merchant_method, number="07701234567"
        )
        self.first = record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=self.wallet,
            actor=self.admin,
            after={"is_active": True},
        )
        self.second = record_audit(
            action=AuditAction.WALLET_CHANGE,
            target=self.wallet,
            actor=self.admin,
            before={"is_active": True},
            after={"is_active": False},
        )

    def test_the_page_lays_the_snapshot_out(self):
        response = self.client.get(reverse("finance:audit_detail", args=[self.second.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["rows"][0]["changed"])

    def test_it_offers_the_other_entries_on_the_same_target(self):
        response = self.client.get(reverse("finance:audit_detail", args=[self.second.pk]))

        neighbours = [entry.pk for entry in response.context["neighbours"]]
        self.assertEqual(neighbours, [self.first.pk])

    def test_an_unknown_entry_is_a_404(self):
        missing = AuditLog.objects.order_by("-pk").first().pk + 1000

        self.assertEqual(
            self.client.get(reverse("finance:audit_detail", args=[missing])).status_code, 404
        )
