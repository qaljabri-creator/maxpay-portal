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

import json
import tempfile
from binascii import unhexlify
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from two_factor.utils import default_device

from apps.accounts.models import User
from apps.core import hours as business_hours
from apps.core.management.commands.seed_demo import (
    DEFAULT_PASSWORD,
    DEMO_MERCHANT_NAME,
    DEV_ISSUER,
    DEVICE_NAME,
    JWKS_PATH,
    MERCHANT_SUBJECT,
    MERCHANT_TOKEN_PATH,
)
from apps.core.models import SystemSettings
from apps.merchants.models import Merchant
from apps.portal.tests.support import StubbedJWKS


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


class MerchantTokenTests(SeedDemoTestCase):
    """``--merchant-token``: the demo merchant, bound and minted like a real one.

    The interesting claim is not "a token was written" — it is that the token
    this prints actually opens the panel, the same way a developer pasting the
    console snippet would find out. ``test_the_minted_token_actually_opens_the_merchant_panel``
    is that claim, driven through the real views rather than through
    ``apps.merchant_panel.session`` directly, because a passing unit test and a
    working local demo are two different things and only one of them is what
    ``seed_demo`` promises.
    """

    def test_it_refuses_before_the_demo_merchant_exists(self):
        with self.assertRaises(CommandError):
            self.seed(merchant_token=True)
        self.assertFalse(Merchant.objects.exists())

    def test_it_binds_the_demo_merchant_to_a_demo_subject(self):
        self.seed()
        self.seed(merchant_token=True)

        merchant = Merchant.objects.get(name=DEMO_MERCHANT_NAME)
        self.assertEqual(merchant.b2core_id, MERCHANT_SUBJECT)

    def test_the_minted_token_is_shaped_like_the_client_token(self):
        """Same claim set as ``mint_token``'s, B2CORE mints one shape for everyone."""
        self.seed()
        self.seed(merchant_token=True)

        token = (Path(self._tmp.name) / MERCHANT_TOKEN_PATH).read_text(encoding="utf-8")
        claims = jwt.decode(token, options={"verify_signature": False})

        self.assertEqual(claims["sub"], MERCHANT_SUBJECT)
        self.assertEqual(claims["iss"], DEV_ISSUER)
        self.assertNotIn("aud", claims)
        self.assertIn("first_name", claims)
        self.assertIn("last_name", claims)

    def test_re_running_converges_rather_than_rebinding(self):
        self.seed()
        self.seed(merchant_token=True)
        first_token = (Path(self._tmp.name) / MERCHANT_TOKEN_PATH).read_text(encoding="utf-8")

        self.seed(merchant_token=True)

        self.assertEqual(Merchant.objects.filter(b2core_id=MERCHANT_SUBJECT).count(), 1)
        second_token = (Path(self._tmp.name) / MERCHANT_TOKEN_PATH).read_text(encoding="utf-8")
        # A fresh token each run — only the binding converges, not the token.
        self.assertNotEqual(first_token, second_token)

    def test_the_minted_token_actually_opens_the_merchant_panel(self):
        """The whole point: paste-ready means it actually works, unframed.

        Drives exactly the loop ``report_merchant_token`` describes — POST the
        token to the session endpoint from the page's own origin, then GET the
        panel with the cookie that came back — with only the JWKS *fetch*
        replaced, so the suite touches no network. Everything downstream of
        that fetch, including the merchant binding in
        ``apps.merchant_panel.session``, runs unmodified.
        """
        self.seed()
        self.seed(merchant_token=True)

        token = (Path(self._tmp.name) / MERCHANT_TOKEN_PATH).read_text(encoding="utf-8")
        jwks_document = json.loads((Path(self._tmp.name) / JWKS_PATH).read_text(encoding="utf-8"))

        with override_settings(
            B2CORE_JWKS_URL="https://b2core.test/.well-known/jwks.json",
            B2CORE_JWT_ISSUER=DEV_ISSUER,
            B2CORE_JWT_AUDIENCE="",
        ), StubbedJWKS(jwks_document):
            response = self.client.post(
                reverse("merchant_panel:session"),
                data=json.dumps({"token": token}),
                content_type="application/json",
                # The Django test client's own origin — exactly what a fetch()
                # from the page it just rendered would send.
                HTTP_ORIGIN="http://testserver",
            )
            self.assertEqual(response.status_code, 201, response.content)
            self.assertTrue(response.json()["authenticated"])

            panel = self.client.get(reverse("merchant_panel:queue"))

        self.assertEqual(panel.status_code, 200)
        self.assertContains(panel, DEMO_MERCHANT_NAME)


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
