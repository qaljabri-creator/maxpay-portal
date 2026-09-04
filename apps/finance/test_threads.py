"""Finance inside a request thread (build-order step 9, spec §3, §9).

Finance is the third participant, not a moderator: they read everything and
write into the thread like anyone else. The only thing that makes their box
different is that it has two audiences behind it — an ordinary message the
client and the merchant both read, and an internal note that stays on the desk.

Which is exactly why the reminder is tested here rather than assumed. Free text
is the one thing spec §2's serializer masking cannot reach, so the warning above
the reply box is a real control and it must actually be on the page.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.choices import ActorRole
from apps.transactions import messaging
from apps.transactions.models import Attachment, Message, RequestStatus

from .test_queue import PNG, QueueTestCase


class FinanceThreadTestCase(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.staff)

    def message_url(self, request_obj=None):
        return reverse(
            "finance:request_message", args=[(request_obj or self.deposit).public_ref]
        )

    def send(self, *, request_obj=None, follow=True, **data):
        payload = {
            f"message-{key}": value for key, value in data.items() if value is not None
        }
        return self.client.post(self.message_url(request_obj), payload, follow=follow)

    def png(self, name="note.png"):
        return SimpleUploadedFile(name, PNG, content_type="image/png")

    def assign(self):
        self.deposit.merchant_assigned = self.merchant
        self.deposit.status = RequestStatus.ASSIGNED
        self.deposit.save(update_fields=["merchant_assigned", "status"])


class WritingTests(FinanceThreadTestCase):
    def test_finance_writes_into_the_thread(self):
        response = self.send(body="راجعنا الإيصال، شكرًا.")
        self.assertEqual(response.status_code, 200)

        message = Message.objects.get()
        self.assertEqual(message.sender_role, ActorRole.FINANCE_STAFF)
        self.assertEqual(message.sender_id, self.staff.pk)
        self.assertFalse(message.is_internal_note)

    def test_finance_can_attach_a_file(self):
        self.send(body="المرفق المطلوب", attachment=self.png())

        attachment = Attachment.objects.get()
        self.assertEqual(attachment.uploaded_by_role, ActorRole.FINANCE_STAFF)
        self.assertEqual(Message.objects.get().attachment, attachment)

    def test_the_checkbox_is_what_makes_a_note_internal(self):
        self.send(body="سبب داخلي", is_internal_note="on")
        self.assertTrue(Message.objects.get().is_internal_note)

    def test_an_empty_message_writes_nothing(self):
        response = self.send(body="  ")
        self.assertContains(response, "اكتب رسالة أو أرفق ملفًا")
        self.assertFalse(Message.objects.exists())

    def test_the_thread_is_not_gated_on_the_status(self):
        for status in (RequestStatus.CLOSED, RequestStatus.REJECTED):
            with self.subTest(status=status):
                Message.objects.all().delete()
                self.deposit.status = status
                self.deposit.save(update_fields=["status"])
                self.send(body=f"متابعة بعد {status}")
                self.assertEqual(Message.objects.count(), 1)

    def test_a_user_without_the_permission_is_refused(self):
        self.revoke("finance_staff", "add_message")
        self.login(self.staff)

        response = self.send(body="مرحبًا", follow=False)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Message.objects.exists())

    def test_a_merchant_cannot_reach_the_finance_composer(self):
        self.login(self.merchant_user)
        response = self.send(body="مرحبًا", follow=False)
        self.assertEqual(response.status_code, 403)

    def test_the_confirmation_says_who_can_now_read_it(self):
        """Which of the two audiences a message went to is the thing worth
        confirming — "saved" would not tell the operator anything."""
        self.assign()
        response = self.send(body="رسالة عامة")
        self.assertContains(response, self.merchant.name)

        response = self.send(body="ملاحظة", is_internal_note="on")
        self.assertContains(response, "لا يراها العميل ولا التاجر")


class ReachTests(FinanceThreadTestCase):
    """Where a Finance message actually lands."""

    def test_an_ordinary_message_reaches_both_the_client_and_the_merchant(self):
        self.assign()
        self.send(body="سنحوّل اليوم")
        message = Message.objects.get()

        self.assertIn(
            message,
            messaging.visible_messages(self.deposit, audience=messaging.CLIENT),
        )
        self.assertIn(
            message,
            messaging.visible_messages(
                self.deposit,
                audience=messaging.MERCHANT,
                viewer_id=self.merchant_user.pk,
            ),
        )

    def test_an_internal_note_reaches_neither(self):
        self.assign()
        self.send(body="ملاحظة للمالية", is_internal_note="on")
        message = Message.objects.get()

        self.assertNotIn(
            message,
            messaging.visible_messages(self.deposit, audience=messaging.CLIENT),
        )
        self.assertNotIn(
            message,
            messaging.visible_messages(
                self.deposit,
                audience=messaging.MERCHANT,
                viewer_id=self.merchant_user.pk,
            ),
        )
        # And Finance keeps it, which is the point of writing one.
        self.assertIn(
            message, messaging.visible_messages(self.deposit, audience=messaging.FINANCE)
        )

    def test_finance_reads_the_whole_thread_including_a_merchants_words(self):
        """Spec §9: full read access. Nothing on a thread is hidden from Finance."""
        self.assign()
        for role, sender in (
            (ActorRole.CLIENT, self.client_record.pk),
            (ActorRole.MERCHANT, self.merchant_user.pk),
        ):
            messaging.post(
                self.deposit, sender_role=role, sender_id=sender, body=f"من {role}"
            )
        response = self.client.get(self.detail_url())
        self.assertEqual(len(response.context["thread"]), 2)


class ReminderTests(FinanceThreadTestCase):
    """The warning on the reply box (asked for explicitly).

    Not conditional on anything the operator has already done: the box is
    always capable of sending prose to a merchant, so the reminder is always
    on it.
    """

    def test_the_reply_box_warns_against_naming_the_client(self):
        response = self.client.get(self.detail_url())
        self.assertContains(response, "لا تذكر اسم العميل")

    def test_it_names_the_merchant_who_is_reading(self):
        self.assign()
        response = self.client.get(self.detail_url())
        self.assertContains(response, self.merchant.name)
        self.assertContains(response, "كما تكتبها بالضبط")

    def test_an_unassigned_request_is_still_warned_about(self):
        """A message written today is read by whichever merchant is routed
        tomorrow, so "not assigned yet" is not "safe to name them"."""
        self.assertIsNone(self.deposit.merchant_assigned_id)
        response = self.client.get(self.detail_url())
        self.assertContains(response, "لا تذكر اسم العميل")
        self.assertContains(response, "يقرأ الخيط كاملًا")

    def test_it_points_at_the_internal_note_as_the_way_out(self):
        response = self.client.get(self.detail_url())
        self.assertContains(response, "ملاحظة داخلية")

    def test_a_user_without_the_permission_sees_no_box_at_all(self):
        self.revoke("finance_staff", "add_message")
        self.login(self.staff)

        response = self.client.get(self.detail_url())
        self.assertNotContains(response, "لا تذكر اسم العميل")
        self.assertContains(response, "لا يملك صلاحية الكتابة")
