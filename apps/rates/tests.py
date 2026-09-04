"""Exchange rates are an immutable history (spec §5)."""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone

from apps.core.models import AppendOnlyError
from apps.rates.models import ExchangeRate, RateType


class ExchangeRateTests(TestCase):
    def test_a_saved_rate_cannot_be_edited(self):
        rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT, iqd_per_usd=Decimal("1450.00")
        )
        rate.iqd_per_usd = Decimal("1500.00")
        with self.assertRaises(AppendOnlyError):
            rate.save()

    def test_a_saved_rate_cannot_be_deleted(self):
        rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT, iqd_per_usd=Decimal("1450.00")
        )
        with self.assertRaises(AppendOnlyError):
            rate.delete()

    def test_current_returns_the_newest_effective_revision(self):
        now = timezone.now()
        ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1400.00"),
            effective_from=now - timezone.timedelta(days=2),
        )
        newest = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1450.00"),
            effective_from=now - timezone.timedelta(minutes=1),
        )
        self.assertEqual(ExchangeRate.current(RateType.DEPOSIT).pk, newest.pk)

    def test_future_revisions_are_not_yet_current(self):
        now = timezone.now()
        today = ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1460.00"),
            effective_from=now - timezone.timedelta(minutes=1),
        )
        ExchangeRate.objects.create(
            rate_type=RateType.WITHDRAWAL,
            iqd_per_usd=Decimal("1470.00"),
            effective_from=now + timezone.timedelta(days=1),
        )
        self.assertEqual(ExchangeRate.current(RateType.WITHDRAWAL).pk, today.pk)

    def test_deposit_and_withdrawal_rates_are_independent(self):
        ExchangeRate.objects.create(rate_type=RateType.DEPOSIT, iqd_per_usd=Decimal("1450.00"))
        self.assertIsNone(ExchangeRate.current(RateType.WITHDRAWAL))

    def test_commission_is_prorated_per_100_usd(self):
        rate = ExchangeRate.objects.create(
            rate_type=RateType.DEPOSIT,
            iqd_per_usd=Decimal("1450.00"),
            commission_iqd_per_100usd=Decimal("2000.00"),
        )
        self.assertEqual(rate.commission_for(Decimal("250")), Decimal("5000.00"))
        self.assertEqual(rate.convert_usd_to_iqd(Decimal("100")), Decimal("145000.00"))

    def test_no_change_or_delete_permission_exists(self):
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="rates", content_type__model="exchangerate"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {"add_exchangerate", "view_exchangerate"})
