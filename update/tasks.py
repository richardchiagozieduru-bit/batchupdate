"""
Async tasks for data processing using Django-Q2.
"""
import os
import logging
import pandas as pd

logger = logging.getLogger(__name__)

from .models import UploadSession, ColumnMapping
from .services import (
    clean_dataframe, read_uploaded_file, save_df_to_excel_robust,
    upload_raw_to_batchupdate, DISPLAY_HEADERS,
    excel_to_csv_streaming, LARGE_FILE_THRESHOLD_MB, CSV_CHUNK_SIZE,
)
from django.conf import settings


def _load_and_clean(file_path, mappings, header_row, session_id):
    """
    Load a file and clean it, using chunked processing for large Excel files.

    Strategy:
    - .xlsx over LARGE_FILE_THRESHOLD_MB: stream-convert to a temp CSV (openpyxl
      read_only mode, row-by-row, never loads the workbook into RAM), then process
      the CSV in CSV_CHUNK_SIZE-row chunks. Peak memory = one chunk of cleaned data
      (6 columns) rather than the full source file.
    - .xls or any file under the threshold: full load via pd.read_excel as before.
    - .csv: full load (CSV chunking can be added later if needed for very large CSVs).

    Cross-chunk deduplication is handled by tracking seen account_number values
    in a set. Within-chunk deduplication is handled by clean_dataframe.

    Returns: (cleaned_df, rejected_df)
    """
    ext = os.path.splitext(file_path)[1].lower()
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    use_chunked = ext == '.xlsx' and file_size_mb > LARGE_FILE_THRESHOLD_MB

    if use_chunked:
        logger.info(
            f"[Session {session_id}] Large Excel ({file_size_mb:.1f} MB > {LARGE_FILE_THRESHOLD_MB} MB) — "
            f"stream-converting to CSV then processing in {CSV_CHUNK_SIZE}-row chunks"
        )
        csv_path = file_path + '.tmp.csv'
        try:
            excel_to_csv_streaming(file_path, csv_path, header_row=header_row)
            # After conversion the header is at row 0 in the CSV
            cleaned_chunks, rejected_chunks = [], []
            seen_accounts = set()  # cross-chunk deduplication

            for chunk in pd.read_csv(csv_path, dtype=str, chunksize=CSV_CHUNK_SIZE, header=0):
                c_df, r_df = clean_dataframe(chunk, mappings, format_for_display=False)

                # Remove rows whose account_number was already seen in a prior chunk
                if 'account_number' in c_df.columns and seen_accounts:
                    before = len(c_df)
                    c_df = c_df[~c_df['account_number'].isin(seen_accounts)]
                    removed = before - len(c_df)
                    if removed:
                        logger.info(f"[Session {session_id}] Cross-chunk dedup removed {removed} rows")

                if 'account_number' in c_df.columns:
                    seen_accounts.update(c_df['account_number'].dropna().tolist())

                cleaned_chunks.append(c_df)
                if not r_df.empty:
                    rejected_chunks.append(r_df)

            cleaned_df = pd.concat(cleaned_chunks, ignore_index=True) if cleaned_chunks else pd.DataFrame()
            rejected_df = pd.concat(rejected_chunks, ignore_index=True) if rejected_chunks else pd.DataFrame()
            logger.info(
                f"[Session {session_id}] Chunked processing complete: "
                f"{len(cleaned_df)} valid, {len(rejected_df)} rejected"
            )
        finally:
            if os.path.exists(csv_path):
                os.remove(csv_path)
                logger.info(f"[Session {session_id}] Deleted temp CSV: {csv_path}")
    else:
        df = read_uploaded_file(file_path, header=header_row)
        logger.info(f"[Session {session_id}] File read: {len(df)} rows, {len(df.columns)} columns")
        cleaned_df, rejected_df = clean_dataframe(df, mappings, format_for_display=False)

    return cleaned_df, rejected_df


