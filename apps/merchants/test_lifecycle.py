"""Retiring a merchant, and getting rid of a wallet.

Finance review of 25 Aug 2026. Full deletion stays forbidden because the audit
trail needs its references, so the two operations here are archiving and — for
the one case where there is nothing to keep — a real delete.

What is asserted, over and over, is the *disappearing*: an archived merchant is
gone from the client's choice, from every dropdown, from Finance's own list, and
from routing. A retirement that leaves the thing reachable from one screen
somebody forgot about is not a retirement.
"""

from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import AuditAction
from apps.core.models import AuditLog
from apps.merchants import lifecycle
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.portal import catalog
from apps.rates.models import ExchangeRate, RateType
from apps.transactions.models import Request, RequestStatus, RequestType
from apps.transactions.services import (
    TransitionError,
    apply_transition,
    eligible_merchants,
)


class LifecycleTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="الرافدين للصرافة")
        self.link = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )
        self.wallet = Wallet.objects.create(merchant_method=self.link, number="07700000001")

        self.other = Merchant.objects.create(name="دجلة للصرافة")
        other_link = MerchantMethod.objects.create(
            merchant=self.other, payment_method=self.method
        )
        Wallet.objects.create(merchant_method=other_link, number="07700000002")

        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1470.00"),
            commission_iqd_per_100usd=Decimal("5000.00"),
        )
        self.client_record = PortalClient.objects.create(b2core_id="sub-1")

    def make_request(self, **overrides) -> Request:
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            wallet=self.wallet,
            wallet_number_snapshot=self.wallet.number,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("152000.00"),
            rate_applied=Decimal("1470.00"),
            commission_applied=Decimal("5000.00"),
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


# ---------------------------------------------------------------------------
# Archiving a merchant
# ---------------------------------------------------------------------------


