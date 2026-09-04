"""The merchant panel's live updates (spec §8, §10) — build-order step 13.

:mod:`.test_api` already sweeps these three routes for client identity, because
every merchant-facing response is swept. What is tested here is what they are
*for*:

* spec §8 — "queue of assigned requests only, auto-refreshing via polling every
  10 seconds". The auto-refresh is only as good as the scoping it inherits, so
  the fragment is tested for the same "assigned only" property the full screen
  is, from the same angle: by having another merchant who must not appear in it.
* spec §10 — "new assigned request appears without page refresh, with an unread
  badge". Both halves: the pulse notices, and the badge clears when — and only
  when — the merchant actually opens the request.

The poll being *read-only* gets its own group. A heartbeat that marked things
read would clear a badge for a tab left open in a window nobody is looking at,
which is the one way a live badge can be worse than no badge.
"""

import json

from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests import make_user, verify_otp
from apps.core.choices import ActorRole
from apps.transactions import reads
from apps.transactions.models import Message, RequestRead, RequestStatus

from .support import MerchantPanelTestCase


class MerchantLiveTestCase(MerchantPanelTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.pulse_url = reverse("merchant_panel:api_pulse")
        self.rows_url = reverse("merchant_panel:queue_rows")

    def pulse(self, **params):
        response = self.client.get(self.pulse_url, params)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content.decode("utf-8"))

    def thread_url(self, request_obj=None):
        return reverse(
            "merchant_panel:request_thread",
            args=[(request_obj or self.request_obj).public_ref],
        )


class PulseTests(MerchantLiveTestCase):
    def test_it_counts_what_the_tabs_show(self):
        body = self.pulse()

        self.assertEqual(body["counts"]["awaiting_me"], 1)
        self.assertEqual(body["counts"]["open"], 1)
        self.assertEqual(body["counts"]["all"], 1)

    def test_another_merchants_request_is_not_counted(self):
        # Spec §8: assigned requests only. A count is a surface too — a badge
        # that rose for somebody else's work would leak that it exists.
        self.make_request(assigned_to=self.other)

        self.assertEqual(self.pulse()["counts"]["all"], 1)

    def test_a_new_assigned_request_raises_the_badge(self):
        # Spec §10, the headline: it appears without a page refresh.
        self.assertEqual(self.pulse()["unread"], 1)

        reads.mark_seen(user=self.user, request_obj=self.request_obj)
        self.assertEqual(self.pulse()["unread"], 0)

        self.make_request()
        self.assertEqual(self.pulse()["unread"], 1)

    def test_the_version_moves_when_a_message_arrives(self):
        before = self.pulse()["queue_version"]
        Message.objects.create(
            request=self.request_obj,
            sender_role=ActorRole.CLIENT,
            body="حوّلت المبلغ.",
        )

        self.assertNotEqual(self.pulse()["queue_version"], before)

    def test_the_version_holds_still_when_nothing_happens(self):
        # Six polls a minute must not cost six fragment renders.
        self.assertEqual(self.pulse()["queue_version"], self.pulse()["queue_version"])

    def test_the_version_follows_the_tab_the_merchant_is_on(self):
        # A merchant watching "بانتظاري" is not told to refresh because
        # something moved on a request they already actioned.
        awaiting = self.pulse(status="awaiting_me")["queue_version"]
        self.make_request(status=RequestStatus.MERCHANT_CONFIRMED)

        self.assertEqual(self.pulse(status="awaiting_me")["queue_version"], awaiting)

    def test_it_answers_get_only(self):
        self.assertEqual(self.client.post(self.pulse_url, {}).status_code, 405)
        self.assertEqual(self.client.delete(self.pulse_url).status_code, 405)


class QueueRowsTests(MerchantLiveTestCase):
    def test_the_fragment_carries_the_row(self):
        response = self.client.get(self.rows_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.request_obj.public_ref)
        # A fragment, not a page.
        self.assertNotContains(response, "<html")
        self.assertNotContains(response, "rail__nav")

    def test_the_fragment_is_assigned_only(self):
        theirs = self.make_request(assigned_to=self.other)

        response = self.client.get(self.rows_url)

        self.assertContains(response, self.request_obj.public_ref)
        self.assertNotContains(response, theirs.public_ref)

    def test_the_fragment_honours_the_tab(self):
        confirmed = self.make_request(status=RequestStatus.MERCHANT_CONFIRMED)

        response = self.client.get(self.rows_url, {"status": "awaiting_me"})

        self.assertContains(response, self.request_obj.public_ref)
        self.assertNotContains(response, confirmed.public_ref)

    def test_the_fragment_marks_what_is_unread(self):
        self.assertContains(self.client.get(self.rows_url), "is-unread")

        reads.mark_seen(user=self.user, request_obj=self.request_obj)

        self.assertNotContains(self.client.get(self.rows_url), "is-unread")

    def test_the_fragment_carries_no_client_identity(self):
        # Swept in test_api too; asserted here as well because this is the one
        # route whose whole job is to be injected into a live page (spec §2).
        self.add_thread()

        self.assertBodyHasNoIdentity(self.client.get(self.rows_url), "queue rows")


