"""The client's half of a request thread (build-order step 9, spec §5, §9).

The conversation is two-way and open: the client writes to the merchant
whenever they want, whatever the request's status, with or without a file. What
is tested here is that endpoint — who reaches it, what it accepts, and that
everything it writes is scoped to the client the *session* names rather than to
any reference the browser supplies (spec §4).

The rules it delegates to are covered in
:mod:`apps.transactions.test_messaging`; what the merchant then sees of these
messages is covered in :mod:`apps.merchant_panel.tests.test_threads`.
"""

import json
from decimal import Decimal

from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Client as PortalClient
from apps.core.choices import ActorRole
from apps.transactions import messaging
from apps.transactions.models import Attachment, Message, Request, RequestStatus, RequestType

from .test_flow import FLOW_SETTINGS, FlowTestCase, png_upload

THREAD_SETTINGS = dict(FLOW_SETTINGS, PORTAL_MESSAGE_RATE="1000/minute")


@override_settings(**THREAD_SETTINGS)
class ClientThreadTestCase(FlowTestCase):
    """A signed-in client with one request of their own."""

    def setUp(self):
        super().setUp()
        self.session = self.sign_in()
        self.client_record = PortalClient.objects.get(
            b2core_id=self.session["client"]["reference"]
        )
        self.request_obj = self.make_request()
        self.url = reverse(
            "portal:request_messages", args=[self.request_obj.public_ref]
        )

    def make_request(self, *, client=None, **overrides) -> Request:
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=client or self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            merchant_assigned=self.merchant,
            wallet_number_snapshot=self.wallet.number,
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("152000.00"),
            rate_applied=Decimal("1470.00"),
            commission_applied=Decimal("5000.00"),
            status=RequestStatus.ASSIGNED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def send(self, *, url=None, origin="http://testserver", token=None, **data):
        headers = {}
        if origin is not None:
            headers["origin"] = origin
        supplied = token if token is not None else getattr(self, "csrf", "")
        if supplied:
            headers["x-portal-csrf"] = supplied
        payload = {key: value for key, value in data.items() if value is not None}
        return self.client.post(url or self.url, data=payload, headers=headers)


class WritingTests(ClientThreadTestCase):
    def test_a_client_can_write_into_their_own_thread(self):
        response = self.send(body="متى يصل المبلغ؟")

        self.assertEqual(response.status_code, 201, response.content)
        payload = self.body(response)["message"]
        self.assertEqual(payload["body"], "متى يصل المبلغ؟")
        self.assertTrue(payload["mine"])
        self.assertEqual(payload["sender_role"], ActorRole.CLIENT)

        message = Message.objects.get()
        self.assertEqual(message.request, self.request_obj)
        self.assertEqual(message.sender_id, self.client_record.pk)
        self.assertFalse(message.is_internal_note)

    def test_a_client_can_attach_a_file_with_the_same_rules_as_a_proof(self):
        response = self.send(body="هذا الإيصال", attachment=png_upload("extra.png"))

        self.assertEqual(response.status_code, 201, response.content)
        attachment = self.body(response)["message"]["attachment"]
        self.assertEqual(attachment["name"], "extra.png")
        self.assertTrue(attachment["is_image"])
        # Spec §11: never a stored path, always a signed time-limited URL.
        self.assertIn("/portal/attachments/", attachment["url"])
        self.assertEqual(
            Attachment.objects.get(pk=attachment["id"]).uploaded_by_role,
            ActorRole.CLIENT,
        )

    def test_a_file_on_its_own_is_a_message(self):
        response = self.send(attachment=png_upload())
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.body(response)["message"]["body"], "")

    def test_an_empty_message_is_refused(self):
        response = self.send(body="   ")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "message_empty")
        self.assertFalse(Message.objects.exists())

    def test_a_body_over_the_limit_is_refused(self):
        response = self.send(body="x" * (messaging.max_body_chars() + 1))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "message_too_long")

    def test_a_file_that_is_not_what_it_claims_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.send(
            body="إيصال",
            attachment=SimpleUploadedFile("x.png", b"not a png", content_type="image/png"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.body(response)["error"], "attachment_invalid")
        self.assertFalse(Attachment.objects.exists())

    def test_the_thread_stays_open_after_the_request_closes(self):
        """Asked for explicitly: no coupling between the thread and the status."""
        for status in (RequestStatus.CLOSED, RequestStatus.REJECTED):
            with self.subTest(status=status):
                self.request_obj.status = status
                self.request_obj.save(update_fields=["status"])
                self.assertEqual(self.send(body=f"سؤال بعد {status}").status_code, 201)

    def test_a_client_cannot_mark_a_message_as_an_internal_note(self):
        """Not a field the endpoint reads at all — the flag is Finance's."""
        self.send(body="سرّي", is_internal_note="true")
        self.assertFalse(Message.objects.filter(is_internal_note=True).exists())


class ScopeTests(ClientThreadTestCase):
    def test_no_session_writes_nothing(self):
        self.client.cookies.clear()
        response = self.send(body="مرحبًا", token="")

        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Message.objects.exists())

    def test_another_clients_request_does_not_exist(self):
        stranger = PortalClient.objects.create(b2core_id="sub-stranger")
        theirs = self.make_request(client=stranger)

        response = self.send(
            url=reverse("portal:request_messages", args=[theirs.public_ref]),
            body="مرحبًا",
        )
        # 404 rather than 403: guessing a reference must not confirm it exists.
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Message.objects.exists())

    def test_a_request_that_never_existed_answers_the_same_way(self):
        response = self.send(
            url=reverse("portal:request_messages", args=["MP-00000"]), body="مرحبًا"
        )
        self.assertEqual(response.status_code, 404)

    def test_a_write_without_the_session_token_is_refused(self):
        """The portal's own CSRF: a cookie a cross-site page can make the
        browser send, plus a token only this page could have read."""
        response = self.send(body="مرحبًا", token="")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)["error"], "invalid_csrf")

    def test_a_write_from_an_unacceptable_origin_is_refused(self):
        response = self.send(body="مرحبًا", origin="https://evil.example")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.body(response)["error"], "forbidden_origin")


