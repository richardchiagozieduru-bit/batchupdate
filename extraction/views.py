"""
Views for the General Extraction feature.
Handles the extraction dashboard UI, subscriber search API,
batch creation, progress polling, and file downloads.
"""

import json
import logging
import os

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django_q.tasks import async_task

from .models import ExtractionBatch, ExtractionSession
from .services import search_subscribers

logger = logging.getLogger('extraction')


# ── Dashboard ────────────────────────────────────────────────────────────────

@login_required
def general_extraction_view(request):
    """
    GET: Render the General Extraction dashboard with subscriber dropdown
    and recent extraction batches.
    """
    recent_batches = ExtractionBatch.objects.filter(
        user=request.user
    ).prefetch_related('sessions')[:10]

    return render(request, 'extraction/general.html', {
        'recent_batches': recent_batches,
    })


# ── Subscriber Search API ───────────────────────────────────────────────────

@login_required
def subscriber_search_api(request):
    """
    GET /extraction/general/search/?q=<search_term>
    Returns JSON list of matching subscribers for live search.
    """
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})

    try:
        results = search_subscribers(query)
        return JsonResponse({'results': results})
    except Exception as e:
        logger.error(f"Subscriber search failed: {e}", exc_info=True)
        return JsonResponse({'error': 'Search failed. Please try again.'}, status=500)


# ── Start Extraction ────────────────────────────────────────────────────────

@login_required
def start_extraction_view(request):
    """
    POST: Create an ExtractionBatch with ExtractionSession per subscriber,
    enqueue Django-Q tasks, and redirect to progress page.

    Expected POST data:
        subscriber_ids: JSON array of subscriber IDs (e.g. "[387, 446]")
        subscriber_names: JSON object mapping ID→name (e.g. '{"387": "GTB", "446": "Access"}')
        include_consumer: "on" or absent
        include_commercial: "on" or absent
    """
    if request.method != 'POST':
        return redirect('extraction:general')

    # Parse subscriber selections
    subscriber_ids_raw = request.POST.get('subscriber_ids', '[]')
    subscriber_names_raw = request.POST.get('subscriber_names', '{}')

    try:
        subscriber_ids = json.loads(subscriber_ids_raw)
        subscriber_names = json.loads(subscriber_names_raw)
    except (json.JSONDecodeError, TypeError):
        messages.error(request, 'Invalid subscriber selection.')
        return redirect('extraction:general')

    if not subscriber_ids:
        messages.warning(request, 'Please select at least one subscriber.')
        return redirect('extraction:general')

    include_consumer = request.POST.get('include_consumer') == 'on'
    include_commercial = request.POST.get('include_commercial') == 'on'

    if not include_consumer and not include_commercial:
        messages.warning(request, 'Please select at least one extraction category (Consumer or Commercial).')
        return redirect('extraction:general')

    # Create batch
    batch = ExtractionBatch.objects.create(
        user=request.user,
        include_consumer=include_consumer,
        include_commercial=include_commercial,
    )

    # Create sessions and enqueue tasks
    for sub_id in subscriber_ids:
        sub_id_int = int(sub_id)
        sub_name = subscriber_names.get(str(sub_id), f'Subscriber {sub_id}')

        session = ExtractionSession.objects.create(
            batch=batch,
            subscriber_id=sub_id_int,
            subscriber_name=sub_name,
        )

        async_task(
            'extraction.tasks.extract_subscriber_task',
            session.id,
            task_name=f'Extract_{sub_name}_{sub_id_int}',
        )

    logger.info(
        f"User {request.user.username} started extraction batch {batch.batch_id} "
        f"with {len(subscriber_ids)} subscriber(s)"
    )

    return redirect('extraction:batch_progress', batch_id=batch.batch_id)


# ── Progress Dashboard ──────────────────────────────────────────────────────

@login_required
def extraction_progress_view(request, batch_id):
    """
    GET: Render the batch extraction progress dashboard.
    """
    batch = get_object_or_404(ExtractionBatch, batch_id=batch_id, user=request.user)
    sessions = batch.sessions.all()

    return render(request, 'extraction/batch_progress.html', {
        'batch': batch,
        'sessions': sessions,
    })


# ── Status Polling API ──────────────────────────────────────────────────────

@login_required
def extraction_batch_status_api(request, batch_id):
    """
    GET: JSON endpoint returning per-session status for AJAX polling.
    """
    batch = get_object_or_404(ExtractionBatch, batch_id=batch_id, user=request.user)
    sessions = list(batch.sessions.all())

    total_consumer = 0
    total_commercial = 0
    total_agric = 0
    completed_count = 0
    error_count = 0
    sessions_data = []

    for s in sessions:
        total_consumer += s.consumer_rows
        total_commercial += s.commercial_rows
        total_agric += s.agric_count
        if s.status == 'completed':
            completed_count += 1
        elif s.status == 'error':
            error_count += 1

        sessions_data.append({
            'id': s.id,
            'subscriber_id': s.subscriber_id,
            'subscriber_name': s.subscriber_name,
            'status': s.status,
            'consumer_rows': s.consumer_rows,
            'commercial_rows': s.commercial_rows,
            'agric_count': s.agric_count,
            'error_message': s.error_message,
            'has_zip': bool(s.subscriber_zip),
        })

    return JsonResponse({
        'batch_status': batch.status,
        'has_master_zip': bool(batch.master_zip),
        'total_sessions': len(sessions),
        'completed_count': completed_count,
        'error_count': error_count,
        'total_consumer_rows': total_consumer,
        'total_commercial_rows': total_commercial,
        'total_agric_count': total_agric,
        'sessions': sessions_data,
    })


# ── Downloads ────────────────────────────────────────────────────────────────

