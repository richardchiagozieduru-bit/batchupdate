"""
Async tasks for data processing using Django-Q2.
"""
import os
import logging
import gc
import hashlib
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

from .models import UploadSession, ColumnMapping
from .progress import set_progress, clear_progress
from .services import (
    clean_dataframe, read_uploaded_file, save_df_to_excel_robust,
    upload_raw_to_batchupdate, DISPLAY_HEADERS,
    excel_to_csv_streaming, LARGE_FILE_THRESHOLD_MB, CSV_CHUNK_SIZE,
    read_account_column_styled, should_use_chunking, upload_parquet_to_batchupdate,
)
from django.conf import settings

# PyArrow schemas matching DISPLAY_HEADERS type rules
CLEANED_ARROW_SCHEMA = pa.schema([
    ('AccountNo', pa.string()),
    ('CurrentBalanceAmt', pa.float64()),
    ('AmountOverdue', pa.float64()),
    ('MonthsInArrears', pa.float64()), # Use float64 to support NaN/NULL safely
    ('LoanClassification', pa.string()),
    ('AccountStatusCode', pa.string()),
])

REJECTED_ARROW_SCHEMA = pa.schema([
    ('AccountNo', pa.string()),
    ('CurrentBalanceAmt', pa.float64()),
    ('AmountOverdue', pa.float64()),
    ('MonthsInArrears', pa.float64()),
    ('LoanClassification', pa.string()),
    ('AccountStatusCode', pa.string()),
    ('Rejection Reason', pa.string()),
])


