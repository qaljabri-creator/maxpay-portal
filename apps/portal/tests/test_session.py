"""The client session: what it stores, what it refuses, and when it dies.

Spec §4, steps 4–6. A portal session is not a Django login — there is no
password and no ``auth`` session — so everything that would normally come free
from ``django.contrib.auth`` has to be established here instead: that the
session cannot outlive its token, that it cannot be fixated, and that it can
never be confused with an internal user's.
"""

import time

from django.test import RequestFactory, TestCase, override_settings

from apps.accounts.models import Client as PortalClient
from apps.portal import session as portal_session
from apps.portal.b2core import Identity
from apps.portal.sessions import new_store

from .support import B2CORE_SETTINGS


def identity(**overrides) -> Identity:
    values = {
        "subject": "b2core-subject-77",
        "email": "client@example.com",
        "display_name": "زينب الجبوري",
        "account_number": "MX-90210",
        "language": "ar",
        "expires_at": int(time.time()) + 900,
    }
    values.update(overrides)
    return Identity(**values)


class SessionTestCase(TestCase):
    """A request carrying the portal store the middleware would have attached."""

    def make_request(self, path="/portal/"):
        request = RequestFactory().get(path)
        request.portal_session = new_store()
        return request


@override_settings(**B2CORE_SETTINGS)
class ClientRecordTests(SessionTestCase):
    def test_a_subject_is_mapped_onto_a_new_client_on_first_use(self):
        client = portal_session.upsert_client(identity())

        self.assertEqual(client.b2core_id, "b2core-subject-77")
        self.assertEqual(client.display_name, "زينب الجبوري")
        self.assertEqual(client.account_number, "MX-90210")
        self.assertEqual(client.preferred_language, "ar")

    def test_the_same_subject_is_never_duplicated(self):
        first = portal_session.upsert_client(identity())
        second = portal_session.upsert_client(identity())

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PortalClient.objects.count(), 1)

    def test_changed_claims_are_written_back(self):
        portal_session.upsert_client(identity())
        client = portal_session.upsert_client(identity(display_name="زينب ج.", email="new@example.com"))

        self.assertEqual(client.display_name, "زينب ج.")
        self.assertEqual(client.email, "new@example.com")

    def test_a_claim_the_token_omits_never_wipes_what_is_stored(self):
        portal_session.upsert_client(identity())
        client = portal_session.upsert_client(
            identity(display_name="", email="", account_number="")
        )

        self.assertEqual(client.display_name, "زينب الجبوري")
        self.assertEqual(client.email, "client@example.com")
        self.assertEqual(client.account_number, "MX-90210")

    def test_a_client_defaults_to_arabic_when_the_token_says_nothing(self):
        client = portal_session.upsert_client(identity(language=""))
        self.assertEqual(client.preferred_language, "ar")


@override_settings(**B2CORE_SETTINGS)
class SessionLifecycleTests(SessionTestCase):
    def test_starting_a_session_stores_the_subject_alongside_the_id(self):
        request = self.make_request()
        client = portal_session.start(request, identity())

        store = request.portal_session
        self.assertEqual(store[portal_session.SESSION_CLIENT_ID], client.pk)
        self.assertEqual(store[portal_session.SESSION_SUBJECT], "b2core-subject-77")
        self.assertTrue(store[portal_session.SESSION_CSRF])

    def test_starting_a_session_cycles_the_key(self):
        """Session fixation: a key handed to the browser before it had an
        identity must not still be valid once it has one."""
        request = self.make_request()
        request.portal_session["anything"] = "forces a key to exist"
        request.portal_session.save()
        before = request.portal_session.session_key

        portal_session.start(request, identity())

        self.assertIsNotNone(before)
        self.assertNotEqual(request.portal_session.session_key, before)

    def test_the_session_expires_with_the_token(self):
        request = self.make_request()
        expiry = int(time.time()) + 300
        portal_session.start(request, identity(expires_at=expiry))

        self.assertEqual(request.portal_session[portal_session.SESSION_EXPIRES_AT], expiry)
        self.assertLessEqual(request.portal_session.get_expiry_age(), 300)

    @override_settings(PORTAL_SESSION_MAX_SECONDS=600)
    def test_a_long_lived_token_is_still_capped(self):
        """A token good for a week must not buy a session good for a week."""
        request = self.make_request()
        portal_session.start(request, identity(expires_at=int(time.time()) + 7 * 86400))

        remaining = request.portal_session[portal_session.SESSION_EXPIRES_AT] - int(time.time())
        self.assertLessEqual(remaining, 600)

    def test_a_token_without_an_expiry_falls_back_to_the_ceiling(self):
        request = self.make_request()
        portal_session.start(request, identity(expires_at=None))

        remaining = request.portal_session[portal_session.SESSION_EXPIRES_AT] - int(time.time())
        self.assertLessEqual(remaining, portal_session.max_session_seconds())
        self.assertGreater(remaining, 0)

    def test_the_client_is_returned_for_a_live_session(self):
        request = self.make_request()
        started = portal_session.start(request, identity())

        self.assertEqual(portal_session.get_client(request).pk, started.pk)

    def test_an_expired_session_yields_nothing_and_is_cleared(self):
        request = self.make_request()
        portal_session.start(request, identity())
        request.portal_session[portal_session.SESSION_EXPIRES_AT] = int(time.time()) - 1

        self.assertIsNone(portal_session.get_client(request))
        self.assertTrue(request.portal_session.is_empty())

    def test_a_deactivated_client_loses_the_session_immediately(self):
        request = self.make_request()
        client = portal_session.start(request, identity())
        PortalClient.objects.filter(pk=client.pk).update(is_active=False)

        self.assertIsNone(portal_session.get_client(request))
        self.assertTrue(request.portal_session.is_empty())

    def test_a_recycled_primary_key_cannot_hand_over_another_clients_session(self):
        """The stored subject is checked against the record, not just its id."""
        request = self.make_request()
        portal_session.start(request, identity())
        request.portal_session[portal_session.SESSION_SUBJECT] = "somebody-else"

        self.assertIsNone(portal_session.get_client(request))
        self.assertTrue(request.portal_session.is_empty())

    def test_a_session_that_was_never_started_yields_nothing(self):
        self.assertIsNone(portal_session.get_client(self.make_request()))

    def test_ending_a_session_flushes_everything(self):
        request = self.make_request()
        portal_session.start(request, identity())

        portal_session.end(request)

        self.assertTrue(request.portal_session.is_empty())
        self.assertIsNone(request.portal_client)
        self.assertIsNone(portal_session.get_client(request))

    def test_re_authenticating_issues_a_new_session_token(self):
        request = self.make_request()
        portal_session.start(request, identity())
        first = portal_session.csrf_token(request)

        portal_session.start(request, identity())

        self.assertTrue(first)
        self.assertNotEqual(portal_session.csrf_token(request), first)

    def test_the_last_seen_stamp_is_touched(self):
        request = self.make_request()
        client = portal_session.start(request, identity())
        client.refresh_from_db()
        self.assertIsNotNone(client.last_seen_at)

    def test_starting_a_session_without_the_middleware_fails_loudly(self):
        request = RequestFactory().get("/portal/")
        with self.assertRaises(RuntimeError):
            portal_session.start(request, identity())


