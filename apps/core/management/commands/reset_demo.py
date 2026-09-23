"""Empty a local database of demo traffic without emptying the accounts.

Development tooling, not part of the product, and the companion to
``seed_demo``: that one fills a database, this one clears it back out so the
next click-through starts from nothing. Deleting the file and migrating again
does the same job, but it also takes the internal users, their roles and their
enrolled second factors with it — and re-enrolling a TOTP device by hand is the
slowest part of setting this project up.

So the split is:

**Cleared**   requests, their messages, attachments and read markers, the audit
              log, the merchant network (merchants, their methods and wallets),
              the payment methods, and the client records B2CORE created.

**Kept**      internal users, their roles and group memberships, their TOTP
              devices, the exchange rates, and the business-hours settings.

``--keep-merchants`` narrows it to the traffic alone: requests, messages,
attachments, read markers and the audit log go; the network and the clients
stay. That is the one to reach for between two run-throughs of the same demo.

**It refuses to run unless ``DEBUG`` is on**, for the same reason ``seed_demo``
does. That command creates accounts with known passwords, which is only safe on
a laptop; this one deletes a merchant network and an audit log, which is only
safe in the same place. The guard is the first thing either does, before it
reads a single argument.

**And DEBUG is not enough on its own.** It is one wrong line in ``.env`` away
from being on where it must not be, so two more guards stand behind it, neither
of which reads DEBUG:

* **Not on PostgreSQL, ever.** PostgreSQL is what production runs; the demo
  runs on SQLite. A reset that finds itself on PostgreSQL is on the wrong
  database, whatever else the settings say.
* **Not without ``ALLOW_DEMO_RESET=true``,** set by name. Off by default, and
  nothing else turns it on.

**The audit log is append-only in the database** (spec §11), by triggers that
refuse ``UPDATE``, ``DELETE`` and — on PostgreSQL — ``TRUNCATE``. Clearing it
therefore means dropping those triggers and putting them back, which this does
by calling the migration's own ``backward`` and ``forward`` rather than keeping
a second copy of the SQL that could drift from it. The restore runs in a
``finally``: a failure part way through must not leave a database whose audit
log is writable. The command checks the triggers are back before it reports
success, and says so out loud.

    python manage.py reset_demo                   # everything below the accounts
    python manage.py reset_demo --keep-merchants  # the traffic only
    python manage.py reset_demo --no-input        # for a script; skips the prompt
"""

import importlib

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction

from apps.accounts.models import Client
from apps.core.checks import AUDITLOG_TRIGGERS, installed_auditlog_triggers
from apps.core.models import AuditLog
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.transactions.models import Attachment, Message, Request, RequestRead

#: The migration that owns the append-only triggers. Imported by name because
#: the module starts with a digit and cannot be written as an import statement —
#: and imported at all so the SQL has exactly one definition in the tree.
TRIGGER_MIGRATION = "apps.core.migrations.0002_auditlog_append_only_triggers"


class _Executor:
    """Just enough of a schema editor for the trigger migration's own SQL.

    ``forward`` and ``backward`` ask for two things: ``.connection.vendor``, to
    pick the dialect, and ``.execute(sql)``. Django's real schema editor does a
    great deal more, and on SQLite it refuses to open inside a transaction at
    all — it insists on disabling foreign-key checks first, which SQLite cannot
    do mid-statement. None of that is wanted here; running the statement is.
    """

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        with self.connection.cursor() as cursor:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)

#: What the operator has to type. Short enough to type without copy-paste,
#: specific enough that it cannot be a stray newline in a terminal.
CONFIRMATION = "reset"

#: Deletion order, innermost first. Django would cascade most of this on its
#: own, but doing it explicitly is what lets the command *count* each kind
#: before it touches anything, and a plan that says "12 attachments" is a plan
#: somebody can read before agreeing to it.
#:
#: ``AuditLog`` is absent on purpose — it is not deletable through the ORM and
#: is handled by :meth:`Command.clear_audit_log`.
TRAFFIC = [
    ("المرفقات", Attachment),
    ("الرسائل", Message),
    ("علامات القراءة", RequestRead),
    ("الطلبات", Request),
]

NETWORK = [
    ("المحافظ", Wallet),
    ("ربط التاجر بالطريقة", MerchantMethod),
    ("التجار", Merchant),
    ("طرق الدفع", PaymentMethod),
    ("العملاء", Client),
]