class ArchiveMerchantTests(LifecycleTestCase):
    def test_it_is_archived_not_deleted(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        fresh = Merchant.objects.get(pk=self.merchant.pk)
        self.assertTrue(fresh.is_archived)
        self.assertIsNotNone(fresh.archived_at)
        self.assertEqual(fresh.archived_by, self.admin)

    def test_archiving_deactivates_too(self):
        """Otherwise it would be invisible everywhere and still routable by
        anything that only asked ``is_active``."""
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        self.assertFalse(Merchant.objects.get(pk=self.merchant.pk).is_active)

    def test_archiving_twice_is_refused_rather_than_silently_restamped(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.archive_merchant(self.merchant, actor=self.admin)

    def test_it_is_written_to_the_audit_log(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        entry = AuditLog.objects.filter(action=AuditAction.MERCHANT_CHANGE).latest("id")
        self.assertEqual(entry.after["event"], "merchant_archived")
        self.assertEqual(entry.actor_id, self.admin.pk)

    def test_their_open_requests_are_left_alone(self):
        """Somebody's money is in flight. Ending it is `cancel`'s job, taken
        per request with a reason the client reads."""
        request_obj = self.make_request()

        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        fresh = Request.objects.get(pk=request_obj.pk)
        self.assertEqual(fresh.status, RequestStatus.SUBMITTED)
        self.assertEqual(fresh.merchant_selected_id, self.merchant.pk)

    # -- the disappearing --------------------------------------------------

    def test_they_are_gone_from_the_client_s_choice(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        offered = catalog.available_merchants(RequestType.DEPOSIT)
        self.assertNotIn(self.merchant, offered)
        self.assertIn(self.other, offered)

    def test_they_are_gone_even_if_something_reactivates_them(self):
        """Two conditions that must both hold, so clearing one is not enough."""
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        Merchant.objects.filter(pk=self.merchant.pk).update(is_active=True)

        self.assertNotIn(
            self.merchant, catalog.available_merchants(RequestType.DEPOSIT)
        )

    def test_they_are_gone_from_the_routing_list(self):
        request_obj = self.make_request(status=RequestStatus.UNDER_REVIEW)
        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        self.assertNotIn(self.merchant, eligible_merchants(request_obj))

    def test_routing_to_them_is_refused_by_name(self):
        request_obj = self.make_request(status=RequestStatus.UNDER_REVIEW)
        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        # Fetched fresh, the way the routing form fetches it. The guard is
        # belt-and-braces — `eligible_merchants` never offers an archived one —
        # and this is the braces being checked on its own.
        archived = Merchant.objects.get(pk=self.merchant.pk)

        with self.assertRaises(TransitionError) as caught:
            apply_transition(request_obj, "route", actor=self.admin, merchant=archived)
        self.assertEqual(caught.exception.code, "merchant_archived")

    def test_they_are_gone_from_the_queue_and_report_filters(self):
        from apps.finance.queue_forms import RequestFilterForm
        from apps.finance.report_forms import FinanceReportFilterForm

        lifecycle.archive_merchant(self.merchant, actor=self.admin)

        for form in (RequestFilterForm(), FinanceReportFilterForm()):
            self.assertNotIn(self.merchant, form.fields["merchant"].queryset)
            self.assertIn(self.other, form.fields["merchant"].queryset)

    def test_they_are_gone_from_the_finance_merchant_list(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        self.login(self.admin)

        body = self.client.get(reverse("finance:merchant_list")).content.decode()

        self.assertNotIn(self.merchant.name, body)
        self.assertIn(self.other.name, body)

    def test_the_archive_is_still_reachable_by_asking_for_it(self):
        """An operation with no way back and nothing to look at is a deletion
        with extra steps."""
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        self.login(self.admin)

        body = self.client.get(
            reverse("finance:merchant_list"), {"status": "archived"}
        ).content.decode()

        self.assertIn(self.merchant.name, body)
        self.assertNotIn(self.other.name, body)

    def test_their_own_page_still_opens_and_says_what_happened(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        self.login(self.admin)

        response = self.client.get(
            reverse("finance:merchant_detail", args=[self.merchant.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مؤرشف")

    def test_the_activate_button_will_not_bring_them_back(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        self.login(self.admin)

        self.client.post(reverse("finance:merchant_toggle", args=[self.merchant.pk]))

        self.assertFalse(Merchant.objects.get(pk=self.merchant.pk).is_active)


class RestoreMerchantTests(LifecycleTestCase):
    def test_restoring_brings_them_back_into_the_lists(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        lifecycle.restore_merchant(self.merchant, actor=self.admin)

        self.assertFalse(Merchant.objects.get(pk=self.merchant.pk).is_archived)

    def test_restoring_does_not_reactivate(self):
        """Coming out of the archive and being open for business are two
        decisions."""
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        lifecycle.restore_merchant(self.merchant, actor=self.admin)

        fresh = Merchant.objects.get(pk=self.merchant.pk)
        self.assertFalse(fresh.is_active)
        self.assertNotIn(fresh, catalog.available_merchants(RequestType.DEPOSIT))

    def test_restoring_one_that_is_not_archived_is_refused(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.restore_merchant(self.merchant, actor=self.admin)

    def test_it_is_audited(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        lifecycle.restore_merchant(self.merchant, actor=self.admin)

        entry = AuditLog.objects.filter(action=AuditAction.MERCHANT_CHANGE).latest("id")
        self.assertEqual(entry.after["event"], "merchant_restored")


# ---------------------------------------------------------------------------
# Getting rid of a wallet
# ---------------------------------------------------------------------------


class RemoveUnusedWalletTests(LifecycleTestCase):
    def test_a_wallet_nothing_was_submitted_against_is_really_deleted(self):
        removal = lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertTrue(removal.deleted)
        self.assertFalse(Wallet.objects.filter(pk=self.wallet.pk).exists())

    def test_the_audit_entry_carries_the_row_that_is_about_to_vanish(self):
        """There will be nothing left to look the row up in."""
        self.wallet.label = "المحفظة الرئيسية"
        self.wallet.save(update_fields=["label"])

        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        entry = AuditLog.objects.filter(action=AuditAction.WALLET_CHANGE).latest("id")
        self.assertEqual(entry.after["event"], "wallet_deleted")
        self.assertEqual(entry.after["reason"], "never_used")
        self.assertEqual(entry.after["number"], "07700000001")
        self.assertEqual(entry.after["label"], "المحفظة الرئيسية")
        self.assertEqual(entry.after["merchant"], "الرافدين للصرافة")

    def test_the_entry_is_recorded_against_the_merchant_that_survives(self):
        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        entry = AuditLog.objects.filter(action=AuditAction.WALLET_CHANGE).latest("id")
        self.assertEqual(entry.target_type, "merchants.Merchant")
        self.assertEqual(entry.target_id, str(self.merchant.pk))


class RemoveUsedWalletTests(LifecycleTestCase):
    def test_a_wallet_with_a_request_against_it_is_archived_not_deleted(self):
        self.make_request()

        removal = lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertFalse(removal.deleted)
        self.assertTrue(Wallet.objects.get(pk=self.wallet.pk).is_archived)

    def test_archiving_deactivates_it_too(self):
        self.make_request()

        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        fresh = Wallet.objects.get(pk=self.wallet.pk)
        self.assertFalse(fresh.is_active)
        self.assertIsNotNone(fresh.deactivated_at)

    def test_a_request_filed_before_the_link_existed_still_counts(self):
        """Old rows carry only the snapshotted number. Matched back on the
        number, the merchant and the method — fail-closed, so an unresolvable
        match archives rather than deletes."""
        self.make_request(wallet=None)

        removal = lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertFalse(removal.deleted)

    def test_a_matching_number_under_a_different_merchant_does_not_count(self):
        elsewhere = Wallet.objects.create(
            merchant_method=MerchantMethod.objects.get(merchant=self.other),
            number="07700000001",
            is_active=False,
        )

        removal = lifecycle.remove_wallet(elsewhere, actor=self.admin)

        self.assertTrue(removal.deleted)

    def test_the_request_keeps_the_number_it_was_shown(self):
        request_obj = self.make_request()

        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertEqual(
            Request.objects.get(pk=request_obj.pk).wallet_number_snapshot,
            "07700000001",
        )

    def test_it_is_audited_with_the_reason(self):
        self.make_request()

        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        entry = AuditLog.objects.filter(action=AuditAction.WALLET_CHANGE).latest("id")
        self.assertEqual(entry.after["event"], "wallet_archived")
        self.assertEqual(entry.after["reason"], "has_requests")

    def test_archiving_twice_is_refused(self):
        self.make_request()
        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.remove_wallet(
                Wallet.objects.get(pk=self.wallet.pk), actor=self.admin
            )

    # -- the disappearing --------------------------------------------------

    def test_an_archived_wallet_is_gone_from_the_client_s_screen(self):
        self.make_request()
        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertIsNone(catalog.active_wallet(self.link))

    def test_the_merchant_loses_the_method_that_had_only_that_wallet(self):
        self.make_request()
        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertNotIn(
            self.merchant, catalog.available_merchants(RequestType.DEPOSIT)
        )

    def test_it_is_gone_from_the_merchant_s_own_wallet_screen(self):
        """Deactivated means stood down and still theirs; archived means
        Finance is done with it."""
        from apps.merchant_panel import scoping

        self.make_request()
        lifecycle.remove_wallet(self.wallet, actor=self.admin)

        self.assertNotIn(self.wallet, scoping.wallets_for(self.merchant))

    def test_it_is_gone_from_the_finance_merchant_page(self):
        self.make_request()
        lifecycle.remove_wallet(self.wallet, actor=self.admin)
        self.login(self.admin)

        body = self.client.get(
            reverse("finance:merchant_detail", args=[self.merchant.pk])
        ).content.decode()

        self.assertNotIn("07700000001", body)

    def test_the_database_refuses_a_delete_even_if_the_rule_is_bypassed(self):
        """``Request.wallet`` is PROTECT. The rule above is the braces; this is
        the belt, and it does not depend on anybody remembering to ask."""
        from django.db.models import ProtectedError

        self.make_request()

        with self.assertRaises(ProtectedError):
            Wallet.objects.get(pk=self.wallet.pk).delete()


# ---------------------------------------------------------------------------
# Who is allowed to
# ---------------------------------------------------------------------------


class LifecycleAccessTests(LifecycleTestCase):
    def test_finance_staff_may_not_archive_a_merchant(self):
        """`manage_merchants` is delegable so staff can add a wallet. Retiring
        the merchant it belongs to is not the same size of act (spec §3)."""
        self.assertFalse(self.staff.has_perm(lifecycle.PERM_ARCHIVE))

        with self.assertRaises(PermissionDenied):
            lifecycle.archive_merchant(self.merchant, actor=self.staff)

    def test_finance_staff_may_not_remove_a_wallet(self):
        with self.assertRaises(PermissionDenied):
            lifecycle.remove_wallet(self.wallet, actor=self.staff)

    def test_the_admin_holds_it(self):
        self.assertTrue(self.admin.has_perm(lifecycle.PERM_ARCHIVE))

    def test_the_route_is_refused_to_staff(self):
        self.login(self.staff)

        response = self.client.post(
            reverse("finance:merchant_archive", args=[self.merchant.pk])
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Merchant.objects.get(pk=self.merchant.pk).is_archived)

    def test_the_wallet_route_is_refused_to_staff(self):
        self.login(self.staff)

        response = self.client.post(
            reverse("finance:wallet_remove", args=[self.wallet.pk])
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Wallet.objects.filter(pk=self.wallet.pk).exists())

    def test_a_merchant_holds_neither(self):
        merchant_user = make_user("m@example.com", Role.MERCHANT)
        self.assertFalse(merchant_user.has_perm(lifecycle.PERM_ARCHIVE))


class LifecycleScreenTests(LifecycleTestCase):
    def test_the_screen_says_which_of_the_two_happened_on_a_delete(self):
        self.login(self.admin)

        response = self.client.post(
            reverse("finance:wallet_remove", args=[self.wallet.pk]), follow=True
        )

        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("حُذفت", text)
        self.assertIn("لم يُقدَّم عليها أي طلب", text)

    def test_the_screen_says_which_of_the_two_happened_on_an_archive(self):
        self.make_request()
        self.login(self.admin)

        response = self.client.post(
            reverse("finance:wallet_remove", args=[self.wallet.pk]), follow=True
        )

        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("أُرشفت", text)
        self.assertIn("سجل التدقيق", text)

    def test_archiving_a_merchant_says_what_it_did_and_did_not_do(self):
        self.login(self.admin)

        response = self.client.post(
            reverse("finance:merchant_archive", args=[self.merchant.pk]), follow=True
        )

        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("اختفى من كل القوائم", text)
        self.assertIn("طلباته القائمة باقية", text)

    def test_restoring_says_it_did_not_reactivate(self):
        lifecycle.archive_merchant(self.merchant, actor=self.admin)
        self.login(self.admin)

        response = self.client.post(
            reverse("finance:merchant_archive", args=[self.merchant.pk]),
            {"restore": "1"},
            follow=True,
        )

        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("موقوف حتى تفعّله", text)