@login_required
def download_extraction_view(request, batch_id):
    """
    GET: Serve the master batch ZIP file.
    """
    batch = get_object_or_404(ExtractionBatch, batch_id=batch_id, user=request.user)

    if not batch.master_zip:
        messages.error(request, 'Master ZIP is not ready yet.')
        return redirect('extraction:batch_progress', batch_id=batch_id)

    zip_path = os.path.join(settings.MEDIA_ROOT, str(batch.master_zip))
    if not os.path.exists(zip_path):
        messages.error(request, 'Master ZIP file not found on disk.')
        return redirect('extraction:batch_progress', batch_id=batch_id)

    return FileResponse(
        open(zip_path, 'rb'),
        as_attachment=True,
        filename=os.path.basename(zip_path),
    )


@login_required
def download_subscriber_zip_view(request, session_id):
    """
    GET: Serve an individual subscriber's ZIP file.
    """
    session = get_object_or_404(ExtractionSession, id=session_id, batch__user=request.user)

    if not session.subscriber_zip:
        messages.error(request, 'Subscriber ZIP is not ready yet.')
        return redirect('extraction:batch_progress', batch_id=session.batch.batch_id)

    zip_path = os.path.join(settings.MEDIA_ROOT, str(session.subscriber_zip))
    if not os.path.exists(zip_path):
        messages.error(request, 'Subscriber ZIP file not found on disk.')
        return redirect('extraction:batch_progress', batch_id=session.batch.batch_id)

    return FileResponse(
        open(zip_path, 'rb'),
        as_attachment=True,
        filename=os.path.basename(zip_path),
    )


# ── Retry Failed Extraction ─────────────────────────────────────────────────

@login_required
def retry_extraction_view(request, session_id):
    """
    POST: Re-enqueue a failed subscriber extraction task.
    """
    if request.method != 'POST':
        return redirect('extraction:general')

    session = get_object_or_404(ExtractionSession, id=session_id, batch__user=request.user)

    if session.status != 'error':
        messages.info(request, f'{session.subscriber_name} is not in an error state.')
        return redirect('extraction:batch_progress', batch_id=session.batch.batch_id)

    # Reset session state
    session.status = 'pending'
    session.error_message = ''
    session.consumer_rows = 0
    session.commercial_rows = 0
    session.agric_count = 0
    session.subscriber_zip = ''
    session.started_at = None
    session.completed_at = None
    session.save()

    # Reset batch status if it was already finalised
    batch = session.batch
    if batch.status in ('completed', 'error'):
        batch.status = 'processing'
        batch.master_zip = ''
        batch.save(update_fields=['status', 'master_zip'])

    async_task(
        'extraction.tasks.extract_subscriber_task',
        session.id,
        task_name=f'Retry_Extract_{session.subscriber_name}_{session.subscriber_id}',
    )

    messages.success(request, f'Retrying extraction for {session.subscriber_name}...')
    return redirect('extraction:batch_progress', batch_id=batch.batch_id)


# ── Cancel Extraction ────────────────────────────────────────────────────────

@login_required
def cancel_batch_view(request, batch_id):
    """
    POST: Cancel an entire extraction batch.
    Sets status = 'cancelled' for the batch and all pending/processing sessions.
    """
    if request.method != 'POST':
        return redirect('extraction:general')

    batch = get_object_or_404(ExtractionBatch, batch_id=batch_id, user=request.user)

    if batch.status in ('completed', 'cancelled'):
        messages.info(request, f'Batch is already {batch.status}.')
        return redirect('extraction:batch_progress', batch_id=batch_id)

    batch.status = 'cancelled'
    batch.save(update_fields=['status'])

    # Cancel all pending/processing sessions in this batch in a single bulk update
    batch.sessions.filter(status__in=['pending', 'processing']).update(
        status='cancelled',
        error_message='Cancelled by user.'
    )

    logger.info(f"User {request.user.username} cancelled extraction batch {batch.batch_id}")
    messages.warning(request, 'Extraction batch has been cancelled.')
    return redirect('extraction:batch_progress', batch_id=batch_id)


@login_required
def cancel_session_view(request, session_id):
    """
    POST: Cancel a single subscriber extraction session.
    Sets session status = 'cancelled'.
    """
    if request.method != 'POST':
        return redirect('extraction:general')

    session = get_object_or_404(ExtractionSession, id=session_id, batch__user=request.user)

    if session.status in ('completed', 'cancelled', 'error'):
        messages.info(request, f'Extraction for {session.subscriber_name} is already {session.status}.')
        return redirect('extraction:batch_progress', batch_id=session.batch.batch_id)

    session.status = 'cancelled'
    session.error_message = 'Cancelled by user.'
    session.save(update_fields=['status', 'error_message'])

    logger.info(f"User {request.user.username} cancelled extraction session {session.id} for {session.subscriber_name}")
    messages.warning(request, f'Extraction for {session.subscriber_name} has been cancelled.')
    return redirect('extraction:batch_progress', batch_id=session.batch.batch_id)


@login_required
def delete_batch_view(request, batch_id):
    """
    POST: Delete an extraction batch and clean up its files from disk.
    """
    if request.method == 'POST':
        batch = get_object_or_404(ExtractionBatch, batch_id=batch_id, user=request.user)
        # Attempt to remove associated zip files
        if batch.master_zip and os.path.exists(batch.master_zip.path):
            try:
                os.remove(batch.master_zip.path)
            except OSError:
                pass
        
        for session in batch.sessions.all():
            if session.subscriber_zip and os.path.exists(session.subscriber_zip.path):
                try:
                    os.remove(session.subscriber_zip.path)
                except OSError:
                    pass
                    
        batch.delete()
        messages.success(request, 'Extraction batch deleted successfully.')
    return redirect('extraction:general')
