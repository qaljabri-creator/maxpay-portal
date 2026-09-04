"""The message thread's rules (build-order step 9, spec §5, §9).

These test what a thread *is*, not the screens that show one: what may be
written, what a file has to be before it is stored, and — the part that matters
most — who may read which message. The three surfaces that drive these rules
have their own suites; if the matrix here is right and they all go through it,
none of them can disagree with the others about who sees what.
"""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user
from apps.core.choices import ActorRole
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions import messaging
from apps.transactions.models import Attachment, Message, Request, RequestStatus, RequestType

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ThreadTestCase(TestCase):
    """One request, and somebody on each side of it."""

    def setUp(self):
        sync_role_groups()
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merchant-a@example.com", Role.MERCHANT)
        self.other_merchant_user = make_user("merchant-b@example.com", Role.MERCHANT)

        method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        merchant = Merchant.objects.create(name="تاجر أ", user=self.merchant_user)
        link = MerchantMethod.objects.create(merchant=merchant, payment_method=method)
        Wallet.objects.create(merchant_method=link, number="07700000001")

        self.client_record = PortalClient.objects.create(
            b2core_id="sub-1", display_name="زينب الجبوري", email="z@example.com"
        )
        self.request_obj = Request.objects.create(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=method,
            merchant_selected=merchant,
            merchant_assigned=merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.ASSIGNED,
        )

    def png(self, name="proof.png"):
        return SimpleUploadedFile(name, PNG, content_type="image/png")


class WritingTests(ThreadTestCase):
    def test_a_message_needs_a_body_or_a_file(self):
        with self.assertRaises(messaging.MessageError) as caught:
            messaging.post(
                self.request_obj, sender_role=ActorRole.CLIENT, sender_id=1, body="   "
            )
        self.assertEqual(caught.exception.code, "message_empty")
        self.assertFalse(Message.objects.exists())

    def test_a_file_alone_is_a_message(self):
        """Sending a receipt with nothing typed is a normal thing to do."""
        message = messaging.post(
            self.request_obj,
            sender_role=ActorRole.CLIENT,
            sender_id=self.client_record.pk,
            upload=self.png(),
        )
        self.assertEqual(message.body, "")
        self.assertIsNotNone(message.attachment)
        self.assertEqual(message.attachment.uploaded_by_role, ActorRole.CLIENT)

    def test_a_body_longer_than_the_limit_is_refused(self):
        with self.assertRaises(messaging.MessageError) as caught:
            messaging.post(
                self.request_obj,
                sender_role=ActorRole.CLIENT,
                sender_id=1,
                body="x" * (messaging.max_body_chars() + 1),
            )
        self.assertEqual(caught.exception.code, "message_too_long")

    def test_a_file_that_is_not_what_it_claims_is_refused(self):
        """The same gate the proof upload passes (spec §11): the bytes decide."""
        with self.assertRaises(messaging.MessageError) as caught:
            messaging.post(
                self.request_obj,
                sender_role=ActorRole.MERCHANT,
                sender_id=self.merchant_user.pk,
                body="التحويل",
                upload=SimpleUploadedFile("x.png", b"not a png", content_type="image/png"),
            )
        self.assertEqual(caught.exception.code, "attachment_invalid")

    def test_a_refused_file_leaves_no_message_and_no_attachment_behind(self):
        with self.assertRaises(messaging.MessageError):
            messaging.post(
                self.request_obj,
                sender_role=ActorRole.MERCHANT,
                sender_id=self.merchant_user.pk,
                body="التحويل",
                upload=SimpleUploadedFile("x.png", b"nope", content_type="image/png"),
            )
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Attachment.objects.exists())

    def test_a_client_may_never_write_an_internal_note(self):
        """The client is the one audience an internal note exists to be kept
        from; letting them author one would be incoherent."""
        with self.assertRaises(messaging.MessageError) as caught:
            messaging.post(
                self.request_obj,
                sender_role=ActorRole.CLIENT,
                sender_id=1,
                body="سرّي",
                is_internal_note=True,
            )
        self.assertEqual(caught.exception.status, 403)

    def test_a_merchant_may_write_one_as_a_handback_note(self):
        """Finance review, 24 Aug 2026. A merchant returning a request has to
        say why, and that reason is not the client's business."""
        note = messaging.post(
            self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=7,
            body="لا سيولة اليوم",
            is_internal_note=True,
        )

        self.assertTrue(note.is_internal_note)
        self.assertEqual(note.sender_role, ActorRole.MERCHANT)

    def test_the_thread_is_not_gated_on_the_requests_status(self):
        """Spec §9 as asked for: an open conversation, whatever the request did.

        A question after a request closed is still a question, and refusing it
        would only move the conversation somewhere nobody can audit.
        """
        for status in (RequestStatus.SUBMITTED, RequestStatus.CLOSED, RequestStatus.REJECTED):
            with self.subTest(status=status):
                self.request_obj.status = status
                self.request_obj.save(update_fields=["status"])
                message = messaging.post(
                    self.request_obj,
                    sender_role=ActorRole.CLIENT,
                    sender_id=self.client_record.pk,
                    body=f"سؤال في الحالة {status}",
                )
                self.assertIsNotNone(message.pk)