@override_settings(**dict(THREAD_SETTINGS, PORTAL_MESSAGE_RATE="2/minute"))
class RateLimitTests(ClientThreadTestCase):
    def test_the_endpoint_is_capped(self):
        """Spec §11. It writes a row and may carry a file, so it is capped."""
        self.assertEqual(self.send(body="واحد").status_code, 201)
        self.assertEqual(self.send(body="اثنان").status_code, 201)

        response = self.send(body="ثلاثة")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.body(response)["error"], "rate_limited")
        self.assertEqual(Message.objects.count(), 2)


class ReadingTests(ClientThreadTestCase):
    def test_the_thread_comes_back_on_the_request_detail(self):
        self.send(body="سؤال")
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.staff_user().pk,
            body="جوابنا",
        )

        response = self.client.get(
            reverse("portal:request_detail", args=[self.request_obj.public_ref])
        )
        bodies = [m["body"] for m in self.body(response)["request"]["messages"]]
        self.assertEqual(bodies, ["سؤال", "جوابنا"])

    def test_an_internal_note_never_reaches_the_client(self):
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.FINANCE_STAFF,
            sender_id=self.staff_user().pk,
            body="ملاحظة للمالية فقط",
            is_internal_note=True,
        )
        response = self.client.get(
            reverse("portal:request_detail", args=[self.request_obj.public_ref])
        )
        self.assertEqual(self.body(response)["request"]["messages"], [])

    def test_a_merchants_reply_reaches_the_client_labelled_by_role(self):
        """The client learns a merchant replied, never which person."""
        from apps.accounts.models import Role
        from apps.accounts.tests import make_user

        merchant_user = make_user("merchant@example.com", Role.MERCHANT)
        self.merchant.user = merchant_user
        self.merchant.save(update_fields=["user"])
        messaging.post(
            self.request_obj,
            sender_role=ActorRole.MERCHANT,
            sender_id=merchant_user.pk,
            body="سأحوّل خلال ساعة",
        )

        response = self.client.get(
            reverse("portal:request_detail", args=[self.request_obj.public_ref])
        )
        message = self.body(response)["request"]["messages"][0]
        self.assertEqual(message["body"], "سأحوّل خلال ساعة")
        self.assertEqual(message["sender"], "تاجر")
        self.assertFalse(message["mine"])
        self.assertNotIn("sender_id", message)
        self.assertNotIn(merchant_user.email, json.dumps(message, ensure_ascii=False))

    def staff_user(self):
        from apps.accounts.models import Role
        from apps.accounts.permissions import sync_role_groups
        from apps.accounts.tests import make_user

        if not hasattr(self, "_staff"):
            sync_role_groups()
            self._staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        return self._staff
