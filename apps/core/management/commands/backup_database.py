"""Take a database backup (spec §11) — build-order step 14.

Spec §11: *daily automated database backups with restore tested before go-live.*
The "automated" half is a scheduler's job and the "tested" half is a person's;
what belongs in the application is the one command both of them call, so that
the backup taken by cron and the backup taken by hand are the same backup.

What it does, and does not:

* It shells out to ``pg_dump`` in **custom** format (``-Fc``), which is what
  ``pg_restore`` wants and what allows a single table to be pulled back without
  replaying the whole dump.
* It writes to ``BACKUP_DIR`` with a UTC timestamp in the name, and prunes
  anything older than ``BACKUP_RETENTION_DAYS``. Pruning is part of the command
  because a backup job that fills the disk stops being a backup job.
* It **never** puts the password on the command line — ``PGPASSWORD`` goes in
  the subprocess environment, so it does not appear in ``ps`` output.
* It refuses SQLite rather than pretending. A file copy is not the same
  operation and quietly doing a different thing under the same name is how a
  restore fails at the worst moment.
* It also archives ``MEDIA_ROOT`` — ``private_media/`` — beside the dump, as
  ``<name>-media-<stamp>.tar.gz`` under the same timestamp. That is where the
  proofs of transfer and thread attachments live, and they are financial
  evidence: a dump restored without them brings back requests pointing at
  receipts that no longer exist. Pruned with the dumps, by the same age.

**It does not encrypt, and it does not ship the file anywhere.** A dump sitting
on the same disk as the database it came from is not a backup of anything that
takes the disk with it. Encryption at rest and off-host copying belong to
whatever the deployment already uses for that, and pointing ``BACKUP_DIR`` at a
mounted volume is the intended way to reach it. Named here because a reader has
to know it is missing rather than assume it is handled.
"""

import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: Nothing is ever written outside this, and it is created if absent.
DEFAULT_DIR = "backups"
DEFAULT_RETENTION_DAYS = 14

#: A dump that has not finished in this long has gone wrong; better to fail the
#: job loudly than to let a hung `pg_dump` hold the schedule open all night.
DEFAULT_TIMEOUT_SECONDS = 60 * 30


class Command(BaseCommand):
    help = (
        "Dump the configured PostgreSQL database, archive the uploaded files "
        "beside it, and prune old backups (spec §11)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Where to write. Defaults to settings.BACKUP_DIR.",
        )
        parser.add_argument(
            "--retention-days",
            type=int,
            default=None,
            help="Delete dumps older than this. Defaults to settings.BACKUP_RETENTION_DAYS.",
        )
        parser.add_argument(
            "--no-prune",
            action="store_true",
            help="Take the dump and leave older ones alone.",
        )

    def handle(self, *args, **options):
        config = self.database_config()
        engine = config.get("ENGINE", "")
        if "postgresql" not in engine:
            raise CommandError(
                f"backup_database only supports PostgreSQL; this deployment uses "
                f"{engine!r}. A SQLite file copy is a different operation and is "
                "not going to be given the same name."
            )

        binary = shutil.which("pg_dump")
        if binary is None:
            raise CommandError(
                "pg_dump is not on PATH. Install the PostgreSQL client tools on "
                "whichever host runs the backup schedule."
            )

        target = Path(
            options["output_dir"]
            or getattr(settings, "BACKUP_DIR", None)
            or Path(settings.BASE_DIR) / DEFAULT_DIR
        )
        target.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = config.get("NAME") or "maxpay"
        path = target / f"{name}-{stamp}.dump"

        self.stdout.write(f"Dumping {name} → {path}")
        self._dump(binary, config, path)

        size = path.stat().st_size
        if size == 0:
            # An empty file is worse than no file: it looks like a backup.
            path.unlink(missing_ok=True)
            raise CommandError("pg_dump produced an empty file; nothing was kept.")
        self.stdout.write(self.style.SUCCESS(f"Wrote {size:,} bytes."))

        media = self.archive_media(target, name, stamp)
        self.stdout.write(
            self.style.SUCCESS(
                f"Archived the uploaded files → {media} ({media.stat().st_size:,} bytes)."
            )
        )

        if not options["no_prune"]:
            days = (
                options["retention_days"]
                if options["retention_days"] is not None
                else int(getattr(settings, "BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
            )
            removed = self.prune(target, days)
            self.stdout.write(f"Pruned {removed} file(s) older than {days} day(s).")

        self.stdout.write(
            "Restore with:  pg_restore --clean --if-exists -d <database> "
            f"{path.name}\n"
            f"         and:  tar -xzf {media.name} -C <project directory>\n"
            "Spec §11 asks for that to have been tried, on a real dump, before "
            "go-live — not for it to be written down."
        )

    # -- pieces ------------------------------------------------------------

    @staticmethod
    def database_config() -> dict:
        return settings.DATABASES["default"]

    @staticmethod
    def archive_media(target: Path, name: str, stamp: str) -> Path:
        """Archive ``MEDIA_ROOT`` beside the dump, under the dump's timestamp.

        The archive holds the directory itself (``private_media/...``), so it is
        extracted into the project directory and lands where it came from. A
        missing directory still produces an archive — an empty one — so every
        dump has its pair and a restore never has to wonder whether one was
        skipped. A failure removes the partial file and fails the command: the
        dump is kept, but a job that lost the receipts must not report success.
        """
        media_root = Path(settings.MEDIA_ROOT)
        path = target / f"{name}-media-{stamp}.tar.gz"
        try:
            with tarfile.open(path, "w:gz") as archive:
                if media_root.is_dir():
                    archive.add(media_root, arcname=media_root.name)
        except OSError as exc:
            path.unlink(missing_ok=True)
            raise CommandError(
                f"Could not archive {media_root}: {exc}. The database dump was "
                "kept; the uploaded files were not backed up."
            ) from exc
        return path

    @staticmethod
    def _dump(binary: str, config: dict, path: Path) -> None:
        command = [
            binary,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--file={path}",
        ]
        if config.get("HOST"):
            command.append(f"--host={config['HOST']}")
        if config.get("PORT"):
            command.append(f"--port={config['PORT']}")
        if config.get("USER"):
            command.append(f"--username={config['USER']}")
        command.append(config.get("NAME", ""))

        environment = os.environ.copy()
        if config.get("PASSWORD"):
            # In the environment, never in argv: the command line of a running
            # process is world-readable on a normal Linux host.
            environment["PGPASSWORD"] = config["PASSWORD"]

        try:
            result = subprocess.run(  # noqa: S603 - fixed binary, no shell
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=int(
                    getattr(settings, "BACKUP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
                ),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            path.unlink(missing_ok=True)
            raise CommandError(f"pg_dump timed out after {exc.timeout}s.") from exc

        if result.returncode != 0:
            path.unlink(missing_ok=True)
            raise CommandError(
                f"pg_dump exited {result.returncode}: {result.stderr.strip()}"
            )

    @staticmethod
    def prune(directory: Path, days: int) -> int:
        """Delete dumps and media archives older than ``days``. Zero or less
        keeps everything."""
        if days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = 0
        candidates = [*directory.glob("*.dump"), *directory.glob("*-media-*.tar.gz")]
        for candidate in candidates:
            modified = datetime.fromtimestamp(
                candidate.stat().st_mtime, tz=UTC
            )
            if modified < cutoff:
                candidate.unlink()
                removed += 1
        return removed
