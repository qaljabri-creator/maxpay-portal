"""The start-up checks (``apps/portal/checks.py``).

Every one of these guards a mistake that produces no error at run time — just a
cookie the browser silently drops, or a token accepted from the wrong place. If
the check itself regresses, nothing else in the suite notices, so it is tested
directly.
"""

from django.test import SimpleTestCase, override_settings

from apps.portal.checks import (
    check_b2core_deployment,
    check_b2core_settings,
    check_portal_session_cookie,
)

from .support import B2CORE_SETTINGS


def ids(issues) -> set[str]:
    return {issue.id for issue in issues}


@override_settings(**B2CORE_SETTINGS)
class B2CoreSettingsCheckTests(SimpleTestCase):
    def test_a_sane_configuration_raises_nothing(self):
        self.assertEqual(check_b2core_settings(None), [])

    @override_settings(B2CORE_ORIGIN="https://portal.example.com/embed")
    def test_an_origin_with_a_path_is_an_error(self):
        """postMessage and CSP both take a bare origin; a path silently breaks both."""
        self.assertIn("portal.E002", ids(check_b2core_settings(None)))

    @override_settings(B2CORE_ORIGIN="portal.example.com")
    def test_an_origin_without_a_scheme_is_an_error(self):
        self.assertIn("portal.E002", ids(check_b2core_settings(None)))

    @override_settings(B2CORE_JWT_ALGORITHMS=[])
    def test_an_empty_algorithm_list_is_an_error(self):
        self.assertIn("portal.E003", ids(check_b2core_settings(None)))

    @override_settings(B2CORE_JWT_ALGORITHMS=["RS256", "HS256"])
    def test_a_symmetric_algorithm_is_an_error(self):
        """The JWKS publishes a *public* key; accepting HMAC would make it a
        signing key for anyone who fetched it."""
        self.assertIn("portal.E004", ids(check_b2core_settings(None)))

    @override_settings(B2CORE_JWT_ALGORITHMS=["none"])
    def test_alg_none_in_the_settings_is_an_error(self):
        self.assertIn("portal.E004", ids(check_b2core_settings(None)))


@override_settings(**B2CORE_SETTINGS)
class SessionCookieCheckTests(SimpleTestCase):
    def test_the_shipped_configuration_raises_nothing(self):
        self.assertEqual(check_portal_session_cookie(None), [])

    @override_settings(
        PORTAL_SESSION_COOKIE_NAME="maxpay_sessionid",
        SESSION_COOKIE_NAME="maxpay_sessionid",
    )
    def test_sharing_a_cookie_name_with_the_internal_panel_is_an_error(self):
        """The decision the whole design rests on: two sessions, two cookies."""
        self.assertIn("portal.E011", ids(check_portal_session_cookie(None)))

    @override_settings(PORTAL_SESSION_COOKIE_NAME="")
    def test_an_unnamed_portal_cookie_is_an_error(self):
        self.assertIn("portal.E010", ids(check_portal_session_cookie(None)))

    @override_settings(
        PORTAL_SESSION_COOKIE_SAMESITE="None",
        PORTAL_SESSION_COOKIE_SECURE=False,
    )
    def test_samesite_none_without_secure_is_an_error(self):
        """Browsers drop that cookie outright, so no client would hold a session."""
        self.assertIn("portal.E012", ids(check_portal_session_cookie(None)))

    @override_settings(
        PORTAL_SESSION_COOKIE_SAMESITE="Lax",
        PORTAL_SESSION_COOKIE_SECURE=False,
    )
    def test_a_lax_portal_cookie_without_secure_is_not_an_error(self):
        """Wrong for the iframe, but not *dropped* — the deploy check says so."""
        self.assertNotIn("portal.E012", ids(check_portal_session_cookie(None)))


@override_settings(**B2CORE_SETTINGS)
class DeploymentCheckTests(SimpleTestCase):
    @override_settings(PORTAL_SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SAMESITE="Lax")
    def test_a_configured_deployment_raises_nothing(self):
        self.assertEqual(check_b2core_deployment(None), [])

    @override_settings(B2CORE_ORIGIN="", B2CORE_JWKS_URL="")
    def test_an_unconfigured_deployment_is_an_error(self):
        issues = check_b2core_deployment(None)
        self.assertIn("portal.E001", ids(issues))
        message = str(issues[0])
        self.assertIn("B2CORE_ORIGIN", message)
        self.assertIn("B2CORE_JWKS_URL", message)

    @override_settings(B2CORE_JWT_ISSUER="")
    def test_an_unverified_issuer_is_a_warning(self):
        self.assertIn("portal.W005", ids(check_b2core_deployment(None)))

    @override_settings(B2CORE_JWT_AUDIENCE="")
    def test_an_unverified_audience_is_a_warning(self):
        self.assertIn("portal.W006", ids(check_b2core_deployment(None)))

    @override_settings(PORTAL_SESSION_COOKIE_SAMESITE="Lax")
    def test_a_portal_cookie_that_cannot_reach_the_iframe_is_a_warning(self):
        self.assertIn("portal.W013", ids(check_b2core_deployment(None)))

    @override_settings(SESSION_COOKIE_SAMESITE="None")
    def test_relaxing_the_internal_cookie_is_a_warning(self):
        """The embed has its own cookie precisely so this one need not move."""
        self.assertIn("portal.W014", ids(check_b2core_deployment(None)))

    def test_the_deployment_checks_do_not_run_on_a_plain_check(self):
        """A developer without B2CORE credentials must still be able to work."""
        from django.core.checks import registry

        deploy_only = registry.registry.get_checks(include_deployment_checks=True)
        default = registry.registry.get_checks(include_deployment_checks=False)

        self.assertIn(check_b2core_deployment, deploy_only)
        self.assertNotIn(check_b2core_deployment, default)
        self.assertIn(check_portal_session_cookie, default)
