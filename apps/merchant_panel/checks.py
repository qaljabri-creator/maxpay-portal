"""Start-up checks for the embedded merchant panel.

The same kind of mistake :mod:`apps.portal.checks` catches, one surface along: a
cookie the browser quietly drops, or a cookie that is quietly the *same* cookie
as one of the other two. There are three session cookies in this project now and
the whole design rests on their being three, so a collision is an error at
``manage.py check`` rather than a surprise in production.

``merchant.E020`` is worth singling out. If the merchant cookie were ever given
the internal panel's name, the ``SameSite=None`` this surface needs in order to
exist inside a frame would be handed straight to the Finance panel — which is
precisely the relaxation these separate cookies exist to prevent.
"""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security)
def check_merchant_session_cookie(app_configs, **kwargs):
    """Three cookies, three names, and the weak ones must be ``Secure``."""
    issues = []
    name = getattr(settings, "MERCHANT_SESSION_COOKIE_NAME", "")
    internal = getattr(settings, "SESSION_COOKIE_NAME", "")
    portal = getattr(settings, "PORTAL_SESSION_COOKIE_NAME", "")
    samesite = str(getattr(settings, "MERCHANT_SESSION_COOKIE_SAMESITE", "")).lower()
    secure = getattr(settings, "MERCHANT_SESSION_COOKIE_SECURE", False)

    if not name:
        issues.append(Error("MERCHANT_SESSION_COOKIE_NAME is unset.", id="merchant.E020"))
    elif name == internal:
        issues.append(
            Error(
                "MERCHANT_SESSION_COOKIE_NAME is the same as SESSION_COOKIE_NAME.",
                hint="The merchant embed session is SameSite=None because it lives "
                "in a B2CORE iframe; sharing its name with the internal panel's "
                "session would hand that relaxation to the Finance panel too.",
                id="merchant.E020",
            )
        )
    elif name == portal:
        issues.append(
            Error(
                "MERCHANT_SESSION_COOKIE_NAME is the same as PORTAL_SESSION_COOKIE_NAME.",
                hint="A client's session and a merchant's session would overwrite "
                "one another in any browser that held both — and spec §2 depends "
                "on those two never meeting.",
                id="merchant.E021",
            )
        )

    if samesite == "none" and not secure:
        issues.append(
            Error(
                "MERCHANT_SESSION_COOKIE_SAMESITE='None' requires "
                "MERCHANT_SESSION_COOKIE_SECURE=True.",
                hint="Browsers drop a SameSite=None cookie that is not Secure, so no "
                "merchant would ever hold a session. Serve the panel over HTTPS.",
                id="merchant.E022",
            )
        )

    path = getattr(settings, "MERCHANT_SESSION_COOKIE_PATH", "")
    prefix = getattr(settings, "MERCHANT_URL_PREFIX", "/merchant/")
    if path != prefix:
        issues.append(
            Warning(
                f"MERCHANT_SESSION_COOKIE_PATH={path!r} does not match "
                f"MERCHANT_URL_PREFIX={prefix!r}.",
                hint="Scoped wider than the panel, the weakest cookie in the "
                "project is sent to surfaces that have no use for it; scoped "
                "narrower, no merchant request carries a session at all.",
                id="merchant.W023",
            )
        )

    return issues


@register(Tags.security, deploy=True)
def check_merchant_embed_deployment(app_configs, **kwargs):
    issues = []

    samesite = str(getattr(settings, "MERCHANT_SESSION_COOKIE_SAMESITE", "")).lower()
    if samesite != "none" and getattr(settings, "B2CORE_ORIGIN", ""):
        issues.append(
            Warning(
                f"MERCHANT_SESSION_COOKIE_SAMESITE={samesite!r} will not be sent "
                "inside the B2CORE iframe.",
                hint="Only 'None' survives a third-party context. Anything else "
                "means every merchant request arrives without a session.",
                id="merchant.W024",
            )
        )

    if getattr(settings, "MERCHANT_PASSWORD_LOGIN", False):
        issues.append(
            Warning(
                "MERCHANT_PASSWORD_LOGIN is on.",
                hint="The emergency door is open: a merchant can sign in with the "
                "password their account was provisioned with, outside the B2CORE "
                "binding that is otherwise the only way into the panel. Intended "
                "for an outage, not for normal running — turn it off afterwards.",
                id="merchant.W025",
            )
        )

    return issues
