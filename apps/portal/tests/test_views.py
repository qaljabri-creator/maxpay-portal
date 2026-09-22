"""The session endpoints and the page B2CORE frames (spec §4, §11).

Read alongside ``test_tokens.py``: that file proves the verification is right,
this one proves nothing gets a session without going through it — and that the
session it produces is the client's alone, on a cookie the internal panel never
sees.
"""

import json
import time

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Client as PortalClient
from apps.accounts.models import Role, User
from apps.portal import session as portal_session

from .support import (
    B2CORE_SETTINGS,
    ORIGIN,
    StubbedJWKS,
    decode_body,
    jwks_document,
    make_token,
    unsigned_token,
)

PORTAL_COOKIE = "maxpay_embed_sid"
INTERNAL_COOKIE = "maxpay_sessionid"

VIEW_SETTINGS = dict(
    B2CORE_SETTINGS,
    PORTAL_SESSION_COOKIE_NAME=PORTAL_COOKIE,
    PORTAL_SESSION_COOKIE_SAMESITE="None",
    PORTAL_SESSION_COOKIE_SECURE=True,
    PORTAL_SESSION_COOKIE_PATH="/portal/",
    SESSION_COOKIE_NAME=INTERNAL_COOKIE,
    SESSION_COOKIE_SAMESITE="Lax",
    # The limiter has its own test; everywhere else it must stay out of the way.
    PORTAL_SESSION_RATE="1000/minute",
)


@override_settings(**VIEW_SETTINGS)
class PortalViewTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.jwks = StubbedJWKS(jwks_document("primary")).start()
        self.addCleanup(self.jwks.stop)
        self.session_url = reverse("portal:session")
        self.preferences_url = reverse("portal:preferences")
        self.bootstrap_url = reverse("portal:bootstrap")

    # -- helpers -----------------------------------------------------------

    def post_json(self, url, payload, *, origin="http://testserver", token=None, **extra):
        headers = {}
        if origin is not None:
            headers["origin"] = origin
        if token:
            headers["x-portal-csrf"] = token
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            headers=headers,
            **extra,
        )

    def authenticate(self, **claim_overrides):
        """Run the real handshake and return the session payload."""
        response = self.post_json(self.session_url, {"token": make_token(**claim_overrides)})
        self.assertEqual(response.status_code, 201, response.content)
        return decode_body(response)


