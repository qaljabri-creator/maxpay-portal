"""The Finance panel's live updates (spec §10) — build-order step 13.

Three properties, and the file is grouped by them:

* **the pulse is cheap and honest** — it reports what the rail and the tabs
  should say, and its version token moves when, and only when, the screen
  should change;
* **the fragments are the page** — a live-refreshed queue is what a reload
  would have produced, filters and identity gating included;
* **nothing here writes** — a poll that marked things read would clear a badge
  for a tab nobody is looking at.
"""

import json
from decimal import Decimal

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client, Role
from apps.core.choices import ActorRole
from apps.transactions import reads
from apps.transactions.models import (
    Message,
    Request,
    RequestRead,
    RequestStatus,
    RequestType,
)

from .tests import FinancePanelTestCase


class LiveTestCase(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.client_record = Client.objects.create(
            b2core_id="b2c-1", display_name="زينب الجبوري", email="z@example.com"
        )
        self.request_obj = self.make_request()
        self.login(self.staff)

    def make_request(self, **overrides):
        values = {
            "type": RequestType.DEPOSIT,
            "client": self.client_record,
            "payment_method": self.method,
            "merchant_selected": self.merchant,
            "status": RequestStatus.SUBMITTED,
            "amount_usd": Decimal("100.00"),
            "amount_iqd": Decimal("147000.00"),
            "rate_applied": Decimal("1470.00"),
            "commission_applied": Decimal("5000.00"),
        }
        values.update(overrides)
        return Request.objects.create(**values)

    def pulse(self, **params):
        response = self.client.get(reverse("finance:pulse"), params)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content.decode("utf-8"))


class PulseTests(LiveTestCase):
    def test_it_reports_the_counts_the_rail_renders(self):
        body = self.pulse()

        self.assertEqual(body["counts"]["awaiting_finance"], 1)
        self.assertEqual(body["counts"]["all"], 1)

    def test_it_reports_what_this_operator_has_not_looked_at(self):
        self.assertEqual(self.pulse()["unread"], 1)

        reads.mark_seen(user=self.staff, request_obj=self.request_obj)

        self.assertEqual(self.pulse()["unread"], 0)

    def test_the_version_moves_when_a_request_arrives(self):
        before = self.pulse()["queue_version"]
        self.make_request()

        self.assertNotEqual(self.pulse()["queue_version"], before)

    def test_the_version_holds_still_when_nothing_happens(self):
        # The whole point: six polls a minute must not cost six re-renders.
        self.assertEqual(self.pulse()["queue_version"], self.pulse()["queue_version"])

    def test_the_version_respects_the_filter_the_screen_is_showing(self):
        # A desk filtered to withdrawals is not told to refresh because a
        # deposit moved.
        withdrawals = self.pulse(type=RequestType.WITHDRAWAL)["queue_version"]
        self.make_request()

        self.assertEqual(self.pulse(type=RequestType.WITHDRAWAL)["queue_version"], withdrawals)

    def test_it_carries_the_portal_state(self):
        # The rail shows "مغلق" beside business hours; the poll keeps it true.
        self.assertIn("portal_closed", self.pulse())

    def test_it_is_not_cached(self):
        response = self.client.get(reverse("finance:pulse"))

        self.assertEqual(response["Cache-Control"], "no-store")

    def test_it_never_writes(self):
        self.client.get(reverse("finance:pulse"))
        self.client.get(reverse("finance:pulse"))

        self.assertFalse(RequestRead.objects.exists())

    def test_it_answers_get_only(self):
        url = reverse("finance:pulse")

        self.assertEqual(self.client.post(url, {}).status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)


