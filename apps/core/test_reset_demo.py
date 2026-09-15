"""`manage.py reset_demo` — the guard first, then what it clears and keeps.

The command exists to make a laptop reusable between demos. What makes it worth
testing rather than trusting is that it is the only thing in the project that
deletes a merchant network and an audit log, and the audit log is append-only in
the storage engine by spec §11 — so clearing it means taking the triggers off,
and anything that takes a guarantee off has to be proven to put it back.
"""

from datetime import time as clock_time
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role, User
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user
from apps.core.checks import AUDITLOG_TRIGGERS, installed_auditlog_triggers
from apps.core.choices import ActorRole, AuditAction
from apps.core.models import AuditLog, SystemSettings
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.rates.models import ExchangeRate, RateType
from apps.transactions.models import (
    Message,
    Request,
    RequestRead,
    RequestStatus,
    RequestType,
)


class ResetDemoTestCase(TestCase):
    """A database with one of everything the command is meant to touch."""

    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)
        self.device = TOTPDevice.objects.create(
            user=self.admin, name="default", confirmed=True
        )

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="تاجر أ", user=self.merchant_user)
        self.link = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )
        self.wallet = Wallet.objects.create(
            merchant_method=self.link, number="07700000001"
        )
        self.rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1510.00"),
            commission_iqd_per_100usd=Decimal("0.00"),
        )
        self.client_record = PortalClient.objects.create(
            b2core_id="sub-1", email="zainab@example.com"
        )

        self.request = self.make_request()
        Message.objects.create(
            request=self.request,
            sender_role=ActorRole.CLIENT,
            body="أرسلت الحوالة",
        )
        RequestRead.objects.create(user=self.admin, request=self.request)
        AuditLog.objects.create(
            action=AuditAction.LOGIN,
            actor_label="root@maxifyfx.com",
            target_type="accounts.User",
            target_id=str(self.admin.pk),
        )

    def make_request(self, **overrides):
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            merchant_assigned=self.merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("155.00"),
            amount_iqd=Decimal("234000.00"),
            rate_applied=Decimal("1510.00"),
            commission_applied=Decimal("0.00"),
            status=RequestStatus.SUBMITTED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    @staticmethod
    def reset(**options):
        out = StringIO()
        call_command("reset_demo", no_input=True, stdout=out, stderr=out, **options)
        return out.getvalue()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class DebugGuardTests(ResetDemoTestCase):
    """The same refusal ``seed_demo`` carries, for the opposite reason.

    ``seed_demo`` must not run outside DEBUG because it *creates* accounts with
    known passwords. This one must not because it *deletes* a merchant network
    and an audit log. Either way the answer is that the command is laptop
    tooling and the guard is the first line of ``handle``.
    """

    @override_settings(DEBUG=False)
    def test_it_refuses_to_run_with_debug_off(self):
        with self.assertRaises(CommandError) as caught:
            self.reset()

        self.assertIn("DEBUG", str(caught.exception))

    @override_settings(DEBUG=False)
    def test_it_deletes_nothing_when_it_refuses(self):
        """The refusal is worth nothing if it happens after the first delete."""
        with self.assertRaises(CommandError):
            self.reset()

        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Merchant.objects.count(), 1)
        self.assertEqual(PortalClient.objects.count(), 1)
        self.assertEqual(AuditLog.objects.count(), 1)

    @override_settings(DEBUG=False)
    def test_it_refuses_before_reading_its_arguments(self):
        """--keep-merchants is not a way past it, and neither is --no-input."""
        for options in ({}, {"keep_merchants": True}):
            with self.subTest(**options):
                with self.assertRaises(CommandError):
                    self.reset(**options)

    @override_settings(DEBUG=False)
    def test_the_refusal_says_why_rather_than_only_that(self):
        with self.assertRaises(CommandError) as caught:
            self.reset()

        message = str(caught.exception)
        self.assertIn("deletes", message)
        self.assertIn("development machine", message)

    @override_settings(DEBUG=True)
    def test_it_runs_with_debug_on(self):
        self.reset()

        self.assertEqual(Request.objects.count(), 0)


