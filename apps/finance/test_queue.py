"""The Finance queue, routing and approval screens (build-order step 7).

The lifecycle rules themselves are covered in
:mod:`apps.transactions.test_services`. What is tested here is the panel: who
reaches it, what the filters return, what a POST actually does, whether client
identity appears only where it is allowed to, and whether a proof file is
reachable by anyone but the person the link was minted for.
"""

from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import Attachment, Request, RequestStatus, RequestType
from apps.transactions.services import apply_transition

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class QueueTestCase(TestCase):
    def setUp(self):
        sync_role_groups()
        self.admin = make_user("root@maxifyfx.com", Role.FINANCE_ADMIN)
        self.staff = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)
        self.merchant_user = make_user("merch@example.com", Role.MERCHANT)

        self.method = PaymentMethod.objects.create(
            code="zaincash", caption_ar="زين كاش", caption_en="ZainCash"
        )
        self.other_method = PaymentMethod.objects.create(
            code="fastpay", caption_ar="فاست باي", caption_en="FastPay"
        )
        self.merchant = self.make_merchant("تاجر أ", self.method, user=self.merchant_user)
        self.alternate = self.make_merchant("تاجر ب", self.method)
        self.unrelated = self.make_merchant("تاجر ج", self.other_method)

        self.client_record = PortalClient.objects.create(
            b2core_id="sub-1",
            display_name="زينب الجبوري",
            email="zainab@example.com",
            account_number="MX-90210",
        )
        self.deposit = self.make_request()

    def make_merchant(self, name, method, user=None) -> Merchant:
        merchant = Merchant.objects.create(name=name, user=user)
        link = MerchantMethod.objects.create(merchant=merchant, payment_method=method)
        Wallet.objects.create(merchant_method=link, number="07700000001")
        return merchant

    def make_request(self, **overrides) -> Request:
        defaults = dict(
            type=RequestType.DEPOSIT,
            client=self.client_record,
            payment_method=self.method,
            merchant_selected=self.merchant,
            wallet_number_snapshot="07700000001",
            amount_usd=Decimal("100.00"),
            amount_iqd=Decimal("145500.00"),
            rate_applied=Decimal("1450.00"),
            commission_applied=Decimal("500.00"),
            status=RequestStatus.SUBMITTED,
        )
        defaults.update(overrides)
        return Request.objects.create(**defaults)

    def login(self, user):
        verify_otp(self.client, user)
        return user

    @staticmethod
    def revoke(role, codename):
        """Take a permission away from a whole role.

        Per-*user* revocation is not available: signing in re-attaches the role
        group (``update_last_login`` fires ``post_save``, which mirrors
        ``User.role`` back into membership), so anything the group grants comes
        straight back. The group is therefore where a permission is actually
        withdrawn, and that is what these tests exercise.
        """
        Group.objects.get(name=role).permissions.remove(
            Permission.objects.get(codename=codename)
        )

    def detail_url(self, request_obj=None):
        return reverse("finance:request_detail", args=[(request_obj or self.deposit).public_ref])

    def action_url(self, action, request_obj=None):
        return reverse(
            "finance:request_action", args=[(request_obj or self.deposit).public_ref, action]
        )

    def post_action(self, action, request_obj=None, **fields):
        payload = {f"{action}-{name}": value for name, value in fields.items()}
        return self.client.post(self.action_url(action, request_obj), payload)


class AccessTests(QueueTestCase):
    def test_a_merchant_cannot_reach_the_queue(self):
        self.login(self.merchant_user)
        self.assertEqual(self.client.get(reverse("finance:request_list")).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 403)

    def test_a_merchant_cannot_post_an_action(self):
        self.login(self.merchant_user)
        self.assertEqual(self.post_action("review").status_code, 403)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.SUBMITTED)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("finance:request_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:login"), response["Location"])

    def test_finance_staff_can_read_both_screens(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(reverse("finance:request_list")).status_code, 200)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 200)

    def test_a_merchant_action_is_not_reachable_from_the_finance_panel(self):
        self.login(self.admin)
        apply_transition(self.deposit, "review", actor=self.admin)
        apply_transition(self.deposit, "route", actor=self.admin, merchant=self.merchant)
        # "confirm" belongs to the merchant panel (step 8), never to this one,
        # even for an admin who holds every permission.
        self.assertEqual(self.post_action("confirm").status_code, 403)


