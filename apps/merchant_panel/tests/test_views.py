"""The merchant panel's screens (spec §8) — build-order step 8.

Two things are proved here that a JSON sweep cannot.

The first is *where* the masking happens. Spec §2 requires it at the serializer
level rather than the template level, and the difference is testable: the
context these views hand a template holds dictionaries, not ``Request`` rows.
A template that tried to render the client would have nothing to render it
from. See :class:`ContextTests`.

The second is that the panel's write path is the shared state machine and
nothing else — a merchant's confirm, payment and rejection go through
``apply_transition``, which stamps, audits, and refuses a move that is not
theirs.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.urls import reverse

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.core.models import AuditLog
from apps.transactions.models import Attachment, Request, RequestStatus, RequestType

from .support import PNG, MerchantPanelTestCase


def _context_dicts(context):
    """Flatten whatever ``response.context`` happens to be into plain dicts."""
    if context is None:
        return []
    if isinstance(context, dict):
        return [context]
    layers = []
    for item in context:
        if isinstance(item, dict):
            layers.append(item)
        else:
            layers.extend(_context_dicts(getattr(item, "dicts", [])))
    return layers


class AccessTests(MerchantPanelTestCase):
    """Who reaches the screens."""

    def screens(self) -> list[str]:
        return [
            reverse("merchant_panel:queue"),
            reverse("merchant_panel:wallets"),
            reverse(
                "merchant_panel:request_detail", args=[self.request_obj.public_ref]
            ),
        ]

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        for url in self.screens():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("two_factor:login"), response["Location"])

    def test_finance_is_refused(self):
        verify_otp(self.client, self.finance)
        for url in self.screens():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_merchant_reaches_their_own_panel(self):
        self.login()
        for url in self.screens():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_another_merchants_request_is_a_404(self):
        self.login()
        theirs = self.make_request(assigned_to=self.other)
        response = self.client.get(
            reverse("merchant_panel:request_detail", args=[theirs.public_ref])
        )
        self.assertEqual(response.status_code, 404)

    def test_login_lands_a_merchant_on_their_own_panel(self):
        self.login()
        response = self.client.get(reverse("home"))
        self.assertRedirects(
            response, reverse("merchant_panel:queue"), fetch_redirect_response=False
        )


class ContextTests(MerchantPanelTestCase):
    """Spec §2 is enforced before the template, so the template has nothing to leak.

    This is the difference between masking in a serializer and masking in a
    template, made into an assertion: there is no ``Request`` and no ``Client``
    in what these views hand to a template, so no template edit — careless,
    malicious, or merely optimistic about what ``req.client`` might do — can
    surface a name.
    """

    def setUp(self):
        super().setUp()
        self.add_thread()
        self.login()

    @staticmethod
    def _model_instances(context):
        """Every model instance sitting directly in a rendered context.

        ``response.context`` is a list of ``Context`` objects, each of which is
        a stack of plain dicts, so both layers have to be unwrapped before the
        values are in reach.
        """
        found = []
        for layer in _context_dicts(context):
            for key, value in layer.items():
                if isinstance(value, models.Model):
                    found.append((key, type(value).__name__))
                elif isinstance(value, models.QuerySet):
                    found.append((key, f"QuerySet[{value.model.__name__}]"))
        return found

    def test_the_detail_screen_renders_from_a_dictionary(self):
        response = self.client.get(
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref])
        )
        self.assertIsInstance(response.context["req"], dict)
        self.assertEqual(
            response.context["req"]["reference"], self.request_obj.public_ref
        )

    def test_the_queue_renders_from_dictionaries(self):
        response = self.client.get(reverse("merchant_panel:queue"))
        rows = response.context["requests"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIsInstance(row, dict)

    def test_no_request_or_client_row_is_ever_put_in_a_template_context(self):
        for url in (
            reverse("merchant_panel:queue"),
            reverse("merchant_panel:wallets"),
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                leaked = [
                    entry
                    for entry in self._model_instances(response.context)
                    if entry[1] in {Request.__name__, PortalClient.__name__}
                    or entry[1].startswith(("QuerySet[Request", "QuerySet[Client"))
                ]
                self.assertEqual(leaked, [], f"{url} put a live row in the context.")

    def test_the_rendered_pages_carry_no_client_identity(self):
        for url in (
            reverse("merchant_panel:queue"),
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref]),
            reverse("merchant_panel:wallets"),
        ):
            with self.subTest(url=url):
                self.assertBodyHasNoIdentity(self.client.get(url), f"GET {url}")

    def test_the_detail_screen_says_the_client_is_hidden_rather_than_absent(self):
        """Silence would read as an oversight. The panel says it is deliberate."""
        response = self.client.get(
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref])
        )
        self.assertContains(response, "هوية العميل غير متاحة للتجار")


class ActionTests(MerchantPanelTestCase):
    """Spec §8: confirm execution, upload proof, reject with reason."""

    def setUp(self):
        super().setUp()
        self.login()
        self.detail_url = reverse(
            "merchant_panel:request_detail", args=[self.request_obj.public_ref]
        )

    def act(self, action, request_obj=None, **data):
        reference = (request_obj or self.request_obj).public_ref
        return self.client.post(
            reverse("merchant_panel:request_action", args=[reference, action]),
            data,
            follow=True,
        )

    def test_confirming_a_deposit_moves_it_and_writes_an_audit_entry(self):
        before = AuditLog.objects.count()
        response = self.act("confirm")
        self.assertEqual(response.status_code, 200)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.MERCHANT_CONFIRMED)
        self.assertIsNotNone(self.request_obj.merchant_actioned_at)
        self.assertEqual(AuditLog.objects.count(), before + 1)

    def test_rejecting_without_a_reason_changes_nothing(self):
        response = self.act("reject", **{"reject-reason": ""})
        self.assertContains(response, "اكتب سبب الرفض")
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.ASSIGNED)

    def test_rejecting_posts_the_reason_into_the_thread(self):
        self.act("reject", **{"reject-reason": "لم يصل المبلغ إلى المحفظة."})
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.REJECTED)
        self.assertEqual(
            self.request_obj.messages.filter(sender_role=ActorRole.MERCHANT).count(), 1
        )

    def test_a_merchant_cannot_act_on_somebody_elses_request(self):
        theirs = self.make_request(assigned_to=self.other)
        response = self.act("confirm", request_obj=theirs)
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, RequestStatus.ASSIGNED)

    def test_a_finance_move_is_not_reachable_from_this_panel(self):
        for action in ("route", "credit", "close", "review"):
            with self.subTest(action=action):
                response = self.act(action)
                self.assertEqual(response.status_code, 403)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.ASSIGNED)

    def test_confirming_twice_is_refused_rather_than_silently_repeated(self):
        self.act("confirm")
        response = self.act("confirm")
        self.assertContains(response, "تغيّرت حالة الطلب")

    def test_a_deposit_cannot_be_marked_paid(self):
        """``pay`` belongs to withdrawals. The lifecycle, not the screen, says so."""
        response = self.act(
            "pay",
            **{"pay-proof": SimpleUploadedFile("p.png", PNG, content_type="image/png")},
        )
        self.assertContains(response, "لا ينطبق على هذا النوع")
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.ASSIGNED)


class WithdrawalActionTests(MerchantPanelTestCase):
    """Spec §6, §8: the merchant pays, and files the proof of transfer.

    The client-facing withdrawal flow is build-order step 10; the merchant's
    half of it is part of the lifecycle and is already in the state machine, so
    the panel wires it now rather than leaving a button that half-works later.
    """

    def setUp(self):
        super().setUp()
        self.login()
        self.withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            destination_account="6274-1111-2222-3333",
            wallet_number_snapshot="",
        )

    def pay(self, **data):
        return self.client.post(
            reverse(
                "merchant_panel:request_action", args=[self.withdrawal.public_ref, "pay"]
            ),
            data,
            follow=True,
        )

    def test_marking_paid_requires_proof(self):
        response = self.pay()
        self.assertContains(response, "أرفق صورة أو ملف PDF")
        self.withdrawal.refresh_from_db()
        self.assertEqual(self.withdrawal.status, RequestStatus.ASSIGNED)

    def test_marking_paid_stores_the_proof_against_the_request(self):
        response = self.pay(
            **{
                "pay-proof": SimpleUploadedFile(
                    "transfer.png", PNG, content_type="image/png"
                )
            }
        )
        self.assertEqual(response.status_code, 200)
        self.withdrawal.refresh_from_db()
        self.assertEqual(self.withdrawal.status, RequestStatus.MERCHANT_PAID)

        attachment = self.withdrawal.attachments.get()
        self.assertEqual(attachment.uploaded_by_role, ActorRole.MERCHANT)
        self.assertEqual(attachment.content_type, "image/png")

    def test_a_file_that_is_not_what_it_claims_is_refused_and_nothing_moves(self):
        response = self.pay(
            **{
                "pay-proof": SimpleUploadedFile(
                    "transfer.png", b"not a png at all", content_type="image/png"
                )
            }
        )
        self.assertEqual(response.status_code, 200)
        self.withdrawal.refresh_from_db()
        self.assertEqual(self.withdrawal.status, RequestStatus.ASSIGNED)
        self.assertFalse(self.withdrawal.attachments.exists())


class AttachmentTests(MerchantPanelTestCase):
    """Spec §11: signed, time-limited, and scoped to the merchant it was minted for."""

    def setUp(self):
        super().setUp()
        self.attachment = Attachment.objects.create(
            request=self.request_obj,
            file=SimpleUploadedFile("proof.png", PNG, content_type="image/png"),
            content_type="image/png",
            uploaded_by_role=ActorRole.CLIENT,
            uploaded_by_id=self.client_record.pk,
        )

    def signed_url(self, user=None):
        from apps.merchant_panel.attachments import sign

        return reverse(
            "merchant_panel:attachment",
            args=[self.attachment.pk, sign(self.attachment, user or self.user)],
        )

    def test_the_owning_merchant_can_open_it(self):
        self.login()
        response = self.client.get(self.signed_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PNG)

    def test_a_token_minted_for_somebody_else_is_a_404(self):
        self.login()
        self.assertEqual(self.client.get(self.signed_url(self.other_user)).status_code, 404)

    def test_another_merchant_cannot_open_it_even_with_their_own_token(self):
        verify_otp(self.client, self.other_user)
        self.assertEqual(self.client.get(self.signed_url(self.other_user)).status_code, 404)

    def test_a_finance_token_does_not_open_a_merchant_url(self):
        from apps.finance.attachments import sign as finance_sign

        self.login()
        url = reverse(
            "merchant_panel:attachment",
            args=[self.attachment.pk, finance_sign(self.attachment, self.user)],
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_stored_path_is_never_a_link(self):
        """MEDIA_ROOT is not routed (spec §11); the only way in is the signed view."""
        self.login()
        self.assertEqual(self.client.get(f"/media/{self.attachment.file.name}").status_code, 404)

    def test_a_merchant_without_the_permission_is_refused(self):
        stripped = make_user("noperm@example.com", Role.MERCHANT)
        self.merchant.user = stripped
        self.merchant.save(update_fields=["user"])
        verify_otp(self.client, stripped)
        # After signing in, not before: logging in stamps ``last_login``, and
        # the post_save signal that mirrors the role into group membership
        # would hand the permission straight back.
        stripped.groups.clear()
        self.assertEqual(self.client.get(self.signed_url(stripped)).status_code, 403)
