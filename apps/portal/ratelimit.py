"""A small fixed-window limiter for the unauthenticated portal endpoints.

Spec §11 asks for rate limiting on the submission endpoints. The session
endpoint is not one of those, but it is unauthenticated and does public-key
cryptography plus — for an unknown ``kid`` — an outbound JWKS fetch, so it is
worth the same treatment before the client flow arrives.

Deliberately coarse: a fixed window in the cache, keyed by client IP. It is a
cost ceiling, not a security control, and it must never be the only thing
standing between a caller and something expensive.
"""

import logging
import re

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("maxpay.b2core")

_RATE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)\s*$")
_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_rate(rate: str) -> tuple[int, int] | None:
    """``"30/minute"`` → ``(30, 60)``. ``None`` disables the limit."""
    if not rate:
        return None
    match = _RATE.match(rate)
    if not match:
        logger.warning("Unparseable rate %r; the limit is disabled.", rate)
        return None
    return int(match.group(1)), _PERIODS[match.group(2)]


def allow(request, *, scope: str, rate: str | None = None) -> bool:
    """Consume one unit of ``scope``'s budget for this caller.

    Returns ``False`` once the window's allowance is spent. Fails *open* on a
    cache backend error: the limiter going down must not take client
    authentication down with it.
    """
    from apps.core.services import client_ip

    configured = rate if rate is not None else getattr(settings, "PORTAL_SESSION_RATE", "")
    parsed = parse_rate(configured)
    if parsed is None:
        return True
    limit, period = parsed

    identity = client_ip(request) or "unknown"
    key = f"portal:rl:{scope}:{identity}"

    try:
        # add() only succeeds on the first call of a window, which is what
        # gives the counter its expiry without a second round trip.
        if cache.add(key, 1, period):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            # The entry expired between add() and incr().
            cache.add(key, 1, period)
            return True
    except Exception as exc:  # noqa: BLE001 — a broken cache must not lock clients out
        logger.warning("Rate limiting unavailable (%s); allowing the request.", exc)
        return True

    if count > limit:
        logger.info("Rate limit hit: scope=%s ip=%s count=%s", scope, identity, count)
        return False
    return True
