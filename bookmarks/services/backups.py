"""
Online full backup of the linkding data folder.

A published backup zip represents a single, recoverable view of the SQLite
database together with the assets, favicons and previews folders:

* During the short snapshot phase an exclusive, cross-process data change lock
  is held, so every file/DB change (uploads, deletes, background generation)
  finishes completely before the snapshot or starts after it. The database is
  copied with SQLite's online backup API and the referenced files are captured
  into a staging folder via hard links (with a file-copy fallback).
* The archive membership is derived from the database snapshot: only files
  referenced by the snapshot are archived, which also prevents orphan files
  from ending up in the backup.
* The slow operations (compression, zip creation, verification) run without
  the lock, so production writes are only blocked briefly.
* The zip is built at a temporary location next to the target and published
  with an atomic replace. Any failure leaves the previous backup (if any) and
  the data folder untouched.
* A manifest with sizes and SHA-256 digests is stored in the zip and a full
  self-verification is performed before the backup is published.

Backups created by older linkding versions (same zip layout, no manifest)
remain readable.
"""

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime

FORMAT_NAME = "linkding-full-backup"
FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1

MANIFEST_FILENAME = "backup-manifest.json"
DATABASE_FILENAME = "db.sqlite3"
LOCK_FILENAME = ".backup.lock"
STAGING_PREFIX = ".full-backup-staging-"
TEMP_ZIP_SUFFIX = ".zip.tmp"

DIRECTORIES = ("assets", "favicons", "previews")

# Queries that return the files referenced by a database snapshot,
# per directory name.
MEMBER_QUERIES = {
    "assets": "SELECT DISTINCT file FROM bookmarks_bookmarkasset WHERE file <> ''",
    "favicons": "SELECT DISTINCT favicon_file FROM bookmarks_bookmark"
    " WHERE favicon_file <> ''",
    "previews": "SELECT DISTINCT preview_image_file FROM bookmarks_bookmark"
    " WHERE preview_image_file <> ''",
}

DEFAULT_LOCK_TIMEOUT = 30
HASH_CHUNK_SIZE = 1024 * 1024


class BackupError(Exception):
    pass


class BackupIntegrityError(BackupError):
    pass


class BackupLockTimeout(BackupError):
    pass


def is_memory_db_name(name: str) -> bool:
    return (
        not name
        or name == ":memory:"
        or name.startswith("file::memory:")
        or name.startswith("file:memorydb_")
    )


def _default_lock_path() -> str | None:
    from django.db import connections

    name = connections["default"].settings_dict.get("NAME", "")
    if is_memory_db_name(name):
        # No on-disk database to coordinate; locking is a no-op. This only
        # occurs in test setups, never in a supported deployment.
        return None
    return os.path.join(os.path.dirname(os.path.abspath(name)), LOCK_FILENAME)


def _validate_member_name(name: str):
    if (
        not name
        or "/" in name
        or "\\" in name
        or name in (".", "..")
        or os.path.isabs(name)
    ):
        raise BackupError(f"Invalid backup member name: {name!r}")


# ---------------------------------------------------------------------------
# Cross-process data change lock
# ---------------------------------------------------------------------------

if os.name == "nt":  # Windows
    import msvcrt

    def _lock_nonblocking(fd: int):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # POSIX
    import fcntl

    def _lock_nonblocking(fd: int):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int):
        fcntl.flock(fd, fcntl.LOCK_UN)


_lock_state = threading.local()


