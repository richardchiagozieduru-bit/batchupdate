"""
Async tasks for data processing using Django-Q2.
"""
import os
import logging
import pandas as pd

logger = logging.getLogger(__name__)

from .models import UploadSession, ColumnMapping
from .services import clean_dataframe, read_uploaded_file, save_df_to_excel_robust, upload_raw_to_batchupdate, DISPLAY_HEADERS


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
        
        df = read_uploaded_file(session.original_file.path, header=session.header_row)
        logger.info(f"[Session {session_id}] File read: {len(df)} rows, {len(df.columns)} columns")
        
        mappings = {m.original_header: m.target_column for m in session.mappings.all() if m.target_column}
        
        if not mappings:
            logger.error(f"[Session {session_id}] No columns mapped")
            session.status = 'error'
            session.error_message = 'No columns mapped'
            session.save()
            return 'Error: No columns mapped'
        
        logger.info(f"[Session {session_id}] Mappings: {mappings}")
        
        # Clean data once — reuse result for both SQL upload and Excel display
        cleaned_df, rejected_df = clean_dataframe(df, mappings, format_for_display=False)
        logger.info(f"[Session {session_id}] Cleaned: {len(cleaned_df)} valid, {len(rejected_df)} rejected")
        
        if len(cleaned_df) == 0:
            logger.error(f"[Session {session_id}] No valid rows after cleaning")
            session.status = 'error'
            session.error_message = 'No valid rows after cleaning'
            session.save()
            return 'Error: No valid rows after cleaning'
        
        os.makedirs(os.path.join('media', 'processed'), exist_ok=True)
        
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
        processed_path = os.path.join('media', 'processed', processed_filename)
        
        final_path, sheet_names = save_df_to_excel_robust(
            display_df, processed_path, sheet_name=base_name
        )
        logger.info(f"[Session {session_id}] Saved processed file: {final_path} (sheets: {sheet_names})")
        
        # Update path in case fallback was used
        session.processed_file = os.path.relpath(final_path, 'media') if final_path != processed_path else f"processed/{processed_filename}"
        
        # Save rejected rows file (if any)
        rejected_count = 0
        if not rejected_df.empty:
            rejected_filename = f"rejected_{base_name}.xlsx"
            rejected_path = os.path.join('media', 'processed', rejected_filename)
            
            rej_final_path, rej_sheets = save_df_to_excel_robust(
                display_rejected, rejected_path, sheet_name=f"rejected_{base_name}"
            )
            session.rejected_file = os.path.relpath(rej_final_path, 'media') if rej_final_path != rejected_path else f"processed/{rejected_filename}"
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