def _load_and_clean(file_path, mappings, header_row, session_id, cleaned_parquet_path, rejected_parquet_path):
    """
    Load a file and clean it, using chunked processing for large Excel or CSV files.
    Saves cleaned and rejected records directly to Parquet on disk to maintain
    flat memory usage.

    Returns: (cleaned_count, rejected_count)
    """
    ext = os.path.splitext(file_path)[1].lower()
    use_chunked = should_use_chunking(file_path)

    # Style-aware account column recovery for Excel
    source_account_col = next(
        (src for src, tgt in mappings.items() if tgt == 'account_number'), None
    )
    corrected_accounts = None
    # Only load corrected_accounts for the single-batch path.
    # The chunked path recovers styling on the fly in excel_to_csv_streaming.
    if not use_chunked and source_account_col and ext == '.xlsx':
        try:
            corrected_accounts = read_account_column_styled(
                file_path, sheet_name=None, header_row=header_row, col_name=source_account_col
            )
            if corrected_accounts is not None:
                logger.info(
                    f"[Session {session_id}] Style-aware account read: "
                    f"{len(corrected_accounts)} values for '{source_account_col}'"
                )
        except Exception as exc:
            logger.warning(
                f"[Session {session_id}] Style-aware account read failed ({exc}); using raw values"
            )
            corrected_accounts = None

    cleaned_count = 0
    rejected_count = 0

    if use_chunked:
        logger.info(
            f"[Session {session_id}] Using CHUNKED processing pipeline for memory safety."
        )
        csv_path = file_path + '.tmp.csv'
        is_excel = ext in ('.xlsx', '.xls', '.xlsb')
        
        try:
            if is_excel:
                excel_to_csv_streaming(file_path, csv_path, header_row=header_row, account_col_name=source_account_col)
                csv_header = 0
            else:
                csv_path = file_path
                csv_header = header_row

            # Set up PyArrow writers
            cleaned_writer = pq.ParquetWriter(cleaned_parquet_path, schema=CLEANED_ARROW_SCHEMA, compression='snappy')
            rejected_writer = pq.ParquetWriter(rejected_parquet_path, schema=REJECTED_ARROW_SCHEMA, compression='snappy')
            
            seen_rows = set()  # Cross-chunk deduplication: stores tuples of row values

            # Process chunk-by-chunk
            for chunk in pd.read_csv(csv_path, dtype=str, chunksize=CSV_CHUNK_SIZE, header=csv_header):
                # No patching required — styling recovered on-the-fly in excel_to_csv_streaming
                c_df, r_df = clean_dataframe(chunk, mappings, format_for_display=False)

                # Cross-chunk exact row deduplication (all columns)
                if not c_df.empty:
                    c_df = c_df.drop_duplicates(keep='first')
                    row_tuples = [tuple(x) for x in c_df.itertuples(index=False)]
                    is_new = []
                    for t in row_tuples:
                        if t in seen_rows:
                            is_new.append(False)
                        else:
                            seen_rows.add(t)
                            is_new.append(True)
                    c_df = c_df[is_new].reset_index(drop=True)

                # Rename columns for PyArrow schema and format types
                if not c_df.empty:
                    c_df = c_df.rename(columns=DISPLAY_HEADERS)
                    for col in ['CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears']:
                        if col in c_df.columns:
                            c_df[col] = pd.to_numeric(c_df[col], errors='coerce')
                        else:
                            c_df[col] = None
                    for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode']:
                        if col in c_df.columns:
                            c_df[col] = c_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
                        else:
                            c_df[col] = None
                    
                    table = pa.Table.from_pandas(c_df[CLEANED_ARROW_SCHEMA.names], schema=CLEANED_ARROW_SCHEMA, preserve_index=False)
                    cleaned_writer.write_table(table)
                    cleaned_count += len(c_df)

                if not r_df.empty:
                    reason_col = r_df['Rejection Reason']
                    r_df = r_df.drop(columns=['Rejection Reason']).rename(columns=DISPLAY_HEADERS)
                    r_df['Rejection Reason'] = reason_col
                    
                    for col in ['CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears']:
                        if col in r_df.columns:
                            r_df[col] = pd.to_numeric(r_df[col], errors='coerce')
                        else:
                            r_df[col] = None
                    for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode', 'Rejection Reason']:
                        if col in r_df.columns:
                            r_df[col] = r_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
                        else:
                            r_df[col] = None
                    
                    table = pa.Table.from_pandas(r_df[REJECTED_ARROW_SCHEMA.names], schema=REJECTED_ARROW_SCHEMA, preserve_index=False)
                    rejected_writer.write_table(table)
                    rejected_count += len(r_df)

                # Garbage collect immediately to keep RAM flat
                del chunk, c_df, r_df
                gc.collect()

            cleaned_writer.close()
            rejected_writer.close()

        finally:
            if is_excel and os.path.exists(csv_path):
                os.remove(csv_path)
                logger.info(f"[Session {session_id}] Deleted temp CSV: {csv_path}")
    else:
        logger.info(
            f"[Session {session_id}] Using SINGLE-BATCH processing pipeline."
        )
        df = read_uploaded_file(file_path, header=header_row)
        
        # Patch account column with style-aware values if available
        if corrected_accounts is not None and source_account_col in df.columns:
            if len(corrected_accounts) == len(df):
                df[source_account_col] = corrected_accounts
            else:
                logger.warning(
                    f"[Session {session_id}] Style-aware account length mismatch "
                    f"({len(corrected_accounts)} vs {len(df)} rows); using raw values"
                )

        c_df, r_df = clean_dataframe(df, mappings, format_for_display=False)
        
        if not c_df.empty:
            c_df = c_df.drop_duplicates(keep='first').rename(columns=DISPLAY_HEADERS)
            for col in ['CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears']:
                if col in c_df.columns:
                    c_df[col] = pd.to_numeric(c_df[col], errors='coerce')
                else:
                    c_df[col] = None
            for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode']:
                if col in c_df.columns:
                    c_df[col] = c_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
                else:
                    c_df[col] = None
            
            table = pa.Table.from_pandas(c_df[CLEANED_ARROW_SCHEMA.names], schema=CLEANED_ARROW_SCHEMA, preserve_index=False)
            pq.write_table(table, cleaned_parquet_path, compression='snappy')
            cleaned_count = len(c_df)
        else:
            # Write empty Parquet with schema
            pq.write_table(CLEANED_ARROW_SCHEMA.empty_table(), cleaned_parquet_path)

        if not r_df.empty:
            reason_col = r_df['Rejection Reason']
            r_df = r_df.drop(columns=['Rejection Reason']).rename(columns=DISPLAY_HEADERS)
            r_df['Rejection Reason'] = reason_col
            
            for col in ['CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears']:
                if col in r_df.columns:
                    r_df[col] = pd.to_numeric(r_df[col], errors='coerce')
                else:
                    r_df[col] = None
            for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode', 'Rejection Reason']:
                if col in r_df.columns:
                    r_df[col] = r_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
                else:
                    r_df[col] = None
            
            table = pa.Table.from_pandas(r_df[REJECTED_ARROW_SCHEMA.names], schema=REJECTED_ARROW_SCHEMA, preserve_index=False)
            pq.write_table(table, rejected_parquet_path, compression='snappy')
            rejected_count = len(r_df)
        else:
            pq.write_table(REJECTED_ARROW_SCHEMA.empty_table(), rejected_parquet_path)

        del df, c_df, r_df
        gc.collect()

    return cleaned_count, rejected_count


