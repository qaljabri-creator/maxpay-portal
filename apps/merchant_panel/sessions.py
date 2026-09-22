"""The merchant panel's own session cookie, for when B2CORE frames it.

The merchant panel became a menu item inside B2CORE, restricted to the merchant
client type and loaded as an iframe from ``https://my.maxifyfx.com``. That makes
it the second surface here to live in a third-party context, and it needs the
same thing the client portal needed for the same reason: a cookie of its own,
``SameSite=None; Secure``, because a ``Lax`` cookie is never sent into a frame
somebody else's page is hosting.

Three separate cookies now, and the separation is the whole point:

===================  ==============  =========  ===============================
Surface              Cookie          SameSite   Path
===================  ==============  =========  ===============================
Finance / admin      ``maxpay_sessionid``  Lax  ``/``
Client portal        ``maxpay_embed_sid``  None ``/portal/``
Merchant panel       ``maxpay_merchant_sid`` None ``/merchant/``
===================  ==============  =========  ===============================

The Finance panel keeps ``Lax`` and keeps ``frame-ancestors 'none'``. Nothing
here relaxes it, and a system check refuses to start if any two of the three
names ever collide — see :mod:`apps.merchant_panel.checks`.

The path matters as much as the ``SameSite`` does: scoped to ``/merchant/``, the
weaker cookie is never even *sent* to the Finance panel, so a bug on this
surface cannot be replayed against that one.
"""

from apps.core.embed_sessions import CookieSessionMiddleware, new_store  # noqa: F401


class MerchantSessionMiddleware(CookieSessionMiddleware):
    """Loads and persists ``request.merchant_session``.

    ``request.session`` — the internal panel's — is never touched, which is
    what lets a merchant hold a password session and an embed session in one
    browser without either standing in for the other.
    """

    request_attribute = "merchant_session"
    setting_prefix = "MERCHANT_SESSION"
    default_cookie_name = "maxpay_merchant_sid"
    default_cookie_path = "/merchant/"


def cookie_name() -> str:
    return MerchantSessionMiddleware.cookie_name()


def cookie_path() -> str:
    return MerchantSessionMiddleware.cookie_path()
