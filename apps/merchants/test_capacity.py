"""Wallet daily caps, in Iraqi dinars.

The unit is the whole point of these: ``daily_cap`` is IQD and is measured
against ``Request.amount_iqd``, the total a client actually transfers, so a
100 USD deposit at 1,450 with 500 commission consumes 145,500 of the cap — not
100, and not 145,000.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client as PortalClient
from apps.merchants import capacity
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import Request, RequestStatus, RequestType


class CapacityTestCase(TestCase):
    def setUp(self):
        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="تاجر أ")
        self.link = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )
        self.wallet = Wallet.objects.create(
            merchant_method=self.link,
            number="07700000001",
            daily_cap=Decimal("1000000.00"),
        )
        self.client_record = PortalClient.objects.create(b2core_id="sub-1")

    def deposit(self, iqd, **overrides):
        fields = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            wallet_number_snapshot=self.wallet.number,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal(iqd),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.SUBMITTED,
        )
        fields.update(overrides)
        return Request.objects.create(**fields)


class ConsumptionTests(CapacityTestCase):
    def test_an_untouched_wallet_has_consumed_nothing(self):
        self.assertEqual(capacity.consumed_iqd(self.wallet), Decimal("0.00"))
        self.assertEqual(capacity.headroom_iqd(self.wallet), Decimal("1000000.00"))

    def test_the_total_transferred_is_what_counts_not_the_usd(self):
        self.deposit("145500.00")
        self.assertEqual(capacity.consumed_iqd(self.wallet), Decimal("145500.00"))

    def test_deposits_accumulate(self):
        self.deposit("145500.00")
        self.deposit("300000.00")
        self.assertEqual(capacity.consumed_iqd(self.wallet), Decimal("445500.00"))
        self.assertEqual(capacity.headroom_iqd(self.wallet), Decimal("554500.00"))

    def test_a_rejected_deposit_frees_its_share(self):
        self.deposit("145500.00", status=RequestStatus.REJECTED)
        self.assertEqual(capacity.consumed_iqd(self.wallet), Decimal("0.00"))

    def test_a_deposit_into_a_different_number_does_not_count(self):
        self.deposit("145500.00", wallet_number_snapshot="07700000009")
        self.assertEqual(capacity.consumed_iqd(self.wallet), Decimal("0.00"))

    def test_yesterdays_deposits_do_not_count(self):
        old = self.deposit("145500.00")
        # submitted_at is auto_now_add, so it has to be moved after the fact.
        Request.objects.filter(pk=old.pk).update(
            submitted_at=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(capacity.consumed_iqd(self.wallet), Decimal("0.00"))

    def test_an_uncapped_wallet_has_no_headroom_figure(self):
        self.wallet.daily_cap = None
        self.wallet.save(update_fields=["daily_cap"])
        self.deposit("999999999.00")
        self.assertIsNone(capacity.headroom_iqd(self.wallet))
        self.assertTrue(capacity.accepts(self.wallet, Decimal("1000000000.00")))

    def test_headroom_never_goes_negative(self):
        self.deposit("1500000.00")
        self.assertEqual(capacity.headroom_iqd(self.wallet), Decimal("0.00"))


class AcceptanceTests(CapacityTestCase):
    def test_an_amount_that_fits_is_accepted(self):
        self.deposit("900000.00")
        self.assertTrue(capacity.accepts(self.wallet, Decimal("100000.00")))

    def test_an_amount_one_dinar_over_is_refused(self):
        self.deposit("900000.00")
        self.assertFalse(capacity.accepts(self.wallet, Decimal("100001.00")))

    def test_a_full_wallet_accepts_nothing(self):
        self.deposit("1000000.00")
        self.assertFalse(capacity.accepts(self.wallet, Decimal("1.00")))


class UsageTests(CapacityTestCase):
    def test_usage_reports_what_a_screen_needs(self):
        self.deposit("250000.00")
        usage = capacity.usage(self.wallet)
        self.assertTrue(usage["capped"])
        self.assertEqual(usage["consumed"], Decimal("250000.00"))
        self.assertEqual(usage["remaining"], Decimal("750000.00"))
        self.assertEqual(usage["percent"], 25)
        self.assertFalse(usage["is_full"])

    def test_a_cap_lowered_below_todays_take_still_renders(self):
        self.deposit("900000.00")
        self.wallet.daily_cap = Decimal("100000.00")
        self.wallet.save(update_fields=["daily_cap"])
        usage = capacity.usage(self.wallet)
        self.assertEqual(usage["percent"], 100)
        self.assertTrue(usage["is_full"])
        self.assertEqual(usage["remaining"], Decimal("0.00"))

    def test_an_uncapped_wallet_reports_itself_as_such(self):
        self.wallet.daily_cap = None
        self.wallet.save(update_fields=["daily_cap"])
        usage = capacity.usage(self.wallet)
        self.assertFalse(usage["capped"])
        self.assertIsNone(usage["cap"])
        self.assertFalse(usage["is_full"])
