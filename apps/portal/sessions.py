"""A second, independent session cookie for the embedded client portal.

The internal panel and the client portal are two different trust domains that
happen to share a hostname, and they must not share a session cookie.

The internal panel's cookie stays ``SameSite=Lax``, which is a large part of
what protects it from cross-site request forgery. The client portal, however,
runs inside a B2CORE iframe: to a browser that is a third-party context, and a
``Lax`` cookie is simply never sent there. The portal therefore needs
``SameSite=None; Secure`` — a weaker cookie, and precisely the reason it has to
be a *separate* cookie rather than a relaxation of the existing one.

Rather than run Django's :class:`~django.contrib.sessions.middleware.SessionMiddleware`
twice — which would have the two instances fighting over ``request.session`` and
over which cookie to write on the way out — this middleware manages its own
store on ``request.portal_session``. ``request.session`` is left entirely alone.
"""

import logging
import time
from importlib import import_module

from django.conf import settings
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date

logger = logging.getLogger("maxpay.b2core")


def cookie_name() -> str:
    return getattr(settings, "PORTAL_SESSION_COOKIE_NAME", "maxpay_embed_sid")


def cookie_path() -> str:
    return getattr(settings, "PORTAL_SESSION_COOKIE_PATH", "/portal/")


def new_store(session_key: str | None = None):
    """A session store of the configured engine, unattached to any request."""
    engine = import_module(settings.SESSION_ENGINE)
    return engine.SessionStore(session_key)


class PortalSessionMiddleware:
    """Loads and persists ``request.portal_session``.

    A near-copy of Django's own session middleware, differing in three ways: it
    writes a different cookie, it reads its cookie attributes from the
    ``PORTAL_SESSION_COOKIE_*`` settings, and it never touches
    ``request.session``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.portal_session = new_store(request.COOKIES.get(cookie_name()))
        response = self.get_response(request)
        return self._persist(request, response)

    # -- response phase ----------------------------------------------------

    def _persist(self, request, response):
        session = getattr(request, "portal_session", None)
        if session is None:
            return response

        name = cookie_name()
        accessed = session.accessed
        modified = session.modified
        empty = session.is_empty()

        # A session that was emptied — logout, or expiry — takes its cookie
        # with it, otherwise the browser keeps presenting a dead key forever.
        if name in request.COOKIES and empty:
            response.delete_cookie(
                name,
                path=cookie_path(),
                domain=getattr(settings, "PORTAL_SESSION_COOKIE_DOMAIN", None),
                samesite=getattr(settings, "PORTAL_SESSION_COOKIE_SAMESITE", "None"),
            )
            patch_vary_headers(response, ("Cookie",))
            return response

        if accessed:
            patch_vary_headers(response, ("Cookie",))

        if not (modified or getattr(settings, "SESSION_SAVE_EVERY_REQUEST", False)):
            return response
        if empty:
            return response
        # Never persist a session created while the response was failing: the
        # client has no way to know whether the write landed.
        if response.status_code >= 500:
            return response

        if session.get_expire_at_browser_close():
            max_age, expires = None, None
        else:
            max_age = session.get_expiry_age()
            expires = http_date(time.time() + max_age)

        try:
            session.save()
        except UpdateError as exc:
            # The row went away underneath us — a concurrent logout, or a
            # cleared session store. Nothing here can recover it.
            raise SessionInterrupted(
                "The portal session could not be saved; it was deleted concurrently."
            ) from exc

        response.set_cookie(
            name,
            session.session_key,
            max_age=max_age,
            expires=expires,
            domain=getattr(settings, "PORTAL_SESSION_COOKIE_DOMAIN", None),
            path=cookie_path(),
            secure=getattr(settings, "PORTAL_SESSION_COOKIE_SECURE", True),
            httponly=getattr(settings, "PORTAL_SESSION_COOKIE_HTTPONLY", True),
            samesite=getattr(settings, "PORTAL_SESSION_COOKIE_SAMESITE", "None"),
        )
        return response
