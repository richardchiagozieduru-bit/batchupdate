"""
Core service layer for the Unupdated Records application.
Handles database connections, parameterized query execution, data cleaning,
openpyxl multi-sheet workbook generation, and ZIP compression.
"""

import calendar
import datetime
import logging
import os
import re
import time
import zipfile
from pathlib import Path

import pyodbc
import xlsxwriter
from django.conf import settings

from .queries import (
    EXCLUDED_ACCOUNT_STATUSES,
    CONSUMER_HEADERS,
    COMMERCIAL_HEADERS,
    SUBSCRIBER_SEARCH_QUERY,
    CONSUMER_MAX_PERIOD_QUERY,
    COMMERCIAL_MAX_PERIOD_QUERY,
    ALL_SUBSCRIBERS_QUERY,
    get_loaded_subscribers_query,
    get_consumer_query,
    get_commercial_query,
)

logger = logging.getLogger('unupdated')


class ExtractionCancelledException(Exception):
    """Raised when an unupdated extraction task is cancelled by the user mid-stream."""
    pass


# Columns that MUST be strictly formatted as Text in Excel to protect leading zeros
TEXT_COLUMNS_CONSUMER = {
    'BankVerificationNumber',
    'HomeTelephoneNumber',
    'FirstCentralReferenceNo',
    'CustomerID',
    'AccountNo',
}

TEXT_COLUMNS_COMMERCIAL = {
    'BusinessRegistrationNumber',
    'FirstCentralReferenceNo',
    'CustomerID',
    'AccountNo',
}


# ── Database Connection ──────────────────────────────────────────────────────

def get_bureau_connection():
    """
    Open a read-only pyodbc connection to the Bureau SQL Server database.
    Reuses settings.BUREAU_DB_* variables configured in settings.py / .env.
    """
    conn_str = (
        f"DRIVER={{{settings.BUREAU_DB_DRIVER}}};"
        f"SERVER={settings.BUREAU_DB_SERVER};"
        f"DATABASE={settings.BUREAU_DB_NAME};"
        f"KeepAlive=30;"
        f"KeepAliveInterval=5;"
    )
    if settings.BUREAU_DB_TRUSTED_CONNECTION.lower() == 'yes':
        conn_str += "Trusted_Connection=yes;"
    else:
        conn_str += (
            f"UID={settings.BUREAU_DB_USERNAME};"
            f"PWD={settings.BUREAU_DB_PASSWORD};"
        )
    return pyodbc.connect(conn_str, timeout=120)


# ── Subscriber Search ────────────────────────────────────────────────────────

def search_subscribers(search_term):
    """
    Search for subscribers in the Bureau database by name or ID.
    Returns: [{'subscriber_id': int, 'subscriber_name': str}, ...]
    """
    conn = get_bureau_connection()
    try:
        cursor = conn.cursor()
        search_like = f"%{search_term}%"
        cursor.execute(SUBSCRIBER_SEARCH_QUERY, (search_like, search_term))
        results = []
        for row in cursor.fetchall():
            results.append({
                'subscriber_name': str(row[0]).strip() if row[0] else '',
                'subscriber_id': int(row[1]) if row[1] else 0,
            })
        return results
    finally:
        conn.close()


