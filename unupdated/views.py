import json
import logging
import os
import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, FileResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django_q.tasks import async_task

from .models import UnupdatedBatch, UnupdatedSession
from .services import (
    search_subscribers,
    compute_period_and_cutoff,
    get_ungenerated_ready_subscribers,
    check_subscriber_readiness,
)

logger = logging.getLogger('unupdated')


@login_required
def index(request):
    """
    Main Unupdated Records form view.
    Handles subscriber selection, date configuration, and batch job dispatch.
    """
    if request.method == 'POST':
        subscriber_ids = request.POST.getlist('subscriber_ids')
        subscriber_names = request.POST.getlist('subscriber_names')
        include_consumer = 'include_consumer' in request.POST
        include_commercial = 'include_commercial' in request.POST
        reporting_month = request.POST.get('reporting_month', '').strip()
        cutoff_override = request.POST.get('cutoff_override', '').strip()

        if not subscriber_ids:
            messages.error(request, "Please select at least one subscriber.")
            return redirect('unupdated:index')

        if not include_consumer and not include_commercial:
            messages.error(request, "Please select at least one account type (Consumer or Commercial).")
            return redirect('unupdated:index')

        # Calculate period and cutoff dates
        rep_month, last_period_num, cutoff_date = compute_period_and_cutoff(
            reporting_month, cutoff_override
        )

        with transaction.atomic():
            batch = UnupdatedBatch.objects.create(
                user=request.user,
                include_consumer=include_consumer,
                include_commercial=include_commercial,
                reporting_month=rep_month,
                last_period_num=last_period_num,
                cutoff_date=cutoff_date,
                status='processing',
            )

            created_sessions = []
            for sub_id, sub_name in zip(subscriber_ids, subscriber_names):
                s = UnupdatedSession.objects.create(
                    batch=batch,
                    subscriber_id=int(sub_id),
                    subscriber_name=sub_name.strip(),
                    status='pending',
                )
                created_sessions.append(s)

        # Dispatch background processing per subscriber for parallel execution
        for s in created_sessions:
            async_task(
                'unupdated.tasks.extract_unupdated_subscriber_task',
                s.id,
                task_name=f'Unupdated_{s.subscriber_name}_{s.subscriber_id}',
            )
        logger.info("Enqueued %d parallel unupdated tasks for batch %s", len(created_sessions), batch.batch_id)

        return redirect('unupdated:batch_progress', batch_id=batch.batch_id)

    # GET request - prepare default form values
    today = datetime.date.today()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - datetime.timedelta(days=1)
    default_month = f"{last_month_end.year:04d}-{last_month_end.month:02d}"
    _, default_last_period, default_cutoff = compute_period_and_cutoff(default_month)

    recent_batches = UnupdatedBatch.objects.prefetch_related('sessions').all().order_by('-created_at')[:10]

    # Stats for top dashboard strip
    total_batches = UnupdatedBatch.objects.count()
    completed_batches = UnupdatedBatch.objects.filter(status='completed').count()
    total_subscribers_audited = UnupdatedSession.objects.filter(status='completed').count()

    context = {
        'default_month': default_month,
        'default_last_period': default_last_period,
        'default_cutoff': default_cutoff,
        'recent_batches': recent_batches,
        'total_batches': total_batches,
        'completed_batches': completed_batches,
        'total_subscribers_audited': total_subscribers_audited,
    }
    return render(request, 'unupdated/index.html', context)


@login_required
def batch_progress(request, batch_id):
    """
    Renders the live progress tracking view for a batch.
    """
    batch = get_object_or_404(UnupdatedBatch, batch_id=batch_id)
    sessions = batch.sessions.all().order_by('id')

    context = {
        'batch': batch,
        'sessions': sessions,
    }
    return render(request, 'unupdated/batch_progress.html', context)


