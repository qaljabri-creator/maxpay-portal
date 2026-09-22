"""A session cookie of one's own, for a surface that lives in someone's iframe.

Two surfaces need this and they need it for the same reason. The internal
panel's cookie is ``SameSite=Lax``, which is a large part of what protects it
from cross-site request forgery. A page framed by B2CORE is, to a browser, a
third-party context, and a ``Lax`` cookie is simply never sent there — so a
framed surface needs ``SameSite=None; Secure``, a weaker cookie, and precisely
therefore a *separate* cookie rather than a relaxation of the existing one.

Running Django's own
:class:`~django.contrib.sessions.middleware.SessionMiddleware` a second time
would have the two instances fighting over ``request.session`` and over which
cookie to write on the way out. This is that middleware, near enough, with
three differences: it writes a cookie of its own, it reads its attributes from a
settings prefix the subclass names, and it never touches ``request.session``.

Subclasses declare four things and nothing else:

``request_attribute``
    where the store is hung on the request.
``setting_prefix``
    ``"PORTAL_SESSION"`` reads ``PORTAL_SESSION_COOKIE_NAME`` and friends.
``default_cookie_name`` / ``default_cookie_path``
    used when the settings are absent.

The two live subclasses are :class:`apps.portal.sessions.PortalSessionMiddleware`
— the client portal — and
:class:`apps.merchant_panel.sessions.MerchantSessionMiddleware` — the merchant
panel, once B2CORE started framing that too. They were one file copied twice
before this one existed; a cookie that is dropped by the browser for want of a
``Secure`` flag is not a bug anybody finds by reading, so the two copies are
better off being the same code.
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


def new_store(session_key: str | None = None):
    """A session store of the configured engine, unattached to any request."""
    engine = import_module(settings.SESSION_ENGINE)
    return engine.SessionStore(session_key)


class CookieSessionMiddleware:
    """Loads and persists one extra session store, under its own cookie."""

    request_attribute = ""
    setting_prefix = ""
    default_cookie_name = ""
    default_cookie_path = "/"

    def __init__(self, get_response):
        self.get_response = get_response

    # -- configuration -----------------------------------------------------

    @classmethod
    def _setting(cls, suffix, default=None):
        return getattr(settings, f"{cls.setting_prefix}_COOKIE_{suffix}", default)

    @classmethod
    def cookie_name(cls) -> str:
        return cls._setting("NAME", cls.default_cookie_name)

    @classmethod
    def cookie_path(cls) -> str:
        return cls._setting("PATH", cls.default_cookie_path)

    # -- request phase -----------------------------------------------------

    def __call__(self, request):
        name = self.cookie_name()
        setattr(request, self.request_attribute, new_store(request.COOKIES.get(name)))
        response = self.get_response(request)
        return self._persist(request, response)

    # -- response phase ----------------------------------------------------

    def _persist(self, request, response):
        session = getattr(request, self.request_attribute, None)
        if session is None:
            return response

        name = self.cookie_name()
        accessed = session.accessed
        modified = session.modified
        empty = session.is_empty()

        # A session that was emptied — logout, or expiry — takes its cookie
        # with it, otherwise the browser keeps presenting a dead key forever.
        if name in request.COOKIES and empty:
            response.delete_cookie(
                name,
                path=self.cookie_path(),
                domain=self._setting("DOMAIN", None),
                samesite=self._setting("SAMESITE", "None"),
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
                f"The {name} session could not be saved; it was deleted concurrently."
            ) from exc

        response.set_cookie(
            name,
            session.session_key,
            max_age=max_age,
            expires=expires,
            domain=self._setting("DOMAIN", None),
            path=self.cookie_path(),
            secure=self._setting("SECURE", True),
            httponly=self._setting("HTTPONLY", True),
            samesite=self._setting("SAMESITE", "None"),
        )
        return response
