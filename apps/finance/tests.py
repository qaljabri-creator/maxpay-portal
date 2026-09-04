"""Finance panel tests — merchant/method/wallet management (build-order step 3)
and exchange-rate management with history (step 4)."""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.portal.b2core import jwks
from apps.rates.models import ExchangeRate, RateType


class FinancePanelTestCase(TestCase):
    """Shared fixtures: an admin who can write, and a staff member who cannot."""

    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="تاجر أ")
        self.merchant_method = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )

    def login(self, user):
        verify_otp(self.client, user)
        return user


class AccessTests(FinancePanelTestCase):
    def test_merchant_cannot_open_the_finance_panel(self):
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(reverse("finance:dashboard")).status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("finance:merchant_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:login"), response["Location"])

    def test_finance_staff_can_read(self):
        self.login(self.staff)
        for name, args in [
            ("finance:dashboard", []),
            ("finance:merchant_list", []),
            ("finance:merchant_detail", [self.merchant.pk]),
            ("finance:payment_method_list", []),
            ("finance:rate_list", []),
        ]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)

    def test_finance_staff_cannot_write_by_default(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(reverse("finance:merchant_create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("finance:rate_create")).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("finance:merchant_toggle", args=[self.merchant.pk])).status_code,
            403,
        )

    def test_a_delegated_permission_lets_staff_write(self):
        """Spec §3 — a finance_admin grants staff permissions without a code change."""
        self.staff.user_permissions.add(
            Permission.objects.get(
                codename="manage_merchants", content_type__app_label="merchants"
            )
        )
        self.login(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get(reverse("finance:merchant_create")).status_code, 200)

    def test_read_only_staff_sees_no_write_controls(self):
        self.login(self.staff)
        body = self.client.get(reverse("finance:merchant_list")).content.decode()
        self.assertNotIn(reverse("finance:merchant_create"), body)


class MerchantManagementTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_creating_a_merchant_writes_an_audit_entry(self):
        response = self.client.post(
            reverse("finance:merchant_create"),
            {"name": "تاجر ب", "user": "", "is_active": "on", "notes": ""},
        )
        created = Merchant.objects.get(name="تاجر ب")
        self.assertRedirects(response, reverse("finance:merchant_detail", args=[created.pk]))
        self.assertTrue(
            AuditLog.objects.filter(
                action="merchant_change", target_id=str(created.pk), actor=self.admin
            ).exists()
        )

    def test_toggling_a_merchant_flips_it_and_is_audited(self):
        response = self.client.post(reverse("finance:merchant_toggle", args=[self.merchant.pk]))
        self.merchant.refresh_from_db()
        self.assertFalse(self.merchant.is_active)
        self.assertEqual(response.status_code, 302)

        entry = AuditLog.objects.filter(
            action="merchant_change", target_id=str(self.merchant.pk)
        ).first()
        self.assertEqual(entry.before["is_active"], True)
        self.assertEqual(entry.after["is_active"], False)

    def test_toggle_rejects_a_get(self):
        response = self.client.get(reverse("finance:merchant_toggle", args=[self.merchant.pk]))
        self.assertEqual(response.status_code, 405)

    def test_a_login_account_can_belong_to_only_one_merchant(self):
        Merchant.objects.create(name="تاجر ج", user=self.merchant_user)
        response = self.client.get(reverse("finance:merchant_create"))
        form = response.context["form"]
        self.assertNotIn(self.merchant_user, form.fields["user"].queryset)

    def test_only_merchant_role_accounts_are_offered_as_login(self):
        response = self.client.get(reverse("finance:merchant_create"))
        queryset = response.context["form"].fields["user"].queryset
        self.assertIn(self.merchant_user, queryset)
        self.assertNotIn(self.staff, queryset)
        self.assertNotIn(self.admin, queryset)

    def test_assigning_a_method_twice_is_rejected(self):
        response = self.client.post(
            reverse("finance:merchant_method_create", args=[self.merchant.pk]),
            {"payment_method": self.method.pk, "is_active": "on"},
        )
        # The already-assigned method is not even in the queryset.
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(
            MerchantMethod.objects.filter(merchant=self.merchant, payment_method=self.method).count(),
            1,
        )

    def test_assigning_a_new_method_works(self):
        other = PaymentMethod.objects.create(code="fib", caption_ar="بنك", caption_en="FIB")
        self.client.post(
            reverse("finance:merchant_method_create", args=[self.merchant.pk]),
            {"payment_method": other.pk, "is_active": "on"},
        )
        self.assertTrue(
            MerchantMethod.objects.filter(merchant=self.merchant, payment_method=other).exists()
        )


class MerchantB2CoreIdPanelTests(FinancePanelTestCase):
    """The identifier as Finance actually meets it: on the edit form, behind
    `manage_merchants`, and in the audit log afterwards."""

    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def payload(self, **overrides):
        data = {"name": "تاجر ب", "b2core_id": "", "user": "", "is_active": "on", "notes": ""}
        data.update(overrides)
        return data

    def test_it_is_offered_on_the_merchant_form(self):
        response = self.client.get(reverse("finance:merchant_create"))
        self.assertIn("b2core_id", response.context["form"].fields)

    def test_it_is_saved_and_appears_in_the_audit_entry(self):
        self.client.post(reverse("finance:merchant_create"), self.payload(b2core_id="B2C-4471"))

        created = Merchant.objects.get(name="تاجر ب")
        self.assertEqual(created.b2core_id, "B2C-4471")
        entry = AuditLog.objects.filter(
            action="merchant_change", target_id=str(created.pk)
        ).first()
        self.assertEqual(entry.after["b2core_id"], "B2C-4471")

    def test_a_change_is_audited_with_both_sides(self):
        """The point of auditing it: which identifier a payout was matched
        against, and who moved it."""
        self.client.post(
            reverse("finance:merchant_update", args=[self.merchant.pk]),
            self.payload(name=self.merchant.name, b2core_id="B2C-880"),
        )
        self.client.post(
            reverse("finance:merchant_update", args=[self.merchant.pk]),
            self.payload(name=self.merchant.name, b2core_id="B2C-991"),
        )

        entry = AuditLog.objects.filter(
            action="merchant_change", target_id=str(self.merchant.pk)
        ).first()
        self.assertEqual(entry.before["b2core_id"], "B2C-880")
        self.assertEqual(entry.after["b2core_id"], "B2C-991")
        self.assertEqual(entry.actor, self.admin)

    def test_leaving_it_empty_is_accepted(self):
        response = self.client.post(reverse("finance:merchant_create"), self.payload())
        created = Merchant.objects.get(name="تاجر ب")
        self.assertRedirects(response, reverse("finance:merchant_detail", args=[created.pk]))
        self.assertIsNone(created.b2core_id)

    def test_a_duplicate_is_refused_on_the_form_rather_than_crashing(self):
        Merchant.objects.create(name="تاجر ج", b2core_id="B2C-333")

        response = self.client.post(
            reverse("finance:merchant_create"), self.payload(b2core_id="B2C-333")
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("b2core_id", response.context["form"].errors)
        self.assertFalse(Merchant.objects.filter(name="تاجر ب").exists())
        # Named, because "already taken" does not tell an operator where to go
        # and "taken by تاجر ج" does — the value was almost certainly pasted
        # from that merchant's row.
        self.assertContains(response, "تاجر ج")

    def test_an_identifier_may_be_re_saved_on_the_merchant_that_holds_it(self):
        """The uniqueness check must not fire against the row being edited."""
        self.merchant.b2core_id = "B2C-444"
        self.merchant.save()

        response = self.client.post(
            reverse("finance:merchant_update", args=[self.merchant.pk]),
            self.payload(name=self.merchant.name, b2core_id="B2C-444"),
        )

        self.assertEqual(response.status_code, 302)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.b2core_id, "B2C-444")

    def test_a_second_merchant_may_also_be_left_empty(self):
        """The `unique` + `blank` trap, reached the way an operator would."""
        self.client.post(reverse("finance:merchant_create"), self.payload(name="تاجر د"))
        response = self.client.post(reverse("finance:merchant_create"), self.payload(name="تاجر هـ"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Merchant.objects.filter(b2core_id__isnull=True).count(), 3)

    def test_staff_without_the_permission_cannot_edit_it(self):
        """Spec §3 — it rides the merchant screen, so it rides its permission."""
        self.client.logout()
        self.login(self.staff)

        response = self.client.post(
            reverse("finance:merchant_update", args=[self.merchant.pk]),
            self.payload(name=self.merchant.name, b2core_id="B2C-777"),
        )

        self.assertEqual(response.status_code, 403)
        self.merchant.refresh_from_db()
        self.assertIsNone(self.merchant.b2core_id)

    def test_it_is_shown_on_the_detail_page(self):
        self.merchant.b2core_id = "B2C-2024"
        self.merchant.save()
        response = self.client.get(reverse("finance:merchant_detail", args=[self.merchant.pk]))
        self.assertContains(response, "B2C-2024")


class WalletManagementTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def _create_wallet(self, number, active=True):
        data = {"number": number, "label": "", "daily_cap": ""}
        if active:
            data["is_active"] = "on"
        return self.client.post(
            reverse("finance:wallet_create", args=[self.merchant_method.pk]), data
        )

    def test_adding_an_active_wallet_stands_down_the_previous_one(self):
        self._create_wallet("07700000001")
        first = Wallet.objects.get(number="07700000001")
        self._create_wallet("07700000002")

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(self.merchant_method.active_wallet.number, "07700000002")

    def test_the_supersession_is_audited_on_both_wallets(self):
        self._create_wallet("07700000001")
        first = Wallet.objects.get(number="07700000001")
        self._create_wallet("07700000002")
        second = Wallet.objects.get(number="07700000002")

        self.assertTrue(
            AuditLog.objects.filter(action="wallet_change", target_id=str(second.pk)).exists()
        )
        stood_down = AuditLog.objects.filter(
            action="wallet_change", target_id=str(first.pk)
        ).first()
        self.assertEqual(stood_down.after["reason"], "superseded_by")

    def test_activating_an_old_wallet_swaps_the_active_slot(self):
        self._create_wallet("07700000001")
        first = Wallet.objects.get(number="07700000001")
        self._create_wallet("07700000002")
        second = Wallet.objects.get(number="07700000002")

        self.client.post(reverse("finance:wallet_activate", args=[first.pk]))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.is_active)
        self.assertFalse(second.is_active)
        self.assertEqual(
            Wallet.objects.filter(merchant_method=self.merchant_method, is_active=True).count(), 1
        )

    def test_deactivating_leaves_the_method_with_no_active_wallet(self):
        self._create_wallet("07700000001")
        wallet = Wallet.objects.get(number="07700000001")

        self.client.post(reverse("finance:wallet_deactivate", args=[wallet.pk]))

        wallet.refresh_from_db()
        self.assertFalse(wallet.is_active)
        self.assertIsNotNone(wallet.deactivated_at)
        self.assertIsNone(self.merchant_method.active_wallet)

    def test_a_duplicate_number_under_the_same_method_is_rejected(self):
        self._create_wallet("07700000001")
        response = self._create_wallet("07700000001")
        self.assertEqual(response.status_code, 200)
        self.assertIn("number", response.context["form"].errors)
        self.assertEqual(Wallet.objects.filter(number="07700000001").count(), 1)

    def test_an_invalid_number_is_rejected(self):
        response = self._create_wallet("ليس رقمًا")
        self.assertEqual(response.status_code, 200)
        self.assertIn("number", response.context["form"].errors)

    def test_editing_a_wallet_does_not_touch_existing_request_snapshots(self):
        """Spec §5 — wallet_number_snapshot is frozen at submission."""
        from apps.accounts.models import Client as PortalClient
        from apps.transactions.models import Request, RequestType

        self._create_wallet("07700000001")
        wallet = Wallet.objects.get(number="07700000001")
        client_record = PortalClient.objects.create(b2core_id="b2c-500")
        request_row = Request.objects.create(
            type=RequestType.DEPOSIT,
            client=client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            wallet_number_snapshot=wallet.number,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145000.00"),
            rate_applied=Decimal("1450.00"),
        )

        self.client.post(
            reverse("finance:wallet_update", args=[wallet.pk]),
            {"number": "07700009999", "label": "معدّلة", "daily_cap": "", "is_active": "on"},
        )

        request_row.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(wallet.number, "07700009999")
        self.assertEqual(request_row.wallet_number_snapshot, "07700000001")

    def test_the_detail_page_marks_the_active_wallet(self):
        self._create_wallet("07700000001")
        body = self.client.get(
            reverse("finance:merchant_detail", args=[self.merchant.pk])
        ).content.decode()
        self.assertIn("07700000001", body)
        self.assertIn("المعروضة للعميل", body)


class PaymentMethodManagementTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_creating_a_payment_method(self):
        self.client.post(
            reverse("finance:payment_method_create"),
            {
                "code": "fastpay", "caption_ar": "فاست باي", "caption_en": "FastPay",
                "supports_deposit": "on", "sort_order": "5",
                "is_active": "on",
            },
        )
        self.assertTrue(PaymentMethod.objects.filter(code="fastpay").exists())

    def test_a_method_supporting_neither_direction_is_rejected(self):
        response = self.client.post(
            reverse("finance:payment_method_create"),
            {"code": "dead", "caption_ar": "لا شيء", "caption_en": "None", "sort_order": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(PaymentMethod.objects.filter(code="dead").exists())

    def test_the_code_is_frozen_after_creation(self):
        response = self.client.get(reverse("finance:payment_method_update", args=[self.method.pk]))
        self.assertTrue(response.context["form"].fields["code"].disabled)

        self.client.post(
            reverse("finance:payment_method_update", args=[self.method.pk]),
            {
                "code": "renamed", "caption_ar": "زين كاش", "caption_en": "ZainCash",
                "supports_deposit": "on", "supports_withdrawal": "on",
                "is_active": "on", "sort_order": "0",
            },
        )
        self.method.refresh_from_db()
        self.assertEqual(self.method.code, "zaincash")

    def test_toggling_a_method_is_audited(self):
        self.client.post(reverse("finance:payment_method_toggle", args=[self.method.pk]))
        self.method.refresh_from_db()
        self.assertFalse(self.method.is_active)
        self.assertTrue(
            AuditLog.objects.filter(action="merchant_change", target_id=str(self.method.pk)).exists()
        )


class ExchangeRateManagementTests(FinancePanelTestCase):
    """Build-order step 4 — rate management with history."""

    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def _set_rate(self, value, rate_type=RateType.DEPOSIT, commission="2000.00", when=None, note=""):
        """Post the rate form. ``when`` must be in the past for the rate to be
        in force immediately — a future ``effective_from`` is scheduled, not
        current, which is the behaviour spec §5 asks for."""
        moment = when or timezone.localtime()
        return self.client.post(
            reverse("finance:rate_create"),
            {
                "rate_type": rate_type,
                "iqd_per_usd": value,
                "commission_iqd_per_100usd": commission,
                "effective_from": moment.strftime("%Y-%m-%dT%H:%M"),
                "note": note,
            },
        )

    def test_setting_a_rate_creates_a_row_and_records_who_set_it(self):
        self._set_rate("1450.00")
        rate = ExchangeRate.objects.get()
        self.assertEqual(rate.iqd_per_usd, Decimal("1450.00"))
        self.assertEqual(rate.set_by, self.admin)

    def test_setting_a_new_rate_never_edits_the_old_one(self):
        """Spec §5 — each change is a new row; history is preserved."""
        self._set_rate("1450.00", when=timezone.localtime() - timezone.timedelta(hours=2))
        first = ExchangeRate.objects.get()
        self._set_rate("1500.00", when=timezone.localtime() - timezone.timedelta(minutes=1))

        self.assertEqual(ExchangeRate.objects.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.iqd_per_usd, Decimal("1450.00"))
        self.assertEqual(ExchangeRate.current(RateType.DEPOSIT).iqd_per_usd, Decimal("1500.00"))

    def test_the_rate_change_is_audited_with_the_superseded_value(self):
        self._set_rate("1450.00", when=timezone.localtime() - timezone.timedelta(hours=2))
        self._set_rate("1500.00", when=timezone.localtime() - timezone.timedelta(minutes=1))

        entry = AuditLog.objects.filter(action="rate_change").first()
        self.assertEqual(entry.before["iqd_per_usd"], "1450.00")
        self.assertEqual(entry.after["iqd_per_usd"], "1500.00")

    def test_deposit_and_withdrawal_rates_do_not_affect_each_other(self):
        self._set_rate("1450.00", RateType.DEPOSIT)
        self._set_rate("1470.00", RateType.WITHDRAWAL)
        self.assertEqual(ExchangeRate.current(RateType.DEPOSIT).iqd_per_usd, Decimal("1450.00"))
        self.assertEqual(ExchangeRate.current(RateType.WITHDRAWAL).iqd_per_usd, Decimal("1470.00"))

    def test_a_future_dated_rate_is_not_yet_in_force(self):
        self._set_rate("1450.00")
        self._set_rate("1600.00", when=timezone.localtime() + timezone.timedelta(days=1))
        self.assertEqual(ExchangeRate.current(RateType.DEPOSIT).iqd_per_usd, Decimal("1450.00"))

    def test_a_zero_rate_is_rejected(self):
        response = self._set_rate("0")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(ExchangeRate.objects.count(), 0)

    def test_the_history_page_marks_current_scheduled_and_superseded(self):
        self._set_rate("1450.00", when=timezone.localtime() - timezone.timedelta(hours=2), note="القديم")
        self._set_rate("1500.00", when=timezone.localtime() - timezone.timedelta(minutes=1), note="الحالي")
        self._set_rate("1600.00", when=timezone.localtime() + timezone.timedelta(days=1), note="المجدول")

        response = self.client.get(reverse("finance:rate_list"))
        body = response.content.decode()
        self.assertEqual(len(response.context["history"]), 3)
        self.assertIn("سارٍ الآن", body)
        self.assertIn("مجدول", body)
        self.assertIn("مُستبدل", body)

    def test_history_can_be_filtered_by_type(self):
        self._set_rate("1450.00", RateType.DEPOSIT)
        self._set_rate("1470.00", RateType.WITHDRAWAL)

        response = self.client.get(reverse("finance:rate_list"), {"rate_type": "withdrawal"})
        history = list(response.context["history"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].rate_type, RateType.WITHDRAWAL)

    def test_existing_requests_keep_their_snapshotted_rate(self):
        """Spec §9 — rate changes apply to new requests only."""
        from apps.accounts.models import Client as PortalClient
        from apps.transactions.models import Request, RequestType

        self._set_rate("1450.00", when=timezone.localtime() - timezone.timedelta(hours=2))
        client_record = PortalClient.objects.create(b2core_id="b2c-600")
        request_row = Request.objects.create(
            type=RequestType.DEPOSIT,
            client=client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145000.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("2000.00"),
        )

        self._set_rate("1900.00", when=timezone.localtime() - timezone.timedelta(minutes=1))

        request_row.refresh_from_db()
        self.assertEqual(request_row.rate_applied, Decimal("1450.00"))
        self.assertEqual(ExchangeRate.current(RateType.DEPOSIT).iqd_per_usd, Decimal("1900.00"))

    def test_the_dashboard_shows_the_rates_in_force(self):
        self._set_rate("1450.00", RateType.DEPOSIT)
        response = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(response.context["deposit_rate"].iqd_per_usd, Decimal("1450.00"))
        self.assertIsNone(response.context["withdrawal_rate"])


class DashboardTests(FinancePanelTestCase):
    def test_methods_without_an_active_wallet_are_flagged(self):
        self.login(self.admin)
        response = self.client.get(reverse("finance:dashboard"))
        self.assertIn(self.merchant_method, list(response.context["gaps"]))

        Wallet.objects.create(merchant_method=self.merchant_method, number="07700000001")
        response = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(list(response.context["gaps"]), [])


B2CORE_SCREEN_SETTINGS = dict(
    B2CORE_ORIGIN="https://portal.example.com",
    B2CORE_JWKS_URL="https://api.example.com/.well-known/jwks.json",
    B2CORE_JWT_ISSUER="https://api.example.com/",
    B2CORE_JWT_AUDIENCE="maxpay",
)


@override_settings(**B2CORE_SCREEN_SETTINGS)
class B2CoreIntegrationScreenTests(FinancePanelTestCase):
    """The read-only integration screen (spec §4, §9).

    It exists to answer "is B2CORE working and what is it pointed at" without
    a shell on the server, so the tests are about exactly two things: that it
    tells the truth, and that it cannot be used to change anything.
    """

    def setUp(self):
        super().setUp()
        jwks.reset_cache()
        self.addCleanup(jwks.reset_cache)
        self.url = reverse("finance:b2core_integration")

    def test_it_shows_the_configured_endpoint_issuer_and_audience(self):
        self.login(self.admin)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://api.example.com/.well-known/jwks.json")
        self.assertContains(response, "maxpay")
        self.assertContains(response, "https://portal.example.com")

    def test_finance_staff_may_read_it(self):
        """No permission beyond the Finance role: nothing here is a credential,
        and the people fielding "تعذّر الاتصال" are the ones who need it."""
        self.login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_merchant_cannot_reach_it(self):
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_it_refuses_to_be_written_to(self):
        """Read-only is a property of the URLconf here, not a convention: there
        is no POST handler, so the configuration cannot be edited from a
        browser even by someone who holds every permission."""
        self.login(self.admin)
        self.assertEqual(self.client.post(self.url, {"B2CORE_JWT_ISSUER": "x"}).status_code, 405)

    def test_it_reports_no_data_before_any_token_has_been_verified(self):
        self.login(self.admin)
        status = self.client.get(self.url).context["jwks_status"]

        self.assertEqual(status["state"], "unknown")
        self.assertIsNone(status["last_success_at"])

    def test_a_successful_key_lookup_is_reported_with_its_time(self):
        from apps.portal.tests.support import StubbedJWKS, make_token

        with StubbedJWKS():
            jwks.get_signing_key(make_token())

            self.login(self.admin)
            status = self.client.get(self.url).context["jwks_status"]

        self.assertEqual(status["state"], "ok")
        self.assertIsNotNone(status["last_success_at"])

    def test_a_failure_is_reported_with_its_reason(self):
        from apps.portal.b2core.errors import B2CoreKeyError
        from apps.portal.tests.support import StubbedJWKS, make_token

        with StubbedJWKS(error=RuntimeError("connection refused")):
            with self.assertRaises(B2CoreKeyError):
                jwks.get_signing_key(make_token())

            self.login(self.admin)
            response = self.client.get(self.url)

        status = response.context["jwks_status"]
        self.assertEqual(status["state"], "failing")
        self.assertIsNotNone(status["last_failure_at"])
        self.assertIn("connection refused", status["last_failure_reason"])

    @override_settings(B2CORE_JWT_ISSUER="", B2CORE_JWT_AUDIENCE="")
    def test_an_unverified_issuer_or_audience_is_called_out(self):
        """The same two gaps `manage.py check --deploy` raises, said to the
        people who would actually notice them."""
        self.login(self.admin)
        gaps = self.client.get(self.url).context["b2core_gaps"]
        self.assertEqual(len(gaps), 2)

    @override_settings(B2CORE_JWKS_URL="", B2CORE_ORIGIN="")
    def test_an_unconfigured_deployment_says_so(self):
        self.login(self.admin)
        response = self.client.get(self.url)
        self.assertFalse(response.context["b2core_configured"])

    def test_it_appears_in_the_panel_navigation(self):
        self.login(self.staff)
        self.assertContains(self.client.get(reverse("finance:dashboard")), self.url)