@login_required
def batch_status_api(request, batch_id):
    """
    JSON API for live polling progress of an unupdated batch.
    """
    batch = get_object_or_404(UnupdatedBatch, batch_id=batch_id)
    sessions = list(batch.sessions.all().order_by('id'))

    session_data = []
    total_consumer = 0
    total_commercial = 0
    completed_count = 0

    for s in sessions:
        total_consumer += s.consumer_rows
        total_commercial += s.commercial_rows
        if s.status in ('completed', 'error', 'cancelled'):
            completed_count += 1

        download_url = None
        if s.subscriber_file:
            download_url = reverse('unupdated:download_session', args=[s.id])

        session_data.append({
            'id': s.id,
            'subscriber_id': s.subscriber_id,
            'subscriber_name': s.subscriber_name,
            'status': s.status,
            'consumer_rows': s.consumer_rows,
            'commercial_rows': s.commercial_rows,
            'download_url': download_url,
            'error_message': s.error_message,
        })

    master_zip_url = None
    if batch.master_zip:
        master_zip_url = reverse('unupdated:download_master_zip', args=[batch.batch_id])

    single_file_url = None
    if len(session_data) == 1 and session_data[0]['download_url']:
        single_file_url = session_data[0]['download_url']

    progress_percent = int((completed_count / len(sessions) * 100)) if sessions else 0

    return JsonResponse({
        'batch_id': str(batch.batch_id),
        'status': batch.status,
        'progress_percent': progress_percent,
        'completed_sessions': completed_count,
        'total_sessions': len(sessions),
        'total_consumer_rows': total_consumer,
        'total_commercial_rows': total_commercial,
        'master_zip_url': master_zip_url,
        'single_file_url': single_file_url,
        'sessions': session_data,
    })


@login_required
def cancel_batch(request, batch_id):
    """
    Cancel an ongoing unupdated extraction batch.
    """
    if request.method == 'POST':
        batch = get_object_or_404(UnupdatedBatch, batch_id=batch_id)
        if batch.status == 'processing':
            batch.status = 'cancelled'
            batch.save(update_fields=['status'])
            batch.sessions.filter(status__in=['pending', 'processing']).update(
                status='cancelled',
                error_message='Cancelled by user.'
            )
            messages.info(request, "Batch processing cancelled.")
    return redirect('unupdated:batch_progress', batch_id=batch_id)


@login_required
def delete_batch(request, batch_id):
    """
    Delete an unupdated batch, its sessions, and associated files on disk.
    """
    if request.method == 'POST':
        batch = get_object_or_404(UnupdatedBatch, batch_id=batch_id)
        
        # Remove directory of generated subscriber reports
        batch_dir = os.path.join(settings.MEDIA_ROOT, 'unupdated', 'reports', str(batch.batch_id))
        if os.path.exists(batch_dir):
            try:
                import shutil
                shutil.rmtree(batch_dir)
            except Exception as e:
                logger.warning("Failed to delete directory %s: %s", batch_dir, str(e))

        # Remove master zip if exists
        if batch.master_zip:
            zip_path = os.path.join(settings.MEDIA_ROOT, batch.master_zip.name)
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except Exception as e:
                    logger.warning("Failed to delete master zip %s: %s", zip_path, str(e))

        batch.delete()
        messages.success(request, "Audit batch deleted successfully.")
    return redirect('unupdated:index')


@login_required
def download_master_zip(request, batch_id):
    """
    Serve the master ZIP containing all subscribers' unupdated reports.
    """
    batch = get_object_or_404(UnupdatedBatch, batch_id=batch_id)
    if not batch.master_zip:
        raise Http404("Master ZIP file not found.")

    file_path = os.path.join(settings.MEDIA_ROOT, batch.master_zip.name)
    if not os.path.exists(file_path):
        raise Http404("File on disk not found.")

    response = FileResponse(open(file_path, 'rb'), as_attachment=True, filename=os.path.basename(file_path))
    return response


@login_required
def download_session_file(request, session_id):
    """
    Serve a single subscriber's unupdated Excel workbook.
    """
    session = get_object_or_404(UnupdatedSession, id=session_id)
    if not session.subscriber_file:
        raise Http404("Excel file not found.")

    file_path = os.path.join(settings.MEDIA_ROOT, session.subscriber_file.name)
    if not os.path.exists(file_path):
        raise Http404("File on disk not found.")

    response = FileResponse(open(file_path, 'rb'), as_attachment=True, filename=os.path.basename(file_path))
    return response


@login_required
def subscriber_search_api(request):
    """
    Search endpoint for subscriber auto-complete dropdown.
    """
    q = request.GET.get('q', '').strip()
    if not q or len(q) < 2:
        return JsonResponse({'results': []})

    try:
        results = search_subscribers(q)
        return JsonResponse({'results': results})
    except Exception as e:
        logger.exception("Subscriber search failed: %s", str(e))
        return JsonResponse({'error': 'Failed to search subscribers', 'results': []}, status=500)