class SessionCreationTests(PortalViewTestCase):
    def test_a_valid_token_establishes_a_session_and_a_client(self):
        body = self.authenticate()

        self.assertTrue(body["authenticated"])
        self.assertEqual(body["client"]["reference"], "b2core-subject-77")
        self.assertTrue(body["csrf_token"])
        self.assertTrue(body["expires_at"])
        self.assertEqual(PortalClient.objects.count(), 1)

    def test_the_session_survives_into_the_next_request(self):
        self.authenticate()
        body = decode_body(self.client.get(self.session_url))

        self.assertTrue(body["authenticated"])
        self.assertEqual(body["client"]["reference"], "b2core-subject-77")

    def test_a_second_client_gets_their_own_record(self):
        self.authenticate()
        self.authenticate(sub="b2core-subject-99")

        self.assertEqual(PortalClient.objects.count(), 2)

    def test_an_invalid_signature_gets_no_session(self):
        response = self.post_json(self.session_url, {"token": make_token(signing_key="attacker")})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(decode_body(response)["error"], "invalid_token")
        self.assertNotIn(PORTAL_COOKIE, response.cookies)
        self.assertEqual(PortalClient.objects.count(), 0)

    def test_an_expired_token_gets_no_session(self):
        now = int(time.time())
        response = self.post_json(
            self.session_url, {"token": make_token(iat=now - 7200, exp=now - 3600)}
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(decode_body(response)["remedy"], "reauthenticate")

    def test_an_unsigned_token_gets_no_session(self):
        response = self.post_json(self.session_url, {"token": unsigned_token()})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(PortalClient.objects.count(), 0)

    def test_the_failure_body_never_explains_which_check_failed(self):
        """A precise reason is a probe; the log gets it, the browser does not."""
        response = self.post_json(self.session_url, {"token": make_token(iss="https://evil.test")})
        body = decode_body(response)

        for leak in ("iss", "issuer", "signature", "audience", "kid"):
            self.assertNotIn(leak, json.dumps(body).lower())

    def test_a_missing_token_is_a_bad_request(self):
        for payload in ({}, {"token": ""}, {"token": "   "}, {"token": 42}, {"token": None}):
            with self.subTest(payload=payload):
                response = self.post_json(self.session_url, payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(decode_body(response)["error"], "missing_token")

    def test_a_body_that_is_not_json_is_a_bad_request(self):
        response = self.client.post(
            self.session_url,
            data="not json at all",
            content_type="application/json",
            headers={"origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_json_array_body_is_a_bad_request(self):
        response = self.post_json(self.session_url, ["token"])
        self.assertEqual(response.status_code, 400)

    def test_an_oversized_body_is_refused(self):
        response = self.post_json(self.session_url, {"token": "A" * 20000})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(decode_body(response)["error"], "invalid_request")

    def test_a_jwks_outage_is_a_retry_not_a_re_authentication(self):
        self.jwks.stop()
        broken = StubbedJWKS(error=TimeoutError("unreachable")).start()
        self.addCleanup(broken.stop)

        response = self.post_json(self.session_url, {"token": make_token()})
        body = decode_body(response)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["error"], "key_unavailable")
        self.assertEqual(body["remedy"], "retry")

    @override_settings(B2CORE_JWKS_URL="")
    def test_an_unconfigured_deployment_says_so_rather_than_blaming_the_client(self):
        response = self.post_json(self.session_url, {"token": make_token()})
        body = decode_body(response)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["error"], "not_configured")
        self.assertEqual(body["remedy"], "contact_support")

    def test_a_deactivated_client_is_refused_despite_a_perfect_token(self):
        self.authenticate()
        self.client.cookies.pop(PORTAL_COOKIE, None)
        PortalClient.objects.update(is_active=False)

        response = self.post_json(self.session_url, {"token": make_token()})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(decode_body(response)["error"], "client_disabled")
        self.assertFalse(decode_body(self.client.get(self.session_url))["authenticated"])

    def test_the_session_endpoint_is_never_cached(self):
        response = self.client.get(self.session_url)
        self.assertIn("no-store", response.headers["Cache-Control"])


class SessionCookieTests(PortalViewTestCase):
    """Decision: two sessions, two cookies. This is where that is enforced."""

    def test_the_portal_cookie_is_the_one_that_gets_set(self):
        response = self.post_json(self.session_url, {"token": make_token()})

        self.assertIn(PORTAL_COOKIE, response.cookies)
        self.assertNotIn(INTERNAL_COOKIE, response.cookies)

    def test_the_portal_cookie_can_survive_a_third_party_iframe(self):
        cookie = self.post_json(self.session_url, {"token": make_token()}).cookies[PORTAL_COOKIE]

        self.assertEqual(cookie["samesite"], "None")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])

    def test_the_portal_cookie_is_scoped_to_the_portal(self):
        """It is never sent to the internal panel, so it cannot be replayed there."""
        cookie = self.post_json(self.session_url, {"token": make_token()}).cookies[PORTAL_COOKIE]
        self.assertEqual(cookie["path"], "/portal/")

    def test_the_internal_session_keeps_its_lax_cookie(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertNotEqual(settings.SESSION_COOKIE_NAME, settings.PORTAL_SESSION_COOKIE_NAME)

    def test_an_internal_login_does_not_produce_a_portal_client(self):
        user = User.objects.create_user(
            email="staff@maxifyfx.com",
            password="portal-test-pass-12345",
            full_name="موظف",
            role=Role.FINANCE_STAFF,
        )
        self.client.force_login(user)

        body = decode_body(self.client.get(self.session_url))

        self.assertFalse(body["authenticated"])

    def test_a_portal_session_does_not_produce_an_internal_login(self):
        self.authenticate()

        response = self.client.get("/finance/")

        # Redirected to the internal login: the portal session buys nothing here.
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith(reverse("two_factor:login")))

    def test_the_two_sessions_coexist_without_touching_each_other(self):
        user = User.objects.create_user(
            email="admin@maxifyfx.com",
            password="portal-test-pass-12345",
            full_name="مدير",
            role=Role.FINANCE_ADMIN,
        )
        self.client.force_login(user)
        internal_before = self.client.cookies[INTERNAL_COOKIE].value

        self.authenticate()

        self.assertEqual(self.client.cookies[INTERNAL_COOKIE].value, internal_before)
        self.assertTrue(self.client.cookies[PORTAL_COOKIE].value)
        self.assertNotEqual(self.client.cookies[PORTAL_COOKIE].value, internal_before)

    def test_a_session_whose_token_has_expired_is_dropped_on_the_next_request(self):
        self.authenticate()

        store = self.client.session  # the internal one; the portal one is separate
        self.assertNotIn(portal_session.SESSION_CLIENT_ID, store)

        # Age the portal session past its token's expiry.
        from apps.portal.sessions import new_store

        portal = new_store(self.client.cookies[PORTAL_COOKIE].value)
        portal[portal_session.SESSION_EXPIRES_AT] = int(time.time()) - 1
        portal.save()

        response = self.client.get(self.session_url)

        self.assertFalse(decode_body(response)["authenticated"])
        self.assertEqual(response.cookies[PORTAL_COOKIE].value, "")


