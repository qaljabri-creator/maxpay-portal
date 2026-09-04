"""
Settings shared by every environment.

Environment-specific modules (``dev``, ``prod``) import everything from here and
override what differs. Nothing secret is ever hard-coded — see ``.env.example``.
"""

from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

from .env import env_bool, env_int, env_list, env_str

BASE_DIR = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

SECRET_KEY = env_str("DJANGO_SECRET_KEY", default="")
DEBUG = env_bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=[])

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

# Mandatory 2FA for internal accounts (spec §4, §11). TOTP authenticator apps
# plus static backup tokens; no SMS or phone gateway is enabled, so no phone
# number of an internal user is ever stored.
TWO_FACTOR_APPS = [
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
    *TWO_FACTOR_APPS,
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.merchants",
    "apps.rates",
    "apps.transactions",
    "apps.finance",
    "apps.merchant_panel",
    "apps.portal",
]

INSTALLED_APPS = [*DJANGO_APPS, *THIRD_PARTY_APPS, *LOCAL_APPS]


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    # Listed early on purpose. The response phase runs in reverse, so this is
    # the *last* middleware to touch the headers and can therefore strip the
    # X-Frame-Options that XFrameOptionsMiddleware sets on portal responses,
    # replacing it with the frame-ancestors CSP spec §11 asks for. It also sets
    # the internal panels' own policy — see apps/core/middleware.py.
    "apps.core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # The client portal carries its own cookie, separate from the internal
    # panel's: it lives in a cross-site iframe and so needs SameSite=None,
    # which the internal session must never be weakened to. See
    # apps/portal/sessions.py.
    "apps.portal.sessions.PortalSessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    # After LocaleMiddleware, deliberately: it settles the language from the
    # `django_language` cookie, which is SameSite=Lax and so never reaches us
    # inside the iframe. What B2CORE announced wins, and can only win by being
    # applied second.
    "apps.portal.middleware.ClientSessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must sit directly after AuthenticationMiddleware: it upgrades
    # request.user with OTP verification state.
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Refuses to let a half-authenticated internal user reach anything before
    # their second factor is set up and verified. Sits after MessageMiddleware
    # because it explains the redirect through the message framework.
    "apps.accounts.middleware.EnforceTwoFactorMiddleware",
    # After the two-factor gate on purpose: a password being changed over a
    # session whose second factor was never verified is a password being
    # changed by whoever holds the password. Build-order step 15.
    "apps.accounts.middleware.ForcePasswordChangeMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database — PostgreSQL (spec §Stack)
# --------------------------------------------------------------------------


def _database_from_url(url: str) -> dict:
    """Parse a ``DATABASE_URL``.

    Only postgres is supported for real environments. ``sqlite://`` is accepted
    so a developer without a local PostgreSQL can smoke-test migrations; it must
    never be used for anything shared.
    """
    parsed = urlparse(url)
    if parsed.scheme in {"sqlite", "sqlite3"}:
        name = parsed.path.lstrip("/") or ":memory:"
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": name if name == ":memory:" else str(BASE_DIR / name),
        }
    if parsed.scheme not in {"postgres", "postgresql", "psql"}:
        raise ImproperlyConfigured(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
    }


_database_url = env_str("DATABASE_URL", default="")
if _database_url:
    DATABASES = {"default": _database_from_url(_database_url)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env_str("POSTGRES_DB", default="maxpay"),
            "USER": env_str("POSTGRES_USER", default="maxpay"),
            "PASSWORD": env_str("POSTGRES_PASSWORD", default=""),
            "HOST": env_str("POSTGRES_HOST", default="127.0.0.1"),
            "PORT": env_str("POSTGRES_PORT", default="5432"),
        }
    }

DATABASES["default"].setdefault("ATOMIC_REQUESTS", False)
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Spec §11, build-order step 14. ModelBackend with a lockout in front of the
# password check, so a stolen email cannot be ground against until something
# works — and so the audit log is not filled by the grinding. See
# apps/accounts/throttling.py; it fails open if the cache is down.
AUTHENTICATION_BACKENDS = ["apps.accounts.throttling.ThrottledModelBackend"]

#: How many failures one (username, IP) pair may accumulate before the pair is
#: refused, and for how long. Deliberately per-pair: per-username alone would
#: hand an attacker a way to lock a colleague out of their own account.
LOGIN_FAILURE_LIMIT = env_int("LOGIN_FAILURE_LIMIT", default=10)
LOGIN_FAILURE_WINDOW_SECONDS = env_int(
    "LOGIN_FAILURE_WINDOW_SECONDS", default=15 * 60
)