@override_settings(**B2CORE_SETTINGS)
class PreferenceTests(SessionTestCase):
    def test_a_known_theme_is_kept(self):
        request = self.make_request()
        self.assertEqual(portal_session.set_theme(request, "dark"), "dark")
        self.assertEqual(portal_session.get_theme(request), "dark")

    def test_an_unknown_theme_is_refused(self):
        request = self.make_request()
        for candidate in ("neon", "", None, 7, "DARK"):
            with self.subTest(candidate=candidate):
                self.assertIsNone(portal_session.set_theme(request, candidate))
        self.assertEqual(portal_session.get_theme(request), "light")

    def test_a_supported_language_is_kept(self):
        request = self.make_request()
        self.assertEqual(portal_session.set_language(request, "en-GB"), "en")
        self.assertEqual(portal_session.get_language(request), "en")

    def test_a_language_the_deployment_does_not_serve_is_refused(self):
        request = self.make_request()
        for candidate in ("fr", "zz", "", None, 12):
            with self.subTest(candidate=candidate):
                self.assertIsNone(portal_session.set_language(request, candidate))

    def test_preferences_survive_a_re_authentication(self):
        """B2CORE announces the theme once, over postMessage — not in the token."""
        request = self.make_request()
        portal_session.set_theme(request, "dark")
        portal_session.set_language(request, "en")

        portal_session.start(request, identity())

        self.assertEqual(portal_session.get_theme(request), "dark")
        self.assertEqual(portal_session.get_language(request), "en")

    def test_the_token_locale_seeds_the_language_when_nothing_was_announced(self):
        request = self.make_request()
        portal_session.start(request, identity(language="en"))
        self.assertEqual(portal_session.get_language(request), "en")

    def test_an_unsupported_token_locale_is_ignored(self):
        request = self.make_request()
        portal_session.start(request, identity(language="fr"))
        self.assertEqual(portal_session.get_language(request), "ar")


@override_settings(**B2CORE_SETTINGS)
class LazyClientTests(SessionTestCase):
    """``request.portal_client`` is lazy, which has one sharp edge."""

    def run_middleware(self, request):
        from django.http import HttpResponse

        from apps.portal.middleware import ClientSessionMiddleware

        return ClientSessionMiddleware(lambda _r: HttpResponse())(request)

    def test_the_lazy_attribute_is_not_none_even_with_no_client(self):
        """A `SimpleLazyObject` wrapping `None` fails an `is None` test, which is
        exactly the mistake `current_client` exists to make impossible."""
        request = self.make_request()
        self.run_middleware(request)

        self.assertIsNotNone(request.portal_client)
        self.assertFalse(bool(request.portal_client))
        self.assertIsNone(portal_session.current_client(request))

    def test_the_lazy_attribute_resolves_to_the_session_client(self):
        request = self.make_request()
        started = portal_session.start(request, identity())
        del request.portal_client

        self.run_middleware(request)

        self.assertEqual(portal_session.current_client(request).pk, started.pk)

    def test_a_non_portal_path_still_gets_the_attribute(self):
        request = RequestFactory().get("/finance/")
        request.portal_session = new_store()

        self.run_middleware(request)

        self.assertIsNone(portal_session.current_client(request))
