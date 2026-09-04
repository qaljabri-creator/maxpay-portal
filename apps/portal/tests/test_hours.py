"""The portal outside business hours (spec §7) — build-order step 11.

Spec §7: "outside business hours all submission screens are replaced by a
closed notice with a live countdown to opening". Two halves to that, and both
are tested here:

* the **catalogue** stops offering anything, and says why, so the screens have
  nothing to render and a reason to render the notice instead;
* the **submission endpoint** refuses independently, so a client who kept a
  form open across closing time — or who never had the screen in the first
  place — gets no further than one who did not.

The countdown itself is arithmetic on what the payload carries; that is tested
in :mod:`apps.core.test_hours`.
"""

import datetime
import json

from django.urls import reverse

from apps.core.models import SystemSettings
from apps.transactions.models import Request

from .test_flow import FlowTestCase


def close_the_desk(**overrides):
    """Shut the portal by a schedule that cannot be open at any hour today."""
    row = SystemSettings.load()
    row.is_open_override = False
    for name, value in overrides.items():
        setattr(row, name, value)
    row.save()
    return row


class CatalogueTests(FlowTestCase):
    def test_an_open_desk_says_so_and_still_offers_the_catalogue(self):
        self.sign_in()

        _response, payload = self.options()

        self.assertTrue(payload["hours"]["open"])
        self.assertTrue(payload["merchants"])

    def test_a_closed_desk_offers_nothing(self):
        self.sign_in()
        close_the_desk()

        response, payload = self.options()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["hours"]["open"])
        # No method, no merchant, no wallet number: a number the client cannot
        # pay into tonight is a number they should not be looking at.
        self.assertEqual(payload["methods"], [])
        self.assertNotIn("wallet", payload)
        self.assertIsNone(payload["rate"])

    def test_the_closed_payload_carries_the_notice_finance_wrote(self):
        self.sign_in()
        close_the_desk(closed_message_ar="مغلق للجرد. نعود صباحًا.")

        _response, payload = self.options()

        self.assertEqual(payload["hours"]["message"], "مغلق للجرد. نعود صباحًا.")

    def test_a_scheduled_close_carries_the_countdown_to_opening(self):
        self.sign_in()
        row = SystemSettings.load()
        row.is_open_override = None
        # A one-minute window: closed at essentially every instant, and the
        # opening it counts to is a real one rather than a manual decision.
        row.open_time = datetime.time(9, 0)
        row.close_time = datetime.time(9, 1)
        row.save()

        _response, payload = self.options()
        hours = payload["hours"]

        self.assertFalse(hours["open"])
        self.assertIsNotNone(hours["opens_at"])
        self.assertGreater(hours["seconds_until_change"], 0)
        self.assertEqual(hours["open_time"], "09:00")

    def test_a_manual_close_has_no_countdown(self):
        self.sign_in()
        close_the_desk()

        _response, payload = self.options()

        self.assertIsNone(payload["hours"]["seconds_until_change"])
        self.assertIsNone(payload["hours"]["opens_at"])


class SubmissionTests(FlowTestCase):
    """The server refuses on its own, whatever the screen believes."""

    def test_a_deposit_is_refused_while_the_desk_is_shut(self):
        self.sign_in()
        close_the_desk()

        response = self.submit()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "portal_closed")
        self.assertFalse(Request.objects.exists())

    def test_a_withdrawal_is_refused_too(self):
        self.sign_in()
        close_the_desk()

        response = self.submit(
            type="withdrawal",
            wallet=None,
            proof=None,
            destination_account="07701234567",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["error"], "portal_closed")
        self.assertFalse(Request.objects.exists())

    def test_the_refusal_carries_the_notice_and_the_countdown(self):
        self.sign_in()
        close_the_desk(closed_message_ar="نغلق الآن.")

        payload = self.body(self.submit())

        self.assertEqual(payload["detail"], "نغلق الآن.")
        self.assertIn("hours", payload)
        self.assertFalse(payload["hours"]["open"])

    def test_the_refusal_lands_before_the_proof_is_stored(self):
        from apps.transactions.models import Attachment

        self.sign_in()
        close_the_desk()

        self.submit()

        self.assertFalse(Attachment.objects.exists())

    def test_reopening_lets_the_same_submission_through(self):
        self.sign_in()
        close_the_desk()
        self.assertEqual(self.submit().status_code, 409)

        row = SystemSettings.load()
        row.is_open_override = True
        row.save()

        self.assertEqual(self.submit().status_code, 201)


class StillReachableTests(FlowTestCase):
    """Closing stops new requests. It does not lock a client out of their own.

    Spec §7 replaces the *submission* screens. A request already filed is still
    a request the client is entitled to read and to write into — see
    :mod:`apps.transactions.messaging`, where the thread is gated on nothing.
    """

    def setUp(self):
        super().setUp()
        self.sign_in()
        created = self.submit()
        self.reference = self.body(created)["request"]["reference"]
        close_the_desk()

    def test_the_history_still_answers(self):
        response = self.client.get(reverse("portal:requests"))

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(len(payload["requests"]), 1)

    def test_the_request_itself_still_opens(self):
        url = reverse("portal:request_detail", kwargs={"reference": self.reference})

        self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_client_can_still_write_into_the_thread(self):
        url = reverse("portal:request_messages", kwargs={"reference": self.reference})

        response = self.client.post(
            url,
            data={"body": "متى يُراجَع طلبي؟"},
            headers={"origin": "http://testserver", "x-portal-csrf": self.csrf},
        )

        self.assertEqual(response.status_code, 201)
