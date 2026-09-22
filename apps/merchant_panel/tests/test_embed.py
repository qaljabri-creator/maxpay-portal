"""The merchant panel inside B2CORE: who gets in, and what gets in with them.

The panel is a menu item in B2CORE now, restricted over there to the merchant
client type and framed from ``https://my.maxifyfx.com``. That restriction is not
a control this system can verify — B2CORE's token carries no client type at all
— so the binding between the token's verified ``sub`` and a merchant record's
``b2core_id`` is the only thing keeping an ordinary client out of a merchant's
queue.

:class:`TheOnlyGuardTests` is that sentence as a test, and it is the reason this
file exists. Everything else here checks the things that follow from it: that a
suspended or archived merchant is refused even though the binding holds, that
the refusal survives into an *open* session, that the cookie the session rides
on is the merchant's own and not one of the other two, and that the emergency
password door is shut.
"""

import time

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests import make_user
from apps.accounts.throttling import merchant_password_login_allowed
from apps.merchant_panel import session as embed_session
from apps.merchant_panel.checks import (
    check_merchant_embed_deployment,
    check_merchant_session_cookie,
)
from apps.merchants.models import Merchant
from apps.portal.tests.support import (
    B2CORE_SETTINGS,
    ORIGIN,
    StubbedJWKS,
    jwks_document,
    make_token,
)
from apps.transactions.models import RequestStatus

from .support import MerchantPanelTestCase

#: The merchant's identifier in B2CORE — the string Finance typed into
#: ``Merchant.b2core_id``, and now the thing that opens the panel.
MERCHANT_SUBJECT = "b2core-merchant-4120"

#: An ordinary client's. A real subject, a real signature, no merchant record.
CLIENT_SUBJECT = "b2core-subject-77"


@override_settings(**B2CORE_SETTINGS)
class EmbedTestCase(MerchantPanelTestCase):
    """One merchant bound to a B2CORE subject, and a stubbed JWKS."""

    def setUp(self):
        super().setUp()
        self.merchant.b2core_id = MERCHANT_SUBJECT
        self.merchant.save(update_fields=["b2core_id", "updated_at"])
        self.jwks = StubbedJWKS(jwks_document("primary")).start()
        self.addCleanup(self.jwks.stop)

        self.session_url = reverse("merchant_panel:session")
        self.queue_url = reverse("merchant_panel:queue")

    # -- helpers -----------------------------------------------------------

    def open_session(self, subject=MERCHANT_SUBJECT, **token_kwargs):
        """Post a token at the door, the way the framed page does."""
        return self.client.post(
            self.session_url,
            data={"token": make_token(sub=subject, **token_kwargs)},
            content_type="application/json",
            HTTP_ORIGIN=ORIGIN,
        )

    def csrf(self) -> str:
        """The per-session token the door handed back."""
        return self._csrf

    def sign_in(self, subject=MERCHANT_SUBJECT, **token_kwargs):
        response = self.open_session(subject, **token_kwargs)
        self.assertEqual(response.status_code, 201, response.content)
        self._csrf = response.json()["csrf_token"]
        return response


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TheOnlyGuardTests(EmbedTestCase):
    """The subject-to-merchant binding, which is all there is."""

    def test_a_bound_merchant_is_let_in(self):
        response = self.sign_in()

        self.assertTrue(response.json()["authenticated"])
        self.assertEqual(response.json()["merchant"]["name"], self.merchant.name)
        # And the panel itself opens, with no Django login anywhere in sight.
        self.assertEqual(self.client.get(self.queue_url).status_code, 200)

    def test_an_ordinary_client_with_a_perfectly_valid_token_is_refused(self):
        """The test this whole surface exists to pass.

        Nothing is wrong with this token: it is signed by B2CORE's own key,
        unexpired, and names a real person. It carries no client type, because
        B2CORE's tokens do not — so if the panel believed the token alone, this
        request would open a merchant's queue for somebody who is not a
        merchant. The binding is what refuses it.
        """
        response = self.open_session(subject=CLIENT_SUBJECT)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "not_a_merchant")
        # No session, and therefore no panel.
        self.assertNotEqual(self.client.get(self.queue_url).status_code, 200)

    def test_a_subject_that_is_nobody_at_all_is_refused(self):
        response = self.open_session(subject="b2core-subject-does-not-exist")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "not_a_merchant")

    def test_an_empty_subject_never_matches_a_merchant_without_one(self):
        """A merchant with no ``b2core_id`` must not be reachable by a blank.

        The column is NULL when unset rather than ``""`` precisely so that
        "nobody has this identifier" cannot collide with "everybody without
        one". This is that, from the authentication side.
        """
        self.assertIsNone(self.other.b2core_id)

        with self.assertRaises(embed_session.MerchantEmbedRefused) as refusal:
            embed_session.resolve_merchant("")
        self.assertEqual(refusal.exception.code, "not_a_merchant")

    def test_a_forged_token_is_refused_as_a_token(self):
        response = self.client.post(
            self.session_url,
            data={"token": make_token(sub=MERCHANT_SUBJECT, signing_key="attacker")},
            content_type="application/json",
            HTTP_ORIGIN=ORIGIN,
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "invalid_token")

    def test_the_binding_is_to_this_merchant_and_not_another(self):
        self.sign_in()

        response = self.client.get(self.queue_url)

        self.assertEqual(response.context["merchant_name"], self.merchant.name)