def process_file_task(session_id):
    """
    Async task to process and clean uploaded file.
    Called by django-q's async_task().
    """
    session = UploadSession.objects.get(id=session_id)
    logger.info(f"[Session {session_id}] Starting processing: {session.original_filename}")
    
    try:
        session.status = 'processing'
        session.save()

        mappings = {m.original_header: m.target_column for m in session.mappings.all() if m.target_column}
        
        if not mappings:
            logger.error(f"[Session {session_id}] No columns mapped")
            session.status = 'error'
            session.error_message = 'No columns mapped'
            session.save()
            return 'Error: No columns mapped'
        
        logger.info(f"[Session {session_id}] Mappings: {mappings}")
        
        cleaned_df, rejected_df = _load_and_clean(
            session.original_file.path, mappings, session.header_row, session_id
        )
        logger.info(f"[Session {session_id}] Cleaned: {len(cleaned_df)} valid, {len(rejected_df)} rejected")
        
        if len(cleaned_df) == 0:
            logger.error(f"[Session {session_id}] No valid rows after cleaning")
            session.status = 'error'
            session.error_message = 'No valid rows after cleaning'
            session.save()
            return 'Error: No valid rows after cleaning'
        
        os.makedirs(os.path.join(settings.MEDIA_ROOT, 'processed'), exist_ok=True)
        
        # Derive display version by renaming columns (avoids running clean_dataframe twice)
        display_df = cleaned_df.rename(columns=DISPLAY_HEADERS)
        if not rejected_df.empty:
            reason_col = rejected_df['Rejection Reason']
            display_rejected = rejected_df.drop(columns=['Rejection Reason']).rename(columns=DISPLAY_HEADERS)
            display_rejected['Rejection Reason'] = reason_col
        else:
            display_rejected = rejected_df
        
        # Save cleaned file (with atomic save, corruption recovery, fallback chain)
        base_name = session.sheet_name if session.sheet_name else os.path.splitext(session.original_filename)[0]
        processed_filename = f"processed_{base_name}.xlsx"
        processed_path = os.path.join(settings.MEDIA_ROOT, 'processed', processed_filename)
        
        final_path, sheet_names = save_df_to_excel_robust(
            display_df, processed_path, sheet_name=base_name
        )
        logger.info(f"[Session {session_id}] Saved processed file: {final_path} (sheets: {sheet_names})")
        
        # Update path in case fallback was used (relative to MEDIA_ROOT, not CWD)
        media_root = str(settings.MEDIA_ROOT)
        session.processed_file = os.path.relpath(final_path, media_root) if final_path != processed_path else f"processed/{processed_filename}"
        
        # Save rejected rows file (if any)
        rejected_count = 0
        if not rejected_df.empty:
            rejected_filename = f"rejected_{base_name}.xlsx"
            rejected_path = os.path.join(settings.MEDIA_ROOT, 'processed', rejected_filename)
            
            rej_final_path, rej_sheets = save_df_to_excel_robust(
                display_rejected, rejected_path, sheet_name=f"rejected_{base_name}"
            )
            session.rejected_file = os.path.relpath(rej_final_path, media_root) if rej_final_path != rejected_path else f"processed/{rejected_filename}"
            rejected_count = len(rejected_df)
            logger.info(f"[Session {session_id}] Saved {rejected_count} rejected rows: {rej_final_path}")
        
        session.rows_processed = len(cleaned_df)
        session.rows_rejected = rejected_count
        session.status = 'processed'
        session.save()

        # Upload cleaned display data to BatchUpdate DB
        if session.sheet_name:
            try:
                upload_raw_to_batchupdate(display_df, session.sheet_name)
                session.batchupdate_uploaded = True
                session.rows_uploaded = len(display_df)
                session.status = 'uploaded'
                session.save()
                logger.info(f"[Session {session_id}] Uploaded {len(display_df)} rows to BatchUpdate table [{session.sheet_name}]")
            except Exception as e:
                logger.error(f"[Session {session_id}] BatchUpdate upload failed: {e}", exc_info=True)
                bu_error = f"BatchUpdate Upload Error: {str(e)}"
                session.error_message = f"{session.error_message}\n{bu_error}".strip() if session.error_message else bu_error
                session.save()

        result = f'Complete! {len(cleaned_df)} processed, {len(display_df)} uploaded, {rejected_count} rejected'
        logger.info(f"[Session {session_id}] {result}")
        return result
        
    except Exception as e:
        logger.error(f"[Session {session_id}] Task failed: {e}", exc_info=True)
        session.status = 'error'
        session.error_message = str(e)
        session.save()
        return f'Error: {str(e)}'


def cleanup_old_sessions_task():
    """
    Scheduled task: delete UploadSession records (and their associated media files)
    older than SESSION_RETENTION_DAYS days (default 30).

    Register in Django-Q admin as a scheduled task running daily, or add to
    Q_CLUSTER schedules in settings.py:

        from django_q.models import Schedule
        Schedule.objects.get_or_create(
            func='update.tasks.cleanup_old_sessions_task',
            defaults={'schedule_type': Schedule.DAILY},
        )
    """
    from datetime import timedelta
    from django.utils import timezone
    from django.conf import settings as _settings

    retention_days = getattr(_settings, 'SESSION_RETENTION_DAYS', 30)
    cutoff = timezone.now() - timedelta(days=retention_days)

    old_sessions = UploadSession.objects.filter(uploaded_at__lt=cutoff)
    deleted_count = 0
    file_errors = 0

    for session in old_sessions:
        # Delete associated files from disk
        for field in (session.original_file, session.processed_file,
                      session.rejected_file, session.generated_script):
            if field:
                try:
                    field.delete(save=False)
                except Exception:
                    file_errors += 1

        session.delete()
        deleted_count += 1

    logger.info(
        f"[Cleanup] Deleted {deleted_count} sessions older than {retention_days} days"
        + (f" ({file_errors} file deletion errors)" if file_errors else "")
    )
    return f"Deleted {deleted_count} old sessions"

