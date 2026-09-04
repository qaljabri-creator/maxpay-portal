"""Token verification (spec §4, step 3).

This is the whole of the client's authentication. Everything downstream — the
session, the client record, every request they ever submit — rests on the
answers here being right, so the failure cases are tested at least as carefully
as the success one.

Each test signs a real token with a real key and puts a real JWKS in front of
PyJWT; only the HTTP fetch is stubbed. See ``support.py``.
"""

import time

import jwt
from django.test import SimpleTestCase, override_settings

from apps.portal.b2core import (
    B2CoreConfigurationError,
    B2CoreKeyError,
    B2CoreTokenError,
    verify_token,
)
from apps.portal.b2core.tokens import require_configuration

from .support import (
    AUDIENCE,
    B2CORE_SETTINGS,
    ISSUER,
    StubbedJWKS,
    hmac_token_signed_with_the_public_key,
    jwks_document,
    key,
    make_token,
    unsigned_token,
)


@override_settings(**B2CORE_SETTINGS)
class TokenVerificationTests(SimpleTestCase):
    """The verification matrix. No database is touched."""

    def setUp(self):
        self.jwks = StubbedJWKS(jwks_document("primary")).start()
        self.addCleanup(self.jwks.stop)

    # -- the happy path ----------------------------------------------------

    def test_a_valid_token_yields_the_identity_it_asserts(self):
        identity = verify_token(make_token())

        self.assertEqual(identity.subject, "b2core-subject-77")
        self.assertEqual(identity.email, "client@example.com")
        self.assertEqual(identity.display_name, "زينب الجبوري")
        self.assertEqual(identity.account_number, "MX-90210")
        # `ar-IQ` is a locale; what the portal renders in is a language.
        self.assertEqual(identity.language, "ar")
        self.assertIsNotNone(identity.expires_at)

    def test_an_es256_token_is_accepted(self):
        self.jwks.publish(jwks_document("ec"))
        identity = verify_token(make_token(signing_key="ec"))
        self.assertEqual(identity.subject, "b2core-subject-77")

    def test_a_bearer_prefix_is_tolerated(self):
        identity = verify_token(f"Bearer  {make_token()}  ")
        self.assertEqual(identity.subject, "b2core-subject-77")

    def test_only_verified_claims_reach_the_identity(self):
        """Spec §4 — never trust a client-supplied account number."""
        identity = verify_token(make_token(account_number="MX-00001"))
        self.assertEqual(identity.account_number, "MX-00001")
        self.assertEqual(identity.claims["sub"], identity.subject)

    # -- signature ---------------------------------------------------------

    def test_a_token_signed_by_another_key_is_rejected(self):
        """The forger's key carries the *published* kid, so the lookup succeeds
        and only the signature check stands between them and a session."""
        forged = make_token(signing_key="attacker")
        self.assertEqual(jwt.get_unverified_header(forged)["kid"], key().kid)

        with self.assertRaises(B2CoreTokenError):
            verify_token(forged)

    def test_a_tampered_payload_is_rejected(self):
        header, payload, signature = make_token().split(".")
        tampered = ".".join([header, jwt.utils.base64url_encode(
            b'{"sub":"somebody-else","exp":99999999999}'
        ).decode(), signature])

        with self.assertRaises(B2CoreTokenError):
            verify_token(tampered)

    # -- expiry ------------------------------------------------------------

    def test_an_expired_token_is_rejected(self):
        now = int(time.time())
        with self.assertRaises(B2CoreTokenError) as caught:
            verify_token(make_token(iat=now - 7200, exp=now - 3600))
        self.assertIn("expired", str(caught.exception).lower())

    def test_a_token_expiring_within_the_leeway_is_still_accepted(self):
        """Clock skew between B2CORE and us must not log a client out."""
        now = int(time.time())
        identity = verify_token(make_token(exp=now - 10))
        self.assertEqual(identity.subject, "b2core-subject-77")

    def test_a_token_expiring_beyond_the_leeway_is_rejected(self):
        now = int(time.time())
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(exp=now - 120))

    def test_a_token_without_an_exp_claim_is_rejected(self):
        """A token that never expires would give a session that never ends."""
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(exp=None))

    # -- issuer and audience ------------------------------------------------

    def test_a_token_from_another_issuer_is_rejected(self):
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(iss="https://api.evil.test"))

    def test_a_token_for_another_audience_is_rejected(self):
        """A token B2CORE minted for a different relying party is not ours."""
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(aud="some-other-service"))

    def test_the_right_audience_among_several_is_accepted(self):
        identity = verify_token(make_token(aud=["some-other-service", AUDIENCE]))
        self.assertEqual(identity.subject, "b2core-subject-77")

    @override_settings(B2CORE_JWT_ISSUER="", B2CORE_JWT_AUDIENCE="")
    def test_issuer_and_audience_are_skipped_only_when_unconfigured(self):
        """Documents the deliberate escape hatch the system check warns about."""
        identity = verify_token(make_token(iss="https://api.elsewhere.test", aud="other"))
        self.assertEqual(identity.subject, "b2core-subject-77")

    # -- algorithm ---------------------------------------------------------

    def test_alg_none_is_rejected(self):
        with self.assertRaises(B2CoreTokenError) as caught:
            verify_token(unsigned_token())
        self.assertIn("algorithm", str(caught.exception).lower())

    def test_alg_none_never_reaches_the_jwks(self):
        """An unsigned token has no key to look up; it must not provoke a fetch."""
        with self.assertRaises(B2CoreTokenError):
            verify_token(unsigned_token())
        self.assertEqual(self.jwks.fetches, 0)

    def test_an_hmac_token_signed_with_the_published_public_key_is_rejected(self):
        """Algorithm confusion: the JWKS public key used as an HMAC secret."""
        with self.assertRaises(B2CoreTokenError) as caught:
            verify_token(hmac_token_signed_with_the_public_key())
        self.assertIn("algorithm", str(caught.exception).lower())
        self.assertEqual(self.jwks.fetches, 0)

    @override_settings(B2CORE_JWT_ALGORITHMS=["RS512"])
    def test_an_algorithm_outside_the_configured_list_is_rejected(self):
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token())  # RS256

    # -- key resolution ----------------------------------------------------

    def test_an_unknown_kid_is_rejected(self):
        with self.assertRaises(B2CoreKeyError):
            verify_token(make_token(headers={"kid": "a-key-nobody-published"}))

    def test_a_token_with_no_kid_at_all_is_rejected(self):
        token = jwt.encode(
            {"sub": "x", "exp": int(time.time()) + 60, "iss": ISSUER, "aud": AUDIENCE},
            key().private_key,
            algorithm="RS256",
        )
        with self.assertRaises(B2CoreKeyError):
            verify_token(token)

    def test_an_unknown_kid_forces_one_refresh_and_is_then_throttled(self):
        """Spec §11 in spirit: an unauthenticated caller must not be able to
        turn a stream of random kids into a stream of requests to B2CORE."""
        with self.assertRaises(B2CoreKeyError):
            verify_token(make_token(headers={"kid": "unknown-1"}))
        after_first = self.jwks.fetches
        self.assertGreaterEqual(after_first, 2, "PyJWT's own refresh should have run")

        with self.assertRaises(B2CoreKeyError):
            verify_token(make_token(headers={"kid": "unknown-2"}))

        # PyJWT still refreshes once; ours is suppressed. Without the throttle
        # this second miss would have cost two fetches, not one.
        self.assertEqual(self.jwks.fetches - after_first, 1)

    def test_a_rotated_key_is_picked_up(self):
        rotated = make_token(signing_key="rotated")
        with self.assertRaises(B2CoreKeyError):
            verify_token(rotated)

        self.jwks.publish(jwks_document("primary", "rotated"))
        # The throttle only governs *forced* refreshes; PyJWT's own refresh on
        # an unknown kid still runs, which is what makes rotation seamless.
        self.assertEqual(verify_token(rotated).subject, "b2core-subject-77")

    def test_an_unreachable_jwks_endpoint_is_a_key_error_not_a_token_error(self):
        """The distinction matters: one asks the client to retry, the other
        sends them back to B2CORE for a new token."""
        self.jwks.stop()
        broken = StubbedJWKS(error=TimeoutError("connection timed out")).start()
        self.addCleanup(broken.stop)

        with self.assertRaises(B2CoreKeyError):
            verify_token(make_token())

    # -- shape and subject --------------------------------------------------

    def test_a_token_without_a_sub_claim_is_rejected(self):
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(sub=None))

    def test_a_blank_subject_is_rejected(self):
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(sub="   "))

    def test_a_non_string_subject_is_rejected(self):
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token(sub=12345))

    def test_an_empty_or_non_string_token_is_rejected(self):
        for candidate in ("", "   ", None, 12345, b"not-a-string", {"token": "x"}):
            with self.subTest(candidate=candidate), self.assertRaises(B2CoreTokenError):
                verify_token(candidate)

    def test_a_malformed_token_is_rejected(self):
        for candidate in ("not.a.jwt", "onlyonesegment", "a.b", "a.b.c.d"):
            with self.subTest(candidate=candidate):
                with self.assertRaises((B2CoreTokenError, B2CoreKeyError)):
                    verify_token(candidate)
        self.assertEqual(self.jwks.fetches, 0)

    def test_an_oversized_token_is_rejected_before_anything_parses_it(self):
        with self.assertRaises(B2CoreTokenError) as caught:
            verify_token("A" * 9000)
        self.assertIn("maximum", str(caught.exception).lower())
        self.assertEqual(self.jwks.fetches, 0)

    @override_settings(B2CORE_MAX_TOKEN_BYTES=64)
    def test_the_size_ceiling_is_configurable(self):
        with self.assertRaises(B2CoreTokenError):
            verify_token(make_token())

    # -- claim mapping ------------------------------------------------------

    def test_the_display_name_falls_back_through_the_claim_list(self):
        identity = verify_token(make_token(name=None, given_name="سارة"))
        self.assertEqual(identity.display_name, "سارة")

    def test_a_missing_optional_claim_is_simply_absent(self):
        identity = verify_token(
            make_token(name=None, email=None, account_number=None, locale=None)
        )
        self.assertEqual(identity.display_name, "")
        self.assertEqual(identity.email, "")
        self.assertEqual(identity.account_number, "")
        self.assertEqual(identity.language, "")

    def test_a_non_string_email_claim_is_dropped_rather_than_coerced(self):
        identity = verify_token(make_token(email=42))
        self.assertEqual(identity.email, "")

    @override_settings(B2CORE_ACCOUNT_CLAIM="trading_account")
    def test_the_account_claim_name_is_configurable(self):
        identity = verify_token(make_token(trading_account="TA-5"))
        self.assertEqual(identity.account_number, "TA-5")

    def test_the_identity_repr_does_not_leak_the_claim_set(self):
        """Identities end up in log lines; the raw claims should not."""
        identity = verify_token(make_token())
        self.assertNotIn("claims", repr(identity))


@override_settings(**B2CORE_SETTINGS)
class ConfigurationTests(SimpleTestCase):
    @override_settings(B2CORE_JWKS_URL="")
    def test_verification_without_a_jwks_url_is_a_configuration_error(self):
        with StubbedJWKS():
            with self.assertRaises(B2CoreConfigurationError):
                verify_token(make_token())

    @override_settings(B2CORE_JWKS_URL="", B2CORE_ORIGIN="")
    def test_require_configuration_names_everything_missing(self):
        with self.assertRaises(B2CoreConfigurationError) as caught:
            require_configuration()
        message = str(caught.exception)
        self.assertIn("B2CORE_JWKS_URL", message)
        self.assertIn("B2CORE_ORIGIN", message)

    def test_require_configuration_passes_when_configured(self):
        require_configuration()  # must not raise

    def test_a_configuration_error_is_not_mistaken_for_a_token_error(self):
        """The remedies differ: one is the client's to act on, one is not."""
        self.assertEqual(B2CoreTokenError.remedy, "reauthenticate")
        self.assertEqual(B2CoreKeyError.remedy, "retry")
        self.assertEqual(B2CoreConfigurationError.remedy, "contact_support")
