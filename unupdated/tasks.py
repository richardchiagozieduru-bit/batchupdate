"""
Background tasks for the Unupdated Records application powered by Django-Q2.
Processes subscriber extractions concurrently with independent task execution.
"""

import logging
import os
import time
from django.utils import timezone
from django.conf import settings
from django_q.tasks import async_task

from .models import UnupdatedBatch, UnupdatedSession
from .services import (
    extract_unupdated_for_subscriber,
    create_master_zip,
    ExtractionCancelledException,
    check_subscriber_readiness,
)

logger = logging.getLogger('unupdated')


def extract_unupdated_subscriber_task(session_id):
    """
    Django-Q worker task that extracts unupdated accounts for a single subscriber.
    Allows parallel multi-worker execution across subscribers in a batch.
    """
    try:
        session = UnupdatedSession.objects.select_related('batch').get(id=session_id)
    except UnupdatedSession.DoesNotExist:
        logger.error("UnupdatedSession %s does not exist", session_id)
        return

    batch = session.batch
    if session.status == 'cancelled' or batch.status == 'cancelled':
        logger.info("[Session %s] Task skipped — session/batch is cancelled.", session_id)
        return

    task_start_t = time.perf_counter()
    session.status = 'processing'
    session.started_at = timezone.now()
    session.save(update_fields=['status', 'started_at'])
    logger.info(
        "[Session %s] Started background extraction task for %s (ID %s)",
        session.id, session.subscriber_name, session.subscriber_id
    )

    # ── Step 1: Pre-Flight File Readiness Verification ───────────────────────
    readiness = check_subscriber_readiness(
        subscriber_id=session.subscriber_id,
        target_last_period_num=batch.last_period_num,
        include_consumer=batch.include_consumer,
        include_commercial=batch.include_commercial,
    )

    session.is_file_ready = readiness['is_ready']
    session.consumer_max_period = readiness.get('consumer_max') or ''
    session.commercial_max_period = readiness.get('commercial_max') or ''

    if not readiness['is_ready']:
        session.status = 'pending_upload'
        session.error_message = readiness['message']
        session.completed_at = timezone.now()
        session.save(update_fields=[
            'is_file_ready', 'consumer_max_period', 'commercial_max_period',
            'status', 'error_message', 'completed_at'
        ])
        task_elapsed = time.perf_counter() - task_start_t
        logger.warning(
            "[Session %s] Halting extraction after %.3fs for %s (ID %s) — %s",
            session.id, task_elapsed, session.subscriber_name, session.subscriber_id, readiness['message']
        )
        _check_and_finalise_unupdated_batch(batch.id)
        return

    session.save(update_fields=['is_file_ready', 'consumer_max_period', 'commercial_max_period'])

    output_dir = os.path.join(settings.MEDIA_ROOT, 'unupdated', 'reports', str(batch.batch_id))
    os.makedirs(output_dir, exist_ok=True)

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
        filepath, c_rows, comm_rows = extract_unupdated_for_subscriber(
            subscriber_id=session.subscriber_id,
            subscriber_name=session.subscriber_name,
            include_consumer=batch.include_consumer,
            include_commercial=batch.include_commercial,
            last_period_num=batch.last_period_num,
            cutoff_date=batch.cutoff_date,
            output_dir=output_dir,
            check_cancelled_fn=check_cancelled,
        )

        rel_path = os.path.relpath(filepath, settings.MEDIA_ROOT)
        session.subscriber_file = rel_path
        session.consumer_rows = c_rows
        session.commercial_rows = comm_rows
        session.status = 'completed'
        session.completed_at = timezone.now()
        session.save(update_fields=[
            'subscriber_file', 'consumer_rows', 'commercial_rows', 'status', 'completed_at'
        ])

        task_elapsed = time.perf_counter() - task_start_t
        logger.info(
            "[Session %s] Completed extraction task in %.2fs for %s: Consumer=%d, Commercial=%d",
            session.id, task_elapsed, session.subscriber_name, c_rows, comm_rows
        )

    except ExtractionCancelledException as ce:
        task_elapsed = time.perf_counter() - task_start_t
        logger.warning(
            "[Session %s] Extraction cancelled after %.2fs: %s",
            session.id, task_elapsed, ce
        )
        session.status = 'cancelled'
        session.error_message = 'Extraction cancelled by user.'
        session.completed_at = timezone.now()
        session.save(update_fields=['status', 'error_message', 'completed_at'])

    except Exception as e:
        task_elapsed = time.perf_counter() - task_start_t
        logger.exception(
            "[Session %s] Error in task after %.2fs (Sub: %s): %s",
            session.id, task_elapsed, session.subscriber_name, str(e)
        )
        session.status = 'error'
        session.error_message = str(e)
        session.completed_at = timezone.now()
        session.save(update_fields=['status', 'error_message', 'completed_at'])

    # Check if all sessions in the batch are finished
    _check_and_finalise_unupdated_batch(batch.id)


def _check_and_finalise_unupdated_batch(batch_id):
    """
    Check if all sessions in an unupdated batch are done.
    If yes, safely triggers the finalise task with atomic guard.
    """
    try:
        batch = UnupdatedBatch.objects.get(id=batch_id)
    except UnupdatedBatch.DoesNotExist:
        return

    total = batch.sessions.count()
    done = batch.sessions.filter(status__in=['completed', 'error', 'cancelled', 'pending_upload']).count()

    if done >= total:
        updated = UnupdatedBatch.objects.filter(
            id=batch_id, status='processing'
        ).update(status='completed')
        if updated:
            async_task('unupdated.tasks.finalise_unupdated_batch_task', batch_id)


def finalise_unupdated_batch_task(batch_id):
    """
    Runs after all subscriber sessions in an unupdated batch are complete.
    Bundles generated subscriber reports into a master ZIP if multiple files exist.
    """
    fin_start_t = time.perf_counter()
    try:
        batch = UnupdatedBatch.objects.get(id=batch_id)
    except UnupdatedBatch.DoesNotExist:
        logger.error("UnupdatedBatch %s not found for finalisation.", batch_id)
        return

    completed_sessions = batch.sessions.filter(status='completed').exclude(subscriber_file='')
    generated_files = []
    for s in completed_sessions:
        if s.subscriber_file:
            full_path = os.path.join(settings.MEDIA_ROOT, str(s.subscriber_file))
            if os.path.exists(full_path):
                generated_files.append(full_path)

    if len(generated_files) > 1:
        zip_dir = os.path.join(settings.MEDIA_ROOT, 'unupdated', 'zips')
        zip_filename = f"unupdated_records_{batch.reporting_month}_{str(batch.batch_id)[:8]}.zip"
        zip_path = os.path.join(zip_dir, zip_filename)
        create_master_zip(generated_files, zip_path)
        batch.master_zip = os.path.relpath(zip_path, settings.MEDIA_ROOT)

    total_sessions = batch.sessions.count()
    error_sessions = batch.sessions.filter(status='error').count()
    if total_sessions > 0 and error_sessions == total_sessions:
        batch.status = 'error'
    else:
        batch.status = 'completed'

    batch.save(update_fields=['status', 'master_zip'])
    fin_elapsed = time.perf_counter() - fin_start_t
    logger.info(
        "[Batch %s] Finalised UnupdatedBatch in %.2fs with status %s (Total: %d, Errors: %d)",
        batch.batch_id, fin_elapsed, batch.status, total_sessions, error_sessions
    )