class VisibilityTests(ThreadTestCase):
    """The matrix. One thread with one of everything in it, read three ways."""

    def setUp(self):
        super().setUp()
        self.from_client = messaging.post(
            self.request_obj,
            sender_role=ActorRole.CLIENT,
            sender_id=self.client_record.pk,
            body="حوّلت المبلغ.",
        )
        self.from_finance = messaging.post(
            self.request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.staff.pk,
            body="بانتظار تأكيد التاجر.",
        )
        self.internal = messaging.post(
            self.request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.staff.pk,
            body="العميل زينب الجبوري سبق أن تأخر.",
            is_internal_note=True,
        )
        self.from_merchant = messaging.post(
            self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=self.merchant_user.pk,
            body="وصلني المبلغ.",
        )
        self.from_other_merchant = messaging.post(
            self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=self.other_merchant_user.pk,
            body="لم يصلني شيء.",
        )

    def seen_by(self, audience, viewer_id=None):
        return messaging.visible_messages(
            self.request_obj, audience=audience, viewer_id=viewer_id
        )

    def test_finance_reads_everything_including_its_own_notes(self):
        self.assertEqual(len(self.seen_by(messaging.FINANCE)), 5)

    def test_the_client_reads_everything_except_internal_notes(self):
        seen = self.seen_by(messaging.CLIENT)
        self.assertNotIn(self.internal, seen)
        self.assertIn(self.from_merchant, seen)
        self.assertIn(self.from_finance, seen)
        self.assertEqual(len(seen), 4)

    def test_the_merchant_reads_neither_notes_nor_another_merchants_words(self):
        seen = self.seen_by(messaging.MERCHANT, viewer_id=self.merchant_user.pk)
        self.assertNotIn(self.internal, seen)
        self.assertNotIn(self.from_other_merchant, seen)
        self.assertIn(self.from_merchant, seen)
        self.assertIn(self.from_client, seen)
        self.assertEqual(len(seen), 3)

    def test_a_merchant_with_no_identity_sees_no_merchant_messages_at_all(self):
        """Belt and braces: an unknown viewer must not fall through to "all"."""
        seen = self.seen_by(messaging.MERCHANT, viewer_id=None)
        self.assertNotIn(self.from_merchant, seen)
        self.assertNotIn(self.from_other_merchant, seen)

    def test_a_sender_role_nobody_whitelisted_reaches_nobody(self):
        stray = Message.objects.create(
            request=self.request_obj, sender_role="auditor", body="من أنا؟"
        )
        self.assertNotIn(stray, self.seen_by(messaging.CLIENT))
        self.assertNotIn(
            stray, self.seen_by(messaging.MERCHANT, viewer_id=self.merchant_user.pk)
        )
        # Finance still sees it — they are the ones who would have to explain it.
        self.assertIn(stray, self.seen_by(messaging.FINANCE))
