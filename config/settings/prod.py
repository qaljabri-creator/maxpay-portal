"""Production settings.

Every value that must not be guessable is read from the environment with no
fallback, so a misconfigured deploy fails loudly at start-up instead of running
insecurely.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import B2CORE_JWT_ISSUER, B2CORE_ORIGIN
from .env import env_bool, env_list, env_str

DEBUG = False

SECRET_KEY = env_str("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

# --- who we believe (spec §4) --------------------------------------------
#
# The issuer is checked here rather than only in a system check, because the
# two failures it guards against are silent in opposite directions and both are
# worse than not starting.
#
# Unset, PyJWT skips the `iss` check entirely and any token signed by any key in
# the configured JWKS is accepted — which is not a theoretical concern when the
# JWKS belongs to a provider serving more than one relying party.
#
# Set to the portal origin, it rejects every real token: B2CORE's issuer is on
# `api.` with a path (`/srvsz/auth/clients/v1/`, trailing slash included) and
# the origin is the portal's own host with none. That is the mistake most
# available to somebody wiring this up, because the two look like they should
# be the same string and are not.
#
# `manage.py check --deploy` reports both as `portal.E005`. This refuses to
# start, which is the only one of the two an operator cannot skip past.
if not B2CORE_JWT_ISSUER:
    raise ImproperlyConfigured(
        "B2CORE_JWT_ISSUER is unset. Without it any correctly-signed token is "
        "accepted regardless of who issued it. Set it to the literal issuer "
        "B2CORE mints, trailing slash included."
    )
if B2CORE_ORIGIN and B2CORE_JWT_ISSUER.rstrip("/") == B2CORE_ORIGIN.rstrip("/"):
    raise ImproperlyConfigured(
        f"B2CORE_JWT_ISSUER is the portal origin ({B2CORE_ORIGIN}). The issuer "
        "is B2CORE's auth service, not the site it frames — no client could "
        "authenticate. See config/settings/base.py for the value."
    )

# --- HTTPS (spec §11) ----------------------------------------------------
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# DENY everywhere, including the two framed surfaces — and then
# SecurityHeadersMiddleware strips it from their responses only, replacing it
# with
# `Content-Security-Policy: frame-ancestors <B2CORE origin>` (spec §11). CSP is
# the header browsers honour for a named ancestor; X-Frame-Options has no
# equivalent form, so leaving it at DENY for everything else costs nothing.
X_FRAME_OPTIONS = "DENY"

# The two framed surfaces are the ones that have to survive a third-party
# iframe. The internal one stays Lax — see apps/core/embed_sessions.py.
SESSION_COOKIE_SAMESITE = "Lax"
PORTAL_SESSION_COOKIE_SAMESITE = "None"
PORTAL_SESSION_COOKIE_SECURE = True
MERCHANT_SESSION_COOKIE_SAMESITE = "None"
MERCHANT_SESSION_COOKIE_SECURE = True

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env_str("EMAIL_HOST", default="")
EMAIL_PORT = int(env_str("EMAIL_PORT", default="587"))
EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env_str("DEFAULT_FROM_EMAIL", default="noreply@maxifyfx.com")
