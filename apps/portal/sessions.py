"""A second, independent session cookie for the embedded client portal.

The internal panel and the client portal are two different trust domains that
happen to share a hostname, and they must not share a session cookie.

The internal panel's cookie stays ``SameSite=Lax``, which is a large part of
what protects it from cross-site request forgery. The client portal, however,
runs inside a B2CORE iframe: to a browser that is a third-party context, and a
``Lax`` cookie is simply never sent there. The portal therefore needs
``SameSite=None; Secure`` — a weaker cookie, and precisely the reason it has to
be a *separate* cookie rather than a relaxation of the existing one.

The machinery that does this lives in :mod:`apps.core.embed_sessions`, because
the merchant panel now needs exactly the same thing for exactly the same reason
and two copies of it would be two places for a cookie attribute to drift. What
is left here is this surface's four answers: where the store hangs on the
request, which settings name its cookie, and what that cookie is called and
scoped to when nothing says otherwise.
"""

from apps.core.embed_sessions import CookieSessionMiddleware, new_store  # noqa: F401


class PortalSessionMiddleware(CookieSessionMiddleware):
    """Loads and persists ``request.portal_session``.

    ``request.session`` — the internal panel's — is never touched.
    """

    request_attribute = "portal_session"
    setting_prefix = "PORTAL_SESSION"
    default_cookie_name = "maxpay_embed_sid"
    default_cookie_path = "/portal/"


def cookie_name() -> str:
    return PortalSessionMiddleware.cookie_name()


def cookie_path() -> str:
    return PortalSessionMiddleware.cookie_path()
