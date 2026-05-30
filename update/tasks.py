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
    if source_account_col and ext == '.xlsx':
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
        is_excel = ext in ('.xlsx', '.xls')
        
        try:
            if is_excel:
                excel_to_csv_streaming(file_path, csv_path, header_row=header_row)
                csv_header = 0
            else:
                csv_path = file_path
                csv_header = header_row

            # Set up PyArrow writers
            cleaned_writer = pq.ParquetWriter(cleaned_parquet_path, schema=CLEANED_ARROW_SCHEMA, compression='snappy')
            rejected_writer = pq.ParquetWriter(rejected_parquet_path, schema=REJECTED_ARROW_SCHEMA, compression='snappy')
            
            seen_rows = set()  # Cross-chunk deduplication: stores tuples of row values
            chunk_offset = 0

            # Process chunk-by-chunk
            for chunk in pd.read_csv(csv_path, dtype=str, chunksize=CSV_CHUNK_SIZE, header=csv_header):
                chunk_len = len(chunk)
                
                # Patch account column with style-aware values if Excel
                if is_excel and corrected_accounts is not None and source_account_col in chunk.columns:
                    slice_ = corrected_accounts[chunk_offset:chunk_offset + chunk_len]
                    if len(slice_) == chunk_len:
                        chunk = chunk.copy()
                        chunk[source_account_col] = slice_
                chunk_offset += chunk_len

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
                        c_df[col] = pd.to_numeric(c_df[col], errors='coerce')
                    for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode']:
                        c_df[col] = c_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
                    
                    table = pa.Table.from_pandas(c_df[CLEANED_ARROW_SCHEMA.names], schema=CLEANED_ARROW_SCHEMA, preserve_index=False)
                    cleaned_writer.write_table(table)
                    cleaned_count += len(c_df)

                if not r_df.empty:
                    reason_col = r_df['Rejection Reason']
                    r_df = r_df.drop(columns=['Rejection Reason']).rename(columns=DISPLAY_HEADERS)
                    r_df['Rejection Reason'] = reason_col
                    
                    for col in ['CurrentBalanceAmt', 'AmountOverdue', 'MonthsInArrears']:
                        r_df[col] = pd.to_numeric(r_df[col], errors='coerce')
                    for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode', 'Rejection Reason']:
                        r_df[col] = r_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
                    
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
                c_df[col] = pd.to_numeric(c_df[col], errors='coerce')
            for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode']:
                c_df[col] = c_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
            
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
                r_df[col] = pd.to_numeric(r_df[col], errors='coerce')
            for col in ['AccountNo', 'LoanClassification', 'AccountStatusCode', 'Rejection Reason']:
                r_df[col] = r_df[col].astype(str).replace(['None', 'nan', '<NA>'], None)
            
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
        session.save()

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

        cleaned_count, rejected_count = _load_and_clean(
            session.original_file.path, mappings, session.header_row, session_id,
            cleaned_parquet_path, rejected_parquet_path
        )
        logger.info(f"[Session {session_id}] Cleaned: {cleaned_count} valid, {rejected_count} rejected")
        
        if cleaned_count == 0:
            logger.error(f"[Session {session_id}] No valid rows after cleaning")
            session.status = 'error'
            session.error_message = 'No valid rows after cleaning'
            session.save()
            return 'Error: No valid rows after cleaning'
        
        # Update path fields in UploadSession
        session.processed_file = f"processed/{cleaned_parquet_filename}"
        if rejected_count > 0:
            session.rejected_file = f"processed/{rejected_parquet_filename}"
        else:
            session.rejected_file = None

        session.rows_processed = cleaned_count
        session.rows_rejected = rejected_count
        session.status = 'processed'
        session.save()

        # Upload cleaned display data to BatchUpdate DB (using fast chunked parquet streamer)
        if session.sheet_name:
            try:
                rows_uploaded = upload_parquet_to_batchupdate(cleaned_parquet_path, session.sheet_name)
                session.batchupdate_uploaded = True
                session.rows_uploaded = rows_uploaded
                session.status = 'uploaded'
                session.save()
                logger.info(f"[Session {session_id}] Uploaded {rows_uploaded} rows to BatchUpdate table [{session.sheet_name}]")
            except Exception as e:
                logger.error(f"[Session {session_id}] BatchUpdate upload failed: {e}", exc_info=True)
                bu_error = f"BatchUpdate Upload Error: {str(e)}"
                session.error_message = f"{session.error_message}\n{bu_error}".strip() if session.error_message else bu_error
                session.save()

        result = f'Complete! {cleaned_count} processed, {session.rows_uploaded} uploaded, {rejected_count} rejected'
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