# ---------------------------------------------------------------------------
# Merchants who are bound and still refused
# ---------------------------------------------------------------------------


class StoodDownMerchantTests(EmbedTestCase):
    """Archived, suspended, or with no login account — the binding is not enough."""

    def test_a_suspended_merchant_is_refused(self):
        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active", "updated_at"])

        response = self.open_session()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "merchant_suspended")

    def test_an_archived_merchant_is_refused(self):
        self.merchant.archived_at = self.merchant.created_at
        self.merchant.save(update_fields=["archived_at", "updated_at"])

        response = self.open_session()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "merchant_archived")

    def test_a_merchant_with_no_login_account_is_refused(self):
        merchant = Merchant.objects.create(name="تاجر بلا حساب", b2core_id="b2core-orphan-1")

        with self.assertRaises(embed_session.MerchantEmbedRefused) as refusal:
            embed_session.resolve_merchant(merchant.b2core_id)

        self.assertEqual(refusal.exception.code, "no_login_account")

    def test_a_merchant_whose_account_was_disabled_is_refused(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = self.open_session()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "no_login_account")

    def test_suspension_takes_effect_on_the_next_click_not_the_next_login(self):
        """An open session is re-checked, not trusted because it exists."""
        self.sign_in()
        self.assertEqual(self.client.get(self.queue_url).status_code, 200)

        self.merchant.is_active = False
        self.merchant.save(update_fields=["is_active", "updated_at"])

        self.assertNotEqual(self.client.get(self.queue_url).status_code, 200)

    def test_archiving_takes_effect_on_the_next_click_too(self):
        self.sign_in()
        self.merchant.archived_at = self.merchant.created_at
        self.merchant.save(update_fields=["archived_at", "updated_at"])

        self.assertNotEqual(self.client.get(self.queue_url).status_code, 200)

    def test_unbinding_a_merchant_ends_the_session_it_opened(self):
        self.sign_in()
        self.merchant.b2core_id = None
        self.merchant.save(update_fields=["b2core_id", "updated_at"])

        self.assertNotEqual(self.client.get(self.queue_url).status_code, 200)


# ---------------------------------------------------------------------------
# The cookie, and the two it must not be
# ---------------------------------------------------------------------------