def process_file_task(session_id):
    """
    Async task to process and clean uploaded file.
    Saves results directly as Parquet files on disk.
    """
    session = UploadSession.objects.get(id=session_id)
    logger.info(f"[Session {session_id}] Starting processing: {session.original_filename}")
    
    try:
        session.status = 'processing'
        session.error_message = ""
        session.save()
        set_progress(session_id, 'clean', step='Starting...', percent=5)

        mappings = {m.original_header: m.target_column for m in session.mappings.all() if m.target_column}
        
        if not mappings:
            logger.error(f"[Session {session_id}] No columns mapped")
            session.status = 'error'
            session.error_message = 'No columns mapped'
            session.save()
            return 'Error: No columns mapped'
        
        logger.info(f"[Session {session_id}] Mappings: {mappings}")
        
        # Prepare processed directory
        os.makedirs(os.path.join(settings.MEDIA_ROOT, 'processed'), exist_ok=True)
        base_name = session.sheet_name if session.sheet_name else os.path.splitext(session.original_filename)[0]
        
        cleaned_parquet_filename = f"processed_{base_name}.parquet"
        cleaned_parquet_path = os.path.join(settings.MEDIA_ROOT, 'processed', cleaned_parquet_filename)
        
        rejected_parquet_filename = f"rejected_{base_name}.parquet"
        rejected_parquet_path = os.path.join(settings.MEDIA_ROOT, 'processed', rejected_parquet_filename)

        set_progress(session_id, 'clean', step='Reading and cleaning file...', percent=15)
        cleaned_count, rejected_count = _load_and_clean(
            session.original_file.path, mappings, session.header_row, session_id,
            cleaned_parquet_path, rejected_parquet_path
        )
        set_progress(session_id, 'clean', step='Cleaning complete', percent=70,
                     detail=f'{cleaned_count:,} valid, {rejected_count:,} rejected')
        logger.info(f"[Session {session_id}] Cleaned: {cleaned_count} valid, {rejected_count} rejected")
        
        if cleaned_count == 0:
            logger.error(f"[Session {session_id}] No valid rows after cleaning ({rejected_count} rejected)")
            session.rows_processed = 0
            session.rows_rejected = rejected_count
            
            # Attach and pre-generate rejected file if rejected rows exist
            if rejected_count > 0:
                session.rejected_file = f"processed/{rejected_parquet_filename}"
                from .services import write_parquet_as_excel
                display_name = session.sheet_name if session.sheet_name else (os.path.splitext(session.original_filename)[0] if session.original_filename else base_name)
                try:
                    rejected_excel_filename = f"rejected_{display_name}.xlsx"
                    rejected_excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', rejected_excel_filename)
                    write_parquet_as_excel(rejected_parquet_path, f"rejected_{display_name}", rejected_excel_path)
                    logger.info(f"[Session {session_id}] Pre-generated cached rejected Excel file for zero-valid-row session")
                except Exception as e:
                    logger.error(f"[Session {session_id}] Rejected Excel pre-generation failed: {e}", exc_info=True)

            # Determine primary rejection reason for explicit UI messaging
            primary_reason = "No valid rows after cleaning"
            if rejected_count > 0 and os.path.exists(rejected_parquet_path):
                try:
                    pf = pq.ParquetFile(rejected_parquet_path)
                    if pf.metadata.num_rows > 0:
                        first_batch = next(pf.iter_batches(batch_size=500))
                        r_df_sample = first_batch.to_pandas()
                        if 'Rejection Reason' in r_df_sample.columns:
                            top_reasons = r_df_sample['Rejection Reason'].value_counts()
                            if not top_reasons.empty:
                                main_reason = top_reasons.index[0]
                                if 'Current Balance Amount is empty' in main_reason or 'CurrentBalanceAmt' in main_reason:
                                    primary_reason = "Skipped: Current Balance column (CurrentBalanceAmt) is empty or missing"
                                else:
                                    primary_reason = f"No valid rows after cleaning (Reason: {main_reason})"
                except Exception as ex:
                    logger.warning(f"[Session {session_id}] Error inspecting rejection reasons: {ex}")

            session.status = 'error'
            session.error_message = primary_reason
            session.save()
            return f'Error: {primary_reason}'
        
        # Update path fields in UploadSession
        session.processed_file = f"processed/{cleaned_parquet_filename}"
        if rejected_count > 0:
            session.rejected_file = f"processed/{rejected_parquet_filename}"
        else:
            session.rejected_file = None
            # Clean up empty 0-row rejected Parquet file from disk if it was created
            if os.path.exists(rejected_parquet_path):
                try:
                    os.remove(rejected_parquet_path)
                    logger.info(f"[Session {session_id}] Removed empty rejected parquet file: {rejected_parquet_path}")
                except Exception as ex:
                    logger.warning(f"[Session {session_id}] Could not remove empty rejected parquet file: {ex}")

        session.rows_processed = cleaned_count
        session.rows_rejected = rejected_count
        session.status = 'processed'
        session.save()

        # Pre-generate Excel files for download caching
        # Prioritize session.sheet_name (standardized subid_ddmmyyyy_name format)
        set_progress(session_id, 'clean', step='Generating download files...', percent=85)
        display_name = session.sheet_name if session.sheet_name else (os.path.splitext(session.original_filename)[0] if session.original_filename else base_name)
        from .services import write_parquet_as_excel
        try:
            excel_filename = f"processed_{display_name}.xlsx"
            excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', excel_filename)
            write_parquet_as_excel(cleaned_parquet_path, display_name, excel_path)
            logger.info(f"[Session {session_id}] Pre-generated cached Excel file at {excel_path}")
        except Exception as e:
            logger.error(f"[Session {session_id}] Cleaned Excel pre-generation failed: {e}", exc_info=True)

        if rejected_count > 0:
            try:
                rejected_excel_filename = f"rejected_{display_name}.xlsx"
                rejected_excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', rejected_excel_filename)
                write_parquet_as_excel(rejected_parquet_path, f"rejected_{display_name}", rejected_excel_path)
                logger.info(f"[Session {session_id}] Pre-generated cached rejected Excel file at {rejected_excel_path}")
            except Exception as e:
                logger.error(f"[Session {session_id}] Rejected Excel pre-generation failed: {e}", exc_info=True)

        # Invalidate batch combined cache if this session belongs to a batch
        if session.batch_id:
            try:
                batch_excel_filename = f"batch_{session.batch_id}.xlsx"
                batch_excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', batch_excel_filename)
                if os.path.exists(batch_excel_path):
                    os.remove(batch_excel_path)
                    logger.info(f"[Session {session_id}] Invalidated cached combined batch Excel: {batch_excel_path}")
            except Exception as e:
                logger.warning(f"[Session {session_id}] Failed to delete cached combined batch Excel: {e}")

        # If upload_to_db was toggled ON at upload time, chain the DB upload as a
        # separate async task so it gets its own progress tracking.
        if session.upload_to_db and session.sheet_name:
            from django_q.tasks import async_task
            set_progress(session_id, 'clean', step='Complete — queueing DB upload...', percent=100)
            async_task('update.tasks.upload_to_db_task', session_id)
            logger.info(f"[Session {session_id}] Cleaning done, chained upload_to_db_task")
        else:
            set_progress(session_id, 'clean', step='Complete!', percent=100,
                         detail=f'{cleaned_count:,} cleaned, {rejected_count:,} rejected')
            logger.info(f"[Session {session_id}] Database upload skipped (upload_to_db={session.upload_to_db}, sheet_name={session.sheet_name})")

        result = f'Complete! {cleaned_count} processed, {rejected_count} rejected'
        logger.info(f"[Session {session_id}] {result}")
        return result
        
    except Exception as e:
        logger.error(f"[Session {session_id}] Task failed: {e}", exc_info=True)
        session.status = 'error'
        session.error_message = str(e)
        session.save()
        set_progress(session_id, 'clean', step=str(e), percent=100)
        return f'Error: {str(e)}'


