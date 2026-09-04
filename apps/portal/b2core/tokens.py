"""Verify a B2CORE JWT and turn it into an identity we are willing to trust.

Spec §4: the backend verifies the signature against B2CORE's JWKS endpoint on
every request, and client identity is derived from the verified token only —
never from anything the browser hands us alongside it.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import jwt
from django.conf import settings
from jwt.exceptions import InvalidTokenError

from .errors import B2CoreConfigurationError, B2CoreTokenError
from .jwks import get_signing_key

logger = logging.getLogger("maxpay.b2core")

#: Asymmetric only. Listing algorithms explicitly is what stops an attacker
#: swapping in ``alg: none`` or an HMAC signed with the public key.
#:
#: ``EdDSA`` is what B2CORE actually signs with — confirmed against a real token
#: on 4 Sep 2026, not from their documentation. It is asymmetric like the rest
#: of this list, so admitting it loosens nothing: the private key still never
#: leaves B2CORE, and the JWKS still publishes only the public half. PyJWT
#: registers it and reads the ``OKP``/``Ed25519`` JWK that carries it, both via
#: ``cryptography``, which is already a pinned dependency.
#:
#: The RSA and EC entries stay. They cost nothing, and a rotation to one of them
#: should not be an outage.
DEFAULT_ALGORITHMS = [
    "EdDSA",
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
]

#: A claim carrying the whole name, in preference order. B2CORE sends none of
#: these — the composition below is what actually runs — but a token that does
#: carry one has said the name better than we could assemble it.
FULL_NAME_CLAIMS = ("name", "full_name", "preferred_username")

#: The two halves, each with the OIDC spelling beside B2CORE's. B2CORE sends
#: ``first_name``/``last_name``; ``given_name``/``family_name`` are what the
#: standard calls the same things, and cost one tuple entry each to accept.
GIVEN_NAME_CLAIMS = ("first_name", "given_name")
FAMILY_NAME_CLAIMS = ("last_name", "family_name")


@dataclass(frozen=True)
class Identity:
    """A verified B2CORE subject.

    Only ``subject`` is authoritative. Everything else is a convenience copied
    from verified claims, and none of it is ever accepted from request input.
    """

    subject: str
    email: str = ""
    display_name: str = ""
    account_number: str = ""
    language: str = ""
    expires_at: int | None = None
    claims: dict[str, Any] = field(default_factory=dict, repr=False)


def _algorithms() -> list[str]:
    return list(getattr(settings, "B2CORE_JWT_ALGORITHMS", DEFAULT_ALGORITHMS))


def _first_claim(claims: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _display_name(claims: dict) -> str:
    """The client's name, however this token chose to say it.

    B2CORE sends ``first_name`` and ``last_name`` and no combined claim, so the
    name has to be assembled. Before this was written the lookup asked only for
    single-claim spellings, found none of them, and returned an empty string —
    which no test caught because nothing was *wrong*, only blank. Finance saw
    «···» where the client's name should be.

    A combined claim still wins when one is present: a name is not always
    "given then family", and a service that sends the whole thing has already
    made that decision in the right place. Only when there is no such claim are
    the halves joined, and either half alone is better than nothing.
    """
    whole = _first_claim(claims, FULL_NAME_CLAIMS)
    if whole:
        return whole
    parts = [
        _first_claim(claims, GIVEN_NAME_CLAIMS),
        _first_claim(claims, FAMILY_NAME_CLAIMS),
    ]
    return " ".join(part for part in parts if part)


def verify_token(token: str) -> Identity:
    """Verify ``token`` and return the identity it asserts.

    Raises :class:`B2CoreTokenError` for anything wrong with the token itself
    and :class:`B2CoreKeyError` for anything wrong with key resolution.
    """
    if not token or not isinstance(token, str):
        raise B2CoreTokenError("No token supplied.")

    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    # Bound the work before any parsing: a JWT this large is not a real one.
    max_length = getattr(settings, "B2CORE_MAX_TOKEN_BYTES", 8192)
    if len(token.encode("utf-8", "ignore")) > max_length:
        raise B2CoreTokenError("Token exceeds the maximum accepted size.")

    # Settle the algorithm from the header *before* going anywhere near the
    # JWKS. `jwt.decode` would reject a bad one anyway, but only after a key
    # lookup — and an `alg: none` token has no `kid` to look up, so the failure
    # would surface as a key error rather than what it is: a forged token.
    # Checking here also means garbage cannot provoke a JWKS fetch at all.
    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        logger.info("B2CORE token header unreadable: %s", exc)
        raise B2CoreTokenError("Token header could not be read.") from exc

    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in _algorithms():
        logger.info("B2CORE token rejected: unaccepted alg %r", algorithm)
        raise B2CoreTokenError(f"Unaccepted signing algorithm: {algorithm!r}.")

    signing_key = get_signing_key(token)

    audience = getattr(settings, "B2CORE_JWT_AUDIENCE", "") or None
    issuer = getattr(settings, "B2CORE_JWT_ISSUER", "") or None

    options = {
        "require": ["exp", "sub"],
        "verify_signature": True,
        "verify_exp": True,
        "verify_iat": True,
        "verify_aud": audience is not None,
        "verify_iss": issuer is not None,
    }

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=_algorithms(),
            audience=audience,
            issuer=issuer,
            leeway=getattr(settings, "B2CORE_JWT_LEEWAY_SECONDS", 30),
            options=options,
        )
    except InvalidTokenError as exc:
        # The reason is useful in the log and useless — sometimes harmful — to
        # the browser, which only needs to know to ask B2CORE for a fresh token.
        logger.info("B2CORE token rejected: %s", exc)
        raise B2CoreTokenError(f"Token verification failed: {exc}") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise B2CoreTokenError("Token has no usable subject claim.")

    # B2CORE's token carries no account number and no client type — checked
    # against a real one on 4 Sep 2026. The setting stays because the claim may
    # appear later and this is where it would be read, but it resolves to
    # nothing today and `Client.account_number` is blank for every client
    # authenticated since.
    #
    # It is deliberately *not* pointed at `sub` or `sid` to fill the column.
    # Both are real identifiers and neither is an account number; writing one
    # into a field Finance reads as "account number" would be worse than the
    # blank, because a blank is obviously absent and a wrong number is not.
    account_claim = getattr(settings, "B2CORE_ACCOUNT_CLAIM", "account_number")
    account_number = claims.get(account_claim)

    language = claims.get("locale") or claims.get("language") or ""
    if isinstance(language, str):
        language = language.split("-")[0].lower()[:8]
    else:
        language = ""

    return Identity(
        subject=subject.strip(),
        email=(claims.get("email") or "").strip() if isinstance(claims.get("email"), str) else "",
        display_name=_display_name(claims),
        account_number=str(account_number).strip() if account_number else "",
        language=language,
        expires_at=claims.get("exp"),
        claims=claims,
    )


def require_configuration() -> None:
    """Fail loudly when the B2CORE settings a deployment needs are absent."""
    missing = [
        name
        for name in ("B2CORE_JWKS_URL", "B2CORE_ORIGIN")
        if not getattr(settings, name, "")
    ]
    if missing:
        raise B2CoreConfigurationError(
            f"B2CORE integration is not configured: {', '.join(missing)} unset."
        )