class SessionCookieTests(EmbedTestCase):
    """Three sessions, three cookies, and only one of them is weak here."""

    def test_the_session_rides_its_own_cookie(self):
        from django.conf import settings

        response = self.sign_in()
        cookie = response.cookies[settings.MERCHANT_SESSION_COOKIE_NAME]

        self.assertEqual(cookie["samesite"], "None")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/merchant/")

    def test_the_internal_session_cookie_is_never_written(self):
        from django.conf import settings

        response = self.sign_in()

        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.PORTAL_SESSION_COOKIE_NAME, response.cookies)

    def test_the_three_cookie_names_are_three_names(self):
        from django.conf import settings

        names = {
            settings.SESSION_COOKIE_NAME,
            settings.PORTAL_SESSION_COOKIE_NAME,
            settings.MERCHANT_SESSION_COOKIE_NAME,
        }

        self.assertEqual(len(names), 3)

    def test_an_embed_session_does_not_open_the_finance_panel(self):
        self.sign_in()

        response = self.client.get(reverse("finance:dashboard"))

        self.assertNotEqual(response.status_code, 200)

    def test_the_session_cannot_outlive_the_token_that_made_it(self):
        expires = int(time.time()) + 120
        response = self.sign_in(exp=expires)

        self.assertEqual(response.json()["expires_at"], expires)

    def test_a_long_lived_token_is_still_capped_by_our_own_ceiling(self):
        far = int(time.time()) + 60 * 60 * 24 * 30
        response = self.sign_in(exp=far)

        self.assertLess(response.json()["expires_at"], far)

    def test_logging_out_ends_the_session(self):
        self.sign_in()
        response = self.client.delete(
            self.session_url,
            HTTP_ORIGIN=ORIGIN,
            HTTP_X_MERCHANT_CSRF=self.csrf(),
        )

        self.assertFalse(response.json()["authenticated"])
        self.assertNotEqual(self.client.get(self.queue_url).status_code, 200)


# ---------------------------------------------------------------------------
# The door itself
# ---------------------------------------------------------------------------