def upload_to_db_task(session_id):
    """
    Standalone async task: upload already-cleaned Parquet to the BatchUpdate SQL
    database.  Can be triggered either as a chained follow-up to process_file_task
    (when upload_to_db was checked at upload time) or manually via the UI when the
    user reviews the output and clicks "Upload to BatchUpdate".
    """
    session = UploadSession.objects.get(id=session_id)
    logger.info(f"[Session {session_id}] Starting BatchUpdate upload for: {session.sheet_name}")

    try:
        session.status = 'uploading_to_db'
        session.error_message = ''
        session.save()
        set_progress(session_id, 'dbupload', step='Connecting to database...', percent=10)

        parquet_path = session.processed_file.path
        if not parquet_path or not os.path.exists(parquet_path):
            raise FileNotFoundError(f"Cleaned parquet file not found: {parquet_path}")

        set_progress(session_id, 'dbupload', step='Uploading rows to BatchUpdate...', percent=30)
        rows_uploaded = upload_parquet_to_batchupdate(parquet_path, session.sheet_name)

        session.batchupdate_uploaded = True
        session.rows_uploaded = rows_uploaded
        session.status = 'uploaded'
        session.save()
        set_progress(session_id, 'dbupload', step='Complete!', percent=100,
                     detail=f'{rows_uploaded:,} rows uploaded')
        logger.info(f"[Session {session_id}] Uploaded {rows_uploaded} rows to BatchUpdate table [{session.sheet_name}]")
        return f'Uploaded {rows_uploaded} rows to [{session.sheet_name}]'

    except Exception as e:
        logger.error(f"[Session {session_id}] BatchUpdate upload failed: {e}", exc_info=True)
        # Revert to processed so user can retry
        session.status = 'processed'
        session.error_message = f"BatchUpdate Upload Error: {str(e)}"
        session.save()
        set_progress(session_id, 'dbupload', step=f'Error: {str(e)}', percent=100)
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

        # Delete cached Excel files
        base_name = session.sheet_name if session.sheet_name else os.path.splitext(session.original_filename)[0]
        for cache_name in (f"processed_{base_name}.xlsx", f"rejected_{base_name}.xlsx"):
            cache_path = os.path.join(_settings.MEDIA_ROOT, 'processed', cache_name)
            if os.path.exists(cache_path):
                try:
                    os.remove(cache_path)
                except Exception:
                    file_errors += 1

        session.delete()
        deleted_count += 1

    # Also clean up dropped files older than DROPPED_FILE_RETENTION_DAYS (default 2 days)
    try:
        cleanup_old_dropped_files_task()
    except Exception as e:
        logger.warning(f"[Cleanup] Dropped files cleanup failed during session cleanup: {e}")

    logger.info(
        f"[Cleanup] Deleted {deleted_count} sessions older than {retention_days} days"
        + (f" ({file_errors} file deletion errors)" if file_errors else "")
    )
    return f"Deleted {deleted_count} old sessions"


