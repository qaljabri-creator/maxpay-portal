"""The demo seeder, tested on the one thing that is easy to get silently wrong.

``seed_demo`` is development tooling, so most of what it does needs no test —
if a wallet is missing you see it on the screen a second later. Its second
factor is different: a TOTP device under the wrong *name* leaves an account that
looks perfectly enrolled, signs in on the password alone, and is then bounced
off ``EnforceTwoFactorMiddleware`` back to the login form **with no message at
all**. Nothing about that failure points at its cause.

So what is asserted here is not "a device was created" but "the login actually
completes" — the property a developer cares about, checked end to end through
the real wizard.
"""

import tempfile
from binascii import unhexlify
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from two_factor.utils import default_device

from apps.accounts.models import User
from apps.core import hours as business_hours
from apps.core.management.commands.seed_demo import DEFAULT_PASSWORD, DEVICE_NAME
from apps.core.models import SystemSettings


class SeedDemoTestCase(TestCase):
    """Runs the real command, with its file output pointed somewhere disposable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The command writes a dev signing key and a JWKS document relative to
        # BASE_DIR. A test has no business leaving either in the project tree.
        self._settings = override_settings(DEBUG=True, BASE_DIR=Path(self._tmp.name))
        self._settings.enable()
        self.addCleanup(self._settings.disable)

    def seed(self, **options):
        call_command("seed_demo", stdout=StringIO(), stderr=StringIO(), **options)

    @staticmethod
    def current_code(device) -> str:
        return str(
            totp(unhexlify(device.key), step=device.step, digits=device.digits)
        ).zfill(device.digits)

    def sign_in(self, email: str):
        """Drive the two-factor wizard exactly as a browser posts it."""
        login_url = reverse("two_factor:login")
        device = TOTPDevice.objects.get(user__email=email)

        self.client.get(login_url)
        self.client.post(
            login_url,
            {
                "login_view-current_step": "auth",
                "auth-username": email,
                "auth-password": DEFAULT_PASSWORD,
            },
        )
        return self.client.post(
            login_url,
            {
                "login_view-current_step": "token",
                "token-otp_token": self.current_code(device),
            },
        )


class AuthenticatorTests(SeedDemoTestCase):
    def test_the_seeded_device_is_the_one_the_login_wizard_looks_for(self):
        """``default_device`` matches on the name and nothing else.

        Returning ``None`` here is what silently reduces the login to a single
        factor, so this is asserted directly rather than only through its
        consequences.
        """
        self.seed()
        user = User.objects.get(email="admin@maxifyfx.com")
        self.assertIsNotNone(default_device(user))
        self.assertEqual(TOTPDevice.objects.get(user=user).name, DEVICE_NAME)

    def test_the_login_asks_for_a_code_rather_than_signing_in_on_the_password(self):
        self.seed()
        login_url = reverse("two_factor:login")
        self.client.get(login_url)
        response = self.client.post(
            login_url,
            {
                "login_view-current_step": "auth",
                "auth-username": "admin@maxifyfx.com",
                "auth-password": DEFAULT_PASSWORD,
            },
        )
        # 200 means the wizard advanced to its token step. A 302 here would mean
        # it had already let the user in — the failure this module exists for.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard"]["steps"].current, "token")

    # The seeded merchant signs in with a password, and merchants no longer may
    # unless MERCHANT_PASSWORD_LOGIN is on. It is switched on here rather than
    # by default: the demo prints the line to put in .env, and the default
    # staying off is the point.
    @override_settings(MERCHANT_PASSWORD_LOGIN=True)
    def test_a_seeded_account_completes_the_login_and_lands_on_its_panel(self):
        self.seed()
        for email, panel in (
            ("admin@maxifyfx.com", "finance:dashboard"),
            ("staff@maxifyfx.com", "finance:dashboard"),
            ("merchant@maxifyfx.com", "merchant_panel:queue"),
        ):
            with self.subTest(email=email):
                self.client.logout()
                response = self.sign_in(email)
                self.assertEqual(response.status_code, 302)
                # The session is OTP-verified, not merely authenticated.
                self.assertIn("otp_device_id", self.client.session)
                # And the redirect actually arrives somewhere, rather than
                # looping back to the login form.
                landed = self.client.get(response["Location"], follow=True)
                self.assertEqual(landed.status_code, 200)
                self.assertEqual(landed.request["PATH_INFO"], reverse(panel))

    def test_re_running_clears_a_device_left_under_another_name(self):
        """An earlier seed's device would otherwise sit there breaking logins."""
        self.seed()
        user = User.objects.get(email="admin@maxifyfx.com")
        TOTPDevice.objects.filter(user=user).update(name="demo")
        self.assertIsNone(default_device(User.objects.get(pk=user.pk)))

        self.seed()
        self.assertEqual(
            list(TOTPDevice.objects.filter(user=user).values_list("name", flat=True)),
            [DEVICE_NAME],
        )
        self.assertIsNotNone(default_device(User.objects.get(pk=user.pk)))

    def test_skipping_enrolment_leaves_the_wizard_to_do_it(self):
        self.seed(no_2fa_devices=True)
        self.assertFalse(TOTPDevice.objects.exists())


class GuardTests(SeedDemoTestCase):
    def test_it_refuses_to_run_with_debug_off(self):
        """Known passwords and known second factors, so DEBUG is the whole guard."""
        with override_settings(DEBUG=False), self.assertRaises(CommandError):
            self.seed()
        self.assertFalse(User.objects.exists())


class BusinessHoursTests(SeedDemoTestCase):
    """Step 11 gave the portal a door; a seeded laptop has to find it open.

    The shipped default is 09:00–21:00 Baghdad, so without this a developer
    running the seeder at midnight would meet the closed notice and reasonably
    conclude that the seeding had failed.
    """

    def test_the_desk_is_seeded_open_at_any_hour(self):
        self.seed()

        midnight = datetime(2026, 8, 22, 3, 0, tzinfo=ZoneInfo("Asia/Baghdad"))
        self.assertTrue(business_hours.is_open(now=midnight))

    def test_it_leaves_the_manual_override_alone(self):
        # The override is something to try from the panel, not something to
        # arrive already thrown.
        self.seed()

        self.assertIsNone(SystemSettings.load().is_open_override)