# Every internal login goes through the two-factor wizard.
LOGIN_URL = "two_factor:login"
# Role-aware: config.urls.home forwards to the Finance panel or the
# merchant panel depending on who just signed in (spec §8, §9).
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "two_factor:login"

# We install AdminSiteOTPRequired explicitly in config/urls.py rather than
# letting the library monkeypatch the admin site.
TWO_FACTOR_PATCH_ADMIN = False
OTP_TOTP_ISSUER = env_str("OTP_TOTP_ISSUER", default="MaxPay Portal")
# Tolerate one 30s step of clock drift either side.
OTP_TOTP_SYNC = True

# Roles whose accounts cannot function without a verified second factor.
# Spec §4 mandates it for finance_admin and finance_staff; §11 states that *all*
# internal accounts require 2FA, so merchants are included too.
TWO_FACTOR_REQUIRED_ROLES = ["finance_admin", "finance_staff", "merchant"]

SESSION_COOKIE_NAME = "maxpay_sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_AGE = 60 * 60 * 8
CSRF_COOKIE_HTTPONLY = False


# --------------------------------------------------------------------------
# Internationalisation — Arabic RTL (spec §Stack)
# --------------------------------------------------------------------------

LANGUAGE_CODE = "ar"
LANGUAGES = [("ar", "العربية"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Asia/Baghdad"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Static and uploaded files
# --------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Spec §11: uploads are stored outside the web root and served through signed
# time-limited URLs. Nothing under here is ever mapped to a URL prefix; the
# signed-URL view arrives with the attachment work in a later build step.
MEDIA_ROOT = BASE_DIR / "private_media"
MEDIA_URL = "/__never_served__/"

MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "pdf"]
ALLOWED_UPLOAD_CONTENT_TYPES = [
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_RATES": {
        # Spec §11: rate limiting on submission endpoints.
        "submission": "20/hour",
    },
}


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_BYTES
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024


# --------------------------------------------------------------------------
# B2CORE integration (spec §4) — build-order step 5
# --------------------------------------------------------------------------

#: Scheme + host of the B2CORE portal that frames us. Everything about the
#: embed keys off it: the frame-ancestors CSP, the postMessage target origin,
#: and which origins may post to the session endpoints.
B2CORE_ORIGIN = env_str("B2CORE_ORIGIN", default="").rstrip("/")

#: Spec §4: `https://api.<domain>/.well-known/jwks.json`.
B2CORE_JWKS_URL = env_str("B2CORE_JWKS_URL", default="")

#: The `iss` B2CORE actually mints, character for character — read off a real
#: token on 4 Sep 2026, not from documentation.
#:
#: **The trailing slash is part of the value.** PyJWT compares `iss` by string
#: equality, so dropping it rejects every client, and so does using the portal
#: origin here: the issuer is on `api.` with a path, the origin is the portal
#: host with none. `portal.E005` refuses a deployment that makes either mistake,
#: and prod.py refuses to boot at all.
B2CORE_JWT_ISSUER = env_str(
    "B2CORE_JWT_ISSUER",
    default="https://api.maxifyfx.com/srvsz/auth/clients/v1/",
)

#: **Must stay empty. B2CORE sends no `aud` claim at all.**
#:
#: Not an oversight to be corrected once somebody finds the right value — there
#: is no value. Setting this turns on PyJWT's audience check, which then refuses
#: every real token for a claim B2CORE never mints, and the portal stops
#: authenticating anybody. `portal.E006` fires if it is set, which is the
#: opposite of what the check here used to say.
#:
#: An empty audience is not a hole the way an empty issuer is: the issuer is
#: what stops us trusting a token signed by a key in someone else's JWKS, and
#: that is the check doing the work.
B2CORE_JWT_AUDIENCE = env_str("B2CORE_JWT_AUDIENCE", default="")