def cleanup_old_dropped_files_task(retention_days=None):
    """
    Scheduled task or helper: delete DroppedFile records and their associated
    files in media/drop_folder/ older than DROPPED_FILE_RETENTION_DAYS days (default 2).
    Also removes any orphaned physical files in media/drop_folder/ older than the threshold.
    """
    from datetime import timedelta
    from django.utils import timezone
    from django.conf import settings as _settings
    from .models import DroppedFile

    if retention_days is None:
        retention_days = getattr(_settings, 'DROPPED_FILE_RETENTION_DAYS', 2)

    cutoff = timezone.now() - timedelta(days=retention_days)
    old_drops = DroppedFile.objects.filter(dropped_at__lt=cutoff)
    deleted_count = 0
    file_errors = 0

    for drop in old_drops:
        if drop.file:
            try:
                drop.file.delete(save=False)
            except Exception as e:
                logger.warning(f"[Cleanup] Error deleting file for DroppedFile {drop.id}: {e}")
                file_errors += 1
        try:
            drop.delete()
            deleted_count += 1
        except Exception as e:
            logger.warning(f"[Cleanup] Error deleting DroppedFile record {drop.id}: {e}")

    # Also clean orphaned files in media/drop_folder/
    orphans_removed = 0
    drop_folder = os.path.join(_settings.MEDIA_ROOT, 'drop_folder')
    if os.path.exists(drop_folder):
        cutoff_ts = timezone.now().timestamp() - (retention_days * 86400)
        try:
            for filename in os.listdir(drop_folder):
                filepath = os.path.join(drop_folder, filename)
                if os.path.isfile(filepath):
                    try:
                        if os.path.getmtime(filepath) < cutoff_ts:
                            rel_name = f"drop_folder/{filename}"
                            if not DroppedFile.objects.filter(file=rel_name).exists():
                                os.remove(filepath)
                                orphans_removed += 1
                    except Exception as fe:
                        logger.warning(f"[Cleanup] Failed to remove orphan drop file {filepath}: {fe}")
        except Exception as de:
            logger.warning(f"[Cleanup] Failed to scan drop_folder directory: {de}")

    logger.info(
        f"[Cleanup] Cleared {deleted_count} dropped files older than {retention_days} days"
        + (f" and {orphans_removed} orphaned files" if orphans_removed else "")
        + (f" ({file_errors} file errors)" if file_errors else "")
    )
    return f"Cleared {deleted_count} dropped files ({orphans_removed} orphans removed)"

