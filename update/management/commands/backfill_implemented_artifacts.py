import os
import logging
from django.core.management.base import BaseCommand
from update.models import UploadSession, ImplementedArtifact
from update.tasks import compute_parquet_fingerprint

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Backfill ImplementedArtifact registry from historical UploadSession records with batchupdate_uploaded=True'

    def handle(self, *args, **options):
        sessions = UploadSession.objects.filter(batchupdate_uploaded=True).order_by('uploaded_at')
        total = sessions.count()
        self.stdout.write(f"Found {total} historical uploaded sessions to evaluate.")

        created_count = 0
        existing_count = 0
        missing_file_count = 0

        for session in sessions:
            if not session.processed_file or not os.path.exists(session.processed_file.path):
                missing_file_count += 1
                continue

            try:
                fingerprint = session.content_fingerprint
                if not fingerprint:
                    fingerprint = compute_parquet_fingerprint(session.processed_file.path)
                    if fingerprint:
                        session.content_fingerprint = fingerprint
                        session.save(update_fields=['content_fingerprint'])

                if not fingerprint:
                    continue

                _, created = ImplementedArtifact.objects.get_or_create(
                    content_fingerprint=fingerprint,
                    defaults={
                        'table_name': session.sheet_name or 'UNKNOWN',
                        'subscriber': session.subscriber,
                        'rows_uploaded': session.rows_uploaded,
                        'implemented_by': session.user,
                        'source_session': session,
                        'is_historical_backfill': True,
                    }
                )

                if created:
                    created_count += 1
                else:
                    existing_count += 1

            except Exception as e:
                self.stderr.write(f"Error processing session {session.id}: {e}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: {created_count} registered, "
                f"{existing_count} already existed, "
                f"{missing_file_count} skipped (file deleted by retention cleanup)."
            )
        )