@contextmanager
def data_change_lock(lock_path: str = None, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """
    Exclusive cross-process lock that coordinates data file/DB changes with
    backups. Reentrant within the same thread. The underlying OS lock is
    released automatically if a process crashes, so there are no stale locks.
    """
    path = lock_path or _default_lock_path()

    if path is None:
        # In-memory database: nothing to coordinate, behave as a no-op.
        yield
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)

    fd = getattr(_lock_state, "fd", None)
    count = getattr(_lock_state, "count", 0)
    if fd is not None:
        # Same thread already holds the lock.
        _lock_state.count = count + 1
        try:
            yield
        finally:
            _lock_state.count -= 1
        return

    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            _lock_nonblocking(fd)
            acquired = True
            break
        except OSError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

    if not acquired:
        os.close(fd)
        raise BackupLockTimeout(
            f"Timed out after {timeout}s waiting for the data change lock: {path}"
        )

    _lock_state.fd = fd
    _lock_state.count = 1
    try:
        yield
    finally:
        _lock_state.count = 0
        _lock_state.fd = None
        with contextlib.suppress(OSError):
            _unlock(fd)
        os.close(fd)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cleanup_stale_artifacts(target_file: str, staging_base: str):
    # Remove staging directories from crashed previous runs.
    try:
        entries = os.listdir(staging_base)
    except FileNotFoundError:
        entries = []
    for entry in entries:
        if entry.startswith(STAGING_PREFIX):
            shutil.rmtree(os.path.join(staging_base, entry), ignore_errors=True)

    # Remove temporary zip files in the target directory.
    target_dir = os.path.dirname(target_file) or "."
    target_base = os.path.basename(target_file)
    prefix = f".{target_base}."
    try:
        entries = os.listdir(target_dir)
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.startswith(prefix) and entry.endswith(TEMP_ZIP_SUFFIX):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(target_dir, entry))


def _referenced_members(db_path: str) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    conn = sqlite3.connect(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            detail = "no result" if integrity is None else str(integrity[0])
            raise BackupIntegrityError(
                f"Database integrity check failed: {detail}"
            )
        for directory in DIRECTORIES:
            rows = conn.execute(MEMBER_QUERIES[directory]).fetchall()
            names = [row[0] for row in rows if row[0]]
            for name in names:
                _validate_member_name(name)
            members[directory] = sorted(set(names))
    finally:
        conn.close()
    return members


def _capture_files(
    members: dict[str, list[str]],
    folders: dict[str, str],
    staging_dir: str,
):
    missing = []
    for directory in DIRECTORIES:
        staged_directory = os.path.join(staging_dir, directory)
        os.makedirs(staged_directory, exist_ok=True)
        source_directory = folders[directory]
        for name in members[directory]:
            source_path = os.path.join(source_directory, name)
            staged_path = os.path.join(staged_directory, name)
            if not os.path.isfile(source_path):
                missing.append(f"{directory}/{name}")
                continue
            try:
                # Hard links are instant and only work on the same volume.
                os.link(source_path, staged_path)
            except OSError:
                try:
                    shutil.copyfile(source_path, staged_path)
                except OSError as error:
                    raise BackupError(
                        f"Failed to copy {directory}/{name}: {error}"
                    ) from error
    if missing:
        raise BackupError(
            "Files referenced by the database are missing on disk: " + ", ".join(missing)
        )


def _staged_files(staging_dir: str) -> dict[str, list[str]]:
    result = {}
    for directory in DIRECTORIES:
        path = os.path.join(staging_dir, directory)
        try:
            result[directory] = sorted(os.listdir(path))
        except FileNotFoundError:
            result[directory] = []
    return result


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------


def create_full_backup(
    backup_file: str,
    db_path: str,
    folders: dict[str, str],
    lock_path: str = None,
    progress=None,
) -> str:
    """
    Creates a full backup zip at backup_file.

    :param backup_file: destination zip path
    :param db_path: path to the source SQLite database file
    :param folders: map of directory name ("assets", "favicons", "previews")
        to the source folder path
    :param lock_path: optional path to the coordination lock file
    :param progress: optional callable that receives progress messages
    :return: the published backup file path
    """

    def log(message):
        if progress:
            progress(message)

    backup_file = os.path.abspath(backup_file)
    db_path = os.path.abspath(db_path)
    if not os.path.isfile(db_path):
        raise BackupError(f"Database file not found: {db_path}")

    lock_path = os.path.abspath(lock_path or _default_lock_path_from_db(db_path))
    staging_base = os.path.dirname(lock_path)

    target_dir = os.path.dirname(backup_file) or "."
    os.makedirs(target_dir, exist_ok=True)

    _cleanup_stale_artifacts(backup_file, staging_base)

    staging_dir = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=staging_base)
    fd, temp_zip_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(backup_file)}.",
        suffix=TEMP_ZIP_SUFFIX,
        dir=target_dir,
    )
    os.close(fd)

    published = False
    try:
        # --- Short snapshot phase: writers are blocked --------------------
        with data_change_lock(lock_path):
            log("Create database backup...")
            staged_db_path = os.path.join(staging_dir, DATABASE_FILENAME)
            _backup_database(db_path, staged_db_path, log)

            members = _referenced_members(staged_db_path)
            log(
                f"Snapshot {sum(len(names) for names in members.values())} files"
                " from assets, favicons and previews..."
            )
            _capture_files(members, folders, staging_dir)

        # --- Long build phase: writers can continue ----------------------
        manifest = _build_manifest(staging_dir)

        log("Create backup archive...")
        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.write(
                os.path.join(staging_dir, DATABASE_FILENAME), DATABASE_FILENAME
            )
            for entry in manifest["files"]:
                directory, name = entry["path"].split("/", 1)
                zip_file.write(
                    os.path.join(staging_dir, directory, name), entry["path"]
                )
            zip_file.writestr(
                MANIFEST_FILENAME,
                json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
            )

        # --- Self-verification before publishing -------------------------
        log("Verify backup archive...")
        _verify_archive(temp_zip_path)

        # --- Atomic publish ----------------------------------------------
        os.replace(temp_zip_path, backup_file)
        published = True
    finally:
        if not published and os.path.exists(temp_zip_path):
            with contextlib.suppress(OSError):
                os.remove(temp_zip_path)
        shutil.rmtree(staging_dir, ignore_errors=True)

    return backup_file


