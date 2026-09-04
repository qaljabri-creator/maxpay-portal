"""A stand-in for B2CORE: real keys, real signatures, no network.

The point of these tests is the verification path, so nothing about it is
faked — tokens are signed with genuine RSA and EC keys and checked against a
genuine JWKS through PyJWT's own client. The only thing replaced is the HTTP
fetch, so the suite neither reaches the internet nor depends on B2CORE being up.

Key generation is slow enough to matter, so the keys are built once per process
and shared. Nothing here mutates them.
"""

import json
import time
from functools import lru_cache
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm
from jwt.jwks_client import PyJWKClient
from jwt.utils import base64url_encode

from apps.portal.b2core import jwks

JWKS_URL = "https://api.b2core.test/.well-known/jwks.json"
ISSUER = "https://api.b2core.test"
AUDIENCE = "maxpay-portal"
ORIGIN = "https://portal.b2core.test"

#: Settings every test in this suite runs under, so each case only has to state
#: what it is actually varying.
B2CORE_SETTINGS = {
    "B2CORE_JWKS_URL": JWKS_URL,
    "B2CORE_ORIGIN": ORIGIN,
    "B2CORE_JWT_ISSUER": ISSUER,
    "B2CORE_JWT_AUDIENCE": AUDIENCE,
    "B2CORE_JWT_LEEWAY_SECONDS": 30,
}


class KeyPair:
    """One signing key, plus the JWK a JWKS endpoint would publish for it."""

    def __init__(self, kid: str, private_key, algorithm: str):
        self.kid = kid
        self.private_key = private_key
        self.algorithm = algorithm

    def jwk(self, **overrides) -> dict:
        if self.algorithm == "EdDSA":
            # OKPAlgorithm has no as_dict, so this one comes back as JSON.
            data = json.loads(OKPAlgorithm.to_jwk(self.private_key.public_key()))
        else:
            serializer = ECAlgorithm if self.algorithm.startswith("ES") else RSAAlgorithm
            data = serializer.to_jwk(self.private_key.public_key(), as_dict=True)
        data.update({"kid": self.kid, "use": "sig", "alg": self.algorithm})
        data.update(overrides)
        return data

    def public_pem(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )


@lru_cache(maxsize=1)
def _keys() -> dict[str, KeyPair]:
    return {
        # The key B2CORE signs with.
        "primary": KeyPair("b2core-key-1", rsa.generate_private_key(public_exponent=65537, key_size=2048), "RS256"),
        # A second RSA key, published only after "rotation".
        "rotated": KeyPair("b2core-key-2", rsa.generate_private_key(public_exponent=65537, key_size=2048), "RS256"),
        # Never published. Anything signed with it is, by construction, forged.
        "attacker": KeyPair("b2core-key-1", rsa.generate_private_key(public_exponent=65537, key_size=2048), "RS256"),
        "ec": KeyPair("b2core-ec-1", ec.generate_private_key(ec.SECP256R1()), "ES256"),
        # What B2CORE actually signs with — see B2CoreShapedTokenTests.
        "ed25519": KeyPair("b2core-ed25519-1", ed25519.Ed25519PrivateKey.generate(), "EdDSA"),
    }


def key(name: str = "primary") -> KeyPair:
    return _keys()[name]


def jwks_document(*names: str) -> dict:
    return {"keys": [key(name).jwk() for name in (names or ("primary",))]}


def claims(**overrides) -> dict:
    """A token body shaped the way B2CORE's documented one is."""
    now = int(time.time())
    payload = {
        "sub": "b2core-subject-77",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 900,
        "email": "client@example.com",
        "name": "زينب الجبوري",
        "account_number": "MX-90210",
        "locale": "ar-IQ",
    }
    payload.update(overrides)
    # An explicit None removes a claim, which is how the "missing exp" and
    # "missing sub" cases are expressed.
    return {name: value for name, value in payload.items() if value is not None}


def make_token(*, signing_key: str = "primary", headers: dict | None = None, **overrides) -> str:
    """Sign a token with one of the fixture keys."""
    pair = key(signing_key)
    head = {"kid": pair.kid}
    head.update(headers or {})
    return jwt.encode(
        claims(**overrides),
        pair.private_key,
        algorithm=head.pop("alg", pair.algorithm),
        headers=head,
    )


def unsigned_token(**overrides) -> str:
    """An ``alg: none`` token — the classic "verify nothing" forgery."""
    return jwt.encode(claims(**overrides), key=None, algorithm="none")


