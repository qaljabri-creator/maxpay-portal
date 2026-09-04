"""The business-hours screen (spec §7, §9) — build-order step 11."""

from datetime import time

from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests import make_user
from apps.core import hours as business_hours
from apps.core.choices import AuditAction
from apps.core.models import AuditLog, SystemSettings

from .tests import FinancePanelTestCase


class AccessTests(FinancePanelTestCase):
    def test_a_merchant_cannot_reach_it(self):
        self.login(self.merchant_user)

        self.assertEqual(
            self.client.get(reverse("finance:business_hours")).status_code, 403
        )

    def test_finance_staff_may_read_it(self):
        self.login(self.staff)

        response = self.client.get(reverse("finance:business_hours"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_set_hours"])

    def test_finance_staff_may_not_write_it(self):
        self.login(self.staff)

        response = self.client.post(
            reverse("finance:business_hours"),
            {
                "override": "closed",
                "open_time": "09:00",
                "close_time": "21:00",
                "timezone": "Asia/Baghdad",
                "closed_message_ar": "مغلق.",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(SystemSettings.load().is_open_override)

    def test_a_finance_admin_may_write_it(self):
        self.login(self.admin)

        self.assertTrue(
            self.client.get(reverse("finance:business_hours")).context["can_set_hours"]
        )


class SavingTests(FinancePanelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.admin)
        self.url = reverse("finance:business_hours")

    def post(self, **overrides):
        data = {
            "override": "",
            "open_time": "09:00",
            "close_time": "21:00",
            "timezone": "Asia/Baghdad",
            "closed_message_ar": "النظام مغلق حاليًا.",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_a_new_schedule_is_saved(self):
        response = self.post(open_time="08:30", close_time="23:45")

        self.assertEqual(response.status_code, 302)
        saved = SystemSettings.load()
        self.assertEqual(saved.open_time, time(8, 30))
        self.assertEqual(saved.close_time, time(23, 45))

    def test_the_override_maps_onto_the_three_states(self):
        self.post(override="closed")
        self.assertIs(SystemSettings.load().is_open_override, False)

        self.post(override="open")
        self.assertIs(SystemSettings.load().is_open_override, True)

        self.post(override="")
        self.assertIsNone(SystemSettings.load().is_open_override)

    def test_forcing_it_closed_shuts_the_portal_immediately(self):
        self.post(override="closed")

        self.assertFalse(business_hours.is_open())

    def test_an_unknown_timezone_is_refused_rather_than_stored(self):
        response = self.post(timezone="Mars/Olympus")

        self.assertEqual(response.status_code, 200)
        self.assertIn("timezone", response.context["form"].errors)
        self.assertEqual(SystemSettings.load().timezone, "Asia/Baghdad")

    def test_the_closed_notice_cannot_be_left_blank(self):
        # It is the only thing a locked-out client is shown, so an empty one is
        # a blank screen at exactly the moment an explanation is owed.
        response = self.post(closed_message_ar="   ")

        self.assertEqual(response.status_code, 200)
        self.assertIn("closed_message_ar", response.context["form"].errors)

    def test_the_change_is_audited_with_before_and_after(self):
        self.post(open_time="10:00", override="closed")

        entry = AuditLog.objects.filter(action=AuditAction.SETTINGS_CHANGE).latest("id")
        self.assertEqual(entry.target_type, "core.SystemSettings")
        self.assertEqual(entry.before["open_time"], "09:00:00")
        self.assertEqual(entry.after["open_time"], "10:00:00")
        self.assertIs(entry.after["is_open_override"], False)
        self.assertIn(self.admin.full_name, entry.actor_label)


class NoticeTests(FinancePanelTestCase):
    """What the panel tells the desk about the state of the door."""

    def test_every_finance_page_says_when_the_portal_is_shut(self):
        self.login(self.staff)
        row = SystemSettings.load()
        row.is_open_override = False
        row.save()

        response = self.client.get(reverse("finance:dashboard"))

        self.assertTrue(response.context["portal_closed"])

    def test_an_open_portal_raises_no_flag(self):
        self.login(self.staff)
        row = SystemSettings.load()
        row.is_open_override = True
        row.save()

        response = self.client.get(reverse("finance:dashboard"))

        self.assertFalse(response.context["portal_closed"])

    def test_the_page_reports_the_state_the_portal_is_actually_in(self):
        self.login(self.admin)
        row = SystemSettings.load()
        row.is_open_override = True
        row.save()

        hours = self.client.get(reverse("finance:business_hours")).context["hours"]

        self.assertTrue(hours.is_open)
        self.assertTrue(hours.is_overridden)


class SuperuserTests(FinancePanelTestCase):
    def test_a_superuser_without_a_finance_role_still_gets_in(self):
        root = make_user("super@maxifyfx.com", Role.FINANCE_ADMIN, is_superuser=True)
        self.login(root)

        self.assertEqual(
            self.client.get(reverse("finance:business_hours")).status_code, 200
        )
