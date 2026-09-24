from django.core.management.base import BaseCommand
from django.conf import settings
from update.tasks import cleanup_old_dropped_files_task


class Command(BaseCommand):
    help = 'Clean up dropped files and records older than the retention threshold (default: 2 days)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=getattr(settings, 'DROPPED_FILE_RETENTION_DAYS', 2),
            help='Retention period in days (default: 2)',
        )

    def handle(self, *args, **options):
        days = options['days']
        self.stdout.write(f"Cleaning up dropped files older than {days} day(s)...")
        result = cleanup_old_dropped_files_task(retention_days=days)
        self.stdout.write(self.style.SUCCESS(f"Done: {result}"))