class OriginAndTokenGuardTests(PortalViewTestCase):
    """Django's CSRF cookie cannot reach us in the frame, so these stand in."""

    def test_a_post_without_an_origin_is_refused(self):
        response = self.post_json(self.session_url, {"token": make_token()}, origin=None)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(decode_body(response)["error"], "forbidden_origin")

    def test_a_post_from_an_unknown_origin_is_refused(self):
        response = self.post_json(
            self.session_url, {"token": make_token()}, origin="https://evil.test"
        )
        self.assertEqual(response.status_code, 403)

    def test_a_post_from_the_b2core_origin_is_accepted(self):
        response = self.post_json(self.session_url, {"token": make_token()}, origin=ORIGIN)
        self.assertEqual(response.status_code, 201)

    def test_a_referer_stands_in_when_the_origin_header_is_absent(self):
        response = self.client.post(
            self.session_url,
            data=json.dumps({"token": make_token()}),
            content_type="application/json",
            headers={"referer": f"{ORIGIN}/some/page"},
        )
        self.assertEqual(response.status_code, 201)

    def test_a_referer_from_elsewhere_does_not(self):
        response = self.client.post(
            self.session_url,
            data=json.dumps({"token": make_token()}),
            content_type="application/json",
            headers={"referer": "https://evil.test/page"},
        )
        self.assertEqual(response.status_code, 403)

    def test_reading_the_session_needs_no_origin(self):
        """A GET changes nothing, and the embed calls it before anything else."""
        self.assertEqual(self.client.get(self.session_url).status_code, 200)

    def test_ending_a_session_needs_the_session_token(self):
        self.authenticate()

        response = self.client.delete(self.session_url, headers={"origin": "http://testserver"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(decode_body(response)["error"], "invalid_csrf")
        self.assertTrue(decode_body(self.client.get(self.session_url))["authenticated"])

    def test_a_wrong_session_token_is_refused(self):
        self.authenticate()

        response = self.client.delete(
            self.session_url,
            headers={"origin": "http://testserver", "x-portal-csrf": "not-the-token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_the_session_token_of_another_session_is_refused(self):
        stolen = self.authenticate()["csrf_token"]
        self.client.cookies.pop(PORTAL_COOKIE, None)
        self.authenticate(sub="b2core-subject-99")

        response = self.client.delete(
            self.session_url,
            headers={"origin": "http://testserver", "x-portal-csrf": stolen},
        )
        self.assertEqual(response.status_code, 403)

    def test_djangos_own_csrf_check_does_not_stand_in_the_way(self):
        """Its cookie is SameSite=Lax and never arrives inside the frame, so
        leaving it enforced here would fail every real client. The Origin check
        and the per-session token above are what replace it."""
        from django.test import Client as TestClient

        strict = TestClient(enforce_csrf_checks=True)
        response = strict.post(
            self.session_url,
            data=json.dumps({"token": make_token()}),
            content_type="application/json",
            headers={"origin": "http://testserver"},
        )

        self.assertEqual(response.status_code, 201, response.content)

    def test_creating_a_session_needs_no_prior_token(self):
        """There is nothing to echo yet — the JWT is the credential."""
        self.assertEqual(
            self.post_json(self.session_url, {"token": make_token()}).status_code, 201
        )


class LogoutTests(PortalViewTestCase):
    def test_logout_clears_the_session_and_its_cookie(self):
        body = self.authenticate()

        response = self.client.delete(
            self.session_url,
            headers={"origin": "http://testserver", "x-portal-csrf": body["csrf_token"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(decode_body(response)["authenticated"])
        self.assertEqual(response.cookies[PORTAL_COOKIE].value, "")
        self.assertFalse(decode_body(self.client.get(self.session_url))["authenticated"])

    def test_logging_out_twice_is_not_an_error(self):
        body = self.authenticate()
        headers = {"origin": "http://testserver", "x-portal-csrf": body["csrf_token"]}
        self.client.delete(self.session_url, headers=headers)

        response = self.client.delete(self.session_url, headers={"origin": "http://testserver"})

        self.assertEqual(response.status_code, 200)

    def test_the_client_record_outlives_the_session(self):
        """Logging out is not deletion — the audit trail keeps its references."""
        body = self.authenticate()
        self.client.delete(
            self.session_url,
            headers={"origin": "http://testserver", "x-portal-csrf": body["csrf_token"]},
        )
        self.assertEqual(PortalClient.objects.count(), 1)


class PreferenceEndpointTests(PortalViewTestCase):
    def test_a_theme_change_is_stored(self):
        token = self.authenticate()["csrf_token"]

        response = self.post_json(self.preferences_url, {"theme": "dark"}, token=token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(decode_body(response)["theme"], "dark")
        self.assertEqual(decode_body(self.client.get(self.session_url))["theme"], "dark")

    def test_a_language_change_is_stored(self):
        token = self.authenticate()["csrf_token"]

        response = self.post_json(self.preferences_url, {"language": "en-GB"}, token=token)

        self.assertEqual(decode_body(response)["language"], "en")

    def test_an_unsupported_value_is_refused_and_nothing_changes(self):
        token = self.authenticate()["csrf_token"]

        response = self.post_json(self.preferences_url, {"theme": "neon"}, token=token)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(decode_body(response)["rejected"], ["theme"])
        self.assertEqual(decode_body(self.client.get(self.session_url))["theme"], "light")

    def test_one_good_value_alongside_one_bad_one_still_applies(self):
        token = self.authenticate()["csrf_token"]

        response = self.post_json(
            self.preferences_url, {"theme": "dark", "language": "fr"}, token=token
        )
        body = decode_body(response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["applied"], {"theme": "dark"})
        self.assertEqual(body["rejected"], ["language"])

    def test_a_preference_may_be_set_before_authentication(self):
        """B2CORE announces the theme as the frame loads, which can beat the token."""
        response = self.post_json(self.preferences_url, {"theme": "dark"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(decode_body(response)["theme"], "dark")

    def test_preferences_still_need_an_acceptable_origin(self):
        token = self.authenticate()["csrf_token"]
        response = self.post_json(
            self.preferences_url, {"theme": "dark"}, origin="https://evil.test", token=token
        )
        self.assertEqual(response.status_code, 403)

    def test_preferences_need_the_session_token_once_a_session_exists(self):
        self.authenticate()
        response = self.post_json(self.preferences_url, {"theme": "dark"})
        self.assertEqual(response.status_code, 403)


class RateLimitTests(PortalViewTestCase):
    @override_settings(PORTAL_SESSION_RATE="2/minute")
    def test_the_session_endpoint_is_capped_per_caller(self):
        payload = {"token": make_token(signing_key="attacker")}

        self.assertEqual(self.post_json(self.session_url, payload).status_code, 401)
        self.assertEqual(self.post_json(self.session_url, payload).status_code, 401)

        response = self.post_json(self.session_url, payload)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(decode_body(response)["error"], "rate_limited")

    @override_settings(PORTAL_SESSION_RATE="")
    def test_an_empty_rate_disables_the_cap(self):
        payload = {"token": make_token(signing_key="attacker")}
        for _ in range(4):
            self.assertEqual(self.post_json(self.session_url, payload).status_code, 401)

    @override_settings(PORTAL_SESSION_RATE="2/minute")
    def test_reading_the_session_is_not_capped(self):
        for _ in range(5):
            self.assertEqual(self.client.get(self.session_url).status_code, 200)


class FrameHeaderTests(PortalViewTestCase):
    def test_the_portal_may_be_framed_by_b2core_and_nobody_else(self):
        response = self.client.get(self.bootstrap_url)
        policy = response.headers["Content-Security-Policy"]

        self.assertIn(f"frame-ancestors {ORIGIN}", policy)
        self.assertIn("default-src 'self'", policy)
        self.assertNotIn("X-Frame-Options", response.headers)

    def test_everything_outside_the_portal_stays_unframeable(self):
        response = self.client.get("/healthz/")

        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")

    @override_settings(B2CORE_ORIGIN="")
    def test_an_unconfigured_origin_frames_nothing(self):
        response = self.client.get(self.bootstrap_url)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_the_session_endpoint_carries_the_policy_too(self):
        response = self.client.get(self.session_url)
        self.assertIn(f"frame-ancestors {ORIGIN}", response.headers["Content-Security-Policy"])


class BootstrapPageTests(PortalViewTestCase):
    def test_the_page_renders_the_handshake_shell(self):
        response = self.client.get(self.bootstrap_url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "portal/bootstrap.html")
        self.assertContains(response, 'id="maxpay-embed-config"')
        self.assertContains(response, "js/embed.js")

    def test_the_page_tells_the_script_which_origin_to_talk_to(self):
        response = self.client.get(self.bootstrap_url)
        config = self.embed_config(response)

        self.assertEqual(config["parentOrigin"], ORIGIN)
        self.assertEqual(config["sessionUrl"], self.session_url)
        self.assertEqual(config["preferencesUrl"], self.preferences_url)

    def test_the_page_carries_no_inline_script(self):
        """The CSP it ships with is `default-src 'self'`; an inline handler
        would need that relaxed for the whole page."""
        content = self.client.get(self.bootstrap_url).content.decode()

        self.assertNotIn("onclick=", content)
        self.assertNotIn('<script>', content)

    def test_the_page_renders_no_client_data(self):
        self.authenticate()
        content = self.client.get(self.bootstrap_url).content.decode()

        self.assertNotIn("b2core-subject-77", content)
        self.assertNotIn("client@example.com", content)
        self.assertNotIn("MX-90210", content)

    def test_the_page_is_never_cached(self):
        response = self.client.get(self.bootstrap_url)
        self.assertIn("no-store", response.headers["Cache-Control"])

    @override_settings(B2CORE_JWKS_URL="")
    def test_an_unconfigured_deployment_says_so_on_the_page(self):
        response = self.client.get(self.bootstrap_url)
        self.assertContains(response, 'data-state="unconfigured"')

    def test_the_theme_follows_the_session(self):
        token = self.authenticate()["csrf_token"]
        self.post_json(self.preferences_url, {"theme": "dark"}, token=token)

        self.assertContains(self.client.get(self.bootstrap_url), 'data-theme="dark"')

    def test_light_is_the_theme_before_the_host_has_said_anything(self):
        """The frame paints before `embed-theme-change` can possibly arrive.

        Whatever it paints then is what the client sees for the first moments
        inside the room, so it is a decision and not a fallback: light. The
        attribute has to be *there* and say so — panel-tokens.css, which this
        surface shares with the two panels, forks on the attribute's value and
        defaults to dark, so an absent attribute would paint the frame dark.
        """
        self.assertContains(self.client.get(self.bootstrap_url), 'data-theme="light"')

    def test_a_theme_announced_before_the_token_survives_the_handshake(self):
        """B2CORE announces the theme as the frame loads — usually before the
        token exchange has finished. The theme is stored on the pre-session
        cookie, and `session.start` carries it across `cycle_key`, so the
        session that finally arrives does not hand back the default and undo
        the room the client is actually sitting in.
        """
        self.post_json(self.preferences_url, {"theme": "dark"})

        self.authenticate()

        self.assertEqual(decode_body(self.client.get(self.session_url))["theme"], "dark")
        self.assertContains(self.client.get(self.bootstrap_url), 'data-theme="dark"')

    def test_the_language_follows_the_session(self):
        token = self.authenticate()["csrf_token"]
        self.post_json(self.preferences_url, {"language": "en"}, token=token)

        response = self.client.get(self.bootstrap_url)

        self.assertEqual(response.headers.get("Content-Language"), "en")
        self.assertContains(response, 'lang="en"')

    def test_arabic_is_the_default_and_renders_right_to_left(self):
        response = self.client.get(self.bootstrap_url)
        self.assertContains(response, 'dir="rtl"')

    @staticmethod
    def embed_config(response) -> dict:
        content = response.content.decode()
        marker = 'id="maxpay-embed-config" type="application/json">'
        start = content.index(marker) + len(marker)
        return json.loads(content[start:content.index("</script>", start)])
