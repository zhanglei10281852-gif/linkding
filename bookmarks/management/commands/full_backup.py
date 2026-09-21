import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from bookmarks.services import backups


class Command(BaseCommand):
    help = "Creates a backup of the linkding data folder"

    def add_arguments(self, parser):
        parser.add_argument("backup_file", type=str, help="Backup zip file destination")

    def handle(self, *args, **options):
        backup_file = options["backup_file"]

        if not settings.USE_SQLITE:
            raise CommandError(
                "full_backup only supports the default SQLite database. Please"
                " back up the database and the data folder manually."
            )

        db_path = connections["default"].settings_dict.get("NAME", "")
        if backups.is_memory_db_name(db_path):
            raise CommandError(
                "full_backup requires a file-based SQLite database, but the"
                " default database has no file path."
            )

        folders = {
            "assets": settings.LD_ASSET_FOLDER,
            "favicons": settings.LD_FAVICON_FOLDER,
            "previews": settings.LD_PREVIEW_FOLDER,
        }

        for name, folder in folders.items():
            if not os.path.exists(folder):
                self.stdout.write(
                    self.style.WARNING(f"No {name} folder found. Skipping...")
                )

        try:
            backups.create_full_backup(
                backup_file=backup_file,
                db_path=db_path,
                folders=folders,
                progress=lambda message: self.stdout.write(message),
            )
        except backups.BackupError as error:
            raise CommandError(f"Backup failed: {error}") from error

        self.stdout.write(self.style.SUCCESS(f"Backup created at {backup_file}"))
