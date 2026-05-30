# DataClean — Improvement Plan

Ordered by priority. Each item tracks status and the exact file(s) to change.

---

## Status Legend
- [ ] Not started
- [x] Done

---

## HIGH — Security & Correctness

### H1 — SQL injection via `]` in table name
- **File:** `update/services.py` → `upload_raw_to_batchupdate()`
- **Fix:** Also escape `]` → `]]` in `safe_table` before using it in bracket identifiers.
- [x] Done

### H2 — `SECRET_KEY` insecure fallback + `DEBUG` defaults to `True`
- **File:** `cleaner/settings.py`
- **Fix:** Raise `ImproperlyConfigured` if `SECRET_KEY` env var is missing. Change `DEBUG` default to `'False'` so a missing `.env` fails safe.
- [x] Done

### H3 — `MEDIA_URL` missing leading `/`
- **File:** `cleaner/settings.py`
- **Fix:** Change `MEDIA_URL = 'media/'` → `MEDIA_URL = '/media/'`. Same for `STATIC_URL`.
- [x] Done

### H4 — No `STATIC_ROOT` defined (breaks `collectstatic` in production)
- **File:** `cleaner/settings.py`
- **Fix:** Add `STATIC_ROOT = BASE_DIR / 'staticfiles'`.
- [x] Done

---

## HIGH — Memory / Large File Handling

### H5 — Entire file loaded into RAM; 500MB limit is dangerously high
- **File:** `update/services.py` → `read_uploaded_file()`, `update/views.py`
- **Fix:**
  - Lower `MAX_FILE_SIZE_MB` to a safe default (e.g. 50MB for Excel, 200MB for CSV).
  - For CSV, use `pd.read_csv(..., chunksize=50_000)` in the task worker and process in chunks.
  - Document the memory-to-filesize ratio in a comment so future maintainers understand the constraint.
- [x] Done

### H6 — `format_excel_sheet()` iterates every cell (extremely slow for large output)
- **File:** `update/services.py` → `format_excel_sheet()`
- **Fix:** Apply column-level number formats via `ws.column_dimensions` and `ColumnDimension` instead of per-cell loops. For text columns, only rewrite cell values where the value is not already a string.
- [x] Done

### H7 — Multi-sheet upload keeps all DataFrames in RAM simultaneously
- **File:** `update/views.py` → `upload_view()` multi-sheet branch
- **Fix:** Process and discard each sheet's DataFrame immediately (don't accumulate `(session, df)` tuples). Re-read the df per session only if needed for auto-mapping; otherwise pass `None` and let the async task handle it.
- [x] Done

---

## MEDIUM — Data & File Lifecycle

### M1 — `os.path.relpath(final_path, 'media')` is relative to CWD, not BASE_DIR
- **File:** `update/tasks.py`
- **Fix:** Use `os.path.relpath(final_path, str(settings.MEDIA_ROOT))` so the path is always correct regardless of the working directory.
- [x] Done

### M2 — Orphaned temp sessions and files on error
- **File:** `update/views.py` → `upload_view()`, `_handle_free_upload()`
- **Fix:** Wrap temp session creation in a try/except; delete the temp session (and its file) in the except block. Use `try/finally` where appropriate.
- [x] Done

### M3 — No file lifecycle / disk cleanup
- **File:** `update/tasks.py` (new task) + `cleaner/settings.py`
- **Fix:** Add a scheduled Django-Q task `cleanup_old_sessions_task` that deletes `UploadSession` records (and their media files) older than a configurable `SESSION_RETENTION_DAYS` (default: 30). Register it in `Q_CLUSTER` as a scheduled task.
- [x] Done

### M4 — Silent `except Exception: pass` throughout views
- **File:** `update/views.py` (≈8 occurrences)
- **Fix:** Replace bare `pass` with `logger.warning(..., exc_info=True)` so failures surface in logs without crashing the user-facing flow.
- [x] Done

---

## MEDIUM — Performance

### M5 — Subscriber list fetched from SQL Server on every page load
- **File:** `update/services.py` → `get_subscribers_from_batchupdate()`, `update/views.py`
- **Fix:** Cache the result using Django's cache framework (`cache.get_or_set('subscribers', ..., timeout=300)`). Add `django.core.cache` backends config (in-memory `LocMemCache` is fine for single-server).
- [x] Done

### M6 — BatchUpdate destination columns all `NVARCHAR(MAX)`
- **File:** `update/services.py` → `upload_raw_to_batchupdate()`
- **Fix:** Use proper SQL types for known columns (`DECIMAL(18,2)` for amounts, `INT` for months, `NVARCHAR(50)` for codes/classification, `NVARCHAR(100)` for account number). Only fall back to `NVARCHAR(255)` for unmapped/unknown columns.
- [x] Done

### M7 — Django-Q retry window too narrow (task can thrash)
- **File:** `cleaner/settings.py` → `Q_CLUSTER`
- **Fix:** Increase `retry` to at least `timeout + 600` (40 minutes), or set `max_attempts` on the task call in `tasks.py` to prevent runaway retries on a failing large-file task.
- [x] Done

---

## LOW — Architecture & Maintainability

### L1 — Dead `celery.py` (project uses Django-Q2, not Celery)
- **File:** `cleaner/celery.py`
- **Fix:** Delete the file. Remove any leftover `celery` imports.
- [x] Done

### L2 — Cross-app view helper imports (`_is_external`, `_require_bound`)
- **File:** `acctmgt/views.py`, `update/views.py`
- **Fix:** Move `_is_external()` and `_require_bound()` to `acctmgt/utils.py`. Update both views to import from there.
- [x] Done

### L3 — No rate limiting on login / register
- **File:** `acctmgt/views.py`
- **Fix:** Use Django's built-in `django.contrib.auth` throttling or add `django-axes` / simple decorator-based attempt tracking to cap failed logins per IP.
- [x] Done

### L4 — Target column definitions duplicated across 5+ locations
- **File:** `update/models.py`, `update/services.py`
- **Fix:** Define a single `TARGET_COLUMN_META` dict in `update/services.py` (or a new `update/columns.py`) that maps internal name → display name, type, and validation rules. All other locations import from it.
- [x] Done

---

## Order of Execution

1. H1 (SQL injection)
2. H2 (SECRET_KEY / DEBUG)
3. H3 + H4 (MEDIA_URL / STATIC_ROOT)
4. H5 (file size / chunked CSV)
5. H6 (Excel formatting loop)
6. H7 (RAM accumulation multi-sheet)
7. M1 (relpath bug)
8. M2 (orphaned sessions)
9. M4 (silent exceptions)
10. M5 (subscriber cache)
11. M6 (column types)
12. M7 (Q retry window)
13. M3 (cleanup task)
14. L1 (dead celery.py)
15. L2 (helper imports)
16. L3 (rate limiting)
17. L4 (column meta consolidation)
