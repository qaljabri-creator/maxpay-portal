"""The start-up checks (``apps/portal/checks.py``).

Every one of these guards a mistake that produces no error at run time — just a
cookie the browser silently drops, or a token accepted from the wrong place. If
the check itself regresses, nothing else in the suite notices, so it is tested
directly.
"""

import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.portal.checks import (
    check_b2core_deployment,
    check_b2core_settings,
    check_portal_session_cookie,
)

from .support import B2CORE_SETTINGS, REAL_B2CORE_SETTINGS


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


#: `REAL_B2CORE_SETTINGS`, not the legacy fixture, and the difference is the
#: subject of half these tests: the old one carries an audience, which is now
#: itself a deployment error. A baseline that asserts "a correct deployment
#: raises nothing" has to be a deployment that is actually correct.
@override_settings(**REAL_B2CORE_SETTINGS)
class DeploymentCheckTests(SimpleTestCase):
    @override_settings(PORTAL_SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SAMESITE="Lax")
    def test_a_configured_deployment_raises_nothing(self):
        """The configuration a real client authenticates under: issuer set to
        the literal B2CORE value, audience empty."""
        self.assertEqual(check_b2core_deployment(None), [])

    @override_settings(B2CORE_ORIGIN="", B2CORE_JWKS_URL="")
    def test_an_unconfigured_deployment_is_an_error(self):
        issues = check_b2core_deployment(None)
        self.assertIn("portal.E001", ids(issues))
        message = str(issues[0])
        self.assertIn("B2CORE_ORIGIN", message)
        self.assertIn("B2CORE_JWKS_URL", message)

    @override_settings(B2CORE_JWT_ISSUER="")
    def test_an_unverified_issuer_is_an_error(self):
        """With `iss` unchecked, any token signed by any key in the configured
        JWKS is believed — including one minted for somebody else."""
        self.assertIn("portal.E005", ids(check_b2core_deployment(None)))

    @override_settings(
        B2CORE_ORIGIN="https://portal.example.com",
        B2CORE_JWT_ISSUER="https://portal.example.com",
    )
    def test_the_issuer_set_to_the_origin_is_an_error(self):
        """The mistake most available to whoever wires this up: the two look
        like they should be the same string. B2CORE's issuer is its auth
        service — api.* with a path — and no client could authenticate."""
        issues = check_b2core_deployment(None)
        self.assertIn("portal.E005", ids(issues))
        self.assertIn("origin", str(issues[0]).lower())

    @override_settings(
        B2CORE_ORIGIN="https://portal.example.com",
        B2CORE_JWT_ISSUER="https://portal.example.com/",
    )
    def test_a_trailing_slash_does_not_disguise_the_origin(self):
        """Nor does its absence. The comparison is on the trimmed pair, because
        the mistake is the value, not its punctuation."""
        self.assertIn("portal.E005", ids(check_b2core_deployment(None)))

    @override_settings(B2CORE_JWT_AUDIENCE="maxpay-portal")
    def test_setting_an_audience_is_an_error(self):
        """Inverted from what this check used to say, and the inversion is the
        point. It warned that an unset audience was unverified and told the
        operator to set it "once B2CORE confirms the value". B2CORE mints no
        `aud` at all, so the old hint was an instruction to refuse every
        client — and clearing deploy warnings before go-live would have done
        exactly that."""
        issues = check_b2core_deployment(None)
        self.assertIn("portal.E006", ids(issues))
        self.assertIn("no 'aud'", str(issues[0]))

    @override_settings(B2CORE_JWT_AUDIENCE="")
    def test_an_empty_audience_is_the_correct_configuration(self):
        """Not an unfinished one. It must raise nothing at all."""
        self.assertNotIn("portal.E006", ids(check_b2core_deployment(None)))

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


class ProductionBootGuardTests(SimpleTestCase):
    """`config/settings/prod.py` refuses to start on a bad issuer.

    A deploy check reports the same two mistakes as `portal.E005`, and a check
    can be skipped: nothing forces `manage.py check --deploy` to run before a
    container starts. These two states are worth more than a report — one lets
    the wrong signer in, the other locks every client out — so production
    refuses to boot at all.

    Each case boots a real interpreter against `config.settings.prod`, because
    "it refuses to start" is a claim about starting and nothing short of
    starting tests it.
    """

    ISSUER = "https://api.maxifyfx.test/srvsz/auth/clients/v1/"
    ORIGIN = "https://portal.maxifyfx.test"

    def boot(self, *, issuer, origin=None):
        """Start Django under the production settings. Returns (code, stderr)."""
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "DJANGO_SECRET_KEY": "x" * 64,
            "DJANGO_ALLOWED_HOSTS": "maxpay.example.com",
            "DATABASE_URL": "sqlite:///smoke.sqlite3",
            "B2CORE_ORIGIN": origin if origin is not None else self.ORIGIN,
            "B2CORE_JWT_ISSUER": issuer,
            "B2CORE_JWT_AUDIENCE": "",
            "PYTHONIOENCODING": "utf-8",
        }
        finished = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=str(settings.BASE_DIR),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        return finished.returncode, (finished.stderr or "")

    def test_a_correct_issuer_boots(self):
        code, stderr = self.boot(issuer=self.ISSUER)

        self.assertEqual(code, 0, stderr)

    def test_an_empty_issuer_cannot_be_reached_from_the_environment(self):
        """Which is why booting with `B2CORE_JWT_ISSUER=""` succeeds.

        `env_str` treats an empty variable as absent and falls back, and the
        fallback is the literal issuer B2CORE mints. So the empty state the
        guard exists for is not something a deployment can produce by leaving a
        line out of `.env` — it would take editing the default in base.py back
        to `""`, which is what it used to be.

        The guard stays for that, and `portal.E005` covers the same state at
        deploy time — see `test_an_unverified_issuer_is_an_error`. What this
        pins is the reason it is unreachable, so that removing the default
        silently reopens it in front of a failing test rather than behind one.
        """
        code, stderr = self.boot(issuer="")
        self.assertEqual(code, 0, stderr)

        self.assertTrue(settings.B2CORE_JWT_ISSUER)
        self.assertTrue(settings.B2CORE_JWT_ISSUER.endswith("/"))
        self.assertNotEqual(
            settings.B2CORE_JWT_ISSUER.rstrip("/"),
            settings.B2CORE_ORIGIN.rstrip("/"),
        )

    def test_the_issuer_set_to_the_origin_refuses_to_boot(self):
        """The mistake most available to whoever wires this up."""
        code, stderr = self.boot(issuer=self.ORIGIN)

        self.assertNotEqual(code, 0)
        self.assertIn("ImproperlyConfigured", stderr)
        self.assertIn("origin", stderr)

    def test_the_comparison_ignores_a_trailing_slash(self):
        """`https://portal.example.com/` is the origin too. The mistake is the
        value, not its punctuation."""
        code, stderr = self.boot(issuer=self.ORIGIN + "/")

        self.assertNotEqual(code, 0)
        self.assertIn("origin", stderr)
