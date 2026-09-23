"""`manage.py backup_database` backs up the receipts, not only the rows.

Proofs of transfer and thread attachments live in ``MEDIA_ROOT``, outside the
database. They are financial evidence, and a dump restored without them brings
back requests that point at files which no longer exist. ``pg_dump`` itself is
stubbed here: what is under test is what the command does around it.
"""

import os
import tarfile
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from apps.core.management.commands import backup_database
from apps.core.management.commands.backup_database import Command

POSTGRES = {"ENGINE": "django.db.backends.postgresql", "NAME": "maxpay"}


def fake_dump(_binary, _config, path):
    Path(path).write_bytes(b"PGDMP fake custom-format dump")


class BackupTestCase(SimpleTestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.media = self.root / "private_media"
        self.backups = self.root / "backups"

        (self.media / "attachments" / "2026" / "09").mkdir(parents=True)
        (self.media / "attachments" / "2026" / "09" / "receipt.png").write_bytes(b"\x89PNG proof")
        (self.media / "messages").mkdir()
        (self.media / "messages" / "note.pdf").write_bytes(b"%PDF note")

        patchers = [
            mock.patch.object(Command, "database_config", staticmethod(lambda: POSTGRES)),
            mock.patch.object(Command, "_dump", staticmethod(fake_dump)),
            mock.patch.object(backup_database.shutil, "which", return_value="/usr/bin/pg_dump"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def backup(self, **options):
        out = StringIO()
        with override_settings(MEDIA_ROOT=str(self.media), BACKUP_DIR=str(self.backups)):
            call_command("backup_database", stdout=out, **options)
        return out.getvalue()

    def archives(self):
        return sorted(self.backups.glob("*-media-*.tar.gz"))


class MediaArchiveTests(BackupTestCase):
    def test_every_dump_gets_a_media_archive_with_its_timestamp(self):
        self.backup()

        dumps = sorted(self.backups.glob("*.dump"))
        self.assertEqual(len(dumps), 1)
        self.assertEqual(len(self.archives()), 1)
        dump_stamp = dumps[0].name.removeprefix("maxpay-").removesuffix(".dump")
        self.assertEqual(self.archives()[0].name, f"maxpay-media-{dump_stamp}.tar.gz")

    def test_the_archive_holds_the_receipts_where_they_came_from(self):
        self.backup()

        with tarfile.open(self.archives()[0]) as archive:
            names = set(archive.getnames())
            self.assertIn("private_media/attachments/2026/09/receipt.png", names)
            self.assertIn("private_media/messages/note.pdf", names)
            receipt = archive.extractfile("private_media/attachments/2026/09/receipt.png")
            self.assertEqual(receipt.read(), b"\x89PNG proof")

    def test_it_restores_into_the_project_directory(self):
        """What the printed restore line does, done."""
        self.backup()
        restored = self.root / "restored"
        restored.mkdir()

        with tarfile.open(self.archives()[0]) as archive:
            archive.extractall(restored, filter="data")

        self.assertEqual(
            (restored / "private_media" / "messages" / "note.pdf").read_bytes(), b"%PDF note"
        )

    def test_the_restore_line_names_both_files(self):
        out = self.backup()
        self.assertIn("pg_restore", out)
        self.assertIn(f"tar -xzf {self.archives()[0].name}", out)

    def test_no_media_directory_still_leaves_a_pair(self):
        """An empty archive, rather than a dump that may or may not have one."""
        for child in sorted(self.media.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.media.rmdir()

        self.backup()

        with tarfile.open(self.archives()[0]) as archive:
            self.assertEqual(archive.getnames(), [])

    def test_an_archive_that_cannot_be_written_fails_the_command(self):
        with mock.patch.object(
            backup_database.tarfile, "open", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(CommandError) as caught:
                self.backup()

        self.assertIn("uploaded files were not backed up", str(caught.exception))
        self.assertEqual(self.archives(), [])
        # The dump is kept: it is still a good backup of the rows.
        self.assertEqual(len(list(self.backups.glob("*.dump"))), 1)


class PruneTests(BackupTestCase):
    def age(self, path, days):
        past = time.time() - days * 86400
        os.utime(path, (past, past))

    def test_old_archives_are_pruned_with_old_dumps(self):
        self.backups.mkdir()
        old_dump = self.backups / "maxpay-20260101T000000Z.dump"
        old_media = self.backups / "maxpay-media-20260101T000000Z.tar.gz"
        for path in (old_dump, old_media):
            path.write_bytes(b"old")
            self.age(path, 30)

        self.backup(retention_days=14)

        self.assertFalse(old_dump.exists())
        self.assertFalse(old_media.exists())
        self.assertEqual(len(self.archives()), 1)

    def test_other_files_in_the_directory_are_left_alone(self):
        self.backups.mkdir()
        unrelated = self.backups / "notes.tar.gz"
        unrelated.write_bytes(b"not ours")
        self.age(unrelated, 30)

        self.backup(retention_days=14)

        self.assertTrue(unrelated.exists())
