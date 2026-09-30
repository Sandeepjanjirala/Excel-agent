from django.core.management.base import BaseCommand
from agent.services.cleanup import cleanup_expired_sessions, get_storage_stats


class Command(BaseCommand):
    help = "Clean up expired media files and project sessions (default > 12 hours old)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=float,
            default=None,
            help="Retention period in hours (default: MEDIA_RETENTION_HOURS or 12).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Purge ALL projects and media files immediately regardless of age.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be cleaned up without deleting anything.",
        )
        parser.add_argument(
            "--stats",
            action="store_true",
            help="Only display current storage statistics.",
        )

    def handle(self, *args, **options):
        if options["stats"]:
            stats = get_storage_stats()
            self.stdout.write(self.style.SUCCESS(f"Total media size: {stats['total_size_mb']} MB ({stats['total_files']} files)"))
            self.stdout.write(f"Active projects: {stats['active_projects']}")
            self.stdout.write(f"Active workbooks: {stats['active_workbooks']}")
            self.stdout.write(f"Configured retention: {stats['retention_hours']} hours")
            self.stdout.write("Breakdown:")
            for cat, data in stats["categories"].items():
                self.stdout.write(f"  - {cat}: {data['count']} file(s), {data['size_mb']} MB")
            return

        dry_run = options["dry_run"]
        hours = 0 if options["all"] else options["hours"]

        mode_str = "[DRY RUN] " if dry_run else ""
        self.stdout.write(f"{mode_str}Running media cleanup (hours threshold: {0 if options['all'] else (hours if hours is not None else 12)})...")

        result = cleanup_expired_sessions(retention_hours=hours, dry_run=dry_run)

        mb_freed = result["mb_freed"]
        self.stdout.write(self.style.SUCCESS(
            f"{mode_str}Cleanup complete: {result['projects_deleted']} project(s) purged, "
            f"{result['files_deleted']} file(s) removed, {mb_freed} MB freed."
        ))
