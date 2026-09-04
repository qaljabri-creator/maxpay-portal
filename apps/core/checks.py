"""Deployment checks for the things spec §11 asks for — build-order step 14.

:mod:`apps.portal.checks` already covers the B2CORE embed. This module covers
the rest of the security posture, and it exists for one reason: every item below
is a misconfiguration that is **silent at run time and expensive later**. A
per-process rate limiter behind four workers looks like a working rate limiter.
An unset ``BACKUP_DIR`` looks like a working backup schedule right up until
somebody needs the backup.

Same split as the portal's:

* **Always** — wrong wherever the code runs, so ``manage.py check`` fails on it.
* **Deploy only** (``manage.py check --deploy``) — wrong in a real deployment
  and unremarkable on a laptop. A developer has no PostgreSQL backup directory
  and refusing to start without one would only teach everyone to pass
  ``--skip-checks``.

* **Database** (``manage.py check --database default``) — the audit log's
  append-only triggers. Tagged separately because a check that opens a
  connection must not run on the ``manage.py`` invocations that *create* the
  database; a check that fails during ``migrate`` is a check nobody keeps.
"""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.db import connections

#: Cache backends that cannot hold a counter shared between worker processes.
#: Both the login lockout and the portal's submission limiter live in the cache,
#: and with one of these each worker enforces its own private allowance.
PER_PROCESS_CACHES = (
    "django.core.cache.backends.locmem.LocMemCache",
    "django.core.cache.backends.dummy.DummyCache",
)


# ---------------------------------------------------------------------------
# Always
# ---------------------------------------------------------------------------


@register(Tags.security)
def check_media_is_never_routed(app_configs, **kwargs):
    """Spec §11: uploads are served through the signed view, never by URL.

    ``MEDIA_URL`` is set to a deliberately absurd prefix so that anything which
    tries to build a media URL produces something obviously broken rather than
    something that works. This check is what stops a future edit from quietly
    making it real.
    """
    issues = []
    media_url = str(getattr(settings, "MEDIA_URL", ""))
    static_url = str(getattr(settings, "STATIC_URL", ""))

    if media_url and static_url and media_url.startswith(static_url):
        issues.append(
            Error(
                f"MEDIA_URL={media_url!r} sits under STATIC_URL={static_url!r}.",
                hint="Uploaded proof files would be served by the static handler, "
                "bypassing the signed time-limited view spec §11 requires.",
                id="core.E001",
            )
        )
    return issues


@register(Tags.security)
def check_panel_poll(app_configs, **kwargs):
    """A poll interval that is wrong in either direction (spec §8, §10)."""
    issues = []
    seconds = getattr(settings, "PANEL_POLL_SECONDS", 10)
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return [Error(f"PANEL_POLL_SECONDS={seconds!r} is not a number.", id="core.E002")]

    if seconds < 2:
        issues.append(
            Error(
                f"PANEL_POLL_SECONDS={seconds} would have every open panel hitting "
                "the server several times a second.",
                hint="Spec §10 asks for ten. Anything under two is a load test.",
                id="core.E003",
            )
        )
    elif seconds > 120:
        issues.append(
            Warning(
                f"PANEL_POLL_SECONDS={seconds} is long enough that the panels are "
                "not meaningfully live.",
                hint="Spec §8 and §10 both say ten seconds.",
                id="core.W004",
            )
        )
    return issues