def _default_lock_path_from_db(db_path: str) -> str:
    return os.path.join(os.path.dirname(db_path), LOCK_FILENAME)


def _backup_database(source_path: str, destination_path: str, log):
    source_db = sqlite3.connect(source_path)
    backup_db = sqlite3.connect(destination_path)
    try:

        def progress(status, remaining, total):
            if total and (remaining == 0 or remaining % 500 == 0):
                log(f"Copied {total - remaining} of {total} pages...")

        with backup_db:
            source_db.backup(backup_db, pages=50, progress=progress)
    finally:
        backup_db.close()
        source_db.close()


def _build_manifest(staging_dir: str) -> dict:
    staged_db_path = os.path.join(staging_dir, DATABASE_FILENAME)
    members = _referenced_members(staged_db_path)
    on_disk = _staged_files(staging_dir)

    # Ensure staging matches the referenced set exactly.
    for directory in DIRECTORIES:
        if set(members[directory]) != set(on_disk[directory]):
            unexpected = set(on_disk[directory]) - set(members[directory])
            missing = set(members[directory]) - set(on_disk[directory])
            details = []
            if unexpected:
                details.append(f"unexpected: {sorted(unexpected)}")
            if missing:
                details.append(f"missing: {sorted(missing)}")
            raise BackupIntegrityError(
                f"Snapshot of {directory} is inconsistent with the database:"
                f" {'; '.join(details)}"
            )

    files = []
    for directory in DIRECTORIES:
        for name in members[directory]:
            staged_path = os.path.join(staging_dir, directory, name)
            stat = os.stat(staged_path)
            files.append(
                {
                    "path": f"{directory}/{name}",
                    "size": stat.st_size,
                    "sha256": _sha256_file(staged_path),
                }
            )

    db_stat = os.stat(staged_db_path)
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "database": {
            "path": DATABASE_FILENAME,
            "size": db_stat.st_size,
            "sha256": _sha256_file(staged_db_path),
        },
        "directories": list(DIRECTORIES),
        "files": sorted(files, key=lambda entry: entry["path"]),
    }


# ---------------------------------------------------------------------------
# Archive verification
# ---------------------------------------------------------------------------


def _safe_zip_names(names) -> list[str]:
    for name in names:
        if name.startswith("/") or "\\" in name or ".." in name.split("/"):
            raise BackupIntegrityError(f"Unsafe member path in backup: {name!r}")
    return list(names)


