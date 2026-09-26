import os
import shutil
import uuid
import json
import logging
import pandas as pd
from io import BytesIO
from django.core.files.base import ContentFile
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponse, JsonResponse, FileResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from django_q.tasks import async_task
from .progress import get_progress
from .tasks import cleanup_old_dropped_files_task

from django.contrib.auth.models import User
from django.db import transaction

from openpyxl import load_workbook
# pyrefly: ignore [missing-import]
from django.http import JsonResponse
from django.utils import timezone
from .models import UploadSession, ColumnMapping, MappingTemplate, Subscriber, DroppedFile
from .columns import TARGET_COLUMN_CHOICES, HEADER_MAPPING_DICTIONARY, DISPLAY_HEADERS
from .services import (
    clean_dataframe,
    read_uploaded_file, read_excel_file,
    MAX_EXCEL_FILE_SIZE_MB, MAX_CSV_FILE_SIZE_MB,
    get_excel_sheet_names, read_uploaded_file_sheet,
    generate_sql_script, upload_raw_to_batchupdate,
    detect_header_row, build_sheet_name, get_subscribers_from_batchupdate,
    extract_sub_id, write_parquet_as_excel, is_sheet_usable,
    get_subscriber_historical_targets, get_file_headers,
)
from acctmgt.utils import (
    is_external as _is_external,
    require_bound as _require_bound,
    is_internal_dropper,
)

logger = logging.getLogger(__name__)


@login_required
def drop_view(request):
    """File drop view for non-technical internal users to deposit raw files."""
    try:
        cleanup_old_dropped_files_task()
    except Exception as exc:
        logger.warning(f"Opportunistic dropped file cleanup failed in drop_view: {exc}")

    subscribers = get_subscribers_from_batchupdate()

    if request.method == 'POST':
        files = request.FILES.getlist('drop_files')
        notes = request.POST.get('notes', '').strip()

        if not files:
            messages.error(request, 'Please select at least one file to drop.')
            return redirect('client_drop')

        file_subscribers = []
        for i, f in enumerate(files):
            sub_id = (request.POST.get(f'file_subscriber_{i}', '') or request.POST.get('subscriber', '')).strip()
            if not sub_id:
                messages.error(request, f'Please select a subscriber institution for "{f.name}".')
                return redirect('client_drop')

            try:
                sub_id_int = int(float(sub_id))
                sub_match = next((s for s in subscribers if s['subscriber_id'] == sub_id_int), None)
                if not sub_match:
                    messages.error(request, f'Invalid subscriber selected for "{f.name}".')
                    return redirect('client_drop')
                selected_sub, _ = Subscriber.objects.get_or_create(
                    subscriber_id=sub_id_int,
                    defaults={'subscriber_name': sub_match['subscriber_name']}
                )
                file_subscribers.append(selected_sub)
            except (ValueError, TypeError):
                messages.error(request, f'Invalid subscriber selected for "{f.name}".')
                return redirect('client_drop')

        for i, f in enumerate(files):
            DroppedFile.objects.create(
                user=request.user,
                subscriber=file_subscribers[i],
                file=f,
                original_filename=f.name,
                file_size_bytes=f.size,
                notes=notes,
            )

        if len(set(s.subscriber_id for s in file_subscribers)) == 1:
            msg = f"Successfully deposited {len(files)} file(s) for {file_subscribers[0].subscriber_name} into the drop box. Awaiting bureau administration review."
        else:
            msg = f"Successfully deposited {len(files)} file(s) across {len(set(s.subscriber_id for s in file_subscribers))} institutions into the drop box. Awaiting bureau administration review."

        messages.success(request, msg)
        return redirect('client_drop')

    user_drops = DroppedFile.objects.filter(user=request.user).select_related('subscriber')[:25]
    return render(request, 'update/drop.html', {'user_drops': user_drops, 'subscribers': subscribers})


@login_required
@require_POST
def delete_dropped_file_view(request, drop_id):
    """Delete a file mistakenly deposited into the drop box."""
    drop = get_object_or_404(DroppedFile, id=drop_id)

    # Permission check: must be the dropper or staff
    if drop.user != request.user and not request.user.is_staff:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'You do not have permission to delete this file.'}, status=403)
        messages.error(request, 'You do not have permission to delete this file.')
        return redirect('client_drop')

    # Status check: only pending drops can be deleted by non-staff droppers
    if drop.status != 'pending' and not request.user.is_staff:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': f'"{drop.original_filename}" has already been imported and cannot be deleted.'}, status=400)
        messages.error(request, f'"{drop.original_filename}" has already been imported by the bureau administration and cannot be deleted.')
        return redirect('client_drop')

    filename = drop.original_filename
    if drop.file:
        try:
            drop.file.delete(save=False)
        except Exception as exc:
            logger.warning(f"Could not delete dropped file on disk ({drop.file}): {exc}")

    drop.delete()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': f'Successfully deleted "{filename}" from the drop box.',
            'drop_id': drop_id,
        })

    messages.success(request, f'Successfully deleted "{filename}" from the drop box.')
    return redirect('client_drop')