@register(Tags.security)
def check_login_throttle(app_configs, **kwargs):
    """The lockout has to be reachable and has to be a limit (spec §11)."""
    issues = []
    backends = list(getattr(settings, "AUTHENTICATION_BACKENDS", []))
    if "apps.accounts.throttling.ThrottledModelBackend" not in backends:
        issues.append(
            Warning(
                "ThrottledModelBackend is not in AUTHENTICATION_BACKENDS.",
                hint="Internal logins would have no brute-force limit. 2FA still "
                "stands in the way, but a password can be ground out and the "
                "audit log filled while it is.",
                id="core.W005",
            )
        )
    limit = int(getattr(settings, "LOGIN_FAILURE_LIMIT", 10))
    if limit < 3:
        issues.append(
            Error(
                f"LOGIN_FAILURE_LIMIT={limit} locks accounts out on ordinary typos.",
                id="core.E006",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Deploy only — `manage.py check --deploy`
# ---------------------------------------------------------------------------


@register(Tags.security, deploy=True)
def check_shared_cache(app_configs, **kwargs):
    """A counter that is not shared is not a limit.

    Both rate limiters in this project are cache counters. Under Gunicorn with
    four workers and ``LocMemCache``, an attacker gets four times the allowance
    and the login lockout resets whenever the load balancer picks a different
    process.
    """
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if backend in PER_PROCESS_CACHES:
        return [
            Warning(
                f"CACHES['default'] is {backend.rsplit('.', 1)[-1]}, which is "
                "per-process.",
                hint="The portal's submission limiter (spec §11) and the login "
                "lockout both count in the cache. With one process per worker "
                "each worker enforces its own private allowance. Use Redis or "
                "Memcached in any environment with more than one worker.",
                id="core.W010",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_backups(app_configs, **kwargs):
    """Spec §11: daily automated database backups, restore tested before go-live."""
    issues = []
    if not getattr(settings, "BACKUP_DIR", ""):
        issues.append(
            Warning(
                "BACKUP_DIR is unset, so `manage.py backup_database` writes "
                "beside the code.",
                hint="Spec §11 requires daily backups. Point this at a volume "
                "that does not share a failure domain with the database, and "
                "schedule the command.",
                id="core.W011",
            )
        )
    retention = int(getattr(settings, "BACKUP_RETENTION_DAYS", 14))
    if retention <= 1:
        issues.append(
            Warning(
                f"BACKUP_RETENTION_DAYS={retention} keeps at most one day of dumps.",
                hint="Corruption is often noticed after the corrupt copy has "
                "already replaced the good one.",
                id="core.W012",
            )
        )
    return issues


@register(Tags.security, deploy=True)
def check_allowed_hosts(app_configs, **kwargs):
    """A wildcard host makes ``Host``-header poisoning somebody else's problem."""
    if "*" in getattr(settings, "ALLOWED_HOSTS", []):
        return [
            Error(
                "ALLOWED_HOSTS contains '*'.",
                hint="Password-reset and absolute URLs are built from the Host "
                "header; a wildcard lets a caller choose what they point at. "
                "Name the hostnames.",
                id="core.E013",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_secret_key(app_configs, **kwargs):
    """The development key must never reach a deployment."""
    key = str(getattr(settings, "SECRET_KEY", ""))
    if "dev-only" in key or "insecure" in key:
        return [
            Error(
                "SECRET_KEY is the development placeholder.",
                hint="Session cookies, signed attachment URLs (spec §11) and the "
                "portal's CSRF tokens are all derived from it. Set "
                "DJANGO_SECRET_KEY in the environment.",
                id="core.E014",
            )
        ]
    if len(key) < 50:
        return [
            Warning(
                f"SECRET_KEY is only {len(key)} characters.",
                hint="Django generates 50. Signed attachment URLs are only as "
                "strong as this value.",
                id="core.W015",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Database — `manage.py check --database default`
# ---------------------------------------------------------------------------

#: What migration ``core.0002_auditlog_append_only_triggers`` installs.
AUDITLOG_TRIGGERS = ("maxpay_auditlog_no_update", "maxpay_auditlog_no_delete")


def installed_auditlog_triggers(connection) -> set[str]:
    """The append-only triggers actually present on ``core_auditlog``.

    Asks the database rather than the migration history, because the question
    is whether the guarantee *holds*, not whether somebody once ran the
    migration that was supposed to establish it. A trigger dropped by hand
    leaves the migration recorded as applied.
    """
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'core_auditlog'::regclass AND NOT tgisinternal"
            )
        elif connection.vendor == "sqlite":
            cursor.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'core_auditlog'"
            )
        else:
            return set()
        return {row[0] for row in cursor.fetchall()}


def _trigger_migration_applied(connection) -> bool:
    """Whether ``core.0002_auditlog_append_only_triggers`` has run here."""
    from django.db.migrations.recorder import MigrationRecorder

    try:
        recorder = MigrationRecorder(connection)
        if not recorder.has_table():
            return False
        return recorder.migration_qs.filter(
            app="core", name="0002_auditlog_append_only_triggers"
        ).exists()
    except Exception:  # pragma: no cover - no database, or no permission
        return False


@register(Tags.database)
def check_auditlog_is_append_only(app_configs, databases=None, **kwargs):
    """Spec §11: no update or delete path to the audit log, anywhere.

    Three layers hold it inside Django and a fourth holds it in the storage
    engine (see the migration). This is the one that notices when the fourth
    has gone missing — which is the only one that can be removed without
    changing a line of code.
    """
    issues = []
    for alias in databases or []:
        connection = connections[alias]
        if connection.vendor not in {"postgresql", "sqlite"}:
            continue
        if not _trigger_migration_applied(connection):
            # Nothing to check yet. Database checks run during ``migrate`` too,
            # and a database that has not reached migration 0002 has no
            # triggers by definition — reporting that as a breach would make
            # ``migrate`` fail on every fresh install.
            continue
        try:
            present = installed_auditlog_triggers(connection)
        except Exception as exc:  # pragma: no cover - unreachable table/permission
            issues.append(
                Warning(
                    f"Could not verify the audit log's append-only triggers on "
                    f"{alias!r}: {exc}",
                    id="core.W020",
                )
            )
            continue

        missing = sorted(set(AUDITLOG_TRIGGERS) - present)
        if missing:
            issues.append(
                Error(
                    f"The audit log is not append-only in the database on "
                    f"{alias!r}: {missing} missing.",
                    hint="Spec §11 requires no update or delete path anywhere. "
                    "Run migrations; if they are already applied, somebody has "
                    "dropped the triggers by hand and that is worth asking about.",
                    id="core.E021",
                )
            )
    return issues
