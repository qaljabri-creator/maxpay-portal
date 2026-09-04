"""Request, attachment and message model invariants (spec §5, §6)."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import Client as PortalClient
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import (
    Message,
    Request,
    RequestStatus,
    RequestType,
    generate_public_ref,
)


class RequestFactoryMixin:
    def build_fixtures(self):
        self.client_record = PortalClient.objects.create(
            b2core_id="b2c-100", display_name="عميل تجريبي", email="client@example.com"
        )
        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.merchant = Merchant.objects.create(name="تاجر أ")
        self.merchant_method = MerchantMethod.objects.create(
            merchant=self.merchant, payment_method=self.method
        )
        self.wallet = Wallet.objects.create(
            merchant_method=self.merchant_method, number="07700000001"
        )

    def make_request(self, **overrides):
        data = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            wallet_number_snapshot=self.wallet.number,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145000.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("2000.00"),
        )
        data.update(overrides)
        return Request.objects.create(**data)


class PublicRefTests(TestCase):
    def test_reference_matches_the_documented_shape(self):
        ref = generate_public_ref()
        self.assertRegex(ref, r"^MP-\d{5}$")

    def test_references_vary(self):
        self.assertGreater(len({generate_public_ref() for _ in range(50)}), 1)


class RequestModelTests(RequestFactoryMixin, TestCase):
    def setUp(self):
        self.build_fixtures()

    def test_a_reference_is_assigned_on_creation(self):
        request = self.make_request()
        self.assertRegex(request.public_ref, r"^MP-\d{5}$")

    def test_references_are_unique_across_requests(self):
        refs = {self.make_request().public_ref for _ in range(15)}
        self.assertEqual(len(refs), 15)

    def test_the_reference_is_not_regenerated_on_later_saves(self):
        request = self.make_request()
        original = request.public_ref
        request.status = RequestStatus.UNDER_REVIEW
        request.save()
        self.assertEqual(request.public_ref, original)

    def test_a_withdrawal_needs_a_destination_account(self):
        request = Request(
            type=RequestType.WITHDRAWAL,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            amount_usd=Decimal("50.00"),
            amount_iqd=Decimal("72500.00"),
            rate_applied=Decimal("1450.00"),
        )
        with self.assertRaises(ValidationError) as ctx:
            request.full_clean()
        self.assertIn("destination_account", ctx.exception.error_dict)

    def test_a_deposit_rejects_a_destination_account(self):
        request = self.make_request()
        request.destination_account = "1234-5678"
        with self.assertRaises(ValidationError) as ctx:
            request.full_clean()
        self.assertIn("destination_account", ctx.exception.error_dict)

    def test_a_deposit_cannot_take_a_withdrawal_only_status(self):
        request = self.make_request()
        request.status = RequestStatus.MERCHANT_PAID
        with self.assertRaises(ValidationError) as ctx:
            request.full_clean()
        self.assertIn("status", ctx.exception.error_dict)

    def test_a_withdrawal_cannot_take_a_deposit_only_status(self):
        request = self.make_request(
            type=RequestType.WITHDRAWAL, destination_account="1234-5678"
        )
        request.status = RequestStatus.CREDITED
        with self.assertRaises(ValidationError) as ctx:
            request.full_clean()
        self.assertIn("status", ctx.exception.error_dict)

    def test_snapshotted_values_are_unaffected_by_later_changes(self):
        """Spec §5 — rate and wallet number are frozen at submission."""
        request = self.make_request()

        self.wallet.deactivate()
        Wallet.objects.create(merchant_method=self.merchant_method, number="07700000099")

        request.refresh_from_db()
        self.assertEqual(request.wallet_number_snapshot, "07700000001")
        self.assertEqual(request.rate_applied, Decimal("1450.00"))

    def test_effective_merchant_prefers_the_routed_one(self):
        other = Merchant.objects.create(name="تاجر ب")
        request = self.make_request()
        self.assertEqual(request.effective_merchant, self.merchant)

        request.merchant_assigned = other
        self.assertEqual(request.effective_merchant, other)

    def test_terminal_statuses_report_as_closed(self):
        request = self.make_request()
        self.assertFalse(request.is_closed)
        request.status = RequestStatus.REJECTED
        self.assertTrue(request.is_closed)

    def test_the_default_status_is_submitted(self):
        self.assertEqual(self.make_request().status, RequestStatus.SUBMITTED)


class MessageTests(RequestFactoryMixin, TestCase):
    def setUp(self):
        self.build_fixtures()
        self.request = self.make_request()

    def test_an_empty_message_is_rejected(self):
        message = Message(request=self.request, sender_role="client", body="   ")
        with self.assertRaises(ValidationError):
            message.full_clean()

    def test_a_client_message_is_labelled_without_identity(self):
        """Spec §5 — merchant-scoped serialisation shows "العميل", never a name."""
        message = Message.objects.create(
            request=self.request, sender_role="client", sender_id=self.client_record.pk,
            body="أرفقت الإيصال",
        )
        self.assertEqual(message.display_sender, "العميل")
        self.assertNotIn(self.client_record.display_name, message.display_sender)

    def test_messages_are_ordered_oldest_first(self):
        first = Message.objects.create(request=self.request, sender_role="client", body="١")
        second = Message.objects.create(request=self.request, sender_role="finance_staff", body="٢")
        self.assertEqual(list(self.request.messages.all()), [first, second])