class ThreadFragmentTests(MerchantLiveTestCase):
    def setUp(self):
        super().setUp()
        self.internal = self.add_thread()

    def test_it_carries_the_conversation(self):
        response = self.client.get(self.thread_url())

        self.assertContains(response, "حوّلت المبلغ، هذا رقم العملية 4471.")

    def test_it_hides_the_internal_note_the_full_page_hides(self):
        # Spec §5: a merchant is not told internal notes exist. A fragment that
        # forgot would be a leak on a route nobody looks at twice.
        response = self.client.get(self.thread_url())

        self.assertNotContains(response, "سبق أن تأخر في الدفع")
        self.assertBodyHasNoIdentity(response, "thread fragment")

    def test_it_leaves_the_composer_alone(self):
        # Spec §9's reply box is not in the fragment: replacing a textarea
        # somebody is typing into, six times a minute, is worse than no refresh.
        response = self.client.get(self.thread_url())

        self.assertNotContains(response, "<form")
        self.assertNotContains(response, "csrfmiddlewaretoken")

    def test_another_merchants_thread_does_not_exist(self):
        theirs = self.make_request(assigned_to=self.other)

        self.assertEqual(self.client.get(self.thread_url(theirs)).status_code, 404)

    def test_an_unknown_reference_is_a_404(self):
        url = reverse("merchant_panel:request_thread", args=["MP-00000"])

        self.assertEqual(self.client.get(url).status_code, 404)


class ReadOnlyTests(MerchantLiveTestCase):
    """The heartbeat observes. Only a person opening a screen marks it read."""

    def test_the_pulse_writes_nothing(self):
        self.client.get(self.pulse_url)
        self.client.get(self.pulse_url)

        self.assertFalse(RequestRead.objects.exists())

    def test_the_row_fragment_writes_nothing(self):
        self.client.get(self.rows_url)

        self.assertFalse(RequestRead.objects.exists())

    def test_the_thread_fragment_writes_nothing(self):
        self.client.get(self.thread_url())

        self.assertFalse(RequestRead.objects.exists())

    def test_opening_the_detail_screen_is_what_marks_it(self):
        self.client.get(
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref])
        )

        self.assertTrue(
            RequestRead.objects.filter(
                user=self.user, request=self.request_obj
            ).exists()
        )

    def test_one_merchants_marker_is_not_anothers(self):
        # Two accounts on the same merchant record would each keep their own
        # idea of what they had looked at.
        colleague = make_user("merchant-a2@example.com", Role.MERCHANT)
        self.client.get(
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref])
        )

        self.assertEqual(self.pulse()["unread"], 0)
        self.assertEqual(
            reads.unread_count(
                user=colleague,
                audience=reads.MERCHANT,
                queryset=type(self.request_obj).objects.all(),
            ),
            1,
        )


class AccessTests(MerchantPanelTestCase):
    """The live routes are the panel; they are refused to everyone it is."""

    def urls(self):
        return [
            reverse("merchant_panel:api_pulse"),
            reverse("merchant_panel:queue_rows"),
            reverse("merchant_panel:request_thread", args=[self.request_obj.public_ref]),
        ]

    def test_anonymous_gets_nothing(self):
        for url in self.urls():
            with self.subTest(url=url):
                self.assertGreaterEqual(self.client.get(url).status_code, 300)

    def test_finance_is_refused(self):
        # Spec §8: this panel is one merchant's worklist. There is no Finance
        # version of it, and a poll is not a back door into one.
        verify_otp(self.client, self.finance)

        for url in self.urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_suspended_merchant_loses_the_poll_too(self):
        self.login()
        self.merchant.is_active = False
        self.merchant.save()

        for url in self.urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)


@override_settings(PANEL_POLL_SECONDS=15)
class ConfigTests(MerchantLiveTestCase):
    def test_the_page_carries_the_poll_configuration(self):
        response = self.client.get(reverse("merchant_panel:queue"))

        self.assertEqual(response.context["poll"]["intervalMs"], 15000)
        self.assertEqual(
            response.context["poll"]["pulseUrl"], reverse("merchant_panel:api_pulse")
        )
        self.assertContains(response, "maxpay-poll-config")
        self.assertContains(response, "js/panel.js")

    def test_the_queue_declares_where_its_rows_come_from(self):
        # The script finds its work through data attributes rather than by
        # knowing which panel it is on.
        response = self.client.get(reverse("merchant_panel:queue"))

        self.assertContains(response, 'data-live-rows="/merchant/queue/rows/"')

    def test_the_detail_screen_declares_where_its_thread_comes_from(self):
        response = self.client.get(
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref])
        )

        self.assertContains(response, f'data-live-thread="{self.thread_url()}"')
