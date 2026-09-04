"""Signing-key lookup against B2CORE's JWKS endpoint (spec §4).

Wraps :class:`jwt.PyJWKClient`, which already caches the key set and refetches
when it meets an unknown ``kid`` — the behaviour key rotation needs. What it
does not do is bound how often that refetch happens, and the ``kid`` comes
straight off an unauthenticated token: anyone who can reach the session endpoint
could otherwise make us hammer B2CORE by sending a stream of random ``kid``
values. :func:`get_signing_key` puts a floor on the interval between forced
refreshes and fails closed in between.
"""

import logging
import threading
import time
from datetime import UTC, datetime

from django.conf import settings
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError

from .errors import B2CoreConfigurationError, B2CoreKeyError

logger = logging.getLogger("maxpay.b2core")

#: Never force two JWKS refetches closer together than this, in seconds.
MIN_REFRESH_INTERVAL = 60

_lock = threading.Lock()
_client: PyJWKClient | None = None
_client_uri: str | None = None
#: ``None`` until a forced refresh has actually happened. A 0.0 sentinel would
#: instead be compared against ``time.monotonic()``, whose zero point is
#: arbitrary — on a freshly booted host that reads as "refreshed just now".
_last_forced_refresh: float | None = None

# --- how it is actually going, for the Finance panel's integration screen ---
#
# These are ``time.time()`` and the one above is ``time.monotonic()``, and the
# split is not an oversight. Monotonic is right for the throttle: it cannot be
# dragged backwards by an NTP correction, so a clock adjustment can never open
# the refetch gate early. It is wrong for a screen, because its zero point is
# arbitrary and it cannot be turned into a date. Throttling and reporting want
# different clocks, so they get different clocks.
#
# Process-local, like the client they describe. Under several workers each
# holds its own view of the integration, and the screen says so out loud — a
# shared store is a great deal of machinery to put behind a diagnostic, and a
# figure presented as global while quietly being per-worker is worse than one
# that is labelled honestly.
_last_success_at: float | None = None
_last_failure_at: float | None = None
_last_failure_reason: str = ""
_last_refresh_at: float | None = None


def _jwks_url() -> str:
    url = getattr(settings, "B2CORE_JWKS_URL", "")
    if not url:
        raise B2CoreConfigurationError(
            "B2CORE_JWKS_URL is not set; client authentication cannot work."
        )
    return url


def get_client() -> PyJWKClient:
    """The process-wide JWKS client, rebuilt if the configured URL changes."""
    global _client, _client_uri

    url = _jwks_url()
    with _lock:
        if _client is None or _client_uri != url:
            _client = PyJWKClient(
                url,
                cache_keys=True,
                cache_jwk_set=True,
                lifespan=getattr(settings, "B2CORE_JWKS_CACHE_SECONDS", 600),
                timeout=getattr(settings, "B2CORE_JWKS_TIMEOUT_SECONDS", 5),
            )
            _client_uri = url
        return _client


def _record_success() -> None:
    global _last_success_at
    with _lock:
        _last_success_at = time.time()


def _record_failure(reason: str) -> None:
    global _last_failure_at, _last_failure_reason
    with _lock:
        _last_failure_at = time.time()
        # Truncated: this is rendered on a page, and the tail of a long
        # exception repr is never the part that says what went wrong.
        _last_failure_reason = str(reason)[:300]


def _record_refresh() -> None:
    global _last_refresh_at
    with _lock:
        _last_refresh_at = time.time()


def get_signing_key(token: str):
    """Resolve the key that signed ``token``.

    Raises :class:`B2CoreKeyError` rather than letting a PyJWT-specific
    exception escape the integration boundary.
    """
    global _last_forced_refresh

    client = get_client()
    try:
        key = client.get_signing_key_from_jwt(token)
    except PyJWKClientError as exc:
        # PyJWT already tried one refresh. Retrying is only worth it — and only
        # safe — if we have not refetched very recently.
        now = time.monotonic()
        with _lock:
            too_soon = (
                _last_forced_refresh is not None
                and (now - _last_forced_refresh) < MIN_REFRESH_INTERVAL
            )
            if not too_soon:
                _last_forced_refresh = now
        if too_soon:
            logger.warning("JWKS refresh suppressed (throttled): %s", exc)
            _record_failure(f"throttled: {exc}")
            raise B2CoreKeyError("Signing key not found in the cached JWKS.") from exc

        try:
            client.get_jwk_set(refresh=True)
            _record_refresh()
            key = client.get_signing_key_from_jwt(token)
        except Exception as retry_exc:  # noqa: BLE001 — boundary: normalise everything
            logger.warning("JWKS lookup failed after refresh: %s", retry_exc)
            _record_failure(str(retry_exc))
            raise B2CoreKeyError("Signing key could not be resolved.") from retry_exc
        _record_success()
        return key
    except Exception as exc:  # noqa: BLE001 — network, TLS, malformed JWKS
        logger.warning("JWKS fetch failed: %s", exc)
        _record_failure(str(exc))
        raise B2CoreKeyError("The JWKS endpoint could not be reached.") from exc
    _record_success()
    return key


def _as_datetime(value):
    return datetime.fromtimestamp(value, tz=UTC) if value else None


def status() -> dict:
    """What this worker has observed of the JWKS endpoint. Read-only.

    Deliberately passive: it reports what ordinary traffic has already found
    out and never reaches for the network itself. A status page that probes on
    every load is a status page that can be refreshed until it takes the
    endpoint down, and it would report on its own probe rather than on the path
    clients actually take.

    ``last_success_at`` is the last time a signing key was *resolved*, which is
    not quite the last time the key set was fetched: PyJWT serves a cached set
    for ``B2CORE_JWKS_CACHE_SECONDS``, so a success may have touched no network
    at all. That is the honest reading and the screen states it in those words
    — resolved, not fetched. ``last_refresh_at`` is the narrower fact: a
    refetch this module forced after meeting an unknown ``kid``.
    """
    with _lock:
        success_at = _last_success_at
        failure_at = _last_failure_at
        failure_reason = _last_failure_reason
        refresh_at = _last_refresh_at

    if success_at is None and failure_at is None:
        state = "unknown"
    elif failure_at is None or (success_at is not None and success_at >= failure_at):
        state = "ok"
    else:
        state = "failing"

    return {
        "state": state,
        "last_success_at": _as_datetime(success_at),
        "last_failure_at": _as_datetime(failure_at),
        "last_failure_reason": failure_reason,
        "last_refresh_at": _as_datetime(refresh_at),
    }


def reset_cache() -> None:
    """Drop the cached client and everything observed through it.

    For tests and for a configuration reload. The observations go with the
    client because they describe *that* client talking to *that* URL: a success
    still showing against an endpoint this process no longer points at is
    exactly the reassuring lie a status screen must not tell.
    """
    global _client, _client_uri, _last_forced_refresh
    global _last_success_at, _last_failure_at, _last_failure_reason, _last_refresh_at
    with _lock:
        _client = None
        _client_uri = None
        _last_forced_refresh = None
        _last_success_at = None
        _last_failure_at = None
        _last_failure_reason = ""
        _last_refresh_at = None
