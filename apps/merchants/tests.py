"""Merchant, method and wallet invariants (spec §5)."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import Role, User
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet


class WalletActivationTests(TestCase):
    def setUp(self):
        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="تاجر أ")
        self.merchant_method = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )

    def _wallet(self, number, **kwargs):
        return Wallet.objects.create(
            merchant_method=self.merchant_method, number=number, **kwargs
        )

    def test_a_new_active_wallet_stands_down_the_previous_one(self):
        first = self._wallet("07700000001")
        second = self._wallet("07700000002")

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertIsNotNone(first.deactivated_at)
        self.assertTrue(second.is_active)
        self.assertEqual(self.merchant_method.active_wallet.pk, second.pk)

    def test_only_one_wallet_is_ever_active(self):
        for i in range(5):
            self._wallet(f"0770000000{i}")
        self.assertEqual(
            Wallet.objects.filter(merchant_method=self.merchant_method, is_active=True).count(),
            1,
        )

    def test_the_database_also_refuses_a_second_active_wallet(self):
        """The partial unique constraint backs up the save() logic."""
        self._wallet("07700000001")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Bypasses save() entirely, which is exactly what the constraint
                # is there to catch.
                Wallet.objects.bulk_create(
                    [Wallet(merchant_method=self.merchant_method, number="07700000009")]
                )

    def test_other_merchant_methods_are_unaffected(self):
        other_method = MerchantMethod.objects.create(
            merchant=Merchant.objects.create(name="تاجر ب"), payment_method=self.method
        )
        mine = self._wallet("07700000001")
        theirs = Wallet.objects.create(merchant_method=other_method, number="07800000001")

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertTrue(mine.is_active)
        self.assertTrue(theirs.is_active)

    def test_deactivate_clears_the_active_slot(self):
        wallet = self._wallet("07700000001")
        wallet.deactivate()
        self.assertIsNone(self.merchant_method.active_wallet)
        self.assertIsNotNone(wallet.deactivated_at)

    def test_reactivating_a_wallet_clears_its_deactivation_time(self):
        first = self._wallet("07700000001")
        self._wallet("07700000002")
        first.refresh_from_db()

        first.is_active = True
        first.save()
        first.refresh_from_db()
        self.assertIsNone(first.deactivated_at)
        self.assertEqual(self.merchant_method.active_wallet.pk, first.pk)

    def test_wallet_number_is_validated(self):
        wallet = Wallet(merchant_method=self.merchant_method, number="not-a-number")
        with self.assertRaises(ValidationError):
            wallet.full_clean()

    def test_daily_cap_is_optional(self):
        wallet = self._wallet("07700000001", daily_cap=Decimal("5000000.00"))
        self.assertEqual(wallet.daily_cap, Decimal("5000000.00"))
        self.assertIsNone(self._wallet("07700000002").daily_cap)


class MerchantMethodTests(TestCase):
    def setUp(self):
        self.method = PaymentMethod.objects.create(
            code="fib", caption_ar="بنك FIB", caption_en="FIB"
        )
        self.merchant = Merchant.objects.create(name="تاجر ج")

    def test_a_merchant_covers_each_method_at_most_once(self):
        MerchantMethod.objects.create(merchant=self.merchant, payment_method=self.method)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MerchantMethod.objects.create(
                    merchant=self.merchant, payment_method=self.method
                )

    def test_login_account_must_hold_the_merchant_role(self):
        staff = User.objects.create_user(
            email="notamerchant@maxifyfx.com",
            password="portal-test-pass-12345",
            full_name="Staff",
            role=Role.FINANCE_STAFF,
        )
        self.merchant.user = staff
        with self.assertRaises(ValidationError):
            self.merchant.full_clean()

    def test_active_methods_excludes_inactive_payment_methods(self):
        MerchantMethod.objects.create(merchant=self.merchant, payment_method=self.method)
        self.assertEqual(self.merchant.active_methods().count(), 1)

        self.method.is_active = False
        self.method.save()
        self.assertEqual(self.merchant.active_methods().count(), 0)


class MerchantB2CoreIdTests(TestCase):
    """The identifier is optional and unique, which is a harder pair than it looks.

    ``unique=True`` on a CharField that is allowed to be empty is a trap: the
    unique index treats ``''`` as an ordinary value, so the *first* merchant
    without an identifier takes the empty slot and the second one cannot be
    saved at all. Absent has to be NULL, which SQL counts as distinct from
    every other NULL. These tests hold that line at all three levels — the
    model's normalisation, the unique index, and the check constraint that
    stops a blank arriving by some other road.
    """

    def test_a_blank_identifier_is_stored_as_null(self):
        merchant = Merchant.objects.create(name="تاجر أ", b2core_id="")
        merchant.refresh_from_db()
        self.assertIsNone(merchant.b2core_id)

    def test_whitespace_only_is_also_null(self):
        merchant = Merchant.objects.create(name="تاجر ب", b2core_id="   ")
        merchant.refresh_from_db()
        self.assertIsNone(merchant.b2core_id)

    def test_surrounding_whitespace_is_trimmed(self):
        """Pasted out of a B2CORE screen, a value usually arrives with a tail."""
        merchant = Merchant.objects.create(name="تاجر ج", b2core_id="  B2C-901  ")
        merchant.refresh_from_db()
        self.assertEqual(merchant.b2core_id, "B2C-901")

    def test_any_number_of_merchants_may_have_none(self):
        """The whole reason blank is NULL. With '' this fails on the second row."""
        Merchant.objects.create(name="تاجر د")
        Merchant.objects.create(name="تاجر هـ", b2core_id="")
        Merchant.objects.create(name="تاجر و", b2core_id=None)
        self.assertEqual(Merchant.objects.filter(b2core_id__isnull=True).count(), 3)

    def test_two_merchants_cannot_share_one_identifier(self):
        Merchant.objects.create(name="تاجر ز", b2core_id="B2C-500")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Merchant.objects.create(name="تاجر ح", b2core_id="B2C-500")

    def test_the_database_refuses_an_empty_string_outright(self):
        """`update()` skips `save()`, so only the constraint is left to say no."""
        merchant = Merchant.objects.create(name="تاجر ط", b2core_id="B2C-600")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Merchant.objects.filter(pk=merchant.pk).update(b2core_id="")

    def test_clean_normalises_before_uniqueness_is_checked(self):
        """`full_clean` runs `clean` then `validate_unique`, and that order is
        what keeps one blank from being compared against another."""
        Merchant.objects.create(name="تاجر ي", b2core_id="")
        second = Merchant(name="تاجر ك", b2core_id="  ")
        second.full_clean()
        self.assertIsNone(second.b2core_id)

    def test_a_duplicate_is_reported_as_a_validation_error_not_a_crash(self):
        Merchant.objects.create(name="تاجر ل", b2core_id="B2C-700")
        with self.assertRaises(ValidationError) as caught:
            Merchant(name="تاجر م", b2core_id="B2C-700").full_clean()
        self.assertIn("b2core_id", caught.exception.error_dict)


class PaymentMethodTests(TestCase):
    def test_a_method_must_support_at_least_one_direction(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentMethod.objects.create(
                    code="dead",
                    caption_ar="بلا اتجاه",
                    caption_en="No direction",
                    supports_deposit=False,
                    supports_withdrawal=False,
                )

    def test_supports_answers_per_direction(self):
        method = PaymentMethod.objects.create(
            code="deposit-only",
            caption_ar="إيداع فقط",
            caption_en="Deposit only",
            supports_withdrawal=False,
        )
        self.assertTrue(method.supports("deposit"))
        self.assertFalse(method.supports("withdrawal"))
