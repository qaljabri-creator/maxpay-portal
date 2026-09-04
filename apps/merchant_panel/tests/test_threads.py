"""The merchant's half of a request thread (build-order step 9, spec §5, §8, §9).

Two things are being held at once here, and they pull in opposite directions.
The conversation has to be a real one — the merchant writes to the client
whenever they want, with files, whatever the request's status. And spec §2 has
to survive it: everything the merchant reads back, messages and attachments
included, still names nobody.

So the sweeps from :mod:`.test_api` are run again over a thread that has one of
everything in it. A masking guarantee that only holds for a request with an
empty thread is not a guarantee.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.transactions import messaging
from apps.transactions.models import Attachment, Message, RequestStatus

from .support import IDENTITY_MARKERS, PNG, MerchantPanelTestCase


class MerchantThreadTestCase(MerchantPanelTestCase):
    """A signed-in merchant holding one request."""

    def setUp(self):
        super().setUp()
        self.login()
        self.url = reverse(
            "merchant_panel:request_message", args=[self.request_obj.public_ref]
        )
        self.detail_url = reverse(
            "merchant_panel:request_detail", args=[self.request_obj.public_ref]
        )
        self.api_url = reverse(
            "merchant_panel:api_request_detail", args=[self.request_obj.public_ref]
        )

    def send(self, *, url=None, follow=True, **data):
        payload = {
            f"message-{key}": value
            for key, value in data.items()
            if value is not None
        }
        return self.client.post(url or self.url, payload, follow=follow)

    def png(self, name="transfer.png"):
        return SimpleUploadedFile(name, PNG, content_type="image/png")

    def thread(self):
        return self.client.get(self.api_url).json()["messages"]


class WritingTests(MerchantThreadTestCase):
    def test_a_merchant_can_write_to_the_client(self):
        response = self.send(body="سأحوّل خلال ساعة.")
        self.assertEqual(response.status_code, 200)

        message = Message.objects.get()
        self.assertEqual(message.sender_role, ActorRole.MERCHANT)
        self.assertEqual(message.sender_id, self.user.pk)
        self.assertFalse(message.is_internal_note)

    def test_a_merchant_can_attach_a_file(self):
        self.send(body="هذا إيصال التحويل", attachment=self.png())

        attachment = Attachment.objects.get()
        self.assertEqual(attachment.uploaded_by_role, ActorRole.MERCHANT)
        self.assertEqual(attachment.content_type, "image/png")
        self.assertEqual(Message.objects.get().attachment, attachment)

    def test_an_empty_message_writes_nothing_and_says_why(self):
        response = self.send(body="   ")
        self.assertContains(response, "اكتب رسالة أو أرفق ملفًا")
        self.assertFalse(Message.objects.exists())

    def test_a_file_that_is_not_what_it_claims_writes_nothing(self):
        self.send(
            body="إيصال",
            attachment=SimpleUploadedFile("x.png", b"nope", content_type="image/png"),
        )
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Attachment.objects.exists())

    def test_the_thread_stays_open_after_the_request_closes(self):
        for status in (RequestStatus.CLOSED, RequestStatus.REJECTED):
            with self.subTest(status=status):
                Message.objects.all().delete()
                self.request_obj.status = status
                self.request_obj.save(update_fields=["status"])
                self.send(body=f"سؤال بعد {status}")
                self.assertEqual(Message.objects.count(), 1)

    def test_a_merchant_cannot_write_into_a_thread_that_is_not_theirs(self):
        theirs = self.make_request(assigned_to=self.other)
        response = self.send(
            url=reverse("merchant_panel:request_message", args=[theirs.public_ref]),
            body="مرحبًا",
            follow=False,
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Message.objects.exists())

    def test_a_merchant_without_the_permission_is_refused(self):
        stripped = make_user("noperm@example.com", Role.MERCHANT)
        self.merchant.user = stripped
        self.merchant.save(update_fields=["user"])
        verify_otp(self.client, stripped)
        # After signing in: logging in re-attaches the role group.
        stripped.groups.clear()

        response = self.send(body="مرحبًا", follow=False)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Message.objects.exists())

    def test_finance_cannot_reach_the_merchant_composer(self):
        verify_otp(self.client, self.finance)
        response = self.send(body="مرحبًا", follow=False)
        self.assertEqual(response.status_code, 403)


class ReadingTests(MerchantThreadTestCase):
    def test_a_merchant_reads_their_own_message_back_as_theirs(self):
        self.send(body="سأحوّل خلال ساعة.")
        message = self.thread()[0]
        self.assertEqual(message["sender"], "أنت")
        self.assertEqual(message["body"], "سأحوّل خلال ساعة.")

    def test_the_client_is_never_named(self):
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.CLIENT,
            sender_id=self.client_record.pk,
            body="حوّلت المبلغ.",
        )
        message = self.thread()[0]
        self.assertEqual(message["sender"], "العميل")
        self.assertNotIn("sender_id", message)

    def test_both_finance_roles_read_as_one_desk(self):
        for role in (ActorRole.FINANCE_ADMIN, ActorRole.FINANCE_STAFF):
            messaging.post(
                self.request_obj,
                sender_role=role,
                sender_id=self.finance.pk,
                body=f"من {role}",
            )
        self.assertEqual({m["sender"] for m in self.thread()}, {"المالية"})

    def test_an_internal_note_is_absent_rather_than_flagged(self):
        self.add_thread()  # its internal note carries the client's real name
        thread = self.thread()
        self.assertEqual(len(thread), 2)
        for message in thread:
            self.assertNotIn("is_internal_note", message)

    def test_another_merchants_message_does_not_travel_with_a_reroute(self):
        Message.objects.create(
            request=self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=self.other_user.pk,
            body="لم يصلني شيء.",
        )
        self.send(body="وصلني المبلغ.")
        self.assertEqual([m["body"] for m in self.thread()], ["وصلني المبلغ."])


class AnonymitySweepTests(MerchantThreadTestCase):
    """Spec §2, over a thread rather than over an empty request.

    A message body is prose a merchant is meant to read, so nothing filters it;
    what must hold is that no *field* around it names the client, in the JSON
    or in the rendered page, however deep it is nested.
    """

    def setUp(self):
        super().setUp()
        # One of everything: both directions, both with files, plus the
        # internal note that carries the client's real name.
        Attachment.objects.create(
            request=self.request_obj,
            file=self.png("client-proof.png"),
            content_type="image/png",
            uploaded_by_role=ActorRole.CLIENT,
            uploaded_by_id=self.client_record.pk,
        )
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.CLIENT,
            sender_id=self.client_record.pk,
            body="حوّلت المبلغ، هذا الإيصال.",
            upload=self.png("client-message.png"),
        )
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.finance.pk,
            body="بانتظار تأكيدك.",
        )
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.finance.pk,
            body=f"العميل {IDENTITY_MARKERS['display_name']} تأخر سابقًا.",
            is_internal_note=True,
        )
        self.send(body="وصلني المبلغ.", attachment=self.png("merchant-proof.png"))

    def test_the_api_payload_names_nobody(self):
        response = self.client.get(self.api_url)
        self.assertNoIdentity(response.json(), "the merchant detail payload")
        self.assertBodyHasNoIdentity(response, "GET the merchant API")

    def test_every_message_and_attachment_is_swept(self):
        payload = self.client.get(self.api_url).json()

        # The sweep proves nothing if the thread came back empty.
        self.assertEqual(len(payload["messages"]), 3)
        self.assertTrue(any(m["attachment"] for m in payload["messages"]))
        self.assertEqual(len(payload["attachments"]), 3)

        self.assertNoIdentity(payload["messages"], "the thread")
        self.assertNoIdentity(payload["attachments"], "the attachments")

    def test_the_rendered_page_names_nobody(self):
        self.assertBodyHasNoIdentity(
            self.client.get(self.detail_url), "the merchant detail page"
        )

    def test_the_composer_is_on_the_page(self):
        response = self.client.get(self.detail_url)
        self.assertContains(response, self.url)
        self.assertContains(response, "اكتب ردك للعميل")

    def test_a_client_uploaded_file_is_credited_to_a_role_not_a_person(self):
        payload = self.client.get(self.api_url).json()
        uploaders = {a["uploaded_by"] for a in payload["attachments"]}
        self.assertEqual(uploaders, {"العميل", "أنت"})