def kept_models():
    """Models whose rows survive, printed so the operator sees what they are
    *not* agreeing to lose.

    Resolved when called rather than at import: pulling the OTP plugin's model
    in at module scope reaches the app registry before it is ready.
    """
    from django.contrib.auth.models import Group
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts.models import User
    from apps.core.models import SystemSettings
    from apps.rates.models import ExchangeRate

    return [
        ("المستخدمون الداخليون", User),
        ("المجموعات والأدوار", Group),
        ("أجهزة المصادقة الثنائية", TOTPDevice),
        ("أسعار الصرف", ExchangeRate),
        ("إعدادات النظام", SystemSettings),
    ]


class Command(BaseCommand):
    help = (
        "Clear demo requests and the merchant network, keeping internal users, "
        "their 2FA and the exchange rates. Refuses unless DEBUG and "
        "ALLOW_DEMO_RESET=true, and always on PostgreSQL."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-merchants",
            action="store_true",
            help=(
                "Clear the traffic only — requests, messages, attachments, read "
                "markers and the audit log. Merchants, wallets, payment methods "
                "and clients stay."
            ),
        )
        parser.add_argument(
            "--no-input",
            "--noinput",
            action="store_true",
            dest="no_input",
            help="Skip the typed confirmation. For scripts and the test suite.",
        )

    # -- the guard ---------------------------------------------------------

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "reset_demo refuses to run with DEBUG off. It deletes requests, "
                "a merchant network and the audit log; that is only ever "
                "acceptable on a development machine."
            )
        # Independent of DEBUG on purpose: each of these holds when DEBUG has
        # been switched on somewhere it should not have been.
        if connection.vendor == "postgresql":
            raise CommandError(
                "reset_demo refuses to run on PostgreSQL. That is the production "
                "engine; the demo runs on SQLite. Whatever DEBUG says, a reset "
                "that finds itself here is pointed at the wrong database."
            )
        if not getattr(settings, "ALLOW_DEMO_RESET", False):
            raise CommandError(
                "reset_demo refuses to run without ALLOW_DEMO_RESET=true. Set it "
                "in the .env of a development machine, and nowhere else."
            )

        keep_merchants = options["keep_merchants"]
        groups = list(TRAFFIC) if keep_merchants else list(TRAFFIC) + list(NETWORK)

        counts = [(label, model, model.objects.count()) for label, model in groups]
        audit_count = AuditLog.objects.count()
        total = sum(count for _, _, count in counts) + audit_count

        self.report_plan(counts, audit_count, keep_merchants)

        if total == 0:
            self.stdout.write(self.style.SUCCESS("\nلا شيء لمسحه. القاعدة نظيفة أصلًا."))
            return

        if not options["no_input"] and not self.confirmed():
            self.stdout.write(self.style.WARNING("أُلغي. لم يُمسّ شيء."))
            return

        self.run_reset(counts, keep_merchants)

    # -- saying what will happen, before it happens -------------------------

    def report_plan(self, counts, audit_count, keep_merchants):
        self.stdout.write(self.style.MIGRATE_HEADING("\nسيُمسح:"))
        for label, _model, count in counts:
            style = self.style.WARNING if count else self.style.SUCCESS
            self.stdout.write(f"  {label:<28} {style(str(count))}")
        style = self.style.WARNING if audit_count else self.style.SUCCESS
        self.stdout.write(f"  {'سجل التدقيق':<28} {style(str(audit_count))}")

        self.stdout.write(self.style.MIGRATE_HEADING("\nسيبقى:"))
        for label, model in kept_models():
            self.stdout.write(f"  {label:<28} {model.objects.count()}")
        if keep_merchants:
            for label, model in NETWORK:
                self.stdout.write(f"  {label:<28} {model.objects.count()}")

        if keep_merchants:
            self.stdout.write(
                "\n--keep-merchants: الشبكة والعملاء يبقون، وتُمسح حركة الطلبات وحدها."
            )

    def confirmed(self) -> bool:
        self.stdout.write(
            f"\nاكتب «{CONFIRMATION}» للمتابعة، أو أي شيء آخر للإلغاء: ", ending=""
        )
        try:
            answer = input()
        except (EOFError, KeyboardInterrupt):
            self.stdout.write("")
            return False
        return answer.strip() == CONFIRMATION

    # -- doing it ----------------------------------------------------------

    def run_reset(self, counts, keep_merchants):
        models = [model for _, model, _ in counts]

        # Files first and outside the transaction: a rolled-back delete can put
        # a row back, and nothing can put a file back. Better an orphaned file
        # on a laptop than a row pointing at one that is gone.
        removed_files = self.delete_files(models)

        with transaction.atomic():
            for _label, model, _count in counts:
                model.objects.all().delete()
            audit_deleted = self.clear_audit_log()
            self.reset_sequences(models + [AuditLog])

        self.report_done(counts, audit_deleted, removed_files, keep_merchants)

    def delete_files(self, models) -> int:
        """Drop the uploads and images the rows point at.

        ``QuerySet.delete()`` does not touch storage — Django stopped doing that
        in 1.3 — so a reset that only cleared rows would leave ``private_media``
        growing across every run.
        """
        removed = 0
        file_fields = {
            Attachment: ["file"],
            PaymentMethod: ["icon"],
            Wallet: ["qr_image"],
        }
        for model, names in file_fields.items():
            if model not in models:
                continue
            for row in model.objects.all():
                for name in names:
                    stored = getattr(row, name, None)
                    if stored:
                        stored.delete(save=False)
                        removed += 1
        return removed

    def clear_audit_log(self) -> int:
        """Empty ``core_auditlog``, triggers down and back up again.

        Spec §11 makes this table append-only in the storage engine, which is
        the point of it and is also why a reset cannot simply call ``delete()``.
        The triggers come off, the rows go, and the triggers go back on in a
        ``finally`` so no failure in between can leave the guarantee off.
        """
        migration = importlib.import_module(TRIGGER_MIGRATION)
        count = AuditLog.objects.count()
        editor = _Executor(connection)

        migration.backward(None, editor)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {AuditLog._meta.db_table}")
        finally:
            migration.forward(None, editor)

        missing = set(AUDITLOG_TRIGGERS) - installed_auditlog_triggers(connection)
        if missing:
            raise CommandError(
                "The audit log's append-only triggers were not restored: "
                f"{', '.join(sorted(missing))}. The table is writable. Re-run "
                "`manage.py migrate core 0001` then `manage.py migrate core` "
                "to reinstall them before doing anything else."
            )
        return count

    def reset_sequences(self, models):
        """Send the emptied tables' auto-increment counters back to 1.

        So the next run-through numbers its rows from the start instead of
        carrying on from wherever the last one stopped — which is the whole
        point of a reset that is not a fresh database.

        Note this is the *primary key* counter. ``Request.public_ref`` — the
        reference a merchant quotes — is five random digits by deliberate
        design, so that the reference leaks no volume information to merchants
        (see ``apps.transactions.models.generate_public_ref``). There is no
        counter behind it to reset, and giving it one would undo that.
        """
        statements = connection.ops.sequence_reset_by_name_sql(
            no_style(), connection.introspection.sequence_list()
        )
        wanted = {model._meta.db_table for model in models}
        with connection.cursor() as cursor:
            for statement in statements:
                if any(table in statement for table in wanted):
                    cursor.execute(statement)
            # PostgreSQL keeps its sequences outside the table, so the generic
            # list above is the wrong instrument there; ask for them by model.
            if connection.vendor == "postgresql":
                for statement in connection.ops.sequence_reset_sql(no_style(), models):
                    cursor.execute(statement)

    # -- saying what happened ----------------------------------------------

    def report_done(self, counts, audit_deleted, removed_files, keep_merchants):
        self.stdout.write(self.style.MIGRATE_HEADING("\nمُسح:"))
        for label, _model, count in counts:
            self.stdout.write(f"  {label:<28} {count}")
        self.stdout.write(f"  {'سجل التدقيق':<28} {audit_deleted}")
        if removed_files:
            self.stdout.write(f"  {'ملفات على القرص':<28} {removed_files}")

        self.stdout.write(
            "\n"
            + self.style.SUCCESS(
                "مُسحت مؤقتات سجل التدقيق وأُعيدت؛ الجدول ما يزال للإضافة فقط."
            )
        )
        self.stdout.write("عدّادات المفاتيح الأساسية أُعيدت إلى 1.")
        self.stdout.write(
            "المرجع العلني (public_ref) عشوائي بالتصميم ولا عدّاد له — "
            "وهو ما يمنع التاجر من استنتاج حجم الحركة."
        )

        if keep_merchants:
            self.stdout.write("\nالشبكة والعملاء كما هم. شغّل الطلبات من جديد.")
        else:
            self.stdout.write(
                "\n" + self.style.WARNING("شغّل `manage.py seed_demo` لإعادة البناء.")
            )
