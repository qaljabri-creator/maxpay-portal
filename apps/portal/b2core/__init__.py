"""The B2CORE integration boundary.

Everything that trusts, parses or fetches something from B2CORE lives here, so
the rest of the portal only ever sees an already-verified :class:`Identity`.
"""

from .errors import (
    B2CoreAuthError,
    B2CoreConfigurationError,
    B2CoreKeyError,
    B2CoreTokenError,
)
from .tokens import Identity, verify_token

__all__ = [
    "B2CoreAuthError",
    "B2CoreConfigurationError",
    "B2CoreKeyError",
    "B2CoreTokenError",
    "Identity",
    "verify_token",
]
