"""Production settings.

Every value that must not be guessable is read from the environment with no
fallback, so a misconfigured deploy fails loudly at start-up instead of running
insecurely.
"""

from .base import *  # noqa: F401,F403
from .env import env_bool, env_list, env_str

DEBUG = False

SECRET_KEY = env_str("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

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

# DENY everywhere, including the portal — and then SecurityHeadersMiddleware
# strips it from portal responses only, replacing it with
# `Content-Security-Policy: frame-ancestors <B2CORE origin>` (spec §11). CSP is
# the header browsers honour for a named ancestor; X-Frame-Options has no
# equivalent form, so leaving it at DENY for everything else costs nothing.
X_FRAME_OPTIONS = "DENY"

# The client session cookie is the one that has to survive a third-party
# iframe. The internal one stays Lax — see apps/portal/sessions.py.
SESSION_COOKIE_SAMESITE = "Lax"
PORTAL_SESSION_COOKIE_SAMESITE = "None"
PORTAL_SESSION_COOKIE_SECURE = True

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env_str("EMAIL_HOST", default="")
EMAIL_PORT = int(env_str("EMAIL_PORT", default="587"))
EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env_str("DEFAULT_FROM_EMAIL", default="noreply@maxifyfx.com")
