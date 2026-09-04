"""Business hours (spec §5, §7) — build-order step 11.

Every case pins ``now`` to a known instant and passes an unsaved
``SystemSettings``, so nothing here depends on the wall clock of the machine
running it — which is the whole failure mode this module has to be tested
against.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.core import hours as business_hours
from apps.core.models import SystemSettings

BAGHDAD = ZoneInfo("Asia/Baghdad")


def at(hour, minute=0, day=22):
    """A local Baghdad instant on 22 August 2026 unless told otherwise."""
    return datetime(2026, 8, day, hour, minute, tzinfo=BAGHDAD)


def settings(open_at=(9, 0), close_at=(21, 0), override=None, zone="Asia/Baghdad"):
    return SystemSettings(
        open_time=time(*open_at),
        close_time=time(*close_at),
        timezone=zone,
        is_open_override=override,
        closed_message_ar="النظام مغلق حاليًا.",
    )


class DaytimeWindowTests(TestCase):
    """09:00–21:00, the shipped default."""

    def test_open_inside_the_window(self):
        state = business_hours.evaluate(settings(), now=at(12))

        self.assertTrue(state.is_open)
        self.assertEqual(state.reason, business_hours.REASON_SCHEDULE)

    def test_the_boundaries_are_inclusive_at_open_and_exclusive_at_close(self):
        self.assertTrue(business_hours.evaluate(settings(), now=at(9, 0)).is_open)
        self.assertFalse(business_hours.evaluate(settings(), now=at(21, 0)).is_open)

    def test_while_open_the_next_change_is_todays_close(self):
        state = business_hours.evaluate(settings(), now=at(12))

        self.assertEqual(state.local(state.closes_at), at(21))
        self.assertIsNone(state.opens_at)
        self.assertEqual(state.seconds_until_change, 9 * 3600)

    def test_before_opening_the_countdown_runs_to_this_mornings_open(self):
        state = business_hours.evaluate(settings(), now=at(6, 30))

        self.assertFalse(state.is_open)
        self.assertEqual(state.local(state.opens_at), at(9))
        self.assertEqual(state.seconds_until_change, 2 * 3600 + 1800)

    def test_after_closing_the_countdown_runs_to_tomorrows_open(self):
        state = business_hours.evaluate(settings(), now=at(22))

        self.assertFalse(state.is_open)
        self.assertEqual(state.local(state.opens_at), at(9, day=23))
        self.assertEqual(state.seconds_until_change, 11 * 3600)


class OvernightWindowTests(TestCase):
    """20:00–02:00 — a legitimate configuration, not a swapped pair."""

    def setUp(self):
        self.hours = settings(open_at=(20, 0), close_at=(2, 0))

    def test_open_before_midnight(self):
        self.assertTrue(business_hours.evaluate(self.hours, now=at(23)).is_open)

    def test_open_after_midnight(self):
        self.assertTrue(business_hours.evaluate(self.hours, now=at(1)).is_open)

    def test_closed_during_the_day(self):
        self.assertFalse(business_hours.evaluate(self.hours, now=at(10)).is_open)

    def test_before_midnight_the_window_ends_tomorrow(self):
        state = business_hours.evaluate(self.hours, now=at(23))

        self.assertEqual(state.local(state.closes_at), at(2, day=23))

    def test_after_midnight_the_window_ends_today(self):
        state = business_hours.evaluate(self.hours, now=at(1))

        self.assertEqual(state.local(state.closes_at), at(2))

    def test_the_daytime_gap_counts_down_to_this_evening(self):
        state = business_hours.evaluate(self.hours, now=at(10))

        self.assertEqual(state.local(state.opens_at), at(20))


class AlwaysOpenTests(TestCase):
    def test_equal_times_read_as_round_the_clock(self):
        state = business_hours.evaluate(settings(open_at=(0, 0), close_at=(0, 0)), now=at(3))

        self.assertTrue(state.is_open)
        self.assertEqual(state.reason, business_hours.REASON_ALWAYS_OPEN)
        self.assertIsNone(state.changes_at)
        self.assertIsNone(state.seconds_until_change)


class OverrideTests(TestCase):
    def test_forced_open_beats_a_schedule_that_says_closed(self):
        state = business_hours.evaluate(settings(override=True), now=at(3))

        self.assertTrue(state.is_open)
        self.assertEqual(state.reason, business_hours.REASON_OVERRIDE_OPEN)
        self.assertTrue(state.is_overridden)

    def test_forced_closed_beats_a_schedule_that_says_open(self):
        state = business_hours.evaluate(settings(override=False), now=at(12))

        self.assertFalse(state.is_open)
        self.assertEqual(state.reason, business_hours.REASON_OVERRIDE_CLOSED)

    def test_a_forced_close_has_nothing_to_count_down_to(self):
        # It reopens when a human says so, and a countdown to an unknown moment
        # is worse than none: it would name a time nobody promised.
        state = business_hours.evaluate(settings(override=False), now=at(12))

        self.assertIsNone(state.changes_at)
        self.assertIsNone(state.payload()["seconds_until_change"])


class PayloadTests(TestCase):
    def test_the_closed_message_only_travels_when_the_desk_is_shut(self):
        self.assertNotIn("message", business_hours.evaluate(settings(), now=at(12)).payload())
        self.assertIn("message", business_hours.evaluate(settings(), now=at(3)).payload())

    def test_the_payload_carries_the_schedule_and_its_timezone(self):
        payload = business_hours.evaluate(settings(), now=at(3)).payload()

        self.assertEqual(payload["open_time"], "09:00")
        self.assertEqual(payload["close_time"], "21:00")
        self.assertEqual(payload["timezone"], "Asia/Baghdad")
        self.assertFalse(payload["open"])

    def test_the_countdown_is_sent_as_seconds_as_well_as_a_timestamp(self):
        # The client anchors to elapsed time, not to its own clock; a device an
        # hour out must still open the door at the right moment.
        payload = business_hours.evaluate(settings(), now=at(8, 30)).payload()

        self.assertEqual(payload["seconds_until_change"], 1800)
        self.assertTrue(payload["opens_at"].startswith("2026-08-22T09:00"))


class TimezoneTests(TestCase):
    def test_the_window_is_read_in_the_configured_zone_not_utc(self):
        # 06:00 UTC is 09:00 in Baghdad: open there, shut in London.
        moment = datetime(2026, 8, 22, 6, 0, tzinfo=ZoneInfo("UTC"))

        self.assertTrue(business_hours.evaluate(settings(), now=moment).is_open)
        self.assertFalse(
            business_hours.evaluate(settings(zone="Europe/London"), now=moment).is_open
        )

    def test_an_unknown_zone_falls_back_instead_of_taking_the_portal_down(self):
        with self.assertLogs("maxpay.audit", level="ERROR"):
            state = business_hours.evaluate(settings(zone="Mars/Olympus"), now=at(12))

        self.assertTrue(state.is_open)


class SingletonTests(TestCase):
    def test_evaluate_reads_the_saved_settings_when_none_are_passed(self):
        row = SystemSettings.load()
        row.is_open_override = False
        row.save()

        self.assertFalse(business_hours.is_open())
