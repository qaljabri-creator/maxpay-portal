"""Make the audit log append-only in the database, not only in Python.

Spec §11: *the audit log is append-only, with no delete or update path exposed
anywhere in the application.* Until this migration that held in three places
and all three were inside Django:

* :class:`apps.core.models.AppendOnlyModel` refuses a second ``save()`` and any
  ``delete()``;
* ``default_permissions = ("add", "view")`` means the ``change`` and ``delete``
  permissions do not exist to be granted;
* nothing in the URLconf routes a write.

Each of those is bypassable from a Django shell — ``QuerySet.update()`` and
``QuerySet.delete()`` never call ``Model.save()`` or ``Model.delete()`` — and
all of them are bypassable from ``psql``. That is the gap build-order step 14
closes: after this migration the guarantee is enforced by the storage engine,
so the only way to alter an audit row is for somebody with schema rights to
drop these triggers first, which is itself an act nobody performs by accident.

``TRUNCATE`` is covered too on PostgreSQL, because it is the one destructive
statement that fires no row-level trigger and is exactly what a "let me just
clear this table" would reach for.

**Both backends are handled.** PostgreSQL is what spec §Stack targets; SQLite is
what the suite smoke-tests on, and a guarantee that is only tested on the
backend nobody runs is not tested.
"""

from django.db import migrations

TABLE = "core_auditlog"
MESSAGE = "core_auditlog is append-only (spec 11); rows may only be inserted"

POSTGRES_FORWARD = f"""
CREATE OR REPLACE FUNCTION maxpay_auditlog_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{MESSAGE}: % refused', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS maxpay_auditlog_no_update ON {TABLE};
CREATE TRIGGER maxpay_auditlog_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION maxpay_auditlog_append_only();

DROP TRIGGER IF EXISTS maxpay_auditlog_no_delete ON {TABLE};
CREATE TRIGGER maxpay_auditlog_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION maxpay_auditlog_append_only();

DROP TRIGGER IF EXISTS maxpay_auditlog_no_truncate ON {TABLE};
CREATE TRIGGER maxpay_auditlog_no_truncate
    BEFORE TRUNCATE ON {TABLE}
    FOR EACH STATEMENT EXECUTE FUNCTION maxpay_auditlog_append_only();
"""

POSTGRES_REVERSE = f"""
DROP TRIGGER IF EXISTS maxpay_auditlog_no_truncate ON {TABLE};
DROP TRIGGER IF EXISTS maxpay_auditlog_no_delete ON {TABLE};
DROP TRIGGER IF EXISTS maxpay_auditlog_no_update ON {TABLE};
DROP FUNCTION IF EXISTS maxpay_auditlog_append_only();
"""

# SQLite has no TRUNCATE and no stored functions, so the message is repeated
# rather than shared. RAISE(ABORT) rolls the statement back and surfaces as an
# IntegrityError through Django's SQLite backend.
SQLITE_FORWARD = [
    "DROP TRIGGER IF EXISTS maxpay_auditlog_no_update;",
    f"""
    CREATE TRIGGER maxpay_auditlog_no_update BEFORE UPDATE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, '{MESSAGE}: UPDATE refused');
    END;
    """,
    "DROP TRIGGER IF EXISTS maxpay_auditlog_no_delete;",
    f"""
    CREATE TRIGGER maxpay_auditlog_no_delete BEFORE DELETE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, '{MESSAGE}: DELETE refused');
    END;
    """,
]

SQLITE_REVERSE = [
    "DROP TRIGGER IF EXISTS maxpay_auditlog_no_delete;",
    "DROP TRIGGER IF EXISTS maxpay_auditlog_no_update;",
]


def _run(schema_editor, postgres: str, sqlite: list[str]) -> None:
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(postgres)
    elif vendor == "sqlite":
        for statement in sqlite:
            schema_editor.execute(statement)
    # Any other backend is unsupported by settings.base._database_from_url, so
    # reaching here means somebody added one. Say nothing rather than pretend
    # the guarantee holds: the Python-level one still does, and the deploy
    # check in apps/core/checks.py reports the missing trigger out loud.


def forward(_apps, schema_editor):
    _run(schema_editor, POSTGRES_FORWARD, SQLITE_FORWARD)


def backward(_apps, schema_editor):
    _run(schema_editor, POSTGRES_REVERSE, SQLITE_REVERSE)


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.RunPython(forward, backward, elidable=False),
    ]
