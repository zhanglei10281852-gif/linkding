import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from unittest import TestCase

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from bookmarks.services import backups


class FullBackupTestCase(TestCase):
    """
    Tests the online full backup against a standalone on-disk SQLite
    database, so the snapshot does not depend on Django's test database.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "db.sqlite3")
        self.folders = {
            directory: os.path.join(self.temp_dir, directory)
            for directory in backups.DIRECTORIES
        }
        for folder in self.folders.values():
            os.makedirs(folder)

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            "CREATE TABLE bookmarks_bookmarkasset("
            "file TEXT NOT NULL DEFAULT '')"
        )
        self.conn.execute(
            "CREATE TABLE bookmarks_bookmark("
            "favicon_file TEXT NOT NULL DEFAULT '', "
            "preview_image_file TEXT NOT NULL DEFAULT '')"
        )
        self.conn.commit()

        self.lock_path = os.path.join(self.temp_dir, ".backup.lock")
        self.backup_path = os.path.join(self.temp_dir, "backup.zip")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def add_file(self, directory, name, content=b"test-content"):
        with open(os.path.join(self.folders[directory], name), "wb") as file:
            file.write(content)

    def create_backup(self):
        return backups.create_full_backup(
            backup_file=self.backup_path,
            db_path=self.db_path,
            folders=self.folders,
            lock_path=self.lock_path,
        )

    def zip_names(self):
        with zipfile.ZipFile(self.backup_path) as zip_file:
            return set(zip_file.namelist())

    def test_creates_backup_with_existing_layout(self):
        self.conn.execute(
            "INSERT INTO bookmarks_bookmarkasset(file) VALUES (?)",
            ("snapshot_2024.html.gz",),
        )
        self.conn.execute(
            "INSERT INTO bookmarks_bookmark(favicon_file, preview_image_file)"
            " VALUES (?, ?)",
            ("https_example_com.png", "abc123.jpg"),
        )
        self.conn.commit()
        self.add_file("assets", "snapshot_2024.html.gz", b"<html/>")
        self.add_file("favicons", "https_example_com.png", b"png-data")
        self.add_file("previews", "abc123.jpg", b"jpg-data")

        self.create_backup()

        self.assertEqual(
            self.zip_names(),
            {
                "db.sqlite3",
                "assets/snapshot_2024.html.gz",
                "favicons/https_example_com.png",
                "previews/abc123.jpg",
                "backup-manifest.json",
            },
        )

        with zipfile.ZipFile(self.backup_path) as zip_file:
            manifest = json.loads(zip_file.read("backup-manifest.json"))
        self.assertEqual(manifest["format"], backups.FORMAT_NAME)
        self.assertEqual(manifest["format_version"], backups.FORMAT_VERSION)
        self.assertEqual(len(manifest["files"]), 3)

        summary = backups.verify_backup(self.backup_path)
        self.assertFalse(summary["legacy"])
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["files"], 3)

    def test_manifest_contains_sizes_and_digests(self):
        content = b"hello-digest"
        self.conn.execute(
            "INSERT INTO bookmarks_bookmarkasset(file) VALUES (?)",
            ("upload_x.html.gz",),
        )
        self.conn.commit()
        self.add_file("assets", "upload_x.html.gz", content)

        self.create_backup()

        with zipfile.ZipFile(self.backup_path) as zip_file:
            manifest = json.loads(zip_file.read("backup-manifest.json"))
        entry = next(
            entry for entry in manifest["files"] if entry["path"] == "assets/upload_x.html.gz"
        )
        self.assertEqual(entry["size"], len(content))
        self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue(manifest["database"]["sha256"])

    def test_excludes_unreferenced_files(self):
        # Files on disk that are not referenced by the database must not end
        # up in the backup, so that no orphan files are restored.
        self.add_file("assets", "inflight.tmp", b"partial")
        self.add_file("favicons", "stale_icon.png", b"old")
        self.add_file("previews", "leftover.jpg", b"old")

        self.create_backup()

        self.assertEqual(
            self.zip_names(), {"db.sqlite3", "backup-manifest.json"}
        )

    def test_change_falls_completely_before_or_after_view(self):
        # File written but not yet referenced: view does not contain the change
        self.add_file("favicons", "new.png", b"png")
        self.create_backup()
        self.assertNotIn("favicons/new.png", self.zip_names())

        # Database reference committed: next view contains the complete change
        self.conn.execute(
            "INSERT INTO bookmarks_bookmark(favicon_file) VALUES (?)",
            ("new.png",),
        )
        self.conn.commit()
        self.create_backup()
        self.assertIn("favicons/new.png", self.zip_names())

    def test_missing_referenced_file_fails_and_cleans_up(self):
        self.conn.execute(
            "INSERT INTO bookmarks_bookmarkasset(file) VALUES (?)",
            ("missing.html.gz",),
        )
        self.conn.commit()

        with self.assertRaises(backups.BackupError):
            self.create_backup()

        # No half-finished backup or temp artifacts are left behind
        self.assertFalse(os.path.exists(self.backup_path))
        leftovers = [
            name
            for name in os.listdir(self.temp_dir)
            if name != ".backup.lock"
            and (
                name.startswith(backups.STAGING_PREFIX)
                or name.endswith(backups.TEMP_ZIP_SUFFIX)
            )
        ]
        self.assertEqual(leftovers, [])

        # After resolving the inconsistency, running again succeeds
        self.conn.execute("DELETE FROM bookmarks_bookmarkasset")
        self.conn.commit()
        self.create_backup()
        self.assertTrue(os.path.isfile(self.backup_path))

    def test_existing_backup_is_preserved_on_failure(self):
        with open(self.backup_path, "wb") as file:
            file.write(b"valid-existing-backup")

        self.conn.execute(
            "INSERT INTO bookmarks_bookmarkasset(file) VALUES (?)",
            ("missing.html.gz",),
        )
        self.conn.commit()

        with self.assertRaises(backups.BackupError):
            self.create_backup()

        with open(self.backup_path, "rb") as file:
            self.assertEqual(file.read(), b"valid-existing-backup")

    def test_missing_folder_does_not_break_backup(self):
        shutil.rmtree(self.folders["favicons"])

        self.create_backup()

        self.assertEqual(
            self.zip_names(), {"db.sqlite3", "backup-manifest.json"}
        )

    def test_lock_blocks_while_held(self):
        result = {}

        def try_lock():
            try:
                with backups.data_change_lock(self.lock_path, timeout=0.2):
                    result["acquired"] = True
            except backups.BackupLockTimeout:
                result["timed_out"] = True

        with backups.data_change_lock(self.lock_path):
            thread = threading.Thread(target=try_lock)
            thread.start()
            thread.join()
        self.assertTrue(result.get("timed_out"))
        self.assertNotIn("acquired", result)

        # Once released, the lock can be acquired
        result.clear()
        thread = threading.Thread(target=try_lock)
        thread.start()
        thread.join()
        self.assertTrue(result.get("acquired"))

    def test_lock_is_reentrant_in_same_thread(self):
        with (
            backups.data_change_lock(self.lock_path),
            backups.data_change_lock(self.lock_path),
        ):
            pass  # must not deadlock

    def test_legacy_backup_remains_compatible(self):
        legacy_path = os.path.join(self.temp_dir, "legacy.zip")
        with zipfile.ZipFile(legacy_path, "w") as zip_file:
            zip_file.writestr("db.sqlite3", b"sqlite-bytes")
            zip_file.writestr("assets/old_snapshot.html.gz", b"old")

        self.assertIsNone(backups.read_backup_manifest(legacy_path))

        summary = backups.verify_backup(legacy_path)
        self.assertTrue(summary["legacy"])
        self.assertEqual(summary["format_version"], backups.LEGACY_FORMAT_VERSION)
        self.assertEqual(summary["files"], 1)

    def test_verify_rejects_corrupt_backup(self):
        bad_path = os.path.join(self.temp_dir, "bad.zip")
        with open(bad_path, "wb") as file:
            file.write(b"not a zip file")

        with self.assertRaises(backups.BackupIntegrityError):
            backups.verify_backup(bad_path)


class FullBackupCommandTestCase(TestCase):
    def test_rejects_non_sqlite_database(self):
        with (
            override_settings(USE_SQLITE=False),
            self.assertRaises(CommandError),
        ):
            call_command("full_backup", "backup.zip")

    def test_passes_arguments_from_settings_to_service(self):
        from unittest.mock import patch

        with (
            patch(
                "bookmarks.management.commands.full_backup.backups.create_full_backup"
            ) as mocked,
            patch(
                "bookmarks.management.commands.full_backup.backups.is_memory_db_name",
                return_value=False,
            ),
        ):
            call_command("full_backup", "backup.zip")

        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["backup_file"], "backup.zip")
        self.assertIn("assets", kwargs["folders"])
        self.assertIn("favicons", kwargs["folders"])
        self.assertIn("previews", kwargs["folders"])

    def test_rejects_in_memory_database(self):
        from unittest.mock import patch

        with (
            patch(
                "bookmarks.management.commands.full_backup.backups.is_memory_db_name",
                return_value=True,
            ),
            self.assertRaises(CommandError),
        ):
            call_command("full_backup", "backup.zip")

    def test_command_failure_raises_command_error(self):
        from unittest.mock import patch

        with (
            patch(
                "bookmarks.management.commands.full_backup.backups.create_full_backup",
                side_effect=backups.BackupError("boom"),
            ),
            patch(
                "bookmarks.management.commands.full_backup.backups.is_memory_db_name",
                return_value=False,
            ),
            self.assertRaises(CommandError),
        ):
            call_command("full_backup", "backup.zip")