def get_ungenerated_ready_subscribers(
    reporting_month_str,
    include_consumer=True,
    include_commercial=True,
    conn=None,
    force_refresh=False,
    return_meta=False,
):
    """
    Finds subscribers whose file is already loaded in the Bureau database for the given
    reporting month, but whose unupdated report has NOT been generated yet (status != 'completed').
    Uses the fast indexed MAX(lastperiodnum) per subscriber from SubscriberDataLoad.
    
    Results are cached in Django's local in-memory cache (LocMemCache in Python RAM)
    for 24 hours (86,400s). The Bentley database is never modified or written to.
    Subscribers completed locally are filtered dynamically from the cache upon access.
    Pass force_refresh=True to query the Bureau database live.
    """
    from .models import UnupdatedSession
    from django.core.cache import cache

    cache_key = f'unupdated_ready_subs_{reporting_month_str}_{include_consumer}_{include_commercial}'

    # 1. Identify subscribers already completed for this reporting month in local DB
    completed_sub_ids = set(
        UnupdatedSession.objects.filter(
            batch__reporting_month=reporting_month_str,
            status='completed',
        ).values_list('subscriber_id', flat=True)
    )

    # 2. Check local memory cache if force_refresh is not requested
    if not force_refresh:
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            raw_subs = cached_payload.get('subscribers', [])
            synced_at = cached_payload.get('synced_at', '')
            filtered = [s for s in raw_subs if s['subscriber_id'] not in completed_sub_ids]
            logger.info(
                "[Ungenerated Detection] Returning %d ready subscribers from local RAM cache (synced: %s) for %s",
                len(filtered), synced_at, reporting_month_str
            )
            meta = {'synced_at': synced_at, 'is_cached': True}
            return (filtered, meta) if return_meta else filtered

    t_start = time.perf_counter()
    _, last_period_num, _ = compute_period_and_cutoff(reporting_month_str)
    target_period_str = str(last_period_num).strip()

    # 3. Query Bureau DB for the list of all active registered subscribers
    close_conn = False
    if conn is None:
        conn = get_bureau_connection()
        close_conn = True

    ungenerated_raw = []
    try:
        cursor = conn.cursor()
        cursor.execute(ALL_SUBSCRIBERS_QUERY)
        all_subs = [
            (int(r[0]), str(r[1]).strip())
            for r in cursor.fetchall()
            if r[0] and r[1]
        ]

        logger.info(
            "[Ungenerated Scan] Querying Bureau DB for %d subscribers for %s (target period: %s).",
            len(all_subs), reporting_month_str, target_period_str
        )

        # 4. Check readiness for each subscriber using the fast indexed queries
        for sub_id, sub_name in all_subs:
            c_max = None
            comm_max = None
            is_ready = False

            if include_consumer:
                cursor.execute(CONSUMER_MAX_PERIOD_QUERY, (sub_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    c_max = str(row[0]).strip()

            if include_commercial:
                cursor.execute(COMMERCIAL_MAX_PERIOD_QUERY, (sub_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    comm_max = str(row[0]).strip()

            # Check if returns have loaded for target period
            if include_consumer and include_commercial:
                if (c_max and c_max >= target_period_str) or (comm_max and comm_max >= target_period_str):
                    is_ready = True
            elif include_consumer:
                if c_max and c_max >= target_period_str:
                    is_ready = True
            elif include_commercial:
                if comm_max and comm_max >= target_period_str:
                    is_ready = True

            if is_ready:
                ungenerated_raw.append({
                    'subscriber_id': sub_id,
                    'subscriber_name': sub_name,
                    'last_period_num': target_period_str,
                    'consumer_max': c_max,
                    'commercial_max': comm_max,
                })
    finally:
        if close_conn:
            conn.close()

    total_elapsed = time.perf_counter() - t_start
    synced_at = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
    logger.info(
        "[Ungenerated Scan] FINISHED live scan in %.3fs: Found %d ready subscribers for %s. Caching in local RAM for 24h.",
        total_elapsed, len(ungenerated_raw), reporting_month_str
    )

    # Cache in local RAM (LocMemCache) for 24 hours (86,400 seconds) - completely off Bentley
    cache.set(cache_key, {
        'synced_at': synced_at,
        'subscribers': ungenerated_raw,
    }, timeout=86400)

    filtered = [s for s in ungenerated_raw if s['subscriber_id'] not in completed_sub_ids]
    meta = {'synced_at': synced_at, 'is_cached': False}
    return (filtered, meta) if return_meta else filtered



# ── Date Calculation Helpers ─────────────────────────────────────────────────

def compute_period_and_cutoff(reporting_month_str=None, cutoff_date_override=None):
    """
    Computes (reporting_month_str, last_period_num, cutoff_date).
    Example:
      Input '2026-07' ->
        reporting_month = '2026-07'
        last_period_num = '20260731' (last day of July 2026)
        cutoff_date     = '2026-08-01' (first day of August 2026)
    """
    if not reporting_month_str:
        today = datetime.date.today()
        # Default to previous month
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - datetime.timedelta(days=1)
        year = last_month_end.year
        month = last_month_end.month
    else:
        parts = reporting_month_str.strip().split('-')
        year = int(parts[0])
        month = int(parts[1])

    formatted_reporting_month = f"{year:04d}-{month:02d}"
    last_day = calendar.monthrange(year, month)[1]
    last_period_num = f"{year:04d}{month:02d}{last_day:02d}"

    if cutoff_date_override and cutoff_date_override.strip():
        cutoff_date = cutoff_date_override.strip()
    else:
        # First day of the following month
        if month == 12:
            next_year = year + 1
            next_month = 1
        else:
            next_year = year
            next_month = month + 1
        cutoff_date = f"{next_year:04d}-{next_month:02d}-01"

    return formatted_reporting_month, last_period_num, cutoff_date


def format_period_label(period_str):
    """
    Format '20260831' -> 'August 2026 (20260831)'
    """
    if not period_str:
        return ''
    s = str(period_str).strip()
    if len(s) >= 6:
        try:
            year = int(s[:4])
            month = int(s[4:6])
            month_name = calendar.month_name[month]
            return f"{month_name} {year} ({s})"
        except Exception:
            pass
    return s


def check_subscriber_readiness(
    subscriber_id,
    target_last_period_num,
    include_consumer=True,
    include_commercial=True,
    conn=None,
):
    """
    Verifies if a subscriber has uploaded/loaded their return for the target reporting period.
    Queries MAX(lastperiodnum) for ConsumerAccount and CommercialAccount.

    Returns dict:
    {
        'is_ready': bool,
        'consumer_max': str or None,
        'commercial_max': str or None,
        'message': str,
    }
    """
    t_start = time.perf_counter()
    logger.info("[Readiness] Checking file readiness for Subscriber ID %s against target period %s", subscriber_id, target_last_period_num)

    close_conn = False
    if conn is None:
        conn = get_bureau_connection()
        close_conn = True

    consumer_max = None
    commercial_max = None
    target_str = str(target_last_period_num).strip()

    tc_elapsed = 0.0
    tcomm_elapsed = 0.0

    try:
        cursor = conn.cursor()

        if include_consumer:
            tc0 = time.perf_counter()
            cursor.execute(CONSUMER_MAX_PERIOD_QUERY, (subscriber_id,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                consumer_max = str(row[0]).strip()
            tc_elapsed = time.perf_counter() - tc0
            logger.info(
                "[Readiness] Consumer MAX(lastperiodnum) for Sub %s: %s (query took %.3fs)",
                subscriber_id, consumer_max, tc_elapsed
            )

        if include_commercial:
            tcomm0 = time.perf_counter()
            cursor.execute(COMMERCIAL_MAX_PERIOD_QUERY, (subscriber_id,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                commercial_max = str(row[0]).strip()
            tcomm_elapsed = time.perf_counter() - tcomm0
            logger.info(
                "[Readiness] Commercial MAX(lastperiodnum) for Sub %s: %s (query took %.3fs)",
                subscriber_id, commercial_max, tcomm_elapsed
            )
    finally:
        if close_conn:
            conn.close()

    # Determine readiness
    target_label = format_period_label(target_str)

    checks_needed = []
    if include_consumer:
        checks_needed.append(('Consumer', consumer_max))
    if include_commercial:
        checks_needed.append(('Commercial', commercial_max))

    # If all inspected account types returned None, subscriber has no records in Bureau tables
    if checks_needed and all(m is None for _, m in checks_needed):
        total_elapsed = time.perf_counter() - t_start
        logger.warning(
            "[Readiness Timer] Sub ID %s: Total = %.3fs (Consumer: %.3fs, Commercial: %.3fs) — Status: NO RECORDS FOUND",
            subscriber_id, total_elapsed, tc_elapsed, tcomm_elapsed
        )
        return {
            'is_ready': False,
            'consumer_max': consumer_max,
            'commercial_max': commercial_max,
            'duration_seconds': round(total_elapsed, 3),
            'message': f"No account records found in bureau database for subscriber ID {subscriber_id}.",
        }

    # Check for unuploaded / stale periods (max < target)
    unuploaded = []
    for acct_type, m in checks_needed:
        if m is not None and m < target_str:
            unuploaded.append((acct_type, m))

    total_elapsed = time.perf_counter() - t_start
    if unuploaded:
        unuploaded_details = [
            f"{acct_type} (Latest: {format_period_label(m)})"
            for acct_type, m in unuploaded
        ]
        details_str = ", ".join(unuploaded_details)
        msg = (
            f"Kindly confirm that the subscriber has uploaded file for {target_label}. "
            f"Latest period found: {details_str}."
        )
        logger.info(
            "[Readiness Timer] Sub ID %s: Total = %.3fs (Consumer: %.3fs, Commercial: %.3fs) — Status: NOT READY (%s)",
            subscriber_id, total_elapsed, tc_elapsed, tcomm_elapsed, details_str
        )
        return {
            'is_ready': False,
            'consumer_max': consumer_max,
            'commercial_max': commercial_max,
            'duration_seconds': round(total_elapsed, 3),
            'message': msg,
        }

    # All non-None records have max >= target
    logger.info(
        "[Readiness Timer] Sub ID %s: Total = %.3fs (Consumer: %.3fs, Commercial: %.3fs) — Status: READY (Matches %s)",
        subscriber_id, total_elapsed, tc_elapsed, tcomm_elapsed, target_label
    )
    return {
        'is_ready': True,
        'consumer_max': consumer_max,
        'commercial_max': commercial_max,
        'duration_seconds': round(total_elapsed, 3),
        'message': f"File verified. Latest cycle matches {target_label}.",
    }


# ── Data Sanitization ────────────────────────────────────────────────────────

def sanitize_value(val, is_text_col=False):
    """
    Clean value and ensure leading zeros / text fields are preserved safely.
    Removes carriage returns, newlines, tabs, and commas from string values.
    """
    if val is None:
        return ''
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.strftime('%Y-%m-%d')

    if is_text_col:
        s = str(val).strip()
        # Remove control characters
        s = re.sub(r'[\r\n\t,]', '', s)
        return s

    if isinstance(val, str):
        s = val.strip()
        return re.sub(r'[\r\n\t,]', '', s)

    return val


def clean_filename(name):
    """Sanitize string for safe filenames."""
    clean = re.sub(r'[^\w\s-]', '', str(name)).strip().replace(' ', '_')
    return clean[:50] or 'subscriber'


# ── Excel Report Generation ──────────────────────────────────────────────────

def extract_unupdated_for_subscriber(
    subscriber_id,
    subscriber_name,
    include_consumer,
    include_commercial,
    last_period_num,
    cutoff_date,
    output_dir,
    check_cancelled_fn=None,
):
    """
    Executes the unupdated queries for a single subscriber and generates an .xlsx workbook
    using xlsxwriter constant_memory mode for minimal memory usage and 5-8x faster performance.
    Returns: (output_filepath, consumer_row_count, commercial_row_count)
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_sub_name = clean_filename(subscriber_name)
    filename = f"{subscriber_id}_{safe_sub_name}_unupdated_records.xlsx"
    filepath = os.path.join(output_dir, filename)

    t_ext_start = time.perf_counter()
    logger.info("[Extraction] Starting unupdated extraction for %s (ID %s)", subscriber_name, subscriber_id)

    wb = xlsxwriter.Workbook(filepath, {'constant_memory': True})
    bold_fmt = wb.add_format({'bold': True})
    text_fmt = wb.add_format({'num_format': '@'})

    consumer_rows = 0
    commercial_rows = 0

    conn = get_bureau_connection()
    try:
        cursor = conn.cursor()
        cursor.arraysize = 10000

        # 1. Consumer Sheet
        if include_consumer:
            t_c_start = time.perf_counter()
            ws_consumer = wb.add_worksheet(name="Consumer")
            for col_idx, h in enumerate(CONSUMER_HEADERS):
                ws_consumer.write(0, col_idx, h, bold_fmt)

            consumer_sql = get_consumer_query()
            params = [subscriber_id] + list(EXCLUDED_ACCOUNT_STATUSES) + [cutoff_date, last_period_num]
            logger.info("Running Consumer unupdated query for subscriber %s (ID %s)", subscriber_name, subscriber_id)
            cursor.execute(consumer_sql, tuple(params))

            row_num = 1
            while True:
                if check_cancelled_fn and check_cancelled_fn():
                    wb.close()
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    raise ExtractionCancelledException("Cancelled by user.")

                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                for row in batch:
                    consumer_rows += 1
                    for idx, header in enumerate(CONSUMER_HEADERS):
                        val = row[idx]
                        is_text = header in TEXT_COLUMNS_CONSUMER
                        clean_val = sanitize_value(val, is_text)
                        if is_text:
                            ws_consumer.write_string(row_num, idx, str(clean_val), text_fmt)
                        else:
                            ws_consumer.write(row_num, idx, clean_val)
                    row_num += 1

            c_elapsed = time.perf_counter() - t_c_start
            logger.info(
                "[Extraction] Consumer completed for %s: %d rows in %.2fs (%.1f rows/s)",
                subscriber_name, consumer_rows, c_elapsed, consumer_rows / max(c_elapsed, 0.001)
            )

        # 2. Commercial Sheet
        if include_commercial:
            t_comm_start = time.perf_counter()
            ws_commercial = wb.add_worksheet(name="Commercial")
            for col_idx, h in enumerate(COMMERCIAL_HEADERS):
                ws_commercial.write(0, col_idx, h, bold_fmt)

            commercial_sql = get_commercial_query()
            params = [subscriber_id] + list(EXCLUDED_ACCOUNT_STATUSES) + [cutoff_date, last_period_num]
            logger.info("Running Commercial unupdated query for subscriber %s (ID %s)", subscriber_name, subscriber_id)
            cursor.execute(commercial_sql, tuple(params))

            row_num = 1
            while True:
                if check_cancelled_fn and check_cancelled_fn():
                    wb.close()
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    raise ExtractionCancelledException("Cancelled by user.")

                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                for row in batch:
                    commercial_rows += 1
                    for idx, header in enumerate(COMMERCIAL_HEADERS):
                        val = row[idx]
                        is_text = header in TEXT_COLUMNS_COMMERCIAL
                        clean_val = sanitize_value(val, is_text)
                        if is_text:
                            ws_commercial.write_string(row_num, idx, str(clean_val), text_fmt)
                        else:
                            ws_commercial.write(row_num, idx, clean_val)
                    row_num += 1

            comm_elapsed = time.perf_counter() - t_comm_start
            logger.info(
                "[Extraction] Commercial completed for %s: %d rows in %.2fs (%.1f rows/s)",
                subscriber_name, commercial_rows, comm_elapsed, commercial_rows / max(comm_elapsed, 0.001)
            )

        wb.close()
        ext_total = time.perf_counter() - t_ext_start
        logger.info(
            "[Extraction] Total report generated for %s (ID %s) in %.2fs (Consumer: %d, Commercial: %d, File: %s)",
            subscriber_name, subscriber_id, ext_total, consumer_rows, commercial_rows, filepath
        )
        return filepath, consumer_rows, commercial_rows
    finally:
        conn.close()


def create_master_zip(file_paths, output_zip_path):
    """
    Compress multiple .xlsx reports into a single master .zip file.
    """
    t_zip = time.perf_counter()
    os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fpath in file_paths:
            if os.path.exists(fpath):
                arcname = os.path.basename(fpath)
                zf.write(fpath, arcname=arcname)
    logger.info(
        "[Compression] Created master ZIP %s with %d files in %.2fs",
        os.path.basename(output_zip_path), len(file_paths), time.perf_counter() - t_zip
    )
    return output_zip_path