class IdentityTests(QueueTestCase):
    def test_finance_sees_the_client(self):
        self.login(self.staff)
        body = self.client.get(self.detail_url()).content.decode()
        self.assertIn("زينب الجبوري", body)
        self.assertIn("MX-90210", body)

    def test_identity_is_withheld_without_the_permission(self):
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        body = self.client.get(self.detail_url()).content.decode()
        self.assertNotIn("زينب الجبوري", body)
        self.assertNotIn("MX-90210", body)

    def test_the_queue_hides_the_client_column_without_the_permission(self):
        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        body = self.client.get(reverse("finance:request_list")).content.decode()
        self.assertNotIn("زينب الجبوري", body)

    def test_searching_by_client_name_needs_the_identity_permission(self):
        self.login(self.staff)
        found = self.client.get(reverse("finance:request_list"), {"q": "زينب"})
        self.assertEqual(len(found.context["requests"]), 1)

        self.revoke(Role.FINANCE_STAFF, "view_client_identity")
        self.login(self.staff)
        # Otherwise the search box would confirm a name it may never display.
        blind = self.client.get(reverse("finance:request_list"), {"q": "زينب"})
        self.assertEqual(len(blind.context["requests"]), 0)


class FilterTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.assigned = self.make_request(
            status=RequestStatus.ASSIGNED,
            merchant_assigned=self.alternate,
            amount_usd=Decimal("50.00"),
        )
        self.closed = self.make_request(status=RequestStatus.CLOSED)
        self.other = self.make_request(
            type=RequestType.WITHDRAWAL,
            payment_method=self.other_method,
            merchant_selected=self.unrelated,
            wallet_number_snapshot="",
            destination_account="1234567890",
        )
        self.login(self.staff)

    def listed(self, **params):
        response = self.client.get(reverse("finance:request_list"), params)
        return {req.pk for req in response.context["requests"]}

    def test_the_queue_opens_on_what_is_still_in_flight(self):
        self.assertEqual(self.listed(), {self.deposit.pk, self.assigned.pk, self.other.pk})

    def test_page_two_of_the_default_queue_is_still_the_default_queue(self):
        # The pager appends its own querystring; if the default only applied to
        # a bare URL, page 2 would quietly include the closed history.
        self.assertNotIn(self.closed.pk, self.listed(page="1"))

    def test_filter_by_status_group(self):
        self.assertEqual(self.listed(status="awaiting_merchant"), {self.assigned.pk})
        self.assertEqual(
            self.listed(status="awaiting_finance"), {self.deposit.pk, self.other.pk}
        )

    def test_filter_by_one_status(self):
        self.assertEqual(self.listed(status="closed"), {self.closed.pk})

    def test_the_empty_status_shows_everything_including_closed(self):
        self.assertEqual(len(self.listed(status="")), 4)

    def test_filter_by_type(self):
        self.assertEqual(self.listed(status="", type="withdrawal"), {self.other.pk})

    def test_filter_by_method(self):
        self.assertEqual(
            self.listed(status="", method=self.other_method.pk), {self.other.pk}
        )

    def test_filter_by_merchant_covers_both_chosen_and_routed(self):
        # ``assigned`` was chosen by the client from self.merchant but routed to
        # self.alternate, so it must appear under either name.
        self.assertIn(self.assigned.pk, self.listed(status="", merchant=self.merchant.pk))
        self.assertEqual(self.listed(status="", merchant=self.alternate.pk), {self.assigned.pk})

    def test_filter_by_date(self):
        today = timezone.localdate()
        self.assertEqual(len(self.listed(status="", date_from=today.isoformat())), 4)
        self.assertEqual(
            len(self.listed(status="", date_to=(today - timezone.timedelta(days=1)).isoformat())),
            0,
        )

    def test_reversed_dates_are_read_the_way_they_were_meant(self):
        today = timezone.localdate()
        tomorrow = today + timezone.timedelta(days=1)
        self.assertEqual(
            len(self.listed(status="", date_from=tomorrow.isoformat(), date_to=today.isoformat())),
            4,
        )

    def test_search_by_reference(self):
        self.assertEqual(self.listed(status="", q=self.deposit.public_ref), {self.deposit.pk})

    def test_the_tab_counts_match_the_buckets(self):
        counts = self.client.get(reverse("finance:request_list")).context["counts"]
        self.assertEqual(counts["awaiting_finance"], 2)
        self.assertEqual(counts["awaiting_merchant"], 1)
        self.assertEqual(counts["all"], 4)


class ActionTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)

    def test_review_moves_the_request_and_reports_it(self):
        response = self.post_action("review")
        self.assertRedirects(response, self.detail_url())
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.UNDER_REVIEW)

    def test_routing_assigns_the_chosen_merchant(self):
        self.post_action("review")
        self.post_action("route", merchant=self.alternate.pk)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.ASSIGNED)
        self.assertEqual(self.deposit.merchant_assigned_id, self.alternate.pk)

    def test_routing_to_a_merchant_who_cannot_execute_it_is_refused(self):
        self.post_action("review")
        self.post_action("route", merchant=self.unrelated.pk)
        self.deposit.refresh_from_db()
        # The choice never reaches the machine: it is not in the field's set.
        self.assertEqual(self.deposit.status, RequestStatus.UNDER_REVIEW)
        self.assertIsNone(self.deposit.merchant_assigned_id)

    def test_routing_with_no_merchant_named_is_refused(self):
        self.post_action("review")
        response = self.post_action("route")
        self.assertRedirects(response, self.detail_url())
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.UNDER_REVIEW)

    def test_rejecting_stores_the_reason_and_posts_it(self):
        self.post_action("reject", reason="الإيصال غير مقروء.")
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.REJECTED)
        self.assertEqual(self.deposit.messages.get().body, "الإيصال غير مقروء.")

    def test_rejecting_without_a_reason_changes_nothing(self):
        self.post_action("reject", reason="")
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.SUBMITTED)
        self.assertFalse(self.deposit.messages.exists())

    def test_an_internal_note_rides_along_and_is_flagged(self):
        self.post_action("review", note="اتصلت بالعميل للتأكيد.")
        note = self.deposit.messages.get()
        self.assertTrue(note.is_internal_note)
        self.assertEqual(note.sender_role, ActorRole.FINANCE_ADMIN)
        self.assertEqual(note.sender_id, self.admin.pk)

    def test_the_action_is_audited_with_the_operator(self):
        self.post_action("review")
        entry = AuditLog.objects.filter(target_id=str(self.deposit.pk)).latest("id")
        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.after["action"], "review")

    def test_an_unknown_action_is_a_403(self):
        self.assertEqual(self.post_action("vanish").status_code, 403)

    def test_actions_are_post_only(self):
        self.assertEqual(self.client.get(self.action_url("review")).status_code, 405)


class PermissionDelegationTests(QueueTestCase):
    """Spec §3: a permission, not a role, decides who may do each step."""

    def setUp(self):
        super().setUp()
        # Routing is taken off the staff role; reviewing stays.
        self.revoke(Role.FINANCE_STAFF, "route_request")
        self.login(self.staff)

    def test_a_step_the_user_holds_is_offered_and_works(self):
        body = self.client.get(self.detail_url()).content.decode()
        self.assertIn(self.action_url("review"), body)
        self.post_action("review")
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.UNDER_REVIEW)

    def test_a_step_the_user_does_not_hold_is_neither_shown_nor_accepted(self):
        self.post_action("review")
        body = self.client.get(self.detail_url()).content.decode()
        self.assertNotIn(self.action_url("route"), body)
        self.assertEqual(self.post_action("route", merchant=self.merchant.pk).status_code, 403)

    def test_granting_the_permission_is_all_it_takes(self):
        self.post_action("review")
        # Granted to this one account rather than back to the whole role —
        # which is the delegation spec §3 asks for.
        self.staff.user_permissions.add(
            Permission.objects.get(codename="route_request")
        )
        self.login(self.staff)  # a fresh session drops the cached permissions
        self.post_action("route", merchant=self.merchant.pk)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, RequestStatus.ASSIGNED)


class AttachmentTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.attachment = Attachment.objects.create(
            request=self.deposit,
            file=SimpleUploadedFile("proof.png", PNG, content_type="image/png"),
            content_type="image/png",
            uploaded_by_role=ActorRole.CLIENT,
            uploaded_by_id=self.client_record.pk,
        )

    def signed_url_for(self, user):
        from apps.finance import attachments

        return attachments.url_for(self.attachment, user)

    def test_finance_can_open_a_link_minted_for_them(self):
        self.login(self.admin)
        response = self.client.get(self.signed_url_for(self.admin))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])

    def test_a_link_minted_for_someone_else_is_inert(self):
        url = self.signed_url_for(self.staff)
        self.login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_tampered_token_is_a_404(self):
        self.login(self.admin)
        url = self.signed_url_for(self.admin)
        # Flip a character inside the token, leaving the trailing slash alone
        # so the request reaches the view rather than an APPEND_SLASH redirect.
        tampered = url[:-6] + ("b" if url[-6] == "a" else "a") + url[-5:]
        self.assertEqual(self.client.get(tampered).status_code, 404)

    def test_an_expired_link_is_a_404(self):
        self.login(self.admin)
        url = self.signed_url_for(self.admin)
        with self.settings(FINANCE_ATTACHMENT_URL_MAX_AGE=-1):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_merchant_cannot_use_a_finance_link(self):
        url = self.signed_url_for(self.merchant_user)
        self.login(self.merchant_user)
        # Blocked at the panel gate, before the signature is even considered.
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_client_token_does_not_open_the_finance_url(self):
        from apps.portal import attachments as portal_attachments

        client_token = portal_attachments.sign(self.attachment, self.client_record)
        self.login(self.admin)
        url = reverse(
            "finance:attachment", kwargs={"pk": self.attachment.pk, "token": client_token}
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_detail_page_links_the_proof(self):
        self.login(self.admin)
        body = self.client.get(self.detail_url()).content.decode()
        self.assertIn(f"/finance/attachments/{self.attachment.pk}/", body)


class DashboardTests(QueueTestCase):
    def test_the_dashboard_counts_what_is_waiting(self):
        self.login(self.staff)
        response = self.client.get(reverse("finance:dashboard"))
        self.assertEqual(response.context["queue"]["awaiting_finance"], 1)
        self.assertEqual(response.context["queue_waiting"], 1)