#: Asymmetric only. An HMAC entry here would let anyone holding the *public*
#: key mint tokens we accept, and "none" needs no key at all.
#:
#: `EdDSA` leads because it is what B2CORE signs with. The others stay so a
#: rotation to an RSA or EC key is not an outage.
B2CORE_JWT_ALGORITHMS = env_list(
    "B2CORE_JWT_ALGORITHMS",
    default=["EdDSA", "RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
)

B2CORE_JWT_LEEWAY_SECONDS = env_int("B2CORE_JWT_LEEWAY_SECONDS", default=30)
#: Which claim would carry the client's B2CORE account number, if one did.
#: **None does.** B2CORE's token has `sub`, `email`, `first_name`, `last_name`,
#: `aal`, `amr`, `sid`, `jti` and the timestamps — no account number and no
#: client type. This stays for the day one appears; until then it resolves to
#: nothing and Finance identifies a client by email. Never point it at `sub` or
#: `sid` to fill the column: both are real identifiers and neither is an
#: account number.
B2CORE_ACCOUNT_CLAIM = env_str("B2CORE_ACCOUNT_CLAIM", default="account_number")
B2CORE_MAX_TOKEN_BYTES = env_int("B2CORE_MAX_TOKEN_BYTES", default=8192)
B2CORE_JWKS_CACHE_SECONDS = env_int("B2CORE_JWKS_CACHE_SECONDS", default=600)
B2CORE_JWKS_TIMEOUT_SECONDS = env_int("B2CORE_JWKS_TIMEOUT_SECONDS", default=5)

# The embed calls us from its own origin, with cookies.
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = [B2CORE_ORIGIN] if B2CORE_ORIGIN else []


# --------------------------------------------------------------------------
# The embedded client portal
# --------------------------------------------------------------------------

#: Everything the client ever touches lives under this prefix, which is also
#: what the frame-ancestors CSP and the portal cookie path key off.
PORTAL_URL_PREFIX = "/portal/"

#: Where the Django admin is mounted. The security-headers middleware reads it
#: to decide which surface a response belongs to; it must match config/urls.py.
ADMIN_URL_PREFIX = "/admin/"

# Two sessions, deliberately. The internal panel keeps SameSite=Lax, which is
# most of what protects it from cross-site request forgery. The client session
# runs inside a third-party iframe, where Lax means "never sent", so it needs
# SameSite=None — a weaker cookie that must therefore be a *different* cookie.
# A system check refuses to start if these two names ever collide.
PORTAL_SESSION_COOKIE_NAME = env_str("PORTAL_SESSION_COOKIE_NAME", default="maxpay_embed_sid")
PORTAL_SESSION_COOKIE_SAMESITE = env_str("PORTAL_SESSION_COOKIE_SAMESITE", default="None")
# SameSite=None without Secure is dropped outright by every current browser.
PORTAL_SESSION_COOKIE_SECURE = env_bool("PORTAL_SESSION_COOKIE_SECURE", default=True)
PORTAL_SESSION_COOKIE_HTTPONLY = True
#: Scoped to the portal, so the internal panel never even receives it.
PORTAL_SESSION_COOKIE_PATH = PORTAL_URL_PREFIX
PORTAL_SESSION_COOKIE_DOMAIN = env_str("PORTAL_SESSION_COOKIE_DOMAIN", default="") or None

#: A portal session never outlives this, however long-lived the token was.
PORTAL_SESSION_MAX_SECONDS = env_int("PORTAL_SESSION_MAX_SECONDS", default=60 * 60 * 8)

#: Spec §11. The session endpoint is unauthenticated and does public-key
#: cryptography, so it is capped per client IP.
PORTAL_SESSION_RATE = env_str("PORTAL_SESSION_RATE", default="30/minute")

#: Lets the bootstrap page run outside an iframe, for local development only.
PORTAL_ALLOW_STANDALONE = env_bool("PORTAL_ALLOW_STANDALONE", default=False)


# --------------------------------------------------------------------------
# The client request flow (spec §7) — build-order step 6
# --------------------------------------------------------------------------

#: Spec §11: rate limiting on the submission endpoints. Matches the throttle
#: rate already declared for DRF above, so both surfaces agree.
PORTAL_SUBMISSION_RATE = env_str("PORTAL_SUBMISSION_RATE", default="20/hour")

#: How long the catalogue endpoint may be hammered while a client browses.
#: Generous: it is read-only and cheap, and a client stepping back and forth
#: through the wizard legitimately calls it a dozen times.
PORTAL_CATALOG_RATE = env_str("PORTAL_CATALOG_RATE", default="240/minute")

#: Bounds on a single deposit, in USD.
#:
#: The spec sets no figure, so these are placeholders that only stop obvious
#: nonsense — a zero, or a number wide enough to overflow the amount columns.
#: **Confirm the real limits with Finance** and set them per environment.
PORTAL_DEPOSIT_MIN_USD = env_str("PORTAL_DEPOSIT_MIN_USD", default="1")
PORTAL_DEPOSIT_MAX_USD = env_str("PORTAL_DEPOSIT_MAX_USD", default="100000")

# --------------------------------------------------------------------------
# The withdrawal flow (spec §6, §7) — build-order step 10
# --------------------------------------------------------------------------

#: Bounds on a single withdrawal, in USD. Separate from the deposit pair
#: because the two directions are not symmetric: a withdrawal moves money the
#: desk has to have on hand at a merchant, so Finance may well want a lower
#: ceiling on it than on money coming in. Same placeholder caveat — confirm the
#: real figures with Finance.
PORTAL_WITHDRAWAL_MIN_USD = env_str("PORTAL_WITHDRAWAL_MIN_USD", default="1")
PORTAL_WITHDRAWAL_MAX_USD = env_str("PORTAL_WITHDRAWAL_MAX_USD", default="100000")

#: How long a destination card or wallet number may be, counted in digits after
#: the separators a client types are stripped out.
#:
#: Iraqi rails span a wide range — an 11-digit mobile wallet number, a 16-digit
#: card — so this is deliberately loose. It is a typo guard, not a checksum:
#: nothing here can tell a valid account from a mistyped one, which is why the
#: client is shown the normalised number back before they commit to it.
#: ``Request.destination_account`` is 64 characters, so the ceiling stays well
#: inside the column.
PORTAL_DESTINATION_MIN_DIGITS = env_int("PORTAL_DESTINATION_MIN_DIGITS", default=6)
PORTAL_DESTINATION_MAX_DIGITS = env_int("PORTAL_DESTINATION_MAX_DIGITS", default=32)

#: Spec §11: attachments are served through signed, time-limited URLs. Short,
#: because the page re-signs on every load and a link is never bookmarked.
PORTAL_ATTACHMENT_URL_MAX_AGE = env_int("PORTAL_ATTACHMENT_URL_MAX_AGE", default=300)

#: How long one message may be — the note a client attaches at submission
#: (spec §6, §7) and every message posted into a thread afterwards (spec §9).
#: One limit, because they are the same thing written at different moments.
PORTAL_MESSAGE_MAX_CHARS = env_int("PORTAL_MESSAGE_MAX_CHARS", default=1000)

#: Spec §11, build-order step 9. Posting into a thread writes a row and may
#: carry a file, so it is capped like the submission endpoint — but far looser,
#: because a conversation is meant to be had.
PORTAL_MESSAGE_RATE = env_str("PORTAL_MESSAGE_RATE", default="30/minute")


# --------------------------------------------------------------------------
# Live updates (spec §8, §10) — build-order step 13
# --------------------------------------------------------------------------

#: How often the two internal panels ask the server whether anything moved.
#: Spec §10 names ten seconds and rules out WebSockets for phase 1. Settable
#: per environment so a deployment under load can widen it without a code
#: change; the panels read it from the page rather than hard-coding it.
PANEL_POLL_SECONDS = env_int("PANEL_POLL_SECONDS", default=10)


# --------------------------------------------------------------------------
# Cache (spec §11) — build-order step 14
# --------------------------------------------------------------------------

#: Both rate limiters in this project are cache counters: the portal's
#: submission limiter (spec §11) and the internal login lockout. Neither is a
#: limit at all unless the counter is **shared between worker processes** —
#: behind four Gunicorn workers with a per-process cache, an attacker simply
#: gets four times the allowance and the lockout resets whenever the balancer
#: picks a different process.
#:
#: So the backend is configurable, and a deploy check (``core.W010``) says so
#: out loud while it is not. Django ships the Redis backend, so pointing
#: ``REDIS_URL`` at one adds no dependency.
#:
#: The default stays LocMem because that is right for a laptop and for the test
#: suite, where there is one process and no Redis to run.
_redis_url = env_str("REDIS_URL", default="")
if _redis_url:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _redis_url,
            # A cache that is down must not take the desk with it. Both
            # limiters already fail open; this keeps a Redis blip from turning
            # into a 500 on an unrelated page.
            "OPTIONS": {"IGNORE_EXCEPTIONS": True},
            "KEY_PREFIX": env_str("CACHE_KEY_PREFIX", default="maxpay"),
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "maxpay-local",
        }
    }


# --------------------------------------------------------------------------
# Backups (spec §11) — build-order step 14
# --------------------------------------------------------------------------

#: Where `manage.py backup_database` writes. Empty means "beside the code",
#: which a deploy check warns about: a dump on the same disk as the database is
#: not a backup of anything that takes the disk with it.
BACKUP_DIR = env_str("BACKUP_DIR", default="")
BACKUP_RETENTION_DAYS = env_int("BACKUP_RETENTION_DAYS", default=14)
BACKUP_TIMEOUT_SECONDS = env_int("BACKUP_TIMEOUT_SECONDS", default=60 * 30)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"],
                               "propagate": False},
        "maxpay.audit": {"level": "INFO", "handlers": ["console"],
                         "propagate": False},
        "maxpay.b2core": {"level": "INFO", "handlers": ["console"],
                          "propagate": False},
    },
}
