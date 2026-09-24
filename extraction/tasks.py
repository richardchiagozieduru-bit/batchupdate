"""
Django-Q async worker tasks for Bureau data extraction.
Each subscriber is processed independently for fault isolation.
"""

import logging
import os

from django.conf import settings
from django.utils import timezone
from django_q.tasks import async_task

from .models import ExtractionBatch, ExtractionSession
from .queries import CONSUMER_HEADERS, COMMERCIAL_HEADERS
from .services import (
    run_extraction_query,
    compress_to_zip,
    build_master_zip,
    ExtractionCancelledException,
)

logger = logging.getLogger('extraction')


def extract_subscriber_task(session_id):
    """
    Async task: Extract Consumer and/or Commercial data for a single subscriber.

    Workflow:
    1. Call run_extraction_query() which auto-selects single vs parallel streaming.
    2. Compress all CSVs into a single subscriber ZIP.
    3. Update session status and row counts.
    4. Check if all sessions in the batch are done → if yes, enqueue finalise_batch_task.
    """
    try:
        session = ExtractionSession.objects.select_related('batch').get(id=session_id)
    except ExtractionSession.DoesNotExist:
        logger.error(f"ExtractionSession {session_id} not found.")
        return

    # Check if already cancelled before starting
    if session.status == 'cancelled' or session.batch.status == 'cancelled':
        logger.info(f"[Session {session_id}] Task skipped — session/batch is cancelled.")
        return

    session.status = 'processing'
    session.started_at = timezone.now()
    session.save(update_fields=['status', 'started_at'])

    batch = session.batch
    subscriber_id = session.subscriber_id
    subscriber_name = session.subscriber_name
    # Sanitise subscriber name for use in filenames
    safe_name = "".join(c if c.isalnum() or c in ('_', '-', ' ') else '_' for c in subscriber_name).strip()
    safe_name = safe_name.replace(' ', '_')

    temp_dir = os.path.join(settings.MEDIA_ROOT, 'extractions', 'temp', str(batch.batch_id))
    zip_dir = os.path.join(settings.MEDIA_ROOT, 'extractions', 'zips')
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(zip_dir, exist_ok=True)

    all_csv_files = []
    consumer_rows = 0
    commercial_rows = 0

    import time
    last_check_time = 0
    cached_cancelled = False

    def check_cancelled():
        nonlocal last_check_time, cached_cancelled
        now = time.time()
        if now - last_check_time < 2.0:
            return cached_cancelled
        last_check_time = now
        try:
            session.refresh_from_db(fields=['status'])
            if session.status == 'cancelled':
                cached_cancelled = True
                return True
            batch.refresh_from_db(fields=['status'])
            cached_cancelled = (batch.status == 'cancelled')
            return cached_cancelled
        except Exception:
            return cached_cancelled

    try:
        # ── Consumer Extraction ──────────────────────────────────────────
        if batch.include_consumer:
            logger.info(f"[Session {session_id}] Starting Consumer extraction for "
                        f"subscriber {subscriber_id} ({subscriber_name})")
            csv_files, rows = run_extraction_query(
                subscriber_id=subscriber_id,
                query_type='consumer',
                output_dir=temp_dir,
                base_filename=f'{subscriber_id}_{safe_name}_Consumer',
                headers=CONSUMER_HEADERS,
                check_cancelled_fn=check_cancelled,
            )
            all_csv_files.extend(csv_files)
            consumer_rows = rows
            logger.info(f"[Session {session_id}] Consumer extraction complete: "
                        f"{rows:,} rows in {len(csv_files)} file(s)")

        # ── Commercial Extraction ────────────────────────────────────────
        if batch.include_commercial:
            logger.info(f"[Session {session_id}] Starting Commercial extraction for "
                        f"subscriber {subscriber_id} ({subscriber_name})")
            csv_files, rows = run_extraction_query(
                subscriber_id=subscriber_id,
                query_type='commercial',
                output_dir=temp_dir,
                base_filename=f'{subscriber_id}_{safe_name}_Commercial',
                headers=COMMERCIAL_HEADERS,
                check_cancelled_fn=check_cancelled,
            )
            all_csv_files.extend(csv_files)
            commercial_rows = rows
            logger.info(f"[Session {session_id}] Commercial extraction complete: "
                        f"{rows:,} rows in {len(csv_files)} file(s)")

        # Check cancellation before zipping
        if check_cancelled():
            raise ExtractionCancelledException("Cancelled before compression")

        # ── Compress to ZIP ──────────────────────────────────────────────
        if all_csv_files:
            zip_filename = f'{subscriber_id}_{safe_name}_Extraction.zip'
            zip_path = os.path.join(zip_dir, zip_filename)
            compress_to_zip(all_csv_files, zip_path)
            session.subscriber_zip = f'extractions/zips/{zip_filename}'
        else:
            logger.warning(f"[Session {session_id}] No data extracted for subscriber {subscriber_id}")

        # ── Update session ───────────────────────────────────────────────
        session.status = 'completed'
        session.consumer_rows = consumer_rows
        session.commercial_rows = commercial_rows
        session.completed_at = timezone.now()
        session.save()

        duration = session.completed_at - session.started_at
        mins, secs = divmod(int(duration.total_seconds()), 60)
        dur_str = f"{mins}m {secs}s" if mins > 0 else f"{duration.total_seconds():.2f}s"

        logger.info(
            f"[Session {session_id}] Task completed at {session.completed_at.strftime('%H:%M:%S')} "
            f"for {subscriber_name} (Duration: {dur_str}): "
            f"Consumer={consumer_rows:,}, Commercial={commercial_rows:,}"
        )

    except ExtractionCancelledException as ce:
        logger.warning(f"[Session {session_id}] Extraction cancelled for subscriber {subscriber_id}: {ce}")
        session.status = 'cancelled'
        session.error_message = 'Extraction cancelled by user.'
        session.completed_at = timezone.now()
        session.save(update_fields=['status', 'error_message', 'completed_at'])

    except Exception as e:
        logger.error(f"[Session {session_id}] Extraction failed for subscriber {subscriber_id}: {e}", exc_info=True)
        session.status = 'error'
        session.error_message = str(e)
        session.completed_at = timezone.now()
        session.save(update_fields=['status', 'error_message', 'completed_at'])

    # ── Check if batch is complete ───────────────────────────────────────
    _check_and_finalise_batch(batch.id)


