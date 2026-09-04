"""Every merchant-context response, swept for client identity (spec §2, §11).

The serializers are tested in :mod:`.test_serializers`. What is tested here is
the property the spec actually promises — *a merchant-scoped API response must
never contain client identifying fields* — asserted against whole responses
rather than against the objects that build them, so anything wrapped around a
serializer is covered too: the paginator, DRF's error bodies, the headers.

:class:`SurfaceCoverageTests` is what keeps this honest over time. It enumerates
the merchant URLconf and fails on any route not listed in :data:`SURFACE`, so a
new merchant endpoint cannot be added without being swept.
"""

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.merchant_panel import urls as merchant_urls
from apps.merchant_panel.attachments import sign
from apps.transactions.models import Attachment, RequestStatus, RequestType

from .support import PNG, MerchantPanelTestCase


class SurfaceTestCase(MerchantPanelTestCase):
    """A merchant with one of everything on their request, and a session."""

    def setUp(self):
        super().setUp()
        self.add_thread()
        self.attachment = Attachment.objects.create(
            request=self.request_obj,
            file=SimpleUploadedFile("proof.png", PNG, content_type="image/png"),
            content_type="image/png",
            uploaded_by_role=ActorRole.CLIENT,
            uploaded_by_id=self.client_record.pk,
        )
        self.withdrawal = self.make_request(
            type=RequestType.WITHDRAWAL,
            destination_account="6274-1111-2222-3333",
            wallet_number_snapshot="",
        )
        self.login()

    def urls(self) -> dict[str, str]:
        """Every GET-able merchant URL, built for this fixture."""
        return {
            "queue": reverse("merchant_panel:queue"),
            "queue_all": reverse("merchant_panel:queue") + "?status=",
            "request_detail": reverse(
                "merchant_panel:request_detail", args=[self.request_obj.public_ref]
            ),
            "withdrawal_detail": reverse(
                "merchant_panel:request_detail", args=[self.withdrawal.public_ref]
            ),
            "wallets": reverse("merchant_panel:wallets"),
            # The live fragments and the pulse (step 13). A fragment is a
            # merchant-facing response like any other, and the pulse is a
            # payload nobody thinks of as "data" — which is exactly the kind
            # that grows a client field later.
            "queue_rows": reverse("merchant_panel:queue_rows"),
            "queue_rows_all": reverse("merchant_panel:queue_rows") + "?status=",
            "request_thread": reverse(
                "merchant_panel:request_thread", args=[self.request_obj.public_ref]
            ),
            "api_pulse": reverse("merchant_panel:api_pulse"),
            "attachment": reverse(
                "merchant_panel:attachment",
                args=[self.attachment.pk, sign(self.attachment, self.user)],
            ),
            "api_requests": reverse("merchant_panel:api_requests"),
            "api_requests_all": reverse("merchant_panel:api_requests") + "?status=",
            "api_request_detail": reverse(
                "merchant_panel:api_request_detail", args=[self.request_obj.public_ref]
            ),
            "api_withdrawal_detail": reverse(
                "merchant_panel:api_request_detail", args=[self.withdrawal.public_ref]
            ),
            "api_wallets": reverse("merchant_panel:api_wallets"),
            # Reporting (step 16). The export is swept here like every other
            # merchant-facing response; what the raw bytes of a zipped workbook
            # cannot prove is checked properly in apps/reports/tests.py, which
            # parses the file and walks its cells.
            "report": reverse("merchant_panel:report"),
            "report_all": reverse("merchant_panel:report") + "?status=",
            "report_export": reverse("merchant_panel:report_export"),
        }