def _verify_archive(zip_path: str):
    manifest = _read_manifest_file(zip_path)
    if manifest is None:
        raise BackupIntegrityError("Backup manifest is missing")

    with zipfile.ZipFile(zip_path, "r") as zip_file:
        corrupt = zip_file.testzip()
        if corrupt is not None:
            raise BackupIntegrityError(f"Corrupt member in backup: {corrupt}")

        names = set(_safe_zip_names(zip_file.namelist()))

        if manifest.get("format") != FORMAT_NAME:
            raise BackupIntegrityError("Unknown backup format in manifest")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise BackupIntegrityError("Unsupported backup format version")

        expected_names = {MANIFEST_FILENAME, DATABASE_FILENAME}
        expected_names.update(entry["path"] for entry in manifest["files"])
        if names != expected_names:
            raise BackupIntegrityError(
                "Backup members do not match manifest: "
                f"missing={sorted(expected_names - names)}, "
                f"unexpected={sorted(names - expected_names)}"
            )

        # Verify size and digest of the database.
        db_entry = manifest["database"]
        db_info = zip_file.getinfo(DATABASE_FILENAME)
        if db_info.file_size != db_entry["size"]:
            raise BackupIntegrityError("Database size does not match manifest")
        db_data = zip_file.read(DATABASE_FILENAME)
        if _sha256_bytes(db_data) != db_entry["sha256"]:
            raise BackupIntegrityError("Database digest does not match manifest")

        # Verify size and digest of every file.
        for entry in manifest["files"]:
            info = zip_file.getinfo(entry["path"])
            if info.file_size != entry["size"]:
                raise BackupIntegrityError(
                    f"Size does not match manifest: {entry['path']}"
                )
            if _sha256_bytes(zip_file.read(entry["path"])) != entry["sha256"]:
                raise BackupIntegrityError(
                    f"Digest does not match manifest: {entry['path']}"
                )

        # Cross-check the archived database against the archived file set.
        _verify_database_file_set(db_data, manifest)


def _verify_database_file_set(db_data: bytes, manifest: dict):
    verify_db_path = None
    conn = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as file:
            file.write(db_data)
            verify_db_path = file.name
        conn = sqlite3.connect(verify_db_path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise BackupIntegrityError("Archived database fails integrity check")

        expected = {entry["path"] for entry in manifest["files"]}
        referenced = set()
        for directory in DIRECTORIES:
            rows = conn.execute(MEMBER_QUERIES[directory]).fetchall()
            for row in rows:
                if row[0]:
                    referenced.add(f"{directory}/{row[0]}")
        if referenced != expected:
            raise BackupIntegrityError(
                "Archived files do not match the database references: "
                f"missing={sorted(referenced - expected)}, "
                f"orphans={sorted(expected - referenced)}"
            )
    finally:
        if conn is not None:
            conn.close()
        if verify_db_path and os.path.exists(verify_db_path):
            os.remove(verify_db_path)


def _read_manifest_file(zip_path: str) -> dict | None:
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        if MANIFEST_FILENAME not in zip_file.namelist():
            return None
        try:
            data = zip_file.read(MANIFEST_FILENAME)
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackupIntegrityError(f"Invalid backup manifest: {error}") from error


def read_backup_manifest(zip_path: str) -> dict | None:
    """
    Returns the manifest of a backup zip, or None for legacy backups that
    were created without a manifest.
    """
    return _read_manifest_file(zip_path)


def verify_backup(zip_path: str) -> dict:
    """
    Verifies a backup zip. Supports both the current format (manifest with
    digests) and legacy backups without a manifest, for which only the zip
    structure and the presence of the database can be checked.
    Returns a short summary of the verification result.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            corrupt = zip_file.testzip()
            if corrupt is not None:
                raise BackupIntegrityError(f"Corrupt member in backup: {corrupt}")
            names = set(_safe_zip_names(zip_file.namelist()))
    except zipfile.BadZipFile as error:
        raise BackupIntegrityError(f"Not a valid backup zip: {error}") from error

    if DATABASE_FILENAME not in names:
        raise BackupIntegrityError(
            f"Backup does not contain {DATABASE_FILENAME}"
        )

    manifest = read_backup_manifest(zip_path)
    if manifest is None:
        # Legacy backup: no digests available, structure check is all we can do.
        return {
            "format": FORMAT_NAME,
            "format_version": LEGACY_FORMAT_VERSION,
            "files": len(names - {DATABASE_FILENAME}),
            "verified": True,
            "legacy": True,
        }

    _verify_archive(zip_path)
    return {
        "format": manifest.get("format"),
        "format_version": manifest.get("format_version"),
        "files": len(manifest.get("files", [])),
        "created_at": manifest.get("created_at"),
        "verified": True,
        "legacy": False,
    }