def _check_and_finalise_batch(batch_id):
    """
    Check if all sessions in a batch are done (completed, error, or cancelled).
    If yes, enqueue the finalise_batch_task safely without race conditions.
    """
    batch = ExtractionBatch.objects.get(id=batch_id)
    total = batch.sessions.count()
    done = batch.sessions.filter(status__in=['completed', 'error', 'cancelled']).count()

    if done >= total:
        # Atomic check: only enqueue if batch is still in processing state
        updated = ExtractionBatch.objects.filter(
            id=batch_id, status='processing'
        ).update(status='completed')
        if updated:
            async_task('extraction.tasks.finalise_batch_task', batch_id)


def finalise_batch_task(batch_id):
    """
    Runs after all subscriber sessions in a batch are complete.
    Combines individual subscriber ZIPs into a master batch ZIP.
    """
    try:
        batch = ExtractionBatch.objects.get(id=batch_id)
    except ExtractionBatch.DoesNotExist:
        logger.error(f"ExtractionBatch {batch_id} not found.")
        return

    sessions = batch.sessions.filter(status='completed').exclude(subscriber_zip='')
    subscriber_zip_paths = []

    for session in sessions:
        if session.subscriber_zip:
            full_path = os.path.join(settings.MEDIA_ROOT, str(session.subscriber_zip))
            if os.path.exists(full_path):
                subscriber_zip_paths.append(full_path)

    if subscriber_zip_paths:
        zip_dir = os.path.join(settings.MEDIA_ROOT, 'extractions', 'zips')
        master_filename = f'Batch_Extraction_{batch.batch_id}.zip'
        master_path = os.path.join(zip_dir, master_filename)
        build_master_zip(subscriber_zip_paths, master_path)
        batch.master_zip = f'extractions/zips/{master_filename}'

    # Determine overall batch status
    error_count = batch.sessions.filter(status='error').count()
    total_count = batch.sessions.count()
    if total_count > 0 and error_count == total_count:
        batch.status = 'error'
    else:
        batch.status = 'completed'

    batch.save(update_fields=['status', 'master_zip'])

    # Clean up temp directory
    temp_dir = os.path.join(settings.MEDIA_ROOT, 'extractions', 'temp', str(batch.batch_id))
    try:
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temp directory: {temp_dir}")
    except OSError:
        logger.warning(f"Could not clean up temp directory: {temp_dir}")

    logger.info(f"Batch {batch.batch_id} finalised: status={batch.status}")