# ---------------------------------------------------------------------------
# What goes
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class FullResetTests(ResetDemoTestCase):
    def test_it_clears_the_traffic(self):
        self.reset()

        self.assertEqual(Request.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(RequestRead.objects.count(), 0)

    def test_it_clears_the_merchant_network(self):
        self.reset()

        self.assertEqual(Merchant.objects.count(), 0)
        self.assertEqual(MerchantMethod.objects.count(), 0)
        self.assertEqual(Wallet.objects.count(), 0)
        self.assertEqual(PaymentMethod.objects.count(), 0)

    def test_it_clears_the_clients(self):
        self.reset()

        self.assertEqual(PortalClient.objects.count(), 0)

    def test_it_clears_the_audit_log(self):
        """Which the ORM cannot do at all — see AuditLogTriggerTests."""
        self.reset()

        self.assertEqual(AuditLog.objects.count(), 0)


# ---------------------------------------------------------------------------
# What stays
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class KeptTests(ResetDemoTestCase):
    """The reason the command exists rather than `rm dev.sqlite3`."""

    def test_the_internal_users_survive(self):
        self.reset()

        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.merchant_user.pk).exists())

    def test_their_roles_survive(self):
        self.reset()

        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.FINANCE_ADMIN)
        self.assertTrue(Group.objects.filter(name=Role.FINANCE_ADMIN).exists())
        self.assertTrue(self.admin.groups.exists())

    def test_the_second_factors_survive(self):
        """Re-enrolling a TOTP device by hand is the slowest part of setting
        this project up, and is the whole reason for the command."""
        self.reset()

        device = TOTPDevice.objects.get(pk=self.device.pk)
        self.assertTrue(device.confirmed)
        self.assertEqual(device.name, "default")

    def test_the_exchange_rates_survive(self):
        self.reset()

        self.assertTrue(ExchangeRate.objects.filter(pk=self.rate.pk).exists())

    def test_the_system_settings_survive(self):
        SystemSettings.objects.update_or_create(
            pk=1, defaults={"open_time": clock_time(9, 0)}
        )

        self.reset()

        self.assertTrue(SystemSettings.objects.exists())

    def test_a_merchant_user_outlives_the_merchant_it_pointed_at(self):
        """`Merchant.user` is a OneToOne onto an internal account. Deleting the
        merchant must not take the login with it."""
        self.reset()

        self.assertEqual(Merchant.objects.count(), 0)
        self.assertTrue(User.objects.filter(pk=self.merchant_user.pk).exists())


