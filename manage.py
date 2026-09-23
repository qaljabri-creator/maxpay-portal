#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

#: The one way to run without naming a settings module: say out loud that this
#: is a laptop. Only then does manage.py fall back to development settings.
LOCAL_DEV_FLAG = "MAXPAY_LOCAL_DEV"
DEV_SETTINGS = "config.settings.dev"


class SettingsNotNamed(Exception):
    """No settings module, and nothing saying this is a development machine."""


def settings_module(environ) -> str:
    """Which settings module to run under, or refuse.

    It used to fall back to ``config.settings.dev`` silently — ``DEBUG`` on by
    default, no HTTPS, none of prod.py's refusals. On a server whose ``.env``
    forgot the line, every ``migrate`` and ``createsuperuser`` then ran against
    the production database under development settings, and nothing said so.
    ``wsgi.py`` falls back to prod; this one now falls back to nothing.
    """
    named = (environ.get("DJANGO_SETTINGS_MODULE") or "").strip()
    if named:
        return named
    if (environ.get(LOCAL_DEV_FLAG) or "").strip().lower() in {"1", "true", "yes", "on"}:
        return DEV_SETTINGS
    raise SettingsNotNamed(
        "DJANGO_SETTINGS_MODULE is not set.\n"
        "  On a server:        DJANGO_SETTINGS_MODULE=config.settings.prod in .env\n"
        f"  On a development machine: DJANGO_SETTINGS_MODULE={DEV_SETTINGS}, "
        f"or {LOCAL_DEV_FLAG}=true\n"
        "manage.py no longer guesses: a guess of 'dev' on a server runs "
        "migrations against production with DEBUG on."
    )


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    try:
        os.environ["DJANGO_SETTINGS_MODULE"] = settings_module(os.environ)
    except SettingsNotNamed as exc:
        sys.exit(f"manage.py: {exc}")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and available "
            "on your PYTHONPATH environment variable? Did you forget to "
            "activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