class DoorTests(EmbedTestCase):
    """What the two public routes say to somebody who has proved nothing."""

    def test_the_handshake_page_renders_without_a_session(self):
        response = self.client.get(reverse("merchant_panel:embed"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.merchant.name)

    def test_the_probe_reveals_no_merchant(self):
        payload = self.client.get(self.session_url).json()

        self.assertFalse(payload["authenticated"])
        self.assertNotIn("merchant", payload)

    def test_a_post_from_a_foreign_origin_is_refused(self):
        response = self.client.post(
            self.session_url,
            data={"token": make_token(sub=MERCHANT_SUBJECT)},
            content_type="application/json",
            HTTP_ORIGIN="https://not-b2core.example.com",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "forbidden_origin")

    def test_a_post_with_no_origin_at_all_is_refused(self):
        response = self.client.post(
            self.session_url,
            data={"token": make_token(sub=MERCHANT_SUBJECT)},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_a_framed_visitor_with_no_session_is_sent_to_the_door(self):
        """…rather than to a login page that cannot render inside a frame."""
        response = self.client.get(self.queue_url, HTTP_SEC_FETCH_DEST="iframe")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("merchant_panel:embed"))

    def test_an_unframed_visitor_still_goes_to_the_login(self):
        response = self.client.get(self.queue_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("two_factor:login"), response["Location"])


# ---------------------------------------------------------------------------
# Writing from inside the frame
# ---------------------------------------------------------------------------


class EmbeddedWriteTests(EmbedTestCase):
    """The panel's forms have to work in the frame — and only from it."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.action_url = reverse(
            "merchant_panel:request_action",
            args=[self.request_obj.public_ref, "confirm"],
        )

    def test_a_form_post_carrying_the_session_token_is_accepted(self):
        response = self.client.post(
            self.action_url,
            {"csrfmiddlewaretoken": self.csrf()},
            HTTP_ORIGIN=ORIGIN,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.MERCHANT_CONFIRMED)

    def test_a_post_without_the_session_token_is_refused(self):
        response = self.client.post(self.action_url, {}, HTTP_ORIGIN=ORIGIN)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "invalid_csrf")
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.ASSIGNED)

    def test_a_post_from_a_foreign_origin_is_refused(self):
        response = self.client.post(
            self.action_url,
            {"csrfmiddlewaretoken": self.csrf()},
            HTTP_ORIGIN="https://not-b2core.example.com",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "forbidden_origin")
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, RequestStatus.ASSIGNED)

    def test_the_rendered_form_carries_the_session_token(self):
        """``{% csrf_token %}`` renders the embed token, or no form works."""
        response = self.client.get(
            reverse("merchant_panel:request_detail", args=[self.request_obj.public_ref])
        )

        self.assertContains(response, self.csrf())

    def test_the_two_factor_link_is_hidden_inside_the_frame(self):
        """It leads to a page that refuses to be framed, so it is not offered."""
        response = self.client.get(self.queue_url)

        self.assertTrue(response.context["embedded"])
        self.assertNotContains(response, reverse("two_factor:profile"))

    def test_the_second_factor_gate_does_not_bounce_an_embed_session(self):
        """The merchant has no TOTP device, and does not need one here.

        B2CORE authenticated this person. There is no password in the session
        for a second factor to stand in front of, and the enrolment wizard is a
        page that cannot render inside the frame.
        """
        self.assertFalse(self.user.has_verified_two_factor)

        self.assertEqual(self.client.get(self.queue_url).status_code, 200)


# ---------------------------------------------------------------------------
# The emergency password door
# ---------------------------------------------------------------------------


class PasswordLoginTests(MerchantPanelTestCase):
    """Spec: kept, and shut. ``MERCHANT_PASSWORD_LOGIN`` is off by default."""

    def authenticate(self, user):
        from django.contrib.auth import authenticate

        return authenticate(
            None, username=user.email, password="portal-test-pass-12345"
        )

    def test_it_is_off_unless_somebody_turns_it_on(self):
        self.assertFalse(merchant_password_login_allowed())

    def test_a_merchant_cannot_sign_in_with_a_password(self):
        self.assertIsNone(self.authenticate(self.user))

    @override_settings(MERCHANT_PASSWORD_LOGIN=True)
    def test_the_emergency_door_still_opens_when_it_is_turned_on(self):
        self.assertEqual(self.authenticate(self.user), self.user)

    def test_finance_is_unaffected(self):
        """The setting is about merchants, and about nobody else."""
        staff = make_user("finance-door@maxifyfx.com", Role.FINANCE_STAFF)

        self.assertEqual(self.authenticate(staff), staff)

    def test_a_wrong_password_is_still_wrong_when_the_door_is_open(self):
        from django.contrib.auth import authenticate

        with override_settings(MERCHANT_PASSWORD_LOGIN=True):
            self.assertIsNone(
                authenticate(None, username=self.user.email, password="not-it")
            )


# ---------------------------------------------------------------------------
# The start-up checks
# ---------------------------------------------------------------------------


class ConfigurationCheckTests(SimpleTestCase):
    """Each of these guards a mistake that is silent at run time.

    A cookie the browser quietly drops, or a cookie that is quietly one of the
    other two, produces no error anywhere — just a surface nobody can sign in
    to, or a relaxation handed to a panel that must not have it.
    """

    @staticmethod
    def ids(issues) -> set[str]:
        return {issue.id for issue in issues}

    def test_the_shipped_configuration_raises_nothing(self):
        self.assertEqual(check_merchant_session_cookie(None), [])

    @override_settings(MERCHANT_SESSION_COOKIE_NAME="maxpay_sessionid")
    def test_sharing_the_internal_cookie_name_is_an_error(self):
        self.assertIn("merchant.E020", self.ids(check_merchant_session_cookie(None)))

    @override_settings(MERCHANT_SESSION_COOKIE_NAME="maxpay_embed_sid")
    def test_sharing_the_client_cookie_name_is_an_error(self):
        self.assertIn("merchant.E021", self.ids(check_merchant_session_cookie(None)))

    @override_settings(
        MERCHANT_SESSION_COOKIE_SAMESITE="None", MERCHANT_SESSION_COOKIE_SECURE=False
    )
    def test_samesite_none_without_secure_is_an_error(self):
        # Browsers drop that cookie outright: the symptom is "no merchant can
        # ever sign in", with nothing in the logs to say why.
        self.assertIn("merchant.E022", self.ids(check_merchant_session_cookie(None)))

    @override_settings(MERCHANT_SESSION_COOKIE_PATH="/")
    def test_a_cookie_scoped_wider_than_the_panel_is_a_warning(self):
        self.assertIn("merchant.W023", self.ids(check_merchant_session_cookie(None)))

    @override_settings(
        B2CORE_ORIGIN="https://portal.b2core.test",
        MERCHANT_SESSION_COOKIE_SAMESITE="Lax",
    )
    def test_a_lax_cookie_on_a_framed_panel_is_flagged_at_deploy(self):
        self.assertIn("merchant.W024", self.ids(check_merchant_embed_deployment(None)))

    @override_settings(MERCHANT_PASSWORD_LOGIN=True)
    def test_the_open_emergency_door_is_said_out_loud_at_deploy(self):
        self.assertIn("merchant.W025", self.ids(check_merchant_embed_deployment(None)))

    def test_the_shut_door_is_not_warned_about(self):
        self.assertNotIn("merchant.W025", self.ids(check_merchant_embed_deployment(None)))
