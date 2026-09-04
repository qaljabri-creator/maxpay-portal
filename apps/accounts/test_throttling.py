"""The internal login's brute-force lockout (spec §11) — build-order step 14.

The property under test is not "a counter went up". It is the two-sided one the
control actually has to hold:

* grinding passwords against a known email stops working after a while, **and**
* the person whose email it is can still get in from their own desk.

The second half is the reason the counter is keyed on ``(username, IP)`` and
not on the username alone, and it is the half a naive implementation gets
wrong — a lockout an attacker can trigger against somebody else is a denial of
service you have handed them.
"""

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts import throttling
from apps.accounts.models import Role
from apps.accounts.permissions import sync_role_groups
from apps.accounts.tests import make_user
from apps.core.choices import AuditAction
from apps.core.models import AuditLog

PASSWORD = "portal-test-pass-12345"
HERE = "198.51.100.7"
ELSEWHERE = "203.0.113.9"


class ThrottleTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        sync_role_groups()
        self.user = make_user("staff@maxifyfx.com", Role.FINANCE_STAFF)


@override_settings(LOGIN_FAILURE_LIMIT=3)
class CounterTests(ThrottleTestCase):
    def test_failures_accumulate_per_pair(self):
        throttling.record_failure("a@example.com", HERE)
        throttling.record_failure("a@example.com", HERE)

        self.assertEqual(throttling.failures("a@example.com", HERE), 2)
        self.assertEqual(throttling.failures("a@example.com", ELSEWHERE), 0)
        self.assertEqual(throttling.failures("b@example.com", HERE), 0)

    def test_the_lockout_trips_at_the_limit(self):
        for _ in range(2):
            throttling.record_failure("a@example.com", HERE)
        self.assertFalse(throttling.is_locked_out("a@example.com", HERE))

        throttling.record_failure("a@example.com", HERE)
        self.assertTrue(throttling.is_locked_out("a@example.com", HERE))

    def test_the_key_is_case_insensitive(self):
        # The email field is case-insensitive at login; a counter that reset on
        # a capital letter would not be a counter.
        throttling.record_failure("Staff@MaxifyFX.com", HERE)

        self.assertEqual(throttling.failures("staff@maxifyfx.com", HERE), 1)

    def test_clearing_forgets_the_pair(self):
        throttling.record_failure("a@example.com", HERE)
        throttling.clear("a@example.com", HERE)

        self.assertEqual(throttling.failures("a@example.com", HERE), 0)

    def test_it_fails_open_when_the_cache_is_down(self):
        # A limiter that takes the desk offline when Redis restarts has cost
        # more than it saved, and 2FA is still in the way.
        from unittest import mock

        with mock.patch.object(cache, "get", side_effect=RuntimeError("down")):
            with self.assertLogs("maxpay.audit", level="WARNING"):
                self.assertFalse(throttling.is_locked_out("a@example.com", HERE))


@override_settings(LOGIN_FAILURE_LIMIT=3)
class BackendTests(ThrottleTestCase):
    """The lockout has to sit at ``authenticate()``, which every form ends at."""

    def authenticate(self, password, ip=HERE, username=None):
        from django.contrib.auth import authenticate as django_authenticate
        from django.test import RequestFactory

        request = RequestFactory().post("/", REMOTE_ADDR=ip)
        return django_authenticate(
            request, username=username or self.user.email, password=password
        )

    def test_a_correct_password_still_works(self):
        self.assertEqual(self.authenticate(PASSWORD), self.user)

    def test_a_wrong_password_is_counted(self):
        self.assertIsNone(self.authenticate("wrong"))

        self.assertEqual(throttling.failures(self.user.email, HERE), 1)

    def test_the_right_password_stops_working_once_locked_out(self):
        # The point of the control: after enough grinding, the credential is
        # worthless from that address even if it is guessed correctly.
        for _ in range(3):
            self.authenticate("wrong")

        self.assertIsNone(self.authenticate(PASSWORD))

    def test_the_owner_can_still_sign_in_from_their_own_desk(self):
        # Keyed on the pair, so an attacker grinding from elsewhere cannot lock
        # the account's real user out of it.
        for _ in range(5):
            self.authenticate("wrong", ip=ELSEWHERE)

        self.assertEqual(self.authenticate(PASSWORD, ip=HERE), self.user)

    def test_a_success_clears_the_counter(self):
        self.authenticate("wrong")
        self.authenticate("wrong")
        self.authenticate(PASSWORD)

        self.assertEqual(throttling.failures(self.user.email, HERE), 0)

    def test_an_unknown_email_is_counted_too(self):
        # Otherwise enumeration is free: guess emails until one starts being
        # rate limited.
        self.authenticate("wrong", username="nobody@example.com")

        self.assertEqual(throttling.failures("nobody@example.com", HERE), 1)

    def test_a_locked_out_attempt_never_reaches_the_password_hasher(self):
        from unittest import mock

        for _ in range(3):
            self.authenticate("wrong")

        with mock.patch(
            "django.contrib.auth.backends.ModelBackend.authenticate"
        ) as inner:
            self.authenticate(PASSWORD)

        inner.assert_not_called()

    def test_the_lockout_is_logged_once_rather_than_on_every_attempt(self):
        for _ in range(2):
            self.authenticate("wrong")

        with self.assertLogs("maxpay.audit", level="WARNING") as captured:
            self.authenticate("wrong")  # the one that trips it
        self.assertEqual(
            len([line for line in captured.output if "Login lockout" in line]), 1
        )

        with self.assertLogs("maxpay.audit", level="WARNING") as captured:
            self.authenticate("wrong")  # already locked; refused, not re-tripped
        self.assertEqual(
            len([line for line in captured.output if "Login lockout" in line]), 0
        )


@override_settings(LOGIN_FAILURE_LIMIT=3)
class LoginFormTests(ThrottleTestCase):
    """Through the real two-factor wizard, which is what an attacker posts to."""

    def post(self, password):
        url = reverse("two_factor:login")
        self.client.get(url)
        return self.client.post(
            url,
            {
                "login_view-current_step": "auth",
                "auth-username": self.user.email,
                "auth-password": password,
            },
            REMOTE_ADDR=HERE,
        )

    def test_grinding_through_the_wizard_trips_the_lockout(self):
        for _ in range(3):
            self.post("wrong")

        self.assertTrue(throttling.is_locked_out(self.user.email, HERE))

    def test_a_locked_out_correct_password_does_not_advance_the_wizard(self):
        for _ in range(3):
            self.post("wrong")

        response = self.post(PASSWORD)

        # Still on the credentials step; the second factor is never offered.
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="token-otp_token"')

    def test_every_failure_is_still_audited(self):
        # Spec §5 wants the attempt recorded; §11 wants the password never
        # stored. Both, on the same row.
        self.post("wrong")

        entry = AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).latest("id")
        self.assertEqual(entry.target_id, self.user.email)
        self.assertNotIn("wrong", str(entry.after))