class FragmentTests(LiveTestCase):
    def test_the_rows_fragment_is_the_queues_rows(self):
        response = self.client.get(reverse("finance:request_rows"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.request_obj.public_ref)
        # A fragment, not a page: no rail, no shell.
        self.assertNotContains(response, "<html")

    def test_the_rows_fragment_honours_the_query_string(self):
        # The script forwards the page's own search; a filtered queue must
        # refresh to the same filter rather than to everything.
        response = self.client.get(
            reverse("finance:request_rows"), {"type": RequestType.WITHDRAWAL}
        )

        self.assertNotContains(response, self.request_obj.public_ref)

    def test_the_rows_fragment_marks_what_is_unread(self):
        self.assertContains(self.client.get(reverse("finance:request_rows")), "is-unread")

        reads.mark_seen(user=self.staff, request_obj=self.request_obj)

        self.assertNotContains(
            self.client.get(reverse("finance:request_rows")), "is-unread"
        )

    def test_the_thread_fragment_carries_the_conversation(self):
        Message.objects.create(
            request=self.request_obj,
            sender_role=ActorRole.CLIENT,
            body="متى يُراجَع طلبي؟",
        )
        response = self.client.get(
            reverse("finance:request_thread", args=[self.request_obj.public_ref])
        )

        self.assertContains(response, "متى يُراجَع طلبي؟")

    def test_the_thread_fragment_leaves_the_composer_alone(self):
        # Spec §9's reply box is not in the fragment: replacing a textarea
        # somebody is typing into, six times a minute, is worse than no refresh.
        response = self.client.get(
            reverse("finance:request_thread", args=[self.request_obj.public_ref])
        )

        self.assertNotContains(response, "<form")
        self.assertNotContains(response, "csrfmiddlewaretoken")

    def test_the_thread_fragment_does_not_mark_the_request_read(self):
        self.client.get(
            reverse("finance:request_thread", args=[self.request_obj.public_ref])
        )

        self.assertFalse(RequestRead.objects.exists())

    def test_opening_the_detail_screen_does_mark_it_read(self):
        self.client.get(
            reverse("finance:request_detail", args=[self.request_obj.public_ref])
        )

        self.assertTrue(
            RequestRead.objects.filter(
                user=self.staff, request=self.request_obj
            ).exists()
        )

    def test_an_unknown_reference_is_a_404_not_a_blank_fragment(self):
        response = self.client.get(
            reverse("finance:request_thread", args=["MP-00000"])
        )

        self.assertEqual(response.status_code, 404)


class IdentityGatingTests(LiveTestCase):
    """The fragment obeys the same gate the full page does (spec §2, §9)."""

    def test_a_staff_member_with_identity_sees_the_client_column(self):
        self.assertContains(
            self.client.get(reverse("finance:request_rows")), "زينب الجبوري"
        )

    def test_without_the_permission_the_fragment_hides_it_too(self):
        """Withdrawn from the *role*, which is the only way to withdraw it.

        Not from the user: signing in re-attaches the role group, because
        ``update_last_login`` fires ``post_save`` and the signal mirrors
        ``User.role`` back into membership. That limitation is real and
        documented; testing around it by editing ``user.groups`` would assert
        a revocation the system does not actually support.
        """
        from django.contrib.auth.models import Group, Permission

        group = Group.objects.get(name=Role.FINANCE_STAFF)
        group.permissions.remove(
            Permission.objects.get(
                content_type__app_label="accounts", codename="view_client_identity"
            )
        )
        self.staff = type(self.staff).objects.get(pk=self.staff.pk)
        self.login(self.staff)

        rows = self.client.get(reverse("finance:request_rows"))
        page = self.client.get(reverse("finance:request_list"))

        # The gate holds on the fragment, and holds identically on the page it
        # is a piece of — a live refresh is not a way around it.
        self.assertNotContains(rows, "زينب الجبوري")
        self.assertNotContains(page, "زينب الجبوري")
        self.assertFalse(page.context["can_see_identity"])


class AccessTests(LiveTestCase):
    def test_a_merchant_reaches_none_of_it(self):
        self.login(self.merchant_user)

        for url in (
            reverse("finance:pulse"),
            reverse("finance:request_rows"),
            reverse("finance:request_thread", args=[self.request_obj.public_ref]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_anonymous_reaches_none_of_it(self):
        self.client.logout()

        for url in (reverse("finance:pulse"), reverse("finance:request_rows")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("two_factor:login"), response["Location"])


@override_settings(PANEL_POLL_SECONDS=25)
class ConfigTests(LiveTestCase):
    def test_the_interval_reaches_the_page_from_settings(self):
        response = self.client.get(reverse("finance:dashboard"))

        self.assertEqual(response.context["poll"]["intervalMs"], 25000)
        self.assertContains(response, "maxpay-poll-config")
        self.assertContains(response, "js/panel.js")

    def test_the_login_page_ships_no_poller(self):
        # Only the two panels set `poll`; nothing else should be asking.
        self.client.logout()
        response = self.client.get(reverse("two_factor:login"))

        self.assertNotContains(response, "maxpay-poll-config")


class RouteOrderTests(LiveTestCase):
    def test_rows_is_not_swallowed_by_the_reference_route(self):
        # "requests/rows/" and "requests/<ref>/" are the same shape; the order
        # in the URLconf is what keeps them apart.
        from django.urls import resolve

        self.assertEqual(resolve("/finance/requests/rows/").url_name, "request_rows")

    def test_thread_is_not_swallowed_by_the_action_route(self):
        from django.urls import resolve

        match = resolve(f"/finance/requests/{self.request_obj.public_ref}/thread/")
        self.assertEqual(match.url_name, "request_thread")


class TimestampTests(LiveTestCase):
    def test_the_pulse_carries_the_servers_clock(self):
        # The panels display no countdown, but a reader diagnosing a stale tab
        # needs to know when the answer was produced.
        stamp = self.pulse()["server_time"]

        self.assertTrue(stamp.startswith(str(timezone.now().year)))
