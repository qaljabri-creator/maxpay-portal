"""Client session handling (spec §4, steps 4–6 of the handshake).

A client is not a Django user — there is no password and no ``auth`` session.
What we keep is the verified subject and the moment its token expires, so a
session cannot outlive the authorisation that created it.

Everything here reads and writes ``request.portal_session``, the separate store
installed by :class:`apps.portal.sessions.PortalSessionMiddleware`. It never
touches ``request.session``: that one belongs to the internal panel, and the two
must not be able to reach each other. See ``apps/portal/sessions.py`` for why
they are separate cookies.
"""

import logging
import secrets

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import Client

from .b2core import Identity

logger = logging.getLogger("maxpay.b2core")

SESSION_CLIENT_ID = "b2core_client_id"
SESSION_SUBJECT = "b2core_sub"
SESSION_EXPIRES_AT = "b2core_exp"
SESSION_CSRF = "b2core_csrf"
SESSION_THEME = "b2core_theme"
SESSION_LANGUAGE = "b2core_language"

#: Fallback when ``PORTAL_SESSION_MAX_SECONDS`` is absent. A portal session is
#: never allowed to outlive this, even if the token says so.
MAX_SESSION_SECONDS = 60 * 60 * 8

ALLOWED_THEMES = {"light", "dark"}


def normalise_language(value) -> str | None:
    """``value`` reduced to a language this deployment actually serves.

    B2CORE may announce anything — ``en-GB``, ``fr``, a number. Anything we do
    not have translations for is dropped rather than stored, so nothing
    downstream has to re-check it.
    """
    if not isinstance(value, str):
        return None
    code = value.strip().split("-")[0].lower()[:8]
    supported = {code for code, _label in getattr(settings, "LANGUAGES", [])}
    return code if code in supported else None


def max_session_seconds() -> int:
    return int(getattr(settings, "PORTAL_SESSION_MAX_SECONDS", MAX_SESSION_SECONDS))


def store(request):
    """The portal session store, or ``None`` if the middleware is not installed."""
    return getattr(request, "portal_session", None)


def upsert_client(identity: Identity) -> Client:
    """Map a verified subject onto a local record, creating it on first use.

    Only fields carried by verified claims are written. A blank claim never
    overwrites something already stored, so a token that happens to omit the
    name does not wipe it.
    """
    client, created = Client.objects.get_or_create(
        b2core_id=identity.subject,
        defaults={
            "display_name": identity.display_name,
            "email": identity.email,
            "account_number": identity.account_number,
            "preferred_language": identity.language or "ar",
        },
    )

    updates = {}
    if identity.display_name and identity.display_name != client.display_name:
        updates["display_name"] = identity.display_name
    if identity.email and identity.email != client.email:
        updates["email"] = identity.email
    if identity.account_number and identity.account_number != client.account_number:
        updates["account_number"] = identity.account_number
    if identity.language and identity.language != client.preferred_language:
        updates["preferred_language"] = identity.language

    if updates:
        for attribute, value in updates.items():
            setattr(client, attribute, value)
        client.save(update_fields=[*updates, "updated_at"])

    if created:
        logger.info("Created client record for B2CORE subject %s", identity.subject)
    return client


def start(request, identity: Identity, client: Client | None = None) -> Client:
    """Establish a portal session for a verified identity.

    ``client`` may be passed by a caller that already resolved the record — the
    session endpoint does, so it can refuse a deactivated client before any
    session exists rather than after.
    """
    client = client or upsert_client(identity)
    session = store(request)
    if session is None:
        raise RuntimeError("PortalSessionMiddleware is not installed.")

    # Preferences are the only thing worth carrying across a re-authentication:
    # B2CORE announced them over postMessage, not in the token.
    theme = session.get(SESSION_THEME)
    language = session.get(SESSION_LANGUAGE)

    # Guard against session fixation: whoever held this session id before does
    # not get to keep it now that it carries an identity.
    session.cycle_key()

    now = int(timezone.now().timestamp())
    ceiling = now + max_session_seconds()
    expires_at = min(identity.expires_at or ceiling, ceiling)

    session[SESSION_CLIENT_ID] = client.pk
    session[SESSION_SUBJECT] = identity.subject
    session[SESSION_EXPIRES_AT] = expires_at
    # Handed to the embed in the response body and required back in a header on
    # every state-changing portal call. A cross-site attacker can send the
    # cookie but cannot read the response that carries this, so it is what
    # stands in for Django's CSRF token — whose own cookie is SameSite=Lax and
    # therefore never arrives inside the iframe.
    session[SESSION_CSRF] = secrets.token_urlsafe(32)
    if theme in ALLOWED_THEMES:
        session[SESSION_THEME] = theme
    language = language or normalise_language(identity.language)
    if language:
        session[SESSION_LANGUAGE] = language

    session.set_expiry(max(0, expires_at - now))

    client.touch()
    request.portal_client = client
    return client


def get_client(request):
    """The client this session belongs to, or ``None``.

    Returns ``None`` — and clears the session — once the underlying token would
    have expired, so the embedded page goes back and asks B2CORE for a new one.
    """
    session = store(request)
    if session is None:
        return None

    client_id = session.get(SESSION_CLIENT_ID)
    if not client_id:
        return None

    expires_at = session.get(SESSION_EXPIRES_AT)
    if expires_at and int(timezone.now().timestamp()) >= int(expires_at):
        end(request)
        return None

    client = Client.objects.filter(pk=client_id, is_active=True).first()
    if client is None:
        # Deactivated or deleted between requests.
        end(request)
        return None

    # The subject is stored alongside the id so a recycled primary key can never
    # silently hand one client another's session.
    if session.get(SESSION_SUBJECT) != client.b2core_id:
        end(request)
        return None

    return client


def current_client(request):
    """``request.portal_client``, resolved.

    The middleware attaches a :class:`~django.utils.functional.SimpleLazyObject`,
    and one wrapping ``None`` is emphatically not ``None``. Everything that
    wants to branch on "is there a client" goes through here.
    """
    client = getattr(request, "portal_client", None)
    return client or None


def end(request) -> None:
    """Clear the session and any cached token (spec §4, step 5)."""
    session = store(request)
    if session is not None:
        session.flush()
    request.portal_client = None


def csrf_token(request) -> str:
    session = store(request)
    return session.get(SESSION_CSRF, "") if session is not None else ""


def expires_at(request) -> int | None:
    session = store(request)
    return session.get(SESSION_EXPIRES_AT) if session is not None else None


def set_theme(request, theme: str) -> str | None:
    if theme not in ALLOWED_THEMES:
        return None
    session = store(request)
    if session is None:
        return None
    session[SESSION_THEME] = theme
    return theme


def get_theme(request) -> str:
    session = store(request)
    return session.get(SESSION_THEME, "light") if session is not None else "light"


def set_language(request, language) -> str | None:
    """Accept a language only if the deployment actually serves it."""
    code = normalise_language(language)
    if code is None:
        return None
    session = store(request)
    if session is None:
        return None
    session[SESSION_LANGUAGE] = code
    return code


def get_language(request) -> str:
    session = store(request)
    default = getattr(settings, "LANGUAGE_CODE", "ar")
    return session.get(SESSION_LANGUAGE, default) if session is not None else default
