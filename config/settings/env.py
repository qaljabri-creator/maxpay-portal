"""Tiny typed reader over ``os.environ``.

Kept dependency-free on purpose: ``.env`` files are loaded by ``manage.py`` /
``wsgi.py`` via python-dotenv, and everything downstream just reads the process
environment.
"""

import os

from django.core.exceptions import ImproperlyConfigured

_MISSING = object()

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ImproperlyConfiguredEnv(ImproperlyConfigured):
    """Raised when a required environment variable is absent or unparseable."""


def env_str(name: str, default=_MISSING) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if default is _MISSING:
            raise ImproperlyConfiguredEnv(
                f"Required environment variable {name!r} is not set."
            )
        return default
    return value


def env_bool(name: str, default=_MISSING) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        if default is _MISSING:
            raise ImproperlyConfiguredEnv(
                f"Required environment variable {name!r} is not set."
            )
        return default
    normalised = raw.strip().lower()
    if normalised in _TRUE:
        return True
    if normalised in _FALSE:
        return False
    raise ImproperlyConfiguredEnv(f"{name}={raw!r} is not a boolean.")


def env_int(name: str, default=_MISSING) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is _MISSING:
            raise ImproperlyConfiguredEnv(
                f"Required environment variable {name!r} is not set."
            )
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfiguredEnv(f"{name}={raw!r} is not an integer.") from exc


def env_list(name: str, default=_MISSING, separator: str = ",") -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if default is _MISSING:
            raise ImproperlyConfiguredEnv(
                f"Required environment variable {name!r} is not set."
            )
        return list(default)
    return [part.strip() for part in raw.split(separator) if part.strip()]