def hmac_token_signed_with_the_public_key(**overrides) -> str:
    """The algorithm-confusion forgery.

    The JWKS publishes B2CORE's *public* key. If HS256 were accepted, anyone who
    fetched that key could use it as an HMAC secret and mint tokens the server
    would verify against the very same bytes it just downloaded.

    Assembled by hand: PyJWT refuses to *encode* one of these, which is its own
    guard and not the one under test. An attacker has no such scruples.
    """
    import hashlib
    import hmac

    secret = key().public_pem()
    header = {"alg": "HS256", "typ": "JWT", "kid": key().kid}
    segments = [
        base64url_encode(json.dumps(header, separators=(",", ":")).encode()),
        base64url_encode(json.dumps(claims(**overrides), separators=(",", ":")).encode()),
    ]
    signing_input = b".".join(segments)
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return b".".join([signing_input, base64url_encode(signature)]).decode()


class StubbedJWKS:
    """Replaces the JWKS HTTP fetch and counts how often it would have run.

    Used as a context manager or started manually. The published document can be
    swapped mid-test, which is how key rotation is exercised.
    """

    def __init__(self, document: dict | None = None, error: Exception | None = None):
        self.document = document if document is not None else jwks_document("primary")
        self.error = error
        self.fetches = 0
        self._patcher = None

    def _fetch(self, client_self):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        if client_self.jwk_set_cache is not None:
            client_self.jwk_set_cache.put(self.document)
        return self.document

    def publish(self, document: dict) -> None:
        """Change what the endpoint serves, as a key rotation would."""
        self.document = document

    def start(self):
        jwks.reset_cache()
        self._patcher = mock.patch.object(PyJWKClient, "fetch_data", autospec=True, side_effect=self._fetch)
        self._patcher.start()
        return self

    def stop(self):
        if self._patcher is not None:
            self._patcher.stop()
            self._patcher = None
        jwks.reset_cache()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()
        return False


def decode_body(response) -> dict:
    return json.loads(response.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# The real B2CORE, as observed
# ---------------------------------------------------------------------------
#
# Everything above this line predates ever seeing a token B2CORE actually
# minted. It models a service that signs RS256, sends an `aud`, gives a
# combined `name` and carries an account number — and every one of those is
# wrong. The fixtures stay, because they exercise the verification path against
# algorithms and shapes we still accept, and because a rotation to an RSA key
# should not be an outage.
#
# What follows is the real thing, read off a live token on 4 Sep 2026. Tests
# that care whether a *client* can sign in use these.

#: The issuer B2CORE mints, character for character. The trailing slash is part
#: of it, and it is on `api.` with a path — not the portal origin.
REAL_ISSUER = "https://api.maxifyfx.test/srvsz/auth/clients/v1/"
REAL_ORIGIN = "https://portal.maxifyfx.test"

#: Settings a real client actually authenticates under. Note the audience: the
#: empty string is the correct value, not an unfinished one.
REAL_B2CORE_SETTINGS = {
    "B2CORE_JWKS_URL": JWKS_URL,
    "B2CORE_ORIGIN": REAL_ORIGIN,
    "B2CORE_JWT_ISSUER": REAL_ISSUER,
    "B2CORE_JWT_AUDIENCE": "",
    "B2CORE_JWT_LEEWAY_SECONDS": 30,
}


def real_claims(**overrides) -> dict:
    """A token body carrying exactly the claims B2CORE sends, and no others.

    No `aud`, no account number, no client type, no combined `name`, no
    `locale`. The absences are the point of the fixture: each one was a
    separate way the integration failed, and adding a convenience claim here
    would put the suite back to testing a service that does not exist.
    """
    now = int(time.time())
    payload = {
        "sub": "0193c4f2-8a1e-7b3c-9d45-6e7f80112233",
        "iss": REAL_ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,          # sixty minutes, which is what B2CORE gives
        "email": "client@example.com",
        "first_name": "زينب",
        "last_name": "الجبوري",
        "aal": "aal1",
        "amr": ["pwd"],
        "sid": "6f1c2d3e4f5a6b7c",
        "jti": "01JHQ2Z9K7XW3M8N5P6Q7R8S9T",
    }
    payload.update(overrides)
    return {name: value for name, value in payload.items() if value is not None}


def real_token(*, signing_key: str = "ed25519", headers: dict | None = None, **overrides) -> str:
    """Sign a realistically-shaped token with the Ed25519 fixture key."""
    pair = key(signing_key)
    head = {"kid": pair.kid}
    head.update(headers or {})
    return jwt.encode(
        real_claims(**overrides),
        pair.private_key,
        algorithm=head.pop("alg", pair.algorithm),
        headers=head,
    )