@login_required
def pending_drops_api(request):
    """JSON endpoint for staff to fetch pending dropped files for the Upload staging modal."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    try:
        cleanup_old_dropped_files_task()
    except Exception as exc:
        logger.warning(f"Opportunistic dropped file cleanup failed in pending_drops_api: {exc}")

    drops = DroppedFile.objects.filter(status='pending').select_related('user', 'subscriber')
    data = [
        {
            'id': d.id,
            'filename': d.original_filename,
            'size_bytes': d.file_size_bytes,
            'size_formatted': d.formatted_size,
            'dropped_by': d.user.username,
            'dropped_at': d.dropped_at.strftime('%b %d, %Y %H:%M'),
            'subscriber_id': d.subscriber.subscriber_id if d.subscriber else None,
            'subscriber_name': d.subscriber.subscriber_name if d.subscriber else '',
            'notes': d.notes or '',
        }
        for d in drops
    ]
    return JsonResponse({'drops': data})


@login_required
def upload_view(request):
    """Excel file upload page — handles multi-sheet files by splitting into one session per sheet."""
    # Gate internal droppers: they only access the drop page
    if is_internal_dropper(request.user) and not request.user.is_staff:
        return redirect('client_drop')

    # Gate: external users must be bound before they can upload
    if _require_bound(request.user):
        return redirect('redeem_token')

    # Resolve subscriber for this user
    if _is_external(request.user):
        subscriber = request.user.subscriber_profile.subscriber
        subscribers = None  # external users have no dropdown
    else:
        subscriber = None
        subscribers = get_subscribers_from_batchupdate()  # live from BatchUpdate Sheet1

    if request.method == 'POST':
        excel_files = request.FILES.getlist('excel_file') or request.FILES.getlist('file')

        # Also collect files staged from the Drop Box modal!
        dropped_file_ids_raw = request.POST.getlist('dropped_file_ids')
        if not dropped_file_ids_raw and request.POST.get('dropped_file_ids'):
            dropped_file_ids_raw = [x.strip() for x in request.POST.get('dropped_file_ids').split(',') if x.strip()]

        dropped_objs = []
        if dropped_file_ids_raw:
            from django.core.files import File
            for drop_id in dropped_file_ids_raw:
                try:
                    drop_obj = DroppedFile.objects.get(id=int(drop_id), status='pending')
                    f = File(drop_obj.file.open('rb'), name=drop_obj.original_filename)
                    excel_files.append(f)
                    dropped_objs.append(drop_obj)
                except Exception as e:
                    logger.warning(f"Could not load dropped file ID {drop_id}: {e}")

        if not excel_files:
            messages.error(request, 'Please select or stage at least one file to upload.')
            return redirect('upload')

        upload_to_db = request.POST.get('upload_to_db') == 'on'
        is_ext = _is_external(request.user)
        assign_subscriber = is_ext or (request.POST.get('assign_subscriber') == 'on') or bool(subscribers)

        default_subscriber = None
        if is_ext:
            default_subscriber = getattr(getattr(request.user, 'subscriber_profile', None), 'subscriber', None)
        elif assign_subscriber:
            sub_id = request.POST.get('subscriber', '').strip()
            if sub_id:
                try:
                    sub_id_int = int(float(sub_id))
                    sub_list = subscribers if subscribers is not None else get_subscribers_from_batchupdate()
                    sub_match = next((s for s in sub_list if s['subscriber_id'] == sub_id_int), None)
                    if sub_match:
                        default_subscriber, _ = Subscriber.objects.get_or_create(
                            subscriber_id=sub_id_int,
                            defaults={'subscriber_name': sub_match['subscriber_name']},
                        )
                except (ValueError, TypeError):
                    pass

        # Collect per-file passwords: excel_password_0, excel_password_1, … or drop_password_<id>
        num_local = len(excel_files) - len(dropped_objs)
        file_passwords = []
        for i in range(len(excel_files)):
            pwd_val = (request.POST.get(f'excel_password_{i}', '') or '').strip() or None
            if not pwd_val and i >= num_local:
                drop_idx = i - num_local
                if drop_idx < len(dropped_objs):
                    pwd_val = (request.POST.get(f'drop_password_{dropped_objs[drop_idx].id}', '') or '').strip() or None
            file_passwords.append(pwd_val)

        if all(p is None for p in file_passwords):
            shared = (request.POST.get('excel_password', '') or '').strip() or None
            file_passwords = [shared] * len(excel_files)

        # Collect per-file subscribers: file_subscriber_0, file_subscriber_1, … or drop_subscriber_<id>
        file_subscribers = []
        for i in range(len(excel_files)):
            sub_val = (request.POST.get(f'file_subscriber_{i}', '') or '').strip() or None
            if not sub_val and i >= num_local:
                drop_idx = i - num_local
                if drop_idx < len(dropped_objs):
                    sub_val = (request.POST.get(f'drop_subscriber_{dropped_objs[drop_idx].id}', '') or '').strip() or None
            file_subscribers.append(sub_val)

        template_signatures = list(
            MappingTemplate.objects.filter(user=request.user)
            .values_list('header_signature', flat=True)
        )

        resp = _handle_free_upload(
            request,
            excel_files,
            template_signatures,
            file_passwords=file_passwords,
            file_subscribers=file_subscribers,
            default_subscriber=default_subscriber,
        )

        # Mark imported dropped files as completed
        for drop_obj in dropped_objs:
            try:
                drop_obj.status = 'imported'
                drop_obj.imported_by = request.user
                drop_obj.imported_at = timezone.now()
                drop_obj.save(update_fields=['status', 'imported_by', 'imported_at'])
            except Exception as e:
                logger.warning(f"Failed to update status for dropped file {drop_obj.id}: {e}")

        return resp
    
    # Aggregate stats from ALL user sessions (not just displayed rows)
    all_user_sessions = UploadSession.objects.filter(user=request.user)
    total_count = all_user_sessions.count()
    uploaded_count = all_user_sessions.filter(status='uploaded').count()
    processed_count = all_user_sessions.filter(status='processed').count()
    pending_count = all_user_sessions.filter(status__in=['pending_mapping', 'processing', 'uploading_to_db']).count()
    error_count = all_user_sessions.filter(status='error').count()

    from django.db.models import Sum
    total_rows_cleaned = all_user_sessions.filter(status__in=['uploaded', 'processed']).aggregate(total=Sum('rows_processed'))['total'] or 0

    # Fetch all errored sessions for the Error Resolution Modal
    error_sessions = list(all_user_sessions.filter(status='error').select_related('subscriber').order_by('-uploaded_at')[:50])

    # Group batch uploads: show one row per batch_id, compute accurate composite status for each batch.
    # Cap at 10 entries for the recent-uploads table.
    _all = all_user_sessions.select_related('subscriber').order_by('-uploaded_at')[:100]
    seen_batches = {}
    sessions = []
    for s in _all:
        if s.batch_id:
            if s.batch_id not in seen_batches:
                batch_sessions = list(all_user_sessions.filter(batch_id=s.batch_id))
                has_error = any(bs.status == 'error' for bs in batch_sessions)
                has_pending = any(bs.status in ['pending_mapping', 'processing', 'uploading_to_db'] for bs in batch_sessions)
                if has_error:
                    s.display_status = 'error'
                elif has_pending:
                    s.display_status = 'pending'
                else:
                    s.display_status = 'uploaded'
                s.sheet_count = len(batch_sessions)
                seen_batches[s.batch_id] = s
                sessions.append(s)
        else:
            s.display_status = 'pending' if s.status in ['pending_mapping', 'processing', 'uploading_to_db'] else ('error' if s.status == 'error' else 'uploaded')
            sessions.append(s)
        if len(sessions) >= 10:
            break

    templates = MappingTemplate.objects.filter(user=request.user)[:5]
    return render(request, 'update/upload.html', {
        'sessions': sessions,
        'templates': templates,
        'subscribers': subscribers,
        'subscriber': subscriber,
        'is_external': _is_external(request.user),
        'total_count': total_count,
        'uploaded_count': uploaded_count + processed_count,
        'pending_count': pending_count,
        'error_count': error_count,
        'total_rows_cleaned': total_rows_cleaned,
        'error_sessions': error_sessions,
    })


def _process_sheet_upload(session, file_path, sheet_name, df=None):
    """Generate SQL script at upload time. BatchUpdate upload happens after cleaning in tasks.py."""
    import logging
    logger = logging.getLogger(__name__)

    # Generate SQL script only — BatchUpdate upload deferred until after cleaning
    try:
        script_path = generate_sql_script(sheet_name)
        session.generated_script = os.path.relpath(script_path, 'media')
        session.save()
    except Exception as e:
        logger.error(f"SQL script generation failed for sheet '{sheet_name}': {e}", exc_info=True)


def _resolve_subscriber_from_name(name_str):
    parts = name_str.split('_')
    try:
        sub_id_int = int(parts[0])
    except (ValueError, IndexError):
        raise ValueError(f'Could not parse subscriber ID from "{name_str}"')
    
    # If there are at least 3 parts, the second part is the date segment
    # e.g., 446_15072026_gtb -> sub_id = 446, date = "15072026", name = "gtb"
    if len(parts) >= 3:
        sub_date = parts[1]
        sub_name = '_'.join(parts[2:])
    else:
        sub_date = None
        sub_name = name_str
        
    return sub_id_int, sub_name, sub_date


def _handle_free_upload(request, excel_files, template_signatures, file_passwords=None, file_subscribers=None, default_subscriber=None):
    """
    Unified file upload handler for both single and multi-file uploads.
    Supports:
    - Per-file subscriber assignment (via file_subscribers list)
    - Default assigned subscriber (from top toggle or user profile)
    - Auto-detection from filename pattern (subid_ddmmyyyy_name)
    - Multisheet fallback mode if single file with non-matching filename
    """
    batch_id = uuid.uuid4()
    created_sessions = []
    max_excel_size = MAX_EXCEL_FILE_SIZE_MB * 1024 * 1024
    max_csv_size = MAX_CSV_FILE_SIZE_MB * 1024 * 1024
    upload_to_db = request.POST.get('upload_to_db') == 'on'

    # Unify parsing logic: use assigned/filename mode if multiple files, or if subscriber is provided, or if filename matches pattern
    use_filename_driven = len(excel_files) > 1 or (file_subscribers and any(file_subscribers)) or (default_subscriber is not None)
    if not use_filename_driven and len(excel_files) == 1:
        try:
            _resolve_subscriber_from_name(os.path.splitext(excel_files[0].name)[0])
            use_filename_driven = True
        except ValueError:
            pass

    if use_filename_driven:
        # ── Multi-file / Assigned Subscriber mode ─────────────────────────────
        has_specific_message = False
        for idx, f in enumerate(excel_files):
            # 1. Resolve subscriber ID: per-file dropdown > default assigned > filename pattern
            target_sub_id = None
            if file_subscribers and idx < len(file_subscribers) and file_subscribers[idx]:
                try:
                    target_sub_id = int(float(file_subscribers[idx]))
                except (ValueError, TypeError):
                    target_sub_id = None

            if target_sub_id is None and default_subscriber:
                target_sub_id = default_subscriber.subscriber_id

            sub_name = None
            sub_date = None
            if target_sub_id is None:
                try:
                    target_sub_id, sub_name, sub_date = _resolve_subscriber_from_name(os.path.splitext(f.name)[0])
                except ValueError:
                    pass

            if target_sub_id is None:
                has_specific_message = True
                messages.warning(request, f'Skipping "{f.name}": please assign a subscriber for this file.')
                continue

            # Validate extension
            if not f.name.endswith(('.xlsx', '.xls', '.xlsb', '.csv')):
                has_specific_message = True
                messages.warning(request, f'Skipping "{f.name}": must be .xlsx, .xls, .xlsb, or .csv.')
                continue

            is_csv = f.name.endswith('.csv')
            is_xlsb_file = f.name.endswith('.xlsb')
            limit_bytes = max_csv_size if is_csv else max_excel_size
            limit_mb = MAX_CSV_FILE_SIZE_MB if is_csv else MAX_EXCEL_FILE_SIZE_MB
            if f.size > limit_bytes:
                has_specific_message = True
                messages.warning(request, f'Skipping "{f.name}": file too large (max {limit_mb} MB).')
                continue

            subscriber = Subscriber.objects.filter(subscriber_id=target_sub_id).first()
            if not subscriber:
                sub_list = get_subscribers_from_batchupdate()
                sub_match = next((s for s in sub_list if s['subscriber_id'] == target_sub_id), None)
                sname = sub_match['subscriber_name'] if sub_match else (sub_name or f'Subscriber {target_sub_id}')
                subscriber, _ = Subscriber.objects.get_or_create(
                    subscriber_id=target_sub_id,
                    defaults={'subscriber_name': sname},
                )

            sheet_name = build_sheet_name(subscriber, date=sub_date)

            # Decrypt if a password was supplied for this file (Excel only)
            password = (file_passwords[idx] if file_passwords and idx < len(file_passwords) else None)
            file_to_save = f
            if password and not is_csv:
                try:
                    xl = read_excel_file(f, f.name, password=password)
                    decrypted_buf = BytesIO()
                    with pd.ExcelWriter(decrypted_buf, engine='openpyxl') as writer:
                        for sheet in xl.sheet_names:
                            xl.parse(sheet, dtype=str).to_excel(writer, sheet_name=sheet, index=False)
                    decrypted_buf.seek(0)
                    file_to_save = ContentFile(decrypted_buf.read(), name=f.name)
                except ValueError as exc:
                    has_specific_message = True
                    messages.warning(request, f'Skipping "{f.name}": {exc}')
                    continue

            session = UploadSession.objects.create(
                user=request.user,
                original_file=file_to_save,
                original_filename=f.name,
                status='pending_mapping',
                sheet_name=sheet_name,
                batch_id=batch_id,
                source_filename=f.name,
                subscriber=subscriber,
                upload_to_db=upload_to_db,
            )
            file_path = session.original_file.path

            # Detect sheets
            try:
                sheet_names = get_excel_sheet_names(file_path)
            except Exception as exc:
                has_specific_message = True
                logger.error(f"Could not read sheets from uploaded file: {exc}", exc_info=True)
                try:
                    session.original_file.delete(save=False)
                    session.delete()
                except Exception:
                    pass
                messages.warning(request, f'Could not read the uploaded file "{f.name}". Skipping.')
                continue

            if sheet_names is not None and len(sheet_names) > 1:
                # Multi-sheet Excel in multi-file upload — mirrors single-subscriber
                # multi-sheet logic: use file-level subscriber + build_sheet_name.
                session.delete()

                # Rename source file to prevent overwrite during sheet extraction
                # (build_sheet_name can produce a name matching the original filename).
                src_read_path = file_path + '.multisheet_src'
                shutil.copy2(file_path, src_read_path)

                try:
                    for sheet_index, sname in enumerate(sheet_names, start=1):
                        try:
                            hrow = detect_header_row(src_read_path, sheet_name=sname, template_signatures=template_signatures)
                            df = read_uploaded_file_sheet(src_read_path, sheet_name=sname, header=hrow)
                        except Exception as e:
                            messages.warning(request, f'Could not read sheet "{sname}" from "{f.name}": {e}')
                            continue

                        if not is_sheet_usable(df, template_signatures=template_signatures):
                            logger.info(f"Skipping unusable sheet '{sname}' in '{f.name}'")
                            messages.info(request, f'Skipped sheet "{sname}" from "{f.name}": empty or no recognized column headers.')
                            continue

                        sheet_name_override = None
                        sheet_date = sub_date
                        try:
                            _, parsed_name, parsed_date = _resolve_subscriber_from_name(sname)
                            if parsed_name and parsed_name != sname:
                                sheet_name_override = parsed_name
                            if parsed_date:
                                sheet_date = parsed_date
                        except Exception:
                            pass

                        if not sheet_name_override:
                            sheet_name_override = sub_name

                        sheet_name = build_sheet_name(
                            subscriber,
                            date=sheet_date,
                            index=sheet_index - 1 if sheet_index > 1 else None,
                            name_override=sheet_name_override
                        )
                        sheet_filename = f"{sheet_name}.xlsx"
                        sheet_dir = os.path.join('media', 'uploads')
                        os.makedirs(sheet_dir, exist_ok=True)
                        sheet_path = os.path.join(sheet_dir, sheet_filename)
                        df.to_excel(sheet_path, index=False, engine='openpyxl')

                        sheet_session = UploadSession.objects.create(
                            user=request.user,
                            original_file=f'uploads/{sheet_filename}',
                            original_filename=sname,
                            status='pending_mapping',
                            sheet_name=sheet_name,
                            batch_id=batch_id,
                            source_filename=f.name,
                            header_row=0,
                            subscriber=subscriber,
                            upload_to_db=upload_to_db,
                        )

                        _process_sheet_upload(sheet_session, sheet_path, sheet_name, df=df)

                        try:
                            auto_map_res = _try_auto_map(request, sheet_session, df)
                            if auto_map_res.get('is_complete'):
                                sheet_session.status = 'processing'
                                sheet_session.save()
                                async_task('update.tasks.process_file_task', sheet_session.id)
                            else:
                                sheet_session.status = 'pending_mapping'
                                sheet_session.save()
                                _flash_auto_map_warning(request, sheet_session, auto_map_res, sheet_name=sname)
                        except Exception:
                            logger.warning(f"Auto-map failed for session {sheet_session.id}", exc_info=True)

                        created_sessions.append(sheet_session)
                finally:
                    try:
                        os.remove(src_read_path)
                    except OSError:
                        pass

            else:
                # Single sheet or CSV — use the created session directly
                _process_sheet_upload(session, file_path, sheet_name)

                try:
                    hrow = detect_header_row(file_path, template_signatures=template_signatures)
                    session.header_row = hrow
                    session.save()
                    df = read_uploaded_file(file_path, header=hrow, nrows=0)  # headers only — data loaded in task
                    auto_map_res = _try_auto_map(request, session, df)
                    if auto_map_res.get('is_complete'):
                        session.status = 'processing'
                        session.save()
                        async_task('update.tasks.process_file_task', session.id)
                    else:
                        session.status = 'pending_mapping'
                        session.save()
                        _flash_auto_map_warning(request, session, auto_map_res)
                except Exception:
                    logger.warning(f"Header detection / auto-map failed for session {session.id}", exc_info=True)
                created_sessions.append(session)

        if not created_sessions:
            logger.warning(
                "Free upload: no files processed",
                extra={'user': request.user.username},
            )
            if not has_specific_message:
                messages.error(request, 'No files could be processed. Please check your uploaded files.')
            return redirect('upload')

        if len(created_sessions) > 1:
            return redirect(f"{reverse('batch', kwargs={'batch_id': batch_id})}?new_upload=1")
        else:
            session = created_sessions[0]
            if session.status == 'processing':
                return redirect('process', session_id=session.id)
            return redirect(f"{reverse('mapping', kwargs={'session_id': session.id})}?needs_mapping=1")

    else:
        # ── Single-file multisheet mode ────────────────────────────────────────
        f = excel_files[0]

        if f.size > (max_csv_size if f.name.endswith('.csv') else max_excel_size):
            limit = MAX_CSV_FILE_SIZE_MB if f.name.endswith('.csv') else MAX_EXCEL_FILE_SIZE_MB
            messages.error(request, f'File too large. Maximum size is {limit} MB.')
            return redirect('upload')
        if not f.name.endswith(('.xlsx', '.xls', '.xlsb', '.csv')):
            messages.error(request, 'Please upload an Excel or CSV file (.xlsx, .xls, .xlsb, .csv).')
            return redirect('upload')

        # Decrypt if a password was supplied
        password = (file_passwords[0] if file_passwords else None)
        file_to_save = f
        if password and not f.name.endswith('.csv'):
            try:
                xl = read_excel_file(f, f.name, password=password)
                decrypted_buf = BytesIO()
                with pd.ExcelWriter(decrypted_buf, engine='openpyxl') as writer:
                    for sheet in xl.sheet_names:
                        xl.parse(sheet, dtype=str).to_excel(writer, sheet_name=sheet, index=False)
                decrypted_buf.seek(0)
                file_to_save = ContentFile(decrypted_buf.read(), name=f.name)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect('upload')

        # Save temporarily to read sheet names
        temp_session = UploadSession.objects.create(
            user=request.user,
            original_file=file_to_save,
            original_filename=f.name,
            status='pending_mapping',
        )
        file_path = temp_session.original_file.path

        # Clean up temp session if the file cannot be read
        try:
            sheet_names = get_excel_sheet_names(file_path)
        except Exception as exc:
            logger.error(f"Could not read sheets from uploaded file: {exc}", exc_info=True)
            try:
                temp_session.original_file.delete(save=False)
                temp_session.delete()
            except Exception:
                pass
            messages.error(request, 'Could not read the uploaded file. Please ensure it is a valid Excel or CSV file.')
            return redirect('upload')

        if sheet_names is None or len(sheet_names) <= 1:
            # Single sheet — subscriber from filename
            sheet_name = os.path.splitext(f.name)[0]
            try:
                sub_id_int, sub_name, sub_date = _resolve_subscriber_from_name(sheet_name)
            except ValueError:
                temp_session.delete()
                messages.error(request, 'Filename does not match the expected pattern (subid_ddmmyyyy_name).')
                return redirect('upload')

            subscriber, _ = Subscriber.objects.get_or_create(
                subscriber_id=sub_id_int,
                defaults={'subscriber_name': sub_name},
            )
            temp_session.sheet_name = sheet_name
            temp_session.subscriber = subscriber
            temp_session.save()

            # Rename the sheet tab inside the Excel file to match the filename convention
            if file_path.endswith(('.xlsx', '.xls')):
                try:
                    wb = load_workbook(file_path)
                    wb.active.title = sheet_name[:31]  # Excel max sheet name length
                    wb.save(file_path)
                except Exception:
                    logger.warning(f"Could not rename sheet tab in {file_path}", exc_info=True)

            _process_sheet_upload(temp_session, file_path, sheet_name)

            try:
                hrow = detect_header_row(file_path, template_signatures=template_signatures)
                temp_session.header_row = hrow
                temp_session.save()
                df = read_uploaded_file(file_path, header=hrow, nrows=0)  # headers only — data loaded in task
                auto_map_res = _try_auto_map(request, temp_session, df)
                if auto_map_res.get('is_complete'):
                    temp_session.status = 'processing'
                    temp_session.save()
                    async_task('update.tasks.process_file_task', temp_session.id)
                    return redirect('process', session_id=temp_session.id)
                else:
                    temp_session.status = 'pending_mapping'
                    temp_session.save()
                    _flash_auto_map_warning(request, temp_session, auto_map_res)
            except Exception:
                logger.warning(f"Header detection / auto-map failed for session {temp_session.id}", exc_info=True)

            return redirect(f"{reverse('mapping', kwargs={'session_id': temp_session.id})}?needs_mapping=1")

        else:
            # Multisheet — subscriber from each sheet tab name
            temp_session.delete()

            for sname in sheet_names:
                try:
                    sub_id_int, sub_name, sub_date = _resolve_subscriber_from_name(sname)
                except ValueError:
                    messages.warning(request, f'Skipping sheet "{sname}": tab name does not match expected pattern (subid_ddmmyyyy_name).')
                    continue

                try:
                    hrow = detect_header_row(file_path, sheet_name=sname, template_signatures=template_signatures)
                    df = read_uploaded_file_sheet(file_path, sheet_name=sname, header=hrow)
                except Exception as e:
                    messages.warning(request, f'Could not read sheet "{sname}": {e}')
                    continue

                if not is_sheet_usable(df, template_signatures=template_signatures):
                    logger.info(f"Skipping unusable sheet '{sname}' in '{f.name}'")
                    messages.info(request, f'Skipped sheet "{sname}": empty or no recognized column headers.')
                    continue

                subscriber, _ = Subscriber.objects.get_or_create(
                    subscriber_id=sub_id_int,
                    defaults={'subscriber_name': sub_name},
                )

                sheet_filename = f"{sname}.xlsx"
                sheet_dir = os.path.join('media', 'uploads')
                os.makedirs(sheet_dir, exist_ok=True)
                sheet_path = os.path.join(sheet_dir, sheet_filename)
                df.to_excel(sheet_path, index=False, engine='openpyxl')

                session = UploadSession.objects.create(
                    user=request.user,
                    original_file=f'uploads/{sheet_filename}',
                    original_filename=sname,
                    status='pending_mapping',
                    sheet_name=sname,
                    batch_id=batch_id,
                    source_filename=f.name,
                    header_row=0,
                    subscriber=subscriber,
                )

                _process_sheet_upload(session, sheet_path, sname, df=df)

                try:
                    auto_map_res = _try_auto_map(request, session, df)
                    if auto_map_res.get('is_complete'):
                        session.status = 'processing'
                        session.save()
                        async_task('update.tasks.process_file_task', session.id)
                    else:
                        session.status = 'pending_mapping'
                        session.save()
                        _flash_auto_map_warning(request, session, auto_map_res, sheet_name=sname)
                except Exception:
                    logger.warning(f"Auto-map failed for session {session.id}", exc_info=True)

                created_sessions.append(session)  # df goes out of scope here

            if not created_sessions:
                messages.error(request, 'No sheets could be processed. Check that sheet tab names follow the pattern: subid_ddmmyyyy_name')
                return redirect('upload')

            return redirect(f"{reverse('batch', kwargs={'batch_id': batch_id})}?new_upload=1")


def _flash_auto_map_warning(request, session, auto_map_res, sheet_name=None):
    """Display user-friendly flash message explaining why auto-processing was halted."""
    label = f"sheet '{sheet_name}'" if sheet_name else f"'{session.original_filename}'"
    sub_label = f"by {session.subscriber}" if session.subscriber else "for this subscriber"
    if auto_map_res.get('missing_historical_targets'):
        missing_names = [DISPLAY_HEADERS.get(t, t) for t in auto_map_res['missing_historical_targets']]
        messages.warning(
            request,
            f"Column(s) previously provided {sub_label} were not recognized in {label}: "
            f"{', '.join(missing_names)}. Please verify column mapping before processing."
        )
    elif not auto_map_res.get('has_acct'):
        messages.warning(
            request,
            f"Account Number column could not be auto-detected in {label}. Please map Account Number."
        )
    elif not auto_map_res.get('is_complete'):
        messages.info(
            request,
            f"First-time upload for {session.subscriber or 'subscriber'}: please review and confirm column mappings."
        )


def _try_auto_map(request, session, df):
    """
    Try to auto-apply a saved mapping template, with subscriber-based fallback
    and historical target column completeness verification.
    
    Returns:
        dict: {
            'is_complete': bool,
            'missing_historical_targets': list,
            'resolved_mappings': dict,
            'has_acct': bool,
            'historical_targets': list,
        }
    """
    headers = sorted(list(df.columns))
    header_signature = json.dumps(headers)
    
    resolved_mappings = {}
    applied_stage = None

    # 1. Exact header match via saved MappingTemplate (fastest path)
    # Match in Python to avoid SQL Server NTEXT/NVARCHAR equality error (pyodbc 42000/402)
    template = next(
        (t for t in MappingTemplate.objects.filter(user=request.user)
         if t.header_signature == header_signature),
        None,
    )

    if template:
        applied_stage = "Stage 1 (Exact template match)"
        resolved_mappings = {h: t for h, t in template.mappings.items() if t}
        template.use_count += 1
        template.save()

    # 2. Subscriber-based fallback: reuse mapping from a previous session for the same subscriber.
    #    This means re-uploads for the same subscriber never require manual re-mapping as long as
    #    the column structure is unchanged — even across different users or upload dates.
    if not resolved_mappings and session.subscriber_id:
        prev_sessions = (
            UploadSession.objects
            .filter(subscriber_id=session.subscriber_id)
            .exclude(id=session.id)
            .prefetch_related('mappings')
            .order_by('-uploaded_at')[:10]
        )
        for prev in prev_sessions:
            prev_mapping_objs = list(prev.mappings.all())
            if not prev_mapping_objs:
                continue
            # Only consider sessions whose full header set matches the current file
            prev_all_headers = sorted(m.original_header for m in prev_mapping_objs)
            if prev_all_headers != headers:
                continue
            # Build a dict of only the columns that were actually mapped
            prev_map = {m.original_header: m.target_column for m in prev_mapping_objs if m.target_column}
            if prev_map:
                resolved_mappings = prev_map
                applied_stage = f"Stage 2 (Subscriber fallback from Session {prev.id})"
                break

    # 3. Always apply heuristic dictionary fallback to any columns that remain unmapped.
    #    This catches newly added or previously skipped required columns like CurrentBalanceAmt.
    mapped_targets = set(resolved_mappings.values())
    heuristic_mappings = {}
    for header in df.columns:
        if header in resolved_mappings:
            continue
        cleaned_header = str(header).strip().lower()
        matched_target = None
        for target_col, synonyms in HEADER_MAPPING_DICTIONARY.items():
            if target_col in mapped_targets:
                continue  # Prevent mapping multiple headers to the same target column
            if cleaned_header in synonyms:
                matched_target = target_col
                break
        if matched_target:
            resolved_mappings[header] = matched_target
            mapped_targets.add(matched_target)
            heuristic_mappings[header] = matched_target

    # ── Historical Target Completeness Verification ──
    current_mapped_targets = set(resolved_mappings.values())
    has_acct = 'account_number' in current_mapped_targets

    historical_targets = set()
    missing_historical_targets = set()
    if session.subscriber_id:
        historical_targets = get_subscriber_historical_targets(
            session.subscriber_id, exclude_session_id=session.id
        )
        if historical_targets:
            missing_historical_targets = historical_targets - current_mapped_targets

    if historical_targets:
        # Known subscriber: must have Account Number AND all historically provided targets
        is_complete = has_acct and (len(missing_historical_targets) == 0)
    else:
        # First-time subscriber: must have Account Number, at least one other target,
        # AND no unmapped columns in the file (if file has unmapped columns, prompt confirmation)
        unmapped_headers = [c for c in df.columns if c not in resolved_mappings]
        is_complete = has_acct and len(current_mapped_targets) >= 2 and (len(unmapped_headers) == 0)

    # Create ColumnMapping objects for all mapped columns so UI displays them
    if resolved_mappings:
        session.mappings.all().delete()
        
        if applied_stage:
            if heuristic_mappings:
                logger.info(f"[Session {session.id}] Auto-mapped via {applied_stage}, and heuristic matched remaining: {heuristic_mappings}. Mappings: {resolved_mappings}")
            else:
                logger.info(f"[Session {session.id}] Auto-mapped via {applied_stage}. Mappings: {resolved_mappings}")
        else:
            logger.info(f"[Session {session.id}] Auto-mapped via Stage 3 (Heuristic dictionary). Mappings: {resolved_mappings}")

        for header, target in resolved_mappings.items():
            ColumnMapping.objects.create(
                session=session,
                original_header=header,
                target_column=target,
            )
        
        # Save as a MappingTemplate ONLY if mapping is fully complete!
        # Do not save incomplete mappings where historical columns are missing.
        if is_complete:
            name = (
                f"Auto Dict: {session.subscriber} ({session.original_filename[:25]})"
                if session.subscriber_id
                else f"Auto Dict from {session.original_filename[:30]}"
            )
            existing_tpl = next(
                (t for t in MappingTemplate.objects.filter(user=request.user)
                 if t.header_signature == header_signature),
                None,
            )
            if existing_tpl:
                existing_tpl.name = name
                existing_tpl.mappings = resolved_mappings
                existing_tpl.save()
            else:
                MappingTemplate.objects.create(
                    user=request.user,
                    header_signature=header_signature,
                    name=name,
                    mappings=resolved_mappings,
                )
        else:
            logger.info(
                f"[Session {session.id}] Incomplete auto-map: missing historical targets={missing_historical_targets}. "
                f"Halting auto-processing and withholding template save."
            )

    return {
        'is_complete': is_complete,
        'missing_historical_targets': list(missing_historical_targets),
        'resolved_mappings': resolved_mappings,
        'has_acct': has_acct,
        'historical_targets': list(historical_targets),
    }



@login_required
def mapping_view(request, session_id):
    """Interactive column mapping page"""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)
    
    try:
        headers = get_file_headers(session.original_file.path, header_row=session.header_row)
    except Exception as e:
        messages.error(request, f'Error reading file: {str(e)}')
        return redirect('upload')
    
    existing_mappings = {m.original_header: m.target_column for m in session.mappings.all()}
    target_columns = TARGET_COLUMN_CHOICES
    
    if request.method == 'POST':
        session.mappings.all().delete()
        mappings_dict = {}
        
        for header in headers:
            target = request.POST.get(f'mapping_{header}', '')
            ColumnMapping.objects.create(
                session=session,
                original_header=header,
                target_column=target
            )
            if target:
                mappings_dict[header] = target

        # Enforce that Account Number is mapped
        if 'account_number' not in mappings_dict.values():
            messages.error(request, 'Account Number column is required. Please select which column contains Account Numbers.')
            return render(request, 'update/mapping.html', {
                'session': session,
                'headers': headers,
                'target_columns': target_columns,
                'existing_mappings': mappings_dict,
            })
        
        # Always save/update the template when a subscriber is set (enables future auto-mapping).
        # Also honour the explicit "save template" checkbox for sessions without a subscriber.
        if mappings_dict and (session.subscriber_id or request.POST.get('save_template')):
            header_signature = json.dumps(sorted(headers))
            name = (
                f"Auto: {session.subscriber} ({session.original_filename[:25]})"
                if session.subscriber_id
                else f"Template from {session.original_filename[:30]}"
            )
            # Match in Python to avoid SQL Server NTEXT/NVARCHAR equality error
            existing_tpl = next(
                (t for t in MappingTemplate.objects.filter(user=request.user)
                 if t.header_signature == header_signature),
                None,
            )
            if existing_tpl:
                existing_tpl.name = name
                existing_tpl.mappings = mappings_dict
                existing_tpl.save()
            else:
                MappingTemplate.objects.create(
                    user=request.user,
                    header_signature=header_signature,
                    name=name,
                    mappings=mappings_dict,
                )

        return redirect('process', session_id=session.id)
    
    return render(request, 'update/mapping.html', {
        'session': session,
        'headers': headers,
        'target_columns': target_columns,
        'existing_mappings': existing_mappings,
    })


@login_required
def process_view(request, session_id):
    """Start async processing and show progress page"""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)
    
    # Check if mappings exist
    mappings = session.mappings.filter(target_column__isnull=False).exclude(target_column='')
    if not mappings.exists():
        messages.error(request, 'No columns mapped. Please map at least one column.')
        return redirect('mapping', session_id=session.id)
    
    # Only start task if not already processing
    if session.status != 'processing':
        session.status = 'processing'
        session.error_message = ""
        session.save()
        async_task('update.tasks.process_file_task', session_id)
    
    # Render processing page - polls session status
    return render(request, 'update/processing.html', {
        'session': session,
    })


@login_required
def task_progress_view(request, session_id):
    """API endpoint to check task progress — returns cache-based granular data."""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)

    if session.status == 'processing':
        prog = get_progress(session_id, 'clean')
        return JsonResponse({
            'phase': 'clean',
            'current': prog['percent'],
            'status': prog['step'],
            'detail': prog.get('detail', ''),
        })
    elif session.status == 'uploading_to_db':
        prog = get_progress(session_id, 'dbupload')
        return JsonResponse({
            'phase': 'dbupload',
            'current': prog['percent'],
            'status': prog['step'],
            'detail': prog.get('detail', ''),
        })
    elif session.status in ('processed', 'uploaded'):
        return JsonResponse({
            'phase': 'done',
            'current': 100,
            'status': 'Complete!',
            'rows_processed': session.rows_processed,
            'rows_uploaded': session.rows_uploaded,
        })
    elif session.status == 'error':
        return JsonResponse({
            'current': 100,
            'status': session.error_message,
            'error': True,
        })
    else:
        return JsonResponse({'current': 0, 'status': 'Waiting to start...'})


@login_required
def result_view(request, session_id):
    """Show processing results"""
    session = get_object_or_404(
        UploadSession.objects.select_related('subscriber'),
        id=session_id, user=request.user
    )
    mappings = session.mappings.filter(target_column__isnull=False).exclude(target_column='')
    external = _is_external(request.user)

    duplicate_artifact = None
    if session.content_fingerprint:
        from .models import ImplementedArtifact
        duplicate_artifact = ImplementedArtifact.objects.filter(
            content_fingerprint=session.content_fingerprint
        ).exclude(source_session_id=session.id).first()

    return render(request, 'update/result.html', {
        'session': session,
        'mappings': mappings,
        'is_external': external,
        'duplicate_artifact': duplicate_artifact,
    })


def _stream_parquet_as_excel(parquet_path, base_name, response):
    """
    Read a Parquet file and stream-write it to an Excel workbook in constant_memory mode.
    The workbook writes directly to the response object.
    """
    import pyarrow.parquet as pq
    import xlsxwriter
    import pandas as pd

    workbook = xlsxwriter.Workbook(response, {'constant_memory': True})
    sheet_title = base_name[:31]  # Excel sheet name limit
    ws = workbook.add_worksheet(sheet_title)

    # Formats must be combined upfront — xlsxwriter applies format at write time
    header_format = workbook.add_format({'bold': False})
    # AccountNo: text + left-aligned in one combined format
    account_format = workbook.add_format({'num_format': '@', 'align': 'left'})
    text_format = workbook.add_format({'num_format': '@'})
    numeric_format = workbook.add_format({'num_format': 'General'})

    numeric_names = {'CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears'}
    text_names = {'LoanClassification', 'AccountStatusCode'}
    account_no_names = {'AccountNo'}

    # Read schema/headers from Parquet metadata
    pf = pq.ParquetFile(parquet_path)
    headers = pf.schema_arrow.names

    # Write header row
    for col_idx, header in enumerate(headers):
        ws.write(0, col_idx, header, header_format)

    # Write data rows in batches to keep memory flat
    row_idx = 1
    for batch in pf.iter_batches(batch_size=5000):
        df = batch.to_pandas()
        for row in df.itertuples(index=False):
            for col_idx, val in enumerate(row):
                header = headers[col_idx]
                if pd.isna(val) or val is None:
                    ws.write_blank(row_idx, col_idx, None)
                    continue

                if header in account_no_names:
                    ws.write_string(row_idx, col_idx, str(val), account_format)
                elif header in text_names:
                    ws.write_string(row_idx, col_idx, str(val), text_format)
                elif header in numeric_names:
                    try:
                        ws.write_number(row_idx, col_idx, float(val), numeric_format)
                    except (ValueError, TypeError):
                        ws.write(row_idx, col_idx, val, numeric_format)
                else:
                    ws.write(row_idx, col_idx, val)
            row_idx += 1

    workbook.close()


@login_required
def download_view(request, session_id):
    """Download processed Excel file — blocked for external users."""
    if _is_external(request.user):
        messages.error(request, 'You do not have permission to download processed files.')
        return redirect('result', session_id=session_id)
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)
    
    if not session.processed_file:
        messages.error(request, 'No processed file available')
        return redirect('result', session_id=session.id)
    
    file_path = session.processed_file.path
    base_name = session.sheet_name if session.sheet_name else (os.path.splitext(session.original_filename)[0] if session.original_filename else 'cleaned')

    from django.conf import settings
    excel_filename = f"processed_{base_name}.xlsx"
    excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', excel_filename)

    if os.path.exists(excel_path):
        response = FileResponse(open(excel_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="cleaned_{base_name}.xlsx"'
        return response

    try:
        write_parquet_as_excel(file_path, base_name, excel_path)
        response = FileResponse(open(excel_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="cleaned_{base_name}.xlsx"'
        return response
    except Exception as e:
        logger.error(f"Failed to generate/stream processed Excel for session {session_id}: {e}", exc_info=True)
        messages.error(request, f"Error generating Excel download: {str(e)}")
        return redirect('result', session_id=session.id)


@login_required
def download_rejected_view(request, session_id):
    """Download rejected rows Excel file"""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)
    
    if not session.rejected_file:
        messages.error(request, 'No rejected rows file available')
        return redirect('result', session_id=session.id)
    
    file_path = session.rejected_file.path
    base_name = session.sheet_name if session.sheet_name else (os.path.splitext(session.original_filename)[0] if session.original_filename else 'rejected')

    from django.conf import settings
    excel_filename = f"rejected_{base_name}.xlsx"
    excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', excel_filename)

    if os.path.exists(excel_path):
        response = FileResponse(open(excel_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="rejected_{base_name}.xlsx"'
        return response

    try:
        write_parquet_as_excel(file_path, f"rejected_{base_name}", excel_path)
        response = FileResponse(open(excel_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="rejected_{base_name}.xlsx"'
        return response
    except Exception as e:
        logger.error(f"Failed to generate/stream rejected Excel for session {session_id}: {e}", exc_info=True)
        messages.error(request, f"Error generating Excel download: {str(e)}")
        return redirect('result', session_id=session.id)


@login_required
@require_POST
def undo_upload_view(request, session_id):
    """Undo/rollback — revert session status back to processed."""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)

    if session.status != 'uploaded':
        messages.error(request, 'Cannot undo - no records were uploaded from this session.')
        return redirect('result', session_id=session.id)

    try:
        session.status = 'processed'
        session.rows_uploaded = 0
        session.batchupdate_uploaded = False
        session.save()
        messages.success(request, 'Session reverted to processed state.')
    except Exception as e:
        messages.error(request, f'Undo failed: {str(e)}')

    return redirect('result', session_id=session.id)


@login_required
@require_POST
def upload_to_db_view(request, session_id):
    """Trigger BatchUpdate DB upload for a single already-processed session."""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)

    if session.status != 'processed' or not session.processed_file or not session.sheet_name:
        return JsonResponse({'error': 'Session is not eligible for upload'}, status=400)

    # Check if duplicate of an already implemented table
    if session.content_fingerprint:
        from .models import ImplementedArtifact
        existing = ImplementedArtifact.objects.filter(
            content_fingerprint=session.content_fingerprint
        ).exclude(source_session_id=session.id).first()
        if existing:
            return JsonResponse({
                'error': f'Upload blocked: Identical dataset already exists in BatchUpdate table [{existing.table_name}] uploaded on {existing.implemented_at.strftime("%Y-%m-%d")}.'
            }, status=400)

    session.status = 'uploading_to_db'
    session.error_message = ''
    session.save()
    async_task('update.tasks.upload_to_db_task', session_id)

    return JsonResponse({'ok': True, 'message': 'Upload started'})


@login_required
@require_POST
def upload_batch_to_db_view(request, batch_id):
    """Trigger BatchUpdate DB upload for ALL processed sessions in a batch."""
    sessions = UploadSession.objects.filter(
        user=request.user,
        batch_id=batch_id,
        status='processed',
    ).exclude(batchupdate_uploaded=True)

    from .models import ImplementedArtifact
    seen_fingerprints = set()
    count = 0
    skipped = 0

    for session in sessions:
        if session.processed_file and session.sheet_name:
            fp = session.content_fingerprint
            if fp:
                # Check intra-batch duplicate or existing implemented artifact
                if fp in seen_fingerprints:
                    session.error_message = 'Duplicate skipped: Identical to another file in this batch.'
                    session.save(update_fields=['error_message'])
                    skipped += 1
                    continue

                existing = ImplementedArtifact.objects.filter(
                    content_fingerprint=fp
                ).exclude(source_session_id=session.id).first()
                if existing:
                    session.error_message = f'Duplicate skipped: Already exists in table [{existing.table_name}].'
                    session.save(update_fields=['error_message'])
                    skipped += 1
                    continue

                seen_fingerprints.add(fp)

            session.status = 'uploading_to_db'
            session.error_message = ''
            session.save()
            async_task('update.tasks.upload_to_db_task', session.id)
            count += 1

    return JsonResponse({'ok': True, 'queued': count, 'skipped': skipped})


@login_required
@require_POST
def retry_session_view(request, session_id):
    """Re-queue processing task for an errored or failed session."""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)

    mappings = session.mappings.filter(target_column__isnull=False).exclude(target_column='')
    if not mappings.exists():
        messages.error(request, 'No columns mapped. Please map at least one column first.')
        return redirect('mapping', session_id=session.id)

    session.status = 'processing'
    session.error_message = ''
    session.save()
    async_task('update.tasks.process_file_task', session.id)
    messages.success(request, f'Retrying processing for {session.original_filename or "session"}.')
    return redirect('process', session_id=session.id)


@login_required
@require_POST
def delete_session_view(request, session_id):
    """Delete an upload session and its associated data"""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)

    try:
        filename = session.original_filename
        
        # Clean up cached Excel files
        from django.conf import settings
        base_name = session.sheet_name if session.sheet_name else os.path.splitext(session.original_filename)[0]
        for cache_name in (f"processed_{base_name}.xlsx", f"rejected_{base_name}.xlsx"):
            cache_path = os.path.join(settings.MEDIA_ROOT, 'processed', cache_name)
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except Exception:
                    pass

        # Invalidate batch combined cache if session belongs to a batch
        if session.batch_id:
            batch_excel_filename = f"batch_{session.batch_id}.xlsx"
            batch_excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', batch_excel_filename)
            if os.path.exists(batch_excel_path):
                try:
                    os.remove(batch_excel_path)
                except Exception:
                    pass

        session.delete()

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            from django.db.models import Sum
            all_s = UploadSession.objects.filter(user=request.user)
            return JsonResponse({
                'success': True,
                'message': f'Deleted session: {filename}',
                'total_count': all_s.count(),
                'uploaded_count': all_s.filter(status__in=['uploaded', 'processed']).count(),
                'pending_count': all_s.filter(status__in=['pending_mapping', 'processing', 'uploading_to_db']).count(),
                'error_count': all_s.filter(status='error').count(),
                'total_rows_cleaned': all_s.filter(status__in=['uploaded', 'processed']).aggregate(total=Sum('rows_processed'))['total'] or 0,
            })

        messages.success(request, f'Deleted session: {filename}')
    except Exception as e:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': str(e)}, status=400)
        messages.error(request, f'Delete failed: {str(e)}')

    return redirect('upload')


@login_required
@require_POST
def delete_batch_view(request, batch_id):
    """Delete all sessions belonging to a batch."""
    sessions = UploadSession.objects.filter(user=request.user, batch_id=batch_id)
    count = sessions.count()
    if count == 0:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'Batch not found.'}, status=404)
        messages.error(request, 'Batch not found.')
    else:
        # Delete combined batch Excel and individual sheet cached Excel files from disk
        from django.conf import settings
        batch_excel_filename = f"batch_{batch_id}.xlsx"
        batch_excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', batch_excel_filename)
        if os.path.exists(batch_excel_path):
            try:
                os.remove(batch_excel_path)
            except Exception:
                pass
                
        for s in sessions:
            base_name = s.sheet_name if s.sheet_name else os.path.splitext(s.original_filename)[0]
            for cache_name in (f"processed_{base_name}.xlsx", f"rejected_{base_name}.xlsx"):
                cache_path = os.path.join(settings.MEDIA_ROOT, 'processed', cache_name)
                if os.path.exists(cache_path):
                    try:
                        os.remove(cache_path)
                    except Exception:
                        pass

        sessions.delete()

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            from django.db.models import Sum
            all_s = UploadSession.objects.filter(user=request.user)
            return JsonResponse({
                'success': True,
                'message': f'Deleted batch ({count} session{"s" if count != 1 else ""}).',
                'total_count': all_s.count(),
                'uploaded_count': all_s.filter(status__in=['uploaded', 'processed']).count(),
                'pending_count': all_s.filter(status__in=['pending_mapping', 'processing', 'uploading_to_db']).count(),
                'error_count': all_s.filter(status='error').count(),
                'total_rows_cleaned': all_s.filter(status__in=['uploaded', 'processed']).aggregate(total=Sum('rows_processed'))['total'] or 0,
            })

        messages.success(request, f'Deleted batch ({count} session{"s" if count != 1 else ""}).')
    return redirect('upload')


@login_required
@require_POST
def clear_all_errors_view(request):
    """Delete all errored sessions for the user."""
    count, _ = UploadSession.objects.filter(user=request.user, status='error').delete()
    messages.success(request, f'Cleared {count} failed upload session{"s" if count != 1 else ""}.')
    return redirect('upload')


@login_required
@require_POST
def clear_all_pending_view(request):
    """Delete all pending/stuck sessions for the user."""
    count, _ = UploadSession.objects.filter(
        user=request.user,
        status__in=['pending_mapping', 'processing', 'uploading_to_db']
    ).delete()
    messages.success(request, f'Cleared {count} pending upload session{"s" if count != 1 else ""}.')
    return redirect('upload')


@login_required
def batch_view(request, batch_id):
    """Landing page for a multi-sheet batch upload."""
    sessions = UploadSession.objects.filter(
        user=request.user,
        batch_id=batch_id
    ).order_by('uploaded_at')
    
    if not sessions.exists():
        messages.error(request, 'Batch not found.')
        return redirect('upload')
    
    source_filename = sessions.first().source_filename or 'Unknown file'
    scripts_count = sessions.filter(generated_script__isnull=False).exclude(generated_script='').count()
    bu_count = sessions.filter(batchupdate_uploaded=True).count()
    uploaded_count = sessions.filter(status='uploaded').count()
    processed_count = sessions.filter(processed_file__isnull=False).exclude(processed_file='').count()
    has_processing = sessions.filter(status='processing').exists()
    has_uploading = sessions.filter(status='uploading_to_db').exists()
    
    unmapped_sheets = list(sessions.filter(status='pending_mapping').values_list('original_filename', flat=True))

    from .models import ImplementedArtifact
    fingerprints = [s.content_fingerprint for s in sessions if s.content_fingerprint]
    implemented_map = {}
    if fingerprints:
        for art in ImplementedArtifact.objects.filter(content_fingerprint__in=fingerprints):
            implemented_map[art.content_fingerprint] = art.table_name

    for s in sessions:
        if s.content_fingerprint and s.content_fingerprint in implemented_map:
            s.is_duplicate = True
            s.duplicate_table = implemented_map[s.content_fingerprint]
        else:
            s.is_duplicate = False
            s.duplicate_table = ''
    
    return render(request, 'update/batch.html', {
        'sessions': sessions,
        'batch_id': batch_id,
        'source_filename': source_filename,
        'scripts_count': scripts_count,
        'bu_count': bu_count,
        'uploaded_count': uploaded_count,
        'processed_count': processed_count,
        'has_processing': has_processing,
        'has_uploading': has_uploading,
        'is_external': _is_external(request.user),
        'unmapped_sheets': unmapped_sheets,
    })


@login_required
def batch_progress_view(request, batch_id):
    """AJAX endpoint returning live status for every session in the batch."""
    sessions = UploadSession.objects.filter(
        user=request.user,
        batch_id=batch_id,
    ).order_by('uploaded_at')

    from .models import ImplementedArtifact
    fingerprints = [s.content_fingerprint for s in sessions if s.content_fingerprint]
    implemented_map = {}
    if fingerprints:
        for art in ImplementedArtifact.objects.filter(content_fingerprint__in=fingerprints):
            implemented_map[art.content_fingerprint] = art.table_name

    sheets = []
    for s in sessions:
        is_dup = False
        dup_table = ''
        if s.content_fingerprint and s.content_fingerprint in implemented_map:
            is_dup = True
            dup_table = implemented_map[s.content_fingerprint]

        sheets.append({
            'id': s.id,
            'name': s.original_filename or s.sheet_name,
            'status': s.status,
            'status_display': s.get_status_display(),
            'rows_processed': s.rows_processed,
            'rows_uploaded': s.rows_uploaded,
            'rows_rejected': s.rows_rejected,
            'batchupdate_uploaded': s.batchupdate_uploaded,
            'is_duplicate': is_dup,
            'duplicate_table': dup_table,
            'error_message': s.error_message or '',
            'has_processed_file': bool(s.processed_file),
            'has_rejected_file': bool(s.rejected_file),
            'has_script': bool(s.generated_script),
        })

    # Enrich processing / uploading_to_db sessions with their live progress
    for sheet in sheets:
        if sheet['status'] == 'processing':
            prog = get_progress(sheet['id'], 'clean')
            sheet['clean_progress'] = prog.get('percent', 0)
            sheet['clean_step'] = prog.get('step', '')
        elif sheet['status'] == 'uploading_to_db':
            prog = get_progress(sheet['id'], 'dbupload')
            sheet['db_upload_progress'] = prog.get('percent', 0)
            sheet['db_upload_step'] = prog.get('step', '')

    total = len(sheets)
    done = sum(1 for s in sheets if s['status'] in ('uploaded', 'processed'))
    errored = sum(1 for s in sheets if s['status'] == 'error')
    in_progress = sum(1 for s in sheets if s['status'] in ('processing', 'uploading_to_db'))
    all_done = (done + errored) == total and in_progress == 0

    return JsonResponse({
        'sheets': sheets,
        'total': total,
        'done': done,
        'errored': errored,
        'processing': in_progress,
        'all_done': all_done,
    })


@login_required
def recent_sessions_status_api(request):
    """
    Lightweight JSON endpoint to poll current statuses for sessions displayed on the dashboard.
    Accepts comma-separated session IDs via ?ids=1,2,3 or defaults to recent active sessions.
    """
    ids_param = request.GET.get('ids', '').strip()
    qs = UploadSession.objects.filter(user=request.user)
    if ids_param:
        try:
            ids = [int(x.strip()) for x in ids_param.split(',') if x.strip().isdigit()]
            qs = qs.filter(id__in=ids)
        except Exception:
            pass
    else:
        qs = qs.filter(status__in=['processing', 'uploading_to_db', 'pending_mapping'])[:50]

    all_user_sessions = UploadSession.objects.filter(user=request.user)
    total_count = all_user_sessions.count()
    uploaded_count = all_user_sessions.filter(status__in=['uploaded', 'processed']).count()
    pending_count = all_user_sessions.filter(status__in=['pending_mapping', 'processing', 'uploading_to_db']).count()
    error_count = all_user_sessions.filter(status='error').count()
    from django.db.models import Sum
    total_rows_cleaned = all_user_sessions.filter(status__in=['uploaded', 'processed']).aggregate(total=Sum('rows_processed'))['total'] or 0

    sessions_data = []
    for s in qs:
        sessions_data.append({
            'id': s.id,
            'status': s.status,
            'status_display': s.get_status_display(),
            'rows_processed': s.rows_processed,
            'rows_uploaded': s.rows_uploaded,
            'rows_rejected': s.rows_rejected,
            'batch_id': str(s.batch_id) if s.batch_id else None,
            'error_message': s.error_message or '',
            'has_processed_file': bool(s.processed_file),
            'has_rejected_file': bool(s.rejected_file),
        })

    return JsonResponse({
        'sessions': sessions_data,
        'stats': {
            'total_count': total_count,
            'uploaded_count': uploaded_count,
            'pending_count': pending_count,
            'error_count': error_count,
            'total_rows_cleaned': total_rows_cleaned,
        }
    })


@login_required
def download_batch_combined(request, batch_id):
    """Download all cleaned sheets from a batch as a single Excel workbook."""
    import pyarrow.parquet as pq
    import xlsxwriter
    import pandas as pd
    from .services import to_excel_safe_sheet_name

    sessions = UploadSession.objects.filter(
        user=request.user,
        batch_id=batch_id
    ).order_by('uploaded_at')

    if not sessions.exists():
        messages.error(request, 'Batch not found.')
        return redirect('upload')

    source_filename = sessions.first().source_filename or str(batch_id)
    base_name = os.path.splitext(source_filename)[0]

    from django.conf import settings
    batch_excel_filename = f"batch_{batch_id}.xlsx"
    batch_excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', batch_excel_filename)

    if os.path.exists(batch_excel_path):
        response = FileResponse(open(batch_excel_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="cleaned_{base_name}.xlsx"'
        return response

    # Initialize the workbook on the filesystem path
    workbook = xlsxwriter.Workbook(batch_excel_path, {'constant_memory': True})

    # Formats must be combined upfront — xlsxwriter applies format at write time
    header_format = workbook.add_format({'bold': False})
    account_format = workbook.add_format({'num_format': '@', 'align': 'left'})
    text_format = workbook.add_format({'num_format': '@'})
    numeric_format = workbook.add_format({'num_format': 'General'})

    numeric_names = {'CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears'}
    text_names = {'LoanClassification', 'AccountStatusCode'}
    account_no_names = {'AccountNo'}

    sheets_added = 0
    file_sheet_counters = {}  # maps case-insensitive base filename to count of worksheets added

    for session in sessions:
        if not session.processed_file:
            continue
        file_path = session.processed_file.path
        if not os.path.exists(file_path):
            continue

        # Prioritize session.sheet_name so worksheet title reflects standardized naming convention
        file_name = session.sheet_name or session.source_filename or session.original_filename
        base_file_name = to_excel_safe_sheet_name(os.path.splitext(file_name)[0] if '.' in file_name else file_name)

        count = file_sheet_counters.get(base_file_name.lower(), 0)
        file_sheet_counters[base_file_name.lower()] = count + 1

        if count == 0:
            sheet_title = base_file_name
        else:
            suffix = f"_{count}"
            sheet_title = f"{base_file_name[:31 - len(suffix)]}{suffix}"

        ws = workbook.add_worksheet(sheet_title)
        
        try:
            pf = pq.ParquetFile(file_path)
            first_batch = next(pf.iter_batches(batch_size=1))
            headers = first_batch.schema.names
            
            # Write headers
            for col_idx, header in enumerate(headers):
                ws.write(0, col_idx, header, header_format)

            # Write rows in batches
            row_idx = 1
            for batch in pf.iter_batches(batch_size=5000):
                df = batch.to_pandas()
                for row in df.itertuples(index=False):
                    for col_idx, val in enumerate(row):
                        header = headers[col_idx]
                        if pd.isna(val) or val is None:
                            ws.write_blank(row_idx, col_idx, None)
                            continue

                        if header in account_no_names:
                            ws.write_string(row_idx, col_idx, str(val), account_format)
                        elif header in text_names:
                            ws.write_string(row_idx, col_idx, str(val), text_format)
                        elif header in numeric_names:
                            try:
                                ws.write_number(row_idx, col_idx, float(val), numeric_format)
                            except (ValueError, TypeError):
                                ws.write(row_idx, col_idx, val, numeric_format)
                        else:
                            ws.write(row_idx, col_idx, val)
                    row_idx += 1
            sheets_added += 1
        except Exception as e:
            logger.error(f"Failed to add sheet '{sheet_title}' to combined batch combined download: {e}", exc_info=True)
            continue

    if sheets_added == 0:
        workbook.close()
        try:
            os.remove(batch_excel_path)
        except Exception:
            pass
        messages.error(request, 'No processed sheets available yet. Complete mapping and processing first.')
        return redirect('batch', batch_id=batch_id)

    workbook.close()

    response = FileResponse(open(batch_excel_path, 'rb'), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="cleaned_{base_name}.xlsx"'
    return response


@login_required
def download_script_view(request, session_id):
    """Download the generated SQL script for a session."""
    session = get_object_or_404(UploadSession, id=session_id, user=request.user)
    
    if not session.generated_script:
        messages.error(request, 'No SQL script available for this session.')
        return redirect('result', session_id=session.id)
    
    file_path = session.generated_script.path
    script_name = session.sheet_name or os.path.splitext(session.original_filename)[0]

    with open(file_path, 'r', encoding='utf-8') as f:
        response = HttpResponse(f.read(), content_type='application/sql')
        response['Content-Disposition'] = f'attachment; filename="{script_name}.sql"'
        return response


@login_required
def batch_mapping_view(request, batch_id):
    """Unified mapping page for all sheets in a batch — map every sheet, then process all at once."""
    sessions = UploadSession.objects.filter(
        user=request.user,
        batch_id=batch_id,
    ).order_by('uploaded_at')

    if not sessions.exists():
        messages.error(request, 'Batch not found.')
        return redirect('upload')

    # Build per-sheet data: headers, existing mappings, status
    sheets_data = []
    for session in sessions:
        if session.status not in ('pending_mapping', 'error'):
            sheets_data.append({
                'session': session,
                'headers': [],
                'existing_mappings': {},
                'already_mapped': True,
            })
            continue

        try:
            headers = get_file_headers(session.original_file.path, header_row=session.header_row)
        except Exception as e:
            sheets_data.append({
                'session': session,
                'headers': [],
                'existing_mappings': {},
                'error': str(e),
                'already_mapped': False,
            })
            continue

        existing_mappings = {m.original_header: m.target_column for m in session.mappings.all()}
        sheets_data.append({
            'session': session,
            'headers': headers,
            'existing_mappings': existing_mappings,
            'already_mapped': False,
        })

    target_columns = TARGET_COLUMN_CHOICES

    if request.method == 'POST':
        sessions_to_process = []

        for sheet in sheets_data:
            session = sheet['session']
            if sheet.get('already_mapped') or sheet.get('error'):
                continue

            headers = sheet['headers']
            session.mappings.all().delete()
            mappings_dict = {}

            for header in headers:
                target = request.POST.get(f'sess_{session.id}_mapping_{header}', '')
                ColumnMapping.objects.create(
                    session=session,
                    original_header=header,
                    target_column=target,
                )
                if target:
                    mappings_dict[header] = target

            if mappings_dict:
                if 'account_number' not in mappings_dict.values():
                    messages.warning(request, f'Sheet "{session.original_filename}" was not processed because Account Number is not mapped. Please map Account Number.')
                    continue

                sessions_to_process.append(session)

                if request.POST.get('save_template'):
                    header_signature = json.dumps(sorted(headers))
                    # Match in Python to avoid SQL Server NTEXT/NVARCHAR equality error
                    _tpl_name = f"Template from {session.original_filename[:30]}"
                    existing_tpl = next(
                        (t for t in MappingTemplate.objects.filter(user=request.user)
                         if t.header_signature == header_signature),
                        None,
                    )
                    if existing_tpl:
                        existing_tpl.name = _tpl_name
                        existing_tpl.mappings = mappings_dict
                        existing_tpl.save()
                    else:
                        MappingTemplate.objects.create(
                            user=request.user,
                            header_signature=header_signature,
                            name=_tpl_name,
                            mappings=mappings_dict,
                        )

        for session in sessions_to_process:
            session.status = 'processing'
            session.error_message = ""
            session.save()
            async_task('update.tasks.process_file_task', session.id)

        if sessions_to_process:
            messages.success(request, f'Processing started for {len(sessions_to_process)} sheet(s).')
        else:
            messages.warning(request, 'No sheets were mapped. Please map at least one column per sheet.')

        return redirect('batch', batch_id=batch_id)

    source_filename = sessions.first().source_filename or 'Unknown file'
    pending_count = sum(1 for s in sheets_data if not s.get('already_mapped') and not s.get('error'))
    done_count = sum(1 for s in sheets_data if s.get('already_mapped'))

    return render(request, 'update/batch_mapping.html', {
        'sheets_data': sheets_data,
        'target_columns': target_columns,
        'batch_id': batch_id,
        'source_filename': source_filename,
        'pending_count': pending_count,
        'done_count': done_count,
    })


@login_required
def download_batch_scripts_zip(request, batch_id):
    """Download all SQL scripts for a batch as a single ZIP file."""
    import zipfile
    import io as _io

    sessions = UploadSession.objects.filter(
        user=request.user,
        batch_id=batch_id
    ).order_by('uploaded_at')

    if not sessions.exists():
        messages.error(request, 'Batch not found.')
        return redirect('upload')

    source_filename = sessions.first().source_filename or str(batch_id)
    buffer = _io.BytesIO()
    files_added = 0

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for session in sessions:
            if not session.generated_script:
                continue
            script_path = session.generated_script.path
            if not os.path.exists(script_path):
                continue
            arcname = f"{session.sheet_name or session.original_filename}.sql"
            zf.write(script_path, arcname=arcname)
            files_added += 1

    if files_added == 0:
        messages.error(request, 'No SQL scripts available yet for this batch.')
        return redirect('batch', batch_id=batch_id)

    base_name = os.path.splitext(source_filename)[0]
    buffer.seek(0)
    response = HttpResponse(buffer.read(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="scripts_{base_name}.zip"'
    return response
