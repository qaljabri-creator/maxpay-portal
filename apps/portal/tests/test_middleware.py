"""The portal's own session middleware and the rate limiter.

``PortalSessionMiddleware`` is the piece that makes two sessions possible at
once, so its response-phase behaviour — when a cookie is written, when it is
deleted, when it is deliberately *not* written — is worth pinning down directly
rather than only through the endpoints.
"""

from django.contrib.sessions.exceptions import SessionInterrupted
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.portal.ratelimit import allow, parse_rate
from apps.portal.sessions import PortalSessionMiddleware

from .support import B2CORE_SETTINGS

COOKIE = "maxpay_embed_sid"

MIDDLEWARE_SETTINGS = dict(
    B2CORE_SETTINGS,
    PORTAL_SESSION_COOKIE_NAME=COOKIE,
    PORTAL_SESSION_COOKIE_PATH="/portal/",
    PORTAL_SESSION_COOKIE_SAMESITE="None",
    PORTAL_SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_NAME="maxpay_sessionid",
)


@override_settings(**MIDDLEWARE_SETTINGS)
class PortalSessionMiddlewareTests(TestCase):
    def run_middleware(self, request, view):
        return PortalSessionMiddleware(view)(request)

    def test_an_untouched_session_sets_no_cookie(self):
        """A page that never authenticates anyone should not hand out a session."""
        response = self.run_middleware(
            RequestFactory().get("/portal/"), lambda _r: HttpResponse()
        )
        self.assertNotIn(COOKIE, response.cookies)

    def test_writing_to_the_session_sets_the_cookie(self):
        def view(request):
            request.portal_session["b2core_sub"] = "subject-1"
            return HttpResponse()

        response = self.run_middleware(RequestFactory().get("/portal/"), view)

        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie.value)
        self.assertEqual(cookie["path"], "/portal/")
        self.assertEqual(cookie["samesite"], "None")
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])

    def test_reading_the_session_varies_the_response_on_cookie(self):
        def view(request):
            request.portal_session.get("b2core_sub")
            return HttpResponse()

        response = self.run_middleware(RequestFactory().get("/portal/"), view)

        self.assertIn("Cookie", response.headers["Vary"])

    def test_a_server_error_does_not_persist_a_new_session(self):
        """The client has no way to know whether a failed write landed."""

        def view(request):
            request.portal_session["b2core_sub"] = "subject-1"
            return HttpResponse(status=500)

        response = self.run_middleware(RequestFactory().get("/portal/"), view)

        self.assertNotIn(COOKIE, response.cookies)

    def test_emptying_the_session_deletes_the_cookie(self):
        def start(request):
            request.portal_session["b2core_sub"] = "subject-1"
            return HttpResponse()

        first = self.run_middleware(RequestFactory().get("/portal/"), start)
        key = first.cookies[COOKIE].value

        def flush(request):
            request.portal_session.flush()
            return HttpResponse()

        request = RequestFactory().get("/portal/")
        request.COOKIES[COOKIE] = key
        response = self.run_middleware(request, flush)

        self.assertEqual(response.cookies[COOKIE].value, "")

    def test_the_internal_session_attribute_is_never_touched(self):
        """Decision: two sessions. This middleware owns exactly one of them."""
        sentinel = object()

        def view(request):
            request.portal_session["b2core_sub"] = "subject-1"
            self.assertIs(request.session, sentinel)
            return HttpResponse()

        request = RequestFactory().get("/portal/")
        request.session = sentinel
        response = self.run_middleware(request, view)

        self.assertNotIn("maxpay_sessionid", response.cookies)

    def test_a_session_deleted_underneath_us_is_reported_not_swallowed(self):
        def view(request):
            request.portal_session["b2core_sub"] = "subject-1"
            request.portal_session.save = _raise_update_error
            return HttpResponse()

        with self.assertRaises(SessionInterrupted):
            self.run_middleware(RequestFactory().get("/portal/"), view)

    def test_a_request_the_middleware_never_saw_is_left_alone(self):
        """The response phase must not invent a session for a request that has none."""
        request = RequestFactory().get("/portal/")
        middleware = PortalSessionMiddleware(lambda _r: HttpResponse())
        response = middleware._persist(request, HttpResponse())

        self.assertNotIn(COOKIE, response.cookies)


def _raise_update_error(*args, **kwargs):
    from django.contrib.sessions.backends.base import UpdateError

    raise UpdateError("gone")


class RateLimitTests(SimpleTestCase):
    def test_rates_parse(self):
        self.assertEqual(parse_rate("30/minute"), (30, 60))
        self.assertEqual(parse_rate("5/second"), (5, 1))
        self.assertEqual(parse_rate(" 100 / hour "), (100, 3600))

    def test_an_unparseable_rate_disables_the_limit_rather_than_crashing(self):
        for candidate in ("", "lots", "30/fortnight", "30", "/minute"):
            with self.subTest(candidate=candidate):
                self.assertIsNone(parse_rate(candidate))

    def test_a_broken_cache_backend_fails_open(self):
        """The limiter is a cost ceiling. It must never be what locks a client out."""
        from unittest import mock

        request = RequestFactory().post("/portal/session/")
        with mock.patch("django.core.cache.cache.add", side_effect=RuntimeError("cache down")):
            self.assertTrue(allow(request, scope="session", rate="1/minute"))
