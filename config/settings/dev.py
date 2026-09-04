"""Local development settings."""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, INSTALLED_APPS, MIDDLEWARE  # noqa: F401
from .env import env_bool, env_list, env_str

DEBUG = env_bool("DJANGO_DEBUG", default=True)

SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY",
    default="dev-only-insecure-key-do-not-use-outside-local-development",
)

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"]
)

# Readable in the console instead of needing a real mailbox.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Keep local logins usable while still exercising the real validators.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
]

INTERNAL_IPS = ["127.0.0.1"]
