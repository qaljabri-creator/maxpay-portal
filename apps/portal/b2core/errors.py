"""Failure modes of the B2CORE handshake.

Kept distinct so the view can tell "your token expired, ask B2CORE for a new
one" apart from "this deployment is misconfigured" — the first is routine and
recoverable in the browser, the second needs an operator.
"""


class B2CoreAuthError(Exception):
    """Base class. The message is safe to log, never to show a client verbatim."""

    #: What the embedded page should do about it.
    remedy = "retry"


class B2CoreConfigurationError(B2CoreAuthError):
    """The deployment is missing B2CORE settings. Not the client's problem."""

    remedy = "contact_support"


class B2CoreKeyError(B2CoreAuthError):
    """The signing key could not be fetched or matched against the JWKS."""

    remedy = "retry"


class B2CoreTokenError(B2CoreAuthError):
    """The token is absent, malformed, expired, or fails verification."""

    remedy = "reauthenticate"
