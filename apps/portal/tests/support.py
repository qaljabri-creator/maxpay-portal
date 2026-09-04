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
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
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
