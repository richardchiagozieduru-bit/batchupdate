"""
Extraction services — pure utility functions.
Handles Bureau DB connections, streaming CSV export with parallel chunking,
and ZIP compression.

Performance architecture:
- Option B (SQL-side formatting): Python receives pre-formatted VARCHAR strings
  from SQL Server. No per-cell type checking, no decimal formatting, no ID wrapping.
  Python just writes rows straight to CSV.
- Option A (Parallel chunking): For large extractions (>500K rows), splits the
  query by primary key range and runs N parallel workers, each with its own
  database connection, streaming to its own CSV file.
"""

import concurrent.futures
import csv
import logging
import os
import queue
import shutil
import threading
import time
import zipfile

import pyodbc
from django.conf import settings

from .queries import (
    SUBSCRIBER_SEARCH_QUERY,
    CONSUMER_RANGE_QUERY, CONSUMER_QUERY, CONSUMER_QUERY_CHUNKED,
    COMMERCIAL_RANGE_QUERY, COMMERCIAL_QUERY, COMMERCIAL_QUERY_CHUNKED,
)

logger = logging.getLogger('extraction')


# ── Database Connection ──────────────────────────────────────────────────────

def get_bureau_connection():
    """
    Open a read-only pyodbc connection to the Bureau SQL Server database.
    Supports both SQL Server authentication (username/password for remote servers)
    and Windows trusted connection as a fallback.
    Includes TCP KeepAlive settings to prevent network firewalls and remote SQL Server
    from dropping long-running streaming connections (TCP 10054).
    Uses settings.BUREAU_DB_* variables configured in settings.py / .env.
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
    conn = pyodbc.connect(conn_str, timeout=60)
    return conn


# ── Subscriber Search ────────────────────────────────────────────────────────

def search_subscribers(search_term):
    """
    Search for subscribers in the Bureau database by name.
    Returns a list of dicts: [{'subscriber_id': int, 'subscriber_name': str}, ...]
    """
    conn = get_bureau_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(SUBSCRIBER_SEARCH_QUERY, (f'%{search_term}%',))
        results = []
        for row in cursor.fetchall():
            results.append({
                'subscriber_name': str(row[0]).strip() if row[0] else '',
                'subscriber_id': int(row[1]) if row[1] else 0,
            })
        return results
    finally:
        conn.close()


class ExtractionCancelledException(Exception):
    """Raised when an extraction task is cancelled by the user mid-stream."""
    pass


# ── Row Count & ID Range ────────────────────────────────────────────────────

def get_row_count_and_range(subscriber_id, query_type):
    """
    Get total row count and primary key range for a subscriber's data.
    This is a fast query (no joins) used to decide single vs parallel streaming
    and to calculate range boundaries for parallel workers.

    Args:
        subscriber_id: Bureau subscriber ID.
        query_type: 'consumer' or 'commercial'.

    Returns:
        tuple: (min_id, max_id, total_count) — or (None, None, 0) if no data.
    """
    range_query = CONSUMER_RANGE_QUERY if query_type == 'consumer' else COMMERCIAL_RANGE_QUERY

    conn = get_bureau_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(range_query, (subscriber_id,))
        row = cursor.fetchone()
        if not row or row[2] == 0:
            return None, None, 0
        return row[0], row[1], row[2]
    finally:
        conn.close()


# ── Streaming CSV Export (simplified — no per-cell formatting) ───────────────

def stream_query_to_csv_chunks(cursor, output_dir, base_filename, headers=None,
                               max_rows=None, fetch_size=None, check_cancelled_fn=None,
                               worker_label=None):
    """
    Stream query results from a pyodbc cursor into CSV chunk files.

    Since SQL-side formatting (Option B) handles all value conversion, NULL
    sanitisation, and Excel-safe wrapping in the query itself, this function
    simply writes rows directly to CSV with zero per-cell processing.

    - Fetches rows in batches of `fetch_size`.
    - When a file reaches `max_rows`, rolls over to the next chunk.
    - If total rows < max_rows, produces a single CSV with no _partN suffix.

    Returns:
        tuple: (list_of_csv_paths, total_rows)
    """
    if max_rows is None:
        max_rows = getattr(settings, 'EXTRACTION_MAX_ROWS_PER_CSV', 1_000_000)
    if fetch_size is None:
        fetch_size = getattr(settings, 'EXTRACTION_FETCH_BATCH_SIZE', 100_000)

    os.makedirs(output_dir, exist_ok=True)

    # Resolve headers from cursor if not provided explicitly
    if headers is None:
        headers = [col[0] for col in cursor.description]

    tag = worker_label or base_filename
    generated_files = []
    chunk_index = 1
    total_rows = 0
    current_chunk_rows = 0
    current_file = None
    writer = None

    start_time = time.time()
    last_log_rows = 0

    logger.info(f"[{tag}] Streaming started at {time.strftime('%H:%M:%S')}. "
                f"Batch size: {fetch_size:,}, Max per file: {max_rows:,}")

    def _open_chunk(part_num):
        """Open a new CSV chunk file and write the header row."""
        if part_num == 1:
            filepath = os.path.join(output_dir, f'{base_filename}_part1.csv')
        else:
            filepath = os.path.join(output_dir, f'{base_filename}_part{part_num}.csv')
        f = open(filepath, 'w', newline='', encoding='utf-8')
        w = csv.writer(f)
        w.writerow(headers)
        generated_files.append(filepath)
        return f, w

    try:
        current_file, writer = _open_chunk(chunk_index)

        while True:
            if check_cancelled_fn and check_cancelled_fn():
                logger.warning(f"[{tag}] Cancelled mid-stream at {total_rows:,} rows.")
                if current_file:
                    current_file.close()
                for fpath in generated_files:
                    if os.path.exists(fpath):
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass
                raise ExtractionCancelledException(
                    f"Extraction cancelled by user at {total_rows:,} rows"
                )

            rows = cursor.fetchmany(fetch_size)
            if not rows:
                break

            for row in rows:
                if current_chunk_rows >= max_rows:
                    current_file.close()
                    chunk_index += 1
                    current_chunk_rows = 0
                    current_file, writer = _open_chunk(chunk_index)
                    logger.info(f"[{tag}] Rolled over to file part {chunk_index} "
                                f"after {total_rows:,} rows")

                # SQL-side formatting means rows are already clean strings.
                # No type checking, no decimal formatting, no ID wrapping needed.
                writer.writerow(row)
                current_chunk_rows += 1
                total_rows += 1

            # Log progress every 100,000 rows
            if total_rows - last_log_rows >= 100_000:
                elapsed = time.time() - start_time
                rate = total_rows / elapsed if elapsed > 0 else 0
                logger.info(f"[{tag}] Streamed {total_rows:,} rows in "
                            f"{elapsed:.1f}s ({rate:,.0f} rows/sec)")
                last_log_rows = total_rows

    finally:
        if current_file:
            current_file.close()

    total_elapsed = time.time() - start_time
    mins, secs = divmod(int(total_elapsed), 60)
    time_str = f"{mins}m {secs}s" if mins > 0 else f"{total_elapsed:.2f}s"
    logger.info(f"[{tag}] Finished at {time.strftime('%H:%M:%S')}. "
                f"Total: {total_rows:,} rows in {time_str} "
                f"into {len(generated_files)} file(s).")

    # If only one chunk was produced, rename to remove the _part1 suffix
    if len(generated_files) == 1:
        original_path = generated_files[0]
        clean_path = os.path.join(output_dir, f'{base_filename}.csv')
        os.rename(original_path, clean_path)
        generated_files[0] = clean_path

    return generated_files, total_rows


# ── High-Level Extraction Entry Point ────────────────────────────────────────

def run_extraction_query(subscriber_id, query_type, output_dir, base_filename,
                         headers, check_cancelled_fn=None):
    """
    High-level extraction function. Automatically decides between single-stream
    and parallel-stream based on the subscriber's row count.

    For small subscribers (≤500K rows): single database connection, single stream.
    For large subscribers (>500K rows): parallel workers with range-based chunking.

    Args:
        subscriber_id: Bureau subscriber ID.
        query_type: 'consumer' or 'commercial'.
        output_dir: Directory for CSV output files.
        base_filename: Base name for CSV files (e.g. '988_MyCredit_Consumer').
        headers: Column headers for CSV files.
        check_cancelled_fn: Optional callback returning True if cancelled.

    Returns:
        tuple: (list_of_csv_paths, total_rows)
    """
    parallel_threshold = 500_000
    num_workers = getattr(settings, 'EXTRACTION_PARALLEL_WORKERS', 4)

    # Step 1: Get count and ID range (fast — no joins)
    min_id, max_id, total_count = get_row_count_and_range(subscriber_id, query_type)

    if total_count == 0:
        logger.info(f"[{base_filename}] No rows found for subscriber {subscriber_id} "
                     f"({query_type}). Skipping.")
        return [], 0

    logger.info(f"[{base_filename}] Found {total_count:,} {query_type} rows for "
                f"subscriber {subscriber_id} (ID range: {min_id}–{max_id})")

    # Step 2: Choose strategy
    if total_count <= parallel_threshold:
        return _single_stream(subscriber_id, query_type, output_dir,
                              base_filename, headers, check_cancelled_fn)
    else:
        return _parallel_stream(subscriber_id, query_type, output_dir,
                                base_filename, headers, min_id, max_id,
                                num_workers, check_cancelled_fn)


def _single_stream(subscriber_id, query_type, output_dir, base_filename,
                   headers, check_cancelled_fn):
    """Single-connection streaming for small extractions."""
    query = CONSUMER_QUERY if query_type == 'consumer' else COMMERCIAL_QUERY

    conn = get_bureau_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query, (subscriber_id,))
        files, rows = stream_query_to_csv_chunks(
            cursor, output_dir, base_filename, headers,
            check_cancelled_fn=check_cancelled_fn,
        )
        return files, rows
    finally:
        conn.close()


def _parallel_stream(subscriber_id, query_type, output_dir, base_filename,
                     headers, min_id, max_id, num_workers, check_cancelled_fn):
    """
    Parallel range-based streaming for large extractions using a dynamic work queue.

    Instead of assigning one fixed ID range per worker, the full primary key range
    is split into many small chunks (num_workers * 8) and placed in a shared queue.
    Each worker thread grabs the next available chunk when it finishes the current
    one, so all workers stay busy until every chunk is processed.

    Each worker opens one persistent DB connection and reuses it across all chunks.
    pyodbc releases the GIL during network I/O, so threads scale well here.
    """
    query = CONSUMER_QUERY_CHUNKED if query_type == 'consumer' else COMMERCIAL_QUERY_CHUNKED

    # Create many small chunks for dynamic distribution (num_workers * 8)
    num_chunks = num_workers * 8
    id_range = max_id - min_id + 1
    chunk_size = id_range // num_chunks
    # Ensure chunk_size is at least 1 to avoid zero-width ranges
    if chunk_size < 1:
        chunk_size = 1
        num_chunks = id_range

    work_queue = queue.Queue()
    for i in range(num_chunks):
        start = min_id + (i * chunk_size)
        # Last chunk takes everything up to max_id + 1 (so max_id is included via <)
        end = max_id + 1 if i == num_chunks - 1 else min_id + ((i + 1) * chunk_size)
        work_queue.put((i + 1, start, end))

    logger.info(f"[{base_filename}] Launching {num_workers} parallel workers "
                f"with {num_chunks} chunks across ID range {min_id}–{max_id}")

    temp_dir = os.path.join(output_dir, f"_temp_{base_filename}")
    os.makedirs(temp_dir, exist_ok=True)

    total_rows = 0
    lock = threading.Lock()
    errors = []

    def _stream_worker(worker_id):
        """Worker thread: grab chunks from the queue until empty."""
        nonlocal total_rows
        worker_tag = f"{base_filename}·W{worker_id}"
        worker_rows = 0
        chunks_done = 0

        try:
            conn = get_bureau_connection()
            try:
                while True:
                    # Check cancellation between chunks
                    if check_cancelled_fn and check_cancelled_fn():
                        logger.warning(f"[{worker_tag}] Cancelled between chunks.")
                        raise ExtractionCancelledException(
                            f"Worker {worker_id} cancelled between chunks"
                        )

                    # Grab next chunk (non-blocking)
                    try:
                        chunk_id, id_start, id_end = work_queue.get_nowait()
                    except queue.Empty:
                        break  # No more work

                    logger.info(
                        f"[{worker_tag}] Processing chunk {chunk_id}/{num_chunks} "
                        f"(ID range {id_start:,}–{id_end:,})"
                    )

                    # Prefix with zero-padded chunk_id (c001, c002...) so temp files sort naturally by primary key order
                    chunk_filename = f"c{chunk_id:03d}_w{worker_id}"
                    cursor = conn.cursor()
                    cursor.execute(query, (subscriber_id, id_start, id_end))
                    _, rows = stream_query_to_csv_chunks(
                        cursor, temp_dir, chunk_filename, headers,
                        check_cancelled_fn=check_cancelled_fn,
                        worker_label=worker_tag,
                    )
                    cursor.close()

                    with lock:
                        total_rows += rows
                    worker_rows += rows
                    chunks_done += 1
                    work_queue.task_done()

            finally:
                conn.close()

        except ExtractionCancelledException:
            raise
        except Exception as e:
            logger.error(f"[{worker_tag}] Worker failed: {e}", exc_info=True)
            with lock:
                errors.append(e)

        logger.info(
            f"[{worker_tag}] Completed {chunks_done} chunks, "
            f"{worker_rows:,} rows total"
        )

    # Run all workers in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = []
        for i in range(num_workers):
            futures.append(
                executor.submit(_stream_worker, i + 1)
            )

        # Wait for completion and propagate any exceptions
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except ExtractionCancelledException:
                # Cancel remaining workers and clean up temp_dir
                for f in futures:
                    f.cancel()
                if os.path.exists(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                    except OSError:
                        pass
                raise

    # If any worker failed, clean up temp_dir and raise the first error
    if errors:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass
        raise errors[0]

    logger.info(f"[{base_filename}] All {num_workers} workers complete. "
                f"Total: {total_rows:,} raw rows extracted across {num_chunks} chunks.")

    # Consolidate raw temp chunk files into clean 1,000,000-row output files
    consolidated_files = _consolidate_chunk_files(
        temp_dir, output_dir, base_filename, headers, total_rows
    )

    return consolidated_files, total_rows


def _consolidate_chunk_files(temp_dir, output_dir, base_filename, headers, total_rows):
    """
    Consolidate raw worker chunk files from temp_dir into clean 1,000,000-row capped
    CSV files in output_dir.

    Files in temp_dir are sorted by name (c001, c002, etc.) to maintain exact primary key order.
    Streams raw lines directly to avoid Python-level CSV re-parsing and serialization overhead.
    """
    max_rows = getattr(settings, 'EXTRACTION_MAX_ROWS_PER_CSV', 1_000_000)
    os.makedirs(output_dir, exist_ok=True)

    temp_files = sorted([
        os.path.join(temp_dir, f) for f in os.listdir(temp_dir)
        if f.endswith('.csv')
    ])

    if not temp_files:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass
        return []

    logger.info(f"[{base_filename}] Consolidating {len(temp_files)} chunk files into clean output CSVs via fast buffered streaming...")

    consolidated_files = []
    current_part = 1
    current_out_file = None
    current_out_rows = 0

    def _open_out_part(part_num):
        filepath = os.path.join(output_dir, f"{base_filename}_part{part_num}.csv")
        f = open(filepath, 'w', newline='', encoding='utf-8')
        w = csv.writer(f)
        w.writerow(headers)
        consolidated_files.append(filepath)
        return f

    try:
        current_out_file = _open_out_part(current_part)

        for temp_file_path in temp_files:
            with open(temp_file_path, 'r', encoding='utf-8', errors='replace') as tf:
                # Skip the header line of this chunk file
                tf.readline()

                for line in tf:
                    if not line:
                        continue
                    if current_out_rows >= max_rows:
                        current_out_file.close()
                        current_part += 1
                        current_out_rows = 0
                        current_out_file = _open_out_part(current_part)

                    current_out_file.write(line)
                    current_out_rows += 1

    finally:
        if current_out_file:
            current_out_file.close()

    # Remove temporary directory and raw worker files
    try:
        shutil.rmtree(temp_dir)
    except OSError as e:
        logger.warning(f"[{base_filename}] Could not remove temp directory {temp_dir}: {e}")

    # If only one consolidated file was produced, rename to remove the _part1 suffix
    if len(consolidated_files) == 1:
        original_path = consolidated_files[0]
        clean_path = os.path.join(output_dir, f'{base_filename}.csv')
        os.rename(original_path, clean_path)
        consolidated_files[0] = clean_path

    logger.info(f"[{base_filename}] Consolidation complete. Created {len(consolidated_files)} clean file(s) for {total_rows:,} rows.")
    return consolidated_files


# ── ZIP Compression ──────────────────────────────────────────────────────────

def compress_to_zip(csv_paths, zip_path):
    """
    Compress a list of CSV files into a single ZIP archive.
    Deletes the raw CSV files after successful compression to free disk space.

    Args:
        csv_paths: List of absolute paths to CSV files.
        zip_path: Absolute path for the output ZIP file.

    Returns:
        str: The zip_path.
    """
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for csv_path in csv_paths:
            arcname = os.path.basename(csv_path)
            zf.write(csv_path, arcname)

    # Clean up temporary CSV files
    for csv_path in csv_paths:
        try:
            os.remove(csv_path)
        except OSError:
            logger.warning(f"Could not delete temp CSV: {csv_path}")

    logger.info(f"Compressed {len(csv_paths)} CSV files into {zip_path}")
    return zip_path


def build_master_zip(subscriber_zip_paths, master_zip_path):
    """
    Combine individual subscriber ZIP files into a single master batch ZIP.
    Does NOT delete individual subscriber ZIPs (they remain downloadable individually).

    Args:
        subscriber_zip_paths: List of absolute paths to per-subscriber ZIP files.
        master_zip_path: Absolute path for the output master ZIP file.

    Returns:
        str: The master_zip_path.
    """
    os.makedirs(os.path.dirname(master_zip_path), exist_ok=True)

    with zipfile.ZipFile(master_zip_path, 'w', zipfile.ZIP_DEFLATED) as master_zf:
        for sub_zip in subscriber_zip_paths:
            if os.path.exists(sub_zip):
                arcname = os.path.basename(sub_zip)
                master_zf.write(sub_zip, arcname)

    logger.info(f"Built master ZIP with {len(subscriber_zip_paths)} subscriber archives → {master_zip_path}")
    return master_zip_path