# ---------------------------------------------------------------------------
# --keep-merchants
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class KeepMerchantsTests(ResetDemoTestCase):
    def test_it_clears_the_traffic(self):
        self.reset(keep_merchants=True)

        self.assertEqual(Request.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(RequestRead.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_it_keeps_the_network(self):
        self.reset(keep_merchants=True)

        self.assertTrue(Merchant.objects.filter(pk=self.merchant.pk).exists())
        self.assertTrue(MerchantMethod.objects.filter(pk=self.link.pk).exists())
        self.assertTrue(Wallet.objects.filter(pk=self.wallet.pk).exists())
        self.assertTrue(PaymentMethod.objects.filter(pk=self.method.pk).exists())

    def test_it_keeps_the_clients(self):
        """"Requests alone" — a client is not part of the traffic."""
        self.reset(keep_merchants=True)

        self.assertTrue(PortalClient.objects.filter(pk=self.client_record.pk).exists())

    def test_the_network_can_take_a_new_request_straight_after(self):
        """The point of the flag: run the demo again without rebuilding it."""
        self.reset(keep_merchants=True)

        fresh = self.make_request()

        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(fresh.merchant_selected, self.merchant)


# ---------------------------------------------------------------------------
# The append-only guarantee it has to borrow and give back
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class AuditLogTriggerTests(ResetDemoTestCase):
    def test_the_log_is_undeletable_to_begin_with(self):
        """Establishes that clearing it is a real problem and not a `delete()`.

        If this ever stops raising, the triggers spec §11 asks for are gone and
        the rest of this class is testing nothing.
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditLog.objects.all().delete()

    def test_the_triggers_are_back_afterwards(self):
        self.reset()

        installed = installed_auditlog_triggers(connection)
        for name in AUDITLOG_TRIGGERS:
            self.assertIn(name, installed)

    def test_the_log_is_undeletable_again_afterwards(self):
        """The names being present is not the same as the triggers firing."""
        self.reset()
        AuditLog.objects.create(
            action=AuditAction.LOGIN,
            actor_label="after@maxifyfx.com",
            target_type="accounts.User",
            target_id=str(self.admin.pk),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditLog.objects.all().delete()

    def test_it_says_the_triggers_went_back(self):
        """Silence about a guarantee that was briefly off is not good enough."""
        output = self.reset()

        self.assertIn("للإضافة فقط", output)

    def test_a_reset_can_be_run_twice(self):
        """The second run finds an empty log and must not trip over its own
        restored triggers."""
        self.reset()
        self.reset()

        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(
            set(AUDITLOG_TRIGGERS) - installed_auditlog_triggers(connection), set()
        )


# ---------------------------------------------------------------------------
# Sequences, and the reference that has none
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class SequenceTests(ResetDemoTestCase):
    def test_the_next_request_starts_from_one_again(self):
        self.assertEqual(Request.objects.get().pk, 1)
        for _ in range(4):
            self.make_request()
        self.assertEqual(Request.objects.order_by("-pk").first().pk, 5)

        self.reset(keep_merchants=True)

        self.assertEqual(self.make_request().pk, 1)

    def test_it_says_the_public_reference_has_no_counter(self):
        """`public_ref` is five random digits on purpose — a sequential one
        would tell every merchant how much volume the desk is doing. So there
        is nothing to reset, and the command says so rather than leaving
        somebody to wonder why references did not restart."""
        output = self.reset()

        self.assertIn("public_ref", output)

    def test_references_stay_random_across_a_reset(self):
        self.reset(keep_merchants=True)

        refs = {self.make_request().public_ref for _ in range(5)}

        self.assertEqual(len(refs), 5)
        for ref in refs:
            self.assertTrue(ref.startswith("MP-"), ref)


# ---------------------------------------------------------------------------
# Saying what it will do, before doing it
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class PlanTests(ResetDemoTestCase):
    def test_it_prints_what_it_will_delete_and_how_many(self):
        output = self.reset()

        self.assertIn("سيُمسح", output)
        for label in ("الطلبات", "الرسائل", "المحافظ", "سجل التدقيق", "العملاء"):
            self.assertIn(label, output)

    def test_it_prints_what_it_will_keep(self):
        output = self.reset()

        self.assertIn("سيبقى", output)
        for label in ("المستخدمون الداخليون", "أجهزة المصادقة الثنائية", "أسعار الصرف"):
            self.assertIn(label, output)

    def test_the_counts_are_the_ones_it_found(self):
        for _ in range(2):
            self.make_request()

        output = self.reset()

        # Three requests, one message: the figures an operator reads before
        # agreeing, not a generic warning.
        self.assertRegex(output, r"الطلبات\s+3")
        self.assertRegex(output, r"الرسائل\s+1")

    def test_an_empty_database_is_told_so_rather_than_reset(self):
        self.reset()

        output = self.reset()

        self.assertIn("نظيفة أصلًا", output)

    def test_keep_merchants_says_what_it_is_sparing(self):
        output = self.reset(keep_merchants=True)

        self.assertIn("--keep-merchants", output)


# ---------------------------------------------------------------------------
# The confirmation
# ---------------------------------------------------------------------------


@override_settings(DEBUG=True)
class ConfirmationTests(ResetDemoTestCase):
    def call(self, answer):
        """Run without --no-input, with ``answer`` standing in for the operator."""
        import builtins
        from unittest import mock

        out = StringIO()
        with mock.patch.object(builtins, "input", return_value=answer):
            call_command("reset_demo", stdout=out, stderr=out)
        return out.getvalue()

    def test_typing_the_word_runs_it(self):
        self.call("reset")

        self.assertEqual(Request.objects.count(), 0)

    def test_anything_else_cancels(self):
        for answer in ("", "y", "yes", "RESET ALL", "لا"):
            with self.subTest(answer=answer):
                output = self.call(answer)

                self.assertIn("أُلغي", output)
                self.assertEqual(Request.objects.count(), 1)
                self.assertEqual(AuditLog.objects.count(), 1)

    def test_surrounding_whitespace_is_forgiven(self):
        self.call("  reset\n")

        self.assertEqual(Request.objects.count(), 0)

    def test_a_cancelled_run_leaves_the_triggers_alone(self):
        self.call("no")

        self.assertEqual(
            set(AUDITLOG_TRIGGERS) - installed_auditlog_triggers(connection), set()
        )