@login_required
def check_ungenerated_subscribers_api(request):
    """
    JSON API that returns subscribers who have uploaded their files for the requested
    month, but have not yet had their unupdated records generated.
    Supports ?refresh=true to query Bentley live; otherwise returns from 24h local RAM cache.
    """
    reporting_month = request.GET.get('month', '').strip()
    include_consumer = request.GET.get('include_consumer', 'true').lower() == 'true'
    include_commercial = request.GET.get('include_commercial', 'true').lower() == 'true'
    force_refresh = request.GET.get('refresh', 'false').lower() == 'true'

    if not reporting_month:
        today = datetime.date.today()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - datetime.timedelta(days=1)
        reporting_month = f"{last_month_end.year:04d}-{last_month_end.month:02d}"

    rep_month, last_period_num, cutoff_date = compute_period_and_cutoff(reporting_month)
    logger.info(
        "[Ungenerated API] Scanning requested for month=%s (consumer=%s, commercial=%s, refresh=%s)",
        rep_month, include_consumer, include_commercial, force_refresh
    )

    try:
        subscribers, meta = get_ungenerated_ready_subscribers(
            reporting_month_str=rep_month,
            include_consumer=include_consumer,
            include_commercial=include_commercial,
            force_refresh=force_refresh,
            return_meta=True,
        )
        return JsonResponse({
            'reporting_month': rep_month,
            'last_period_num': last_period_num,
            'cutoff_date': cutoff_date,
            'count': len(subscribers),
            'subscribers': subscribers,
            'last_synced': meta.get('synced_at', ''),
            'is_cached': meta.get('is_cached', False),
        })
    except Exception as e:
        logger.exception("Failed to check ungenerated subscribers: %s", str(e))
        return JsonResponse({'error': str(e), 'subscribers': [], 'count': 0}, status=500)


@login_required
def dispatch_ungenerated_batch(request):
    """
    Endpoint to trigger unupdated records extraction for:
    - ALL detected ungenerated ready subscribers (if generate_all=true)
    - OR selected subscriber IDs from the ungenerated dropdown.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required.'}, status=405)

    generate_all = request.POST.get('generate_all', '').lower() == 'true'
    reporting_month = request.POST.get('reporting_month', '').strip()
    include_consumer = 'include_consumer' in request.POST or request.POST.get('include_consumer') == 'true'
    include_commercial = 'include_commercial' in request.POST or request.POST.get('include_commercial') == 'true'

    if not include_consumer and not include_commercial:
        include_consumer = True
        include_commercial = True

    rep_month, last_period_num, cutoff_date = compute_period_and_cutoff(reporting_month)

    subscribers_to_run = []
    if generate_all:
        ready_subs = get_ungenerated_ready_subscribers(
            reporting_month_str=rep_month,
            include_consumer=include_consumer,
            include_commercial=include_commercial,
        )
        subscribers_to_run = [
            (s['subscriber_id'], s['subscriber_name'])
            for s in ready_subs
        ]
    else:
        # Specific selected subscribers
        sub_ids = request.POST.getlist('subscriber_ids')
        sub_names = request.POST.getlist('subscriber_names')
        if not sub_names or len(sub_names) != len(sub_ids):
            subscribers_to_run = [(int(sid), f"Subscriber {sid}") for sid in sub_ids if sid]
        else:
            subscribers_to_run = [
                (int(sid), sname.strip())
                for sid, sname in zip(sub_ids, sub_names)
                if sid
            ]

    if not subscribers_to_run:
        messages.warning(request, "No ungenerated subscribers found or selected to process.")
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
            return JsonResponse({'error': 'No subscribers to process.'}, status=400)
        return redirect('unupdated:index')

    with transaction.atomic():
        batch = UnupdatedBatch.objects.create(
            user=request.user,
            include_consumer=include_consumer,
            include_commercial=include_commercial,
            reporting_month=rep_month,
            last_period_num=last_period_num,
            cutoff_date=cutoff_date,
            status='processing',
        )

        created_sessions = []
        for s_id, s_name in subscribers_to_run:
            s = UnupdatedSession.objects.create(
                batch=batch,
                subscriber_id=s_id,
                subscriber_name=s_name,
                status='pending',
            )
            created_sessions.append(s)

    for s in created_sessions:
        async_task(
            'unupdated.tasks.extract_unupdated_subscriber_task',
            s.id,
            task_name=f'Unupdated_{s.subscriber_name}_{s.subscriber_id}',
        )

    logger.info(
        "Dispatched batch %s for %d ungenerated subscribers (%s)",
        batch.batch_id, len(created_sessions), "ALL" if generate_all else "SELECTED"
    )

    redirect_url = reverse('unupdated:batch_progress', args=[batch.batch_id])
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.META.get('HTTP_ACCEPT', ''):
        return JsonResponse({
            'success': True,
            'batch_id': str(batch.batch_id),
            'redirect_url': redirect_url,
            'enqueued_count': len(created_sessions),
        })

    return redirect('unupdated:batch_progress', batch_id=batch.batch_id)