class AnonymitySweepTests(SurfaceTestCase):
    """The headline guarantee, on every response a merchant can obtain."""

    def test_no_merchant_response_contains_any_client_identifying_value(self):
        for label, url in self.urls().items():
            with self.subTest(surface=label):
                response = self.client.get(url)
                self.assertLess(response.status_code, 400, f"{label} → {response.status_code}")
                self.assertBodyHasNoIdentity(response, f"GET {url}")

    def test_no_merchant_json_response_contains_any_identifying_key(self):
        for label, url in self.urls().items():
            if not label.startswith("api_"):
                continue
            with self.subTest(surface=label):
                response = self.client.get(url)
                self.assertNoIdentity(response.json(), f"GET {url}")

    def test_error_responses_are_swept_too(self):
        """A 404 body is a response like any other, and DRF writes it, not us."""
        theirs = self.make_request(assigned_to=self.other)
        for url in (
            reverse("merchant_panel:api_request_detail", args=[theirs.public_ref]),
            reverse("merchant_panel:api_request_detail", args=["MP-00000"]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertNoIdentity(response.json(), f"GET {url}")
                self.assertBodyHasNoIdentity(response, f"GET {url}")

    def test_the_thread_reaches_the_merchant_but_not_the_internal_note(self):
        """A sweep that passes because the payload is empty proves nothing."""
        payload = self.client.get(self.urls()["api_request_detail"]).json()
        bodies = [m["body"] for m in payload["messages"]]
        self.assertIn("حوّلت المبلغ، هذا رقم العملية 4471.", bodies)
        self.assertEqual(len(bodies), 2)

    def test_the_queue_actually_returns_the_request(self):
        payload = self.client.get(self.urls()["api_requests"]).json()
        self.assertEqual(
            {row["reference"] for row in payload["results"]},
            {self.request_obj.public_ref, self.withdrawal.public_ref},
        )


class SurfaceCoverageTests(SurfaceTestCase):
    """Nothing on this surface may exist without having been swept.

    A merchant endpoint added later is caught here rather than in production:
    the URLconf is the source of truth, and this test refuses to pass while it
    names a route the sweep does not visit.
    """

    #: Route name → how it is exercised above. ``request_action`` is a POST and
    #: is covered by :mod:`.test_views`.
    SURFACE = {
        "queue": "swept as GET",
        "request_detail": "swept as GET",
        "request_action": "POST — covered by test_views",
        "request_message": "POST — covered by test_threads",
        "request_amount": "POST — covered by apps.transactions.test_amounts",
        "wallets": "swept as GET",
        "attachment": "swept as GET",
        "queue_rows": "swept as GET",
        "request_thread": "swept as GET",
        "api_requests": "swept as GET",
        "api_request_detail": "swept as GET",
        "api_wallets": "swept as GET",
        "api_pulse": "swept as GET",
        "report": "swept as GET",
        "report_export": "swept as GET — and its cells are read in apps/reports",
    }

    def test_every_merchant_route_is_accounted_for(self):
        registered = {pattern.name for pattern in merchant_urls.urlpatterns}
        self.assertEqual(
            registered,
            set(self.SURFACE),
            "A merchant route was added or removed. Every route on this surface "
            "must be swept for client identity (spec §2) — add it to SURFACE and "
            "to AnonymitySweepTests.urls().",
        )

    def test_every_get_route_is_actually_visited_by_the_sweep(self):
        visited = set()
        for url in self.urls().values():
            match = merchant_urls.app_name, url
            visited.add(match)
        # Resolve each swept URL back to its route name, so a URL builder that
        # silently stopped covering a route is caught as well.
        from django.urls import resolve

        names = {resolve(url.split("?")[0]).url_name for url in self.urls().values()}
        expected = {
            name
            for name, note in self.SURFACE.items()
            if note.startswith("swept as GET")
        }
        self.assertEqual(names, expected)
        self.assertTrue(visited)


class ScopeTests(SurfaceTestCase):
    """Spec §8: a merchant sees the requests assigned to them, and only those."""

    def test_another_merchants_request_is_absent_from_the_queue(self):
        theirs = self.make_request(assigned_to=self.other)
        payload = self.client.get(self.urls()["api_requests"]).json()
        self.assertNotIn(
            theirs.public_ref, {row["reference"] for row in payload["results"]}
        )

    def test_another_merchants_request_does_not_exist_rather_than_being_forbidden(self):
        theirs = self.make_request(assigned_to=self.other)
        response = self.client.get(
            reverse("merchant_panel:api_request_detail", args=[theirs.public_ref])
        )
        # 404, not 403: a 403 would confirm the reference names something real.
        self.assertEqual(response.status_code, 404)

    def test_being_the_merchant_the_client_chose_is_not_enough(self):
        """Finance may route elsewhere (spec §5). Selection grants nothing."""
        elsewhere = self.make_request(assigned_to=self.other)
        elsewhere.merchant_selected = self.merchant
        elsewhere.save(update_fields=["merchant_selected"])

        response = self.client.get(
            reverse("merchant_panel:api_request_detail", args=[elsewhere.public_ref])
        )
        self.assertEqual(response.status_code, 404)

    def test_a_request_not_yet_routed_to_anyone_is_invisible(self):
        pending = self.make_request(
            merchant_assigned=None, status=RequestStatus.SUBMITTED
        )
        payload = self.client.get(self.urls()["api_requests"] + "?status=").json()
        self.assertNotIn(
            pending.public_ref, {row["reference"] for row in payload["results"]}
        )

    def test_wallets_are_the_merchants_own_only(self):
        payload = self.client.get(self.urls()["api_wallets"]).json()
        numbers = {wallet["number"] for wallet in payload}
        self.assertEqual(
            numbers,
            set(
                self.merchant.methods.first()
                .wallets.values_list("number", flat=True)
            ),
        )
        self.assertEqual(len(payload), 1)


class AccessTests(MerchantPanelTestCase):
    """Who reaches this surface at all."""

    def api_urls(self) -> list[str]:
        return [
            reverse("merchant_panel:api_requests"),
            reverse("merchant_panel:api_wallets"),
            reverse(
                "merchant_panel:api_request_detail", args=[self.request_obj.public_ref]
            ),
        ]

    def test_an_anonymous_caller_gets_nothing(self):
        for url in self.api_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(response.status_code, (401, 403))

    def test_finance_is_refused_the_merchant_panel(self):
        """Not because Finance may not see the data — they see more of it.

        This panel is one merchant's worklist, and there is no Finance version
        of it. A door left open "for support" is a surface on which the
        anonymity guarantee would have to be argued rather than simply held.
        """
        verify_otp(self.client, self.finance)
        for url in self.api_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_merchant_account_with_no_merchant_record_is_refused(self):
        orphan = make_user("orphan@example.com", Role.MERCHANT)
        verify_otp(self.client, orphan)
        self.assertEqual(self.client.get(reverse("merchant_panel:api_requests")).status_code, 403)

    def test_a_suspended_merchant_loses_the_panel_and_keeps_the_history(self):
        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active"])
        self.login()
        self.assertEqual(self.client.get(reverse("merchant_panel:api_requests")).status_code, 403)
        # The rows are still there; it is the surface that closed.
        self.assertTrue(self.merchant.requests_assigned.exists())

    def test_the_api_is_read_only(self):
        self.login()
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    reverse("merchant_panel:api_requests"),
                    data=json.dumps({}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 405)
