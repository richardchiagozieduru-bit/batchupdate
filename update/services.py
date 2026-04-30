import os
import re
import hashlib
import logging
import warnings
import zipfile
import pyodbc
import numpy as np
import pandas as pd
from datetime import datetime
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.utils.exceptions import InvalidFileException

from .models import UploadSession

logger = logging.getLogger(__name__)


def detect_header_row(file_path, sheet_name=None, template_signatures=None, max_scan_rows=15):
    """
    Detect which row (0-based) contains the column headers.

    Strategy:
      1. If template_signatures (list of JSON strings) provided, scan rows and
         return the first row whose sorted values match a known signature.
      2. Fall back to heuristic scoring:
         - all strings, no blanks, no duplicates  → header-like
         - next row contains at least one numeric  → data-like
         Row 0 gets a tiebreaker bonus so default behaviour is unchanged.

    Returns: int (0-based index to pass as pandas header= parameter).
    """
    import json as _json

    ext = os.path.splitext(file_path)[1].lower()

    def _is_numeric(v):
        try:
            float(v)
            return True
        except (ValueError, TypeError):
            return False

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=UserWarning, module='openpyxl')
        try:
            kw = dict(header=None, nrows=max_scan_rows, dtype=str)
            if ext == '.csv':
                raw = pd.read_csv(file_path, **kw)
            elif ext == '.xlsx':
                if sheet_name:
                    kw['sheet_name'] = sheet_name
                raw = pd.read_excel(file_path, engine='openpyxl', **kw)
            elif ext == '.xls':
                if sheet_name:
                    kw['sheet_name'] = sheet_name
                raw = pd.read_excel(file_path, engine='xlrd', **kw)
            else:
                if sheet_name:
                    kw['sheet_name'] = sheet_name
                try:
                    raw = pd.read_excel(file_path, engine='openpyxl', **kw)
                except Exception:
                    raw = pd.read_excel(file_path, engine='xlrd', **kw)
        except Exception:
            return 0

    n_rows = len(raw)
    if n_rows == 0:
        return 0

    sig_set = set(template_signatures) if template_signatures else set()

    # ── Step 1: exact template-signature match ──
    for row_idx in range(min(n_rows, max_scan_rows)):
        candidate = raw.iloc[row_idx].astype(str).str.strip().tolist()
        candidate_clean = [v for v in candidate if v and v.lower() not in ('nan', 'none', '')]
        if not candidate_clean:
            continue
        sig = _json.dumps(sorted(candidate_clean))
        if sig in sig_set:
            logger.info(f"Header row detected via template match at row {row_idx}: {file_path}")
            return row_idx

    # ── Step 2: heuristic scoring ──
    best_row, best_score = 0, -1
    scan_limit = min(n_rows - 1, max_scan_rows - 1)

    for row_idx in range(scan_limit + 1):
        row_vals = raw.iloc[row_idx].astype(str).str.strip().tolist()
        non_empty = [v for v in row_vals if v and v.lower() not in ('nan', 'none', '')]
        if not non_empty:
            continue

        score = 0
        if len(non_empty) == len(row_vals):          # no blanks
            score += 2
        if len(set(non_empty)) == len(non_empty):    # no duplicates
            score += 2
        if all(not _is_numeric(v) for v in non_empty):  # all look like text
            score += 3
        if row_idx + 1 < n_rows:                     # next row has a numeric value
            nxt = raw.iloc[row_idx + 1].astype(str).str.strip().tolist()
            if any(_is_numeric(v) for v in nxt if v and v.lower() not in ('nan', 'none', '')):
                score += 3
        if row_idx == 0:                             # tiebreaker: prefer row 0
            score += 1

        if score > best_score:
            best_score, best_row = score, row_idx

    logger.info(f"Header row detected via heuristic: row {best_row} (score={best_score}) in {file_path}")
    return best_row


def read_uploaded_file(file_path, header=0):
    """Read CSV or Excel file into a DataFrame with engine auto-detection."""
    ext = os.path.splitext(file_path)[1].lower()
    logger.info(f"Reading file: {file_path} (extension: {ext}, header_row={header})")
    
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=UserWarning, module='openpyxl')
        if ext == '.csv':
            return pd.read_csv(file_path, dtype=str, header=header)
        elif ext == '.xlsx':
            return pd.read_excel(file_path, dtype=str, engine='openpyxl', header=header)
        elif ext == '.xls':
            return pd.read_excel(file_path, dtype=str, engine='xlrd', header=header)
        else:
            # Try openpyxl first, fall back to xlrd
            try:
                return pd.read_excel(file_path, dtype=str, engine='openpyxl', header=header)
            except Exception:
                logger.warning(f"openpyxl failed for {file_path}, trying xlrd")
                return pd.read_excel(file_path, dtype=str, engine='xlrd', header=header)


# Configuration
MAX_FILE_SIZE_MB = 500  # Maximum upload size in MB

# Target column definitions with their data types
NUMERIC_COLUMNS = ['CurrentBalanceAmt', 'overdue_amount', 'months_in_arrears']
TEXT_COLUMNS = ['loan_classification', 'account_status_code']
STRING_COLUMNS = ['account_number']
# Validation rules for numeric columns (max only - negatives stripped during cleaning)
VALIDATION_RULES = {
    'CurrentBalanceAmt': {'max': 999999999999},
    'overdue_amount': {'max': 999999999999},
    'months_in_arrears': {'max': 9999},
}

# Display-friendly headers for Excel output
DISPLAY_HEADERS = {
    'account_number': 'AccountNo',
    'CurrentBalanceAmt': 'CurrentBalanceAmt',
    'overdue_amount': 'AmountOverdue',
    'months_in_arrears': 'MonthsInArrears',
    'loan_classification': 'LoanClassification',
    'account_status_code': 'AccountStatusCode',
}

# Value transformation mappings
ACCOUNT_STATUS_MAP = {
    'Open': ['001', 'open', '01', '1', 'opened', 'active'],
    'Closed': ['002', 'closed', 'close', '02', '2', 'cloed'],
    'Writtenoff': ['003', 'written off', 'written0ff', '03', '3', 'writenoff', 'writtenoff', 'writeoff', 'write off', ',write off'],
    'Performing': ['performing', 'perfroming']
}

LOAN_CLASSIFICATION_MAP = {
    'Performing': ['001', 'performing', '1', '01', 'perform', 'performingloansperformingadvances', 
                   'performing loans', 'performing advances', 'performingloans', 'performingadvances', 'performimg'],
    'Watchlist': ['002', 'watchlist', '02', '2', 'pass and watch', 'passwatch', 'pass watch', 
                  'paasandwatch', 'pw', 'p&w', 'passandwatch'],
    'Sub standard': ['003', 'sub standard', 'substandard', '03', '3', 'sub', 'subs', 
                     'substandardloans', 'substandardadvances'],
    'Doubtful': ['004', 'doubtful', '04', '4', 'very doubtful', 'verydoubtful', 'doub', 'doubt', 
                 'doubtfulloans', 'doubtfuladvances'],
    'Lost': ['005', 'lost', '05', '5', 'loss', 'l'],
    'Writtenoff': ['write off', 'written off', 'writeoff']
}


def calculate_file_hash(file_path):
    """Calculate MD5 hash of a file for duplicate detection."""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_value(value, mapping_dict):
    """
    Normalize a value using a mapping dictionary.
    Converts variations to their canonical form.
    """
    if pd.isna(value) or value is None:
        return None
    
    value_str = str(value).lower().strip()
    
    for canonical, variations in mapping_dict.items():
        if value_str in [v.lower() for v in variations]:
            return canonical
    
    return value_str


def validate_numeric(value, column_name):
    """Validate numeric value against rules. Returns (valid, value)."""
    if value is None:
        return True, None
    
    rules = VALIDATION_RULES.get(column_name, {})
    min_val = rules.get('min')
    max_val = rules.get('max')
    
    try:
        num_val = float(value) if not isinstance(value, (int, float, Decimal)) else float(value)
        
        if min_val is not None and num_val < min_val:
            return False, None
        if max_val is not None and num_val > max_val:
            return False, None
            
        return True, value
    except (ValueError, TypeError):
        return False, None


def clean_value(value, column_name):
    """
    Clean a single value based on column type.
    - Numeric: Remove special chars except decimal point, validate
    - Text: Remove all special chars, convert to string
    - Special columns: Apply value transformations
    """
    try:
        if pd.isna(value):
            return None
    except (ValueError, TypeError):
        return None
    
    value_str = str(value).strip()
    
    if column_name in NUMERIC_COLUMNS:
        # Keep only digits and decimal point
        cleaned = re.sub(r'[^\d.]', '', value_str)
        if not cleaned:
            return None
        try:
            float_val = float(cleaned)
            # CurrentBalanceAmt and overdue_amount keep decimal precision
            # months_in_arrears is always a whole number
            if column_name in ('CurrentBalanceAmt', 'overdue_amount'):
                result = float_val
            else:
                result = int(float_val)

            # Validate against rules
            valid, validated = validate_numeric(result, column_name)
            return validated if valid else None

        except (ValueError, InvalidOperation):
            return None
    
    elif column_name == 'account_status_code':
        return normalize_value(value, ACCOUNT_STATUS_MAP)
    
    elif column_name == 'loan_classification':
        return normalize_value(value, LOAN_CLASSIFICATION_MAP)
    
    elif column_name in STRING_COLUMNS:
        return value_str if value_str else None
    
    else:
        cleaned = re.sub(r'[^a-zA-Z0-9\s]', '', value_str)
        return cleaned.strip() if cleaned.strip() else None


def is_row_empty(row, columns):
    """Check if all mapped columns in a row are empty/None."""
    for col in columns:
        val = row.get(col)
        if val is not None and str(val).strip():
            return False
    return True


def clean_dataframe(df, mappings, format_for_display=False):
    """
    Clean dataframe based on column mappings.
    Applies business validation rules, skips invalid rows, removes duplicates.
    
    Returns: (cleaned_df, rejected_df)
    """
    columns_to_keep = list(mappings.keys())
    df_subset = df[columns_to_keep].copy()
    df_subset.rename(columns=mappings, inplace=True)
    
    # Handle duplicate target columns (e.g. two source cols mapped to same target)
    # Keep only the first occurrence of each column name
    if df_subset.columns.duplicated().any():
        dupes = df_subset.columns[df_subset.columns.duplicated(keep=False)].unique().tolist()
        logger.warning(f"Duplicate target columns detected and deduplicated: {dupes}")
        df_subset = df_subset.loc[:, ~df_subset.columns.duplicated(keep='first')]
    
    # Clean each column
    for column in df_subset.columns:
        df_subset[column] = df_subset[column].apply(lambda x: clean_value(x, column))
    
    # Remove empty rows (where all mapped columns are None) - NOT tracked as rejected
    mapped_columns = list(mappings.values())
    df_subset = df_subset.dropna(subset=mapped_columns, how='all')
    
    # Ensure all target columns exist so auto-fill logic can inject defaults
    # for columns that were not mapped (e.g. overdue_amount missing from source file)
    _ALL_TARGET_COLS = ['account_number', 'CurrentBalanceAmt', 'overdue_amount',
                        'months_in_arrears', 'loan_classification', 'account_status_code']
    _injected = []
    for _col in _ALL_TARGET_COLS:
        if _col not in df_subset.columns:
            df_subset[_col] = None
            _injected.append(_col)
    if _injected:
        logger.info(f"Auto-injected missing columns (will be filled with 0 where balance=0): {_injected}")
    
    # --- Business Validation Rules (returns valid + rejected) ---
    # Pass all target columns (including injected ones) so auto-fill for balance=0 works
    all_target_cols = list(df_subset.columns)
    df_subset, rejected_df = apply_business_rules(df_subset, mapped_columns, all_target_cols)
    
    # Remove duplicate rows (where ALL values match exactly)
    df_subset = df_subset.drop_duplicates(keep='first')
    
    # Reset index after filtering
    df_subset = df_subset.reset_index(drop=True)
    
    # Rename columns for display (Proper Case without underscores)
    if format_for_display:
        df_subset.rename(columns=DISPLAY_HEADERS, inplace=True)
        # Also rename rejected df columns for display
        if not rejected_df.empty:
            reason_col = rejected_df['Rejection Reason']
            rejected_df = rejected_df.drop(columns=['Rejection Reason'])
            rejected_df.rename(columns=DISPLAY_HEADERS, inplace=True)
            rejected_df['Rejection Reason'] = reason_col
    
    return df_subset, rejected_df


def apply_business_rules(df, mapped_columns, all_columns=None):
    """
    Apply business validation rules to cleaned data using vectorized pandas operations.
    
    Returns: (valid_df, rejected_df)
    
    Rejected rows include a 'Rejection Reason' column.
    """
    if all_columns is None:
        all_columns = mapped_columns

    has_outstanding = 'CurrentBalanceAmt' in all_columns
    
    if not has_outstanding:
        return df, pd.DataFrame()

    # Convert CurrentBalanceAmt to numeric; non-numeric → NaN
    balance = pd.to_numeric(df['CurrentBalanceAmt'], errors='coerce')
    
    # ── Rejection masks ──
    # Rule 1: balance is null/empty
    original_empty = df['CurrentBalanceAmt'].isna() | (df['CurrentBalanceAmt'].astype(str).str.strip() == '')
    not_numeric = balance.isna() & ~original_empty
    reject_empty = original_empty
    reject_nan = not_numeric
    
    # Build helper empty-masks for each field
    def _is_empty(series):
        s = series.astype(str).str.strip().str.lower()
        return series.isna() | (s == '') | (s == 'none') | (s == 'nan')
    
    overdue_empty = _is_empty(df['overdue_amount'])
    days_empty = _is_empty(df['months_in_arrears'])
    classification_empty = _is_empty(df['loan_classification'])
    status_empty = _is_empty(df['account_status_code'])
    
    days_numeric = pd.to_numeric(df['months_in_arrears'], errors='coerce')

    # ── Cross-field rule: "Closed"/"Cloed" in loan_classification → remap ──
    lc_lower = df['loan_classification'].astype(str).str.lower().str.strip()
    closed_mask = lc_lower.isin(['closed', 'cloed']) & ~classification_empty
    if closed_mask.any():
        df.loc[closed_mask, 'loan_classification'] = 'Performing'
        df.loc[closed_mask, 'account_status_code'] = 'Closed'
        classification_empty = classification_empty & ~closed_mask
        status_empty = status_empty & ~closed_mask
        logger.info(f"Auto-corrected {closed_mask.sum()} rows: loan_classification 'Closed'/'Cloed' -> 'Performing'")

    # ── Rule 2a: balance > 0 and months_in_arrears == 0 → auto-fill overdue = 0 ──
    bal_positive = balance > 0
    r2a_mask = bal_positive & (days_numeric == 0) & overdue_empty
    if r2a_mask.any():
        df.loc[r2a_mask, 'overdue_amount'] = 0
        overdue_empty = overdue_empty & ~r2a_mask
        logger.info(f"Auto-filled overdue_amount=0 for {r2a_mask.sum()} rows (balance>0, months_in_arrears=0)")

    # ── Rule 2b: balance > 0, months_in_arrears == 0, overdue == 0 → force Performing + Open ──
    # Re-read overdue after Rule 2a may have just auto-filled zeros
    overdue_numeric = pd.to_numeric(df['overdue_amount'], errors='coerce')
    performing_open_mask = bal_positive & (days_numeric == 0) & (overdue_numeric == 0)
    if performing_open_mask.any():
        df.loc[performing_open_mask, 'loan_classification'] = 'Performing'
        df.loc[performing_open_mask, 'account_status_code'] = 'Open'
        # Update empty masks so rejection check doesn't flag these rows
        classification_empty = classification_empty & ~performing_open_mask
        status_empty = status_empty & ~performing_open_mask
        logger.info(
            f"Auto-corrected {performing_open_mask.sum()} rows: "
            f"balance>0, months_in_arrears=0, overdue=0 → loan_classification=Performing, account_status_code=Open"
        )

    # ── Rule 2: balance > 0 but critical fields still empty → reject ──
    missing_overdue = bal_positive & overdue_empty
    missing_days = bal_positive & days_empty
    missing_class = bal_positive & classification_empty
    reject_bal_positive = missing_overdue | missing_days | missing_class

    # ── Rule 3: balance == 0 → auto-fill missing fields ──
    bal_zero = balance == 0
    
    fill_overdue = bal_zero & overdue_empty
    fill_days = bal_zero & days_empty
    fill_class = bal_zero & classification_empty
    fill_status = bal_zero & status_empty

    # Rule 3b: balance=0 and months=0 given → ensure overdue=0
    fill_overdue_3b = bal_zero & (days_numeric == 0) & overdue_empty
    fill_overdue = fill_overdue | fill_overdue_3b

    if fill_overdue.any():
        df.loc[fill_overdue, 'overdue_amount'] = 0
        logger.info(f"Auto-filled overdue_amount=0 for {fill_overdue.sum()} rows (balance=0)")
    if fill_days.any():
        df.loc[fill_days, 'months_in_arrears'] = 0
        logger.info(f"Auto-filled months_in_arrears=0 for {fill_days.sum()} rows (balance=0)")
    if fill_class.any():
        df.loc[fill_class, 'loan_classification'] = 'Performing'
        logger.info(f"Auto-filled loan_classification=Performing for {fill_class.sum()} rows (balance=0)")
    if fill_status.any():
        df.loc[fill_status, 'account_status_code'] = 'Closed'
        logger.info(f"Auto-filled account_status_code=Closed for {fill_status.sum()} rows (balance=0)")

    # ── Rule 4: balance is zero or missing but overdue_amount > 0 → reject ──
    # Re-read overdue after all auto-fills so Rule 3 zeros are accounted for
    overdue_final = pd.to_numeric(df['overdue_amount'], errors='coerce')
    reject_zero_bal_with_overdue = (bal_zero | original_empty | balance.isna()) & (overdue_final > 0)

    # ── Build rejection reasons ──
    reasons = pd.Series('', index=df.index)
    reasons = reasons.where(~reject_empty, 'Current Balance Amount is empty')
    reasons = reasons.where(~reject_nan, 'Current Balance Amount is not a valid number')
    reasons = reasons.where(~reject_zero_bal_with_overdue, 'AmountOverdue > 0 but CurrentBalanceAmt is zero or missing')
    
    # Build "missing: x, y, z" reasons for balance > 0 rejections
    reject_bal_only = reject_bal_positive & ~reject_empty & ~reject_nan
    if reject_bal_only.any():
        parts = []
        if missing_overdue.any():
            parts.append(('overdue_amount', missing_overdue))
        if missing_days.any():
            parts.append(('months_in_arrears', missing_days))
        if missing_class.any():
            parts.append(('loan_classification', missing_class))
        
        for field, mask in parts:
            combined = reject_bal_only & mask
            reasons = reasons.where(
                ~combined,
                reasons.where(
                    reasons == '',
                    reasons + ', ' + field
                ).where(reasons != '', 'Current Balance > 0 but missing: ' + field)
            )

    # ── Split valid / rejected ──
    all_reject = reject_empty | reject_nan | reject_bal_positive | reject_zero_bal_with_overdue
    
    if all_reject.any():
        rejected_df = df.loc[all_reject].copy()
        rejected_df['Rejection Reason'] = reasons[all_reject]
        df = df.loc[~all_reject]
        logger.info(f"Rejected {all_reject.sum()} rows total ({reject_empty.sum()} empty, {reject_nan.sum()} non-numeric, {reject_bal_positive.sum()} missing fields, {reject_zero_bal_with_overdue.sum()} zero-balance with overdue)")
    else:
        rejected_df = pd.DataFrame()
    
    return df, rejected_df


EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_DATA_ROWS_PER_SHEET = EXCEL_MAX_ROWS - 1  # header uses first row


def to_excel_safe_sheet_name(name):
    """Excel sheet names cannot contain []:*?/\\ and must be <=31 chars."""
    safe = re.sub(r'[\[\]\:\*\?/\\]', '_', str(name)).strip()
    if not safe:
        safe = 'cleaned'
    return safe[:31]


def validate_excel_workbook(file_path):
    """Check if an .xlsx file is a valid zip-based workbook."""
    if not os.path.exists(file_path):
        return False
    if not zipfile.is_zipfile(file_path):
        return False
    try:
        check_wb = load_workbook(file_path, read_only=True)
        check_wb.close()
        return True
    except Exception:
        return False


def save_workbook_atomically(wb, workbook_path):
    """Save workbook via temp file → validate → replace original. Prevents corruption."""
    temp_path = f"{workbook_path}.tmp.xlsx"
    wb.save(temp_path)
    if not validate_excel_workbook(temp_path):
        raise OSError(f"Temporary workbook is invalid: {temp_path}")
    os.replace(temp_path, workbook_path)


def format_excel_sheet(ws, display_headers=None):
    """
    Apply formatting to a worksheet:
    - Numeric columns: General number format
    - Text columns: @ text format with string-typed values
    - Headers: no bold, no border
    - AccountNo / Account Number: left-aligned
    """
    header_row = 1
    col_map = {}
    for col_idx, cell in enumerate(ws[header_row], start=1):
        col_map[cell.value] = col_idx

    # Match both internal and display header names
    numeric_names = ['CurrentBalanceAmt', 'Current Balance Amount',
                     'overdue_amount', 'Overdue Amount', 'AmountOverdue',
                     'months_in_arrears', 'Months In Arrears', 'MonthsInArrears',
                     'AmountOverdue', 'Monthsinarrears']
    text_names = ['account_number', 'Account Number', 'AccountNo',
                  'account_status_code', 'Account Status Code', 'AccountStatusCode',
                  'loan_classification', 'Loan Classification', 'LoanClassification']
    account_no_names = ['account_number', 'Account Number', 'AccountNo']

    # Strip header bold/border
    for cell in ws[header_row]:
        cell.font = Font(
            name=cell.font.name, sz=cell.font.sz, bold=False,
            italic=cell.font.italic, underline=None, color=cell.font.color
        )
        cell.border = Border()

    # General format for numeric columns
    for name in numeric_names:
        if name in col_map:
            j = col_map[name]
            for r in range(2, ws.max_row + 1):
                ws.cell(row=r, column=j).number_format = 'General'

    # Text format for text columns — set format AND re-write value as string
    for name in text_names:
        if name in col_map:
            j = col_map[name]
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(row=r, column=j)
                cell.number_format = '@'
                if cell.value is not None:
                    cell.value = str(cell.value)

    # Left-align account number
    for name in account_no_names:
        if name in col_map:
            j = col_map[name]
            for r in range(2, ws.max_row + 1):
                ws.cell(row=r, column=j).alignment = Alignment(horizontal='left', vertical='center')


def save_df_to_excel_robust(df, file_path, sheet_name='cleaned', chunk_size=None):
    """
    Save DataFrame to Excel with:
    - Multi-sheet splitting if data exceeds Excel row limit
    - Atomic save (write to temp, validate, replace)
    - Corruption recovery (backup corrupt files)
    - PermissionError fallback chain (3 attempts)
    - Per-sheet formatting
    
    Returns: (final_path, sheet_names_list)
    """
    if chunk_size is None:
        chunk_size = EXCEL_MAX_DATA_ROWS_PER_SHEET

    sheet_name = to_excel_safe_sheet_name(sheet_name)

    def _build_workbook(dataframe, base_sheet_name):
        """Build an openpyxl Workbook from DataFrame, splitting if needed."""
        wb = Workbook()
        # Remove default sheet
        if wb.active:
            wb.remove(wb.active)

        total_rows = len(dataframe)
        num_parts = max(1, int(np.ceil(total_rows / chunk_size)))
        saved_names = []

        for part_idx in range(num_parts):
            start = part_idx * chunk_size
            end = (part_idx + 1) * chunk_size
            chunk_df = dataframe.iloc[start:end]

            if num_parts == 1:
                part_name = base_sheet_name
            else:
                part_name = to_excel_safe_sheet_name(f"{base_sheet_name}_part{part_idx + 1}")

            ws = wb.create_sheet(title=part_name)
            saved_names.append(part_name)

            for row in dataframe_to_rows(chunk_df, index=False, header=True):
                ws.append(row)

            format_excel_sheet(ws)

        return wb, saved_names

    def _try_save(target_path):
        """Attempt to build and atomically save the workbook."""
        # If file exists but is corrupt, back it up
        if os.path.exists(target_path):
            if not validate_excel_workbook(target_path):
                corrupted_backup = f"{target_path}.corrupt_{datetime.now().strftime('%H%M%S')}"
                try:
                    os.replace(target_path, corrupted_backup)
                    logger.warning(f"Backed up corrupt workbook: {corrupted_backup}")
                except OSError:
                    pass

        wb, names = _build_workbook(df, sheet_name)
        save_workbook_atomically(wb, target_path)
        return target_path, names

    # Attempt 1: primary path
    try:
        return _try_save(file_path)
    except PermissionError:
        logger.warning(f"PermissionError on {file_path}, trying timestamped fallback")

    # Attempt 2: timestamped fallback (file might be open by user)
    base, ext = os.path.splitext(file_path)
    fallback_path = f"{base}_{datetime.now().strftime('%H%M%S')}{ext}"
    try:
        return _try_save(fallback_path)
    except PermissionError:
        logger.warning(f"PermissionError on {fallback_path}, trying last resort")

    # Attempt 3: last resort with microseconds
    last_resort_path = f"{base}_recover_{datetime.now().strftime('%H%M%S%f')}{ext}"
    return _try_save(last_resort_path)


def get_excel_sheet_names(file_path):
    """Return list of sheet names from an Excel file. Returns None for CSV."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.csv':
        return None
    try:
        xl = pd.ExcelFile(file_path)
        return xl.sheet_names
    except Exception:
        return None


def read_uploaded_file_sheet(file_path, sheet_name=None, header=0):
    """Read a specific sheet from an Excel file, or the full CSV."""
    ext = os.path.splitext(file_path)[1].lower()
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=UserWarning, module='openpyxl')
        if ext == '.csv':
            return pd.read_csv(file_path, dtype=str, header=header)
        elif ext == '.xlsx':
            return pd.read_excel(file_path, sheet_name=sheet_name, dtype=str, engine='openpyxl', header=header)
        elif ext == '.xls':
            return pd.read_excel(file_path, sheet_name=sheet_name, dtype=str, engine='xlrd', header=header)
        else:
            try:
                return pd.read_excel(file_path, sheet_name=sheet_name, dtype=str, engine='openpyxl', header=header)
            except Exception:
                return pd.read_excel(file_path, sheet_name=sheet_name, dtype=str, engine='xlrd', header=header)


def extract_sub_id(sheet_name):
    """Extract subscriber ID from sheet name pattern: subid_date_client."""
    parts = str(sheet_name).split('_')
    if parts:
        return parts[0]
    return ''


def build_sheet_name(subscriber, date=None, index=None):
    """
    Build a normalised sheet name from a Subscriber instance.
    Pattern: {subscriber_id}_{ddmmyyyy}_{subscriber_name_lower}
    Appends _{index} suffix for multi-sheet uploads (index >= 2).

    Args:
        subscriber: Subscriber model instance
        date: datetime.date or datetime.datetime; defaults to today
        index: int or None — if provided and >= 2, appended as suffix
    Returns:
        str sheet name safe for use as Excel sheet name and SQL table name
    """
    from datetime import date as _date
    d = date or _date.today()
    base = f"{subscriber.subscriber_id}_{d.strftime('%d%m%Y')}_{subscriber.subscriber_name.lower()}"
    if index is not None and index >= 2:
        base = f"{base}_{index}"
    return base


def generate_sql_script(sheet_name):
    """
    Generate a SQL script by replacing the template base name and subscriber ID
    with values derived from the sheet name.
    Returns the path to the generated script file.
    """
    template_path = settings.SQL_TEMPLATE_PATH
    base_name = settings.SQL_TEMPLATE_BASE_NAME
    base_sub_id = settings.SQL_TEMPLATE_BASE_SUBID

    with open(template_path, 'r', encoding='utf-8') as f:
        script = f.read()

    # Replace all instances of the base table name (handles V1, V2, V1_comm, etc.)
    script = script.replace(base_name, sheet_name)

    # Replace subscriber ID
    new_sub_id = extract_sub_id(sheet_name)
    if new_sub_id and new_sub_id != base_sub_id:
        script = script.replace(f'SubscriberID={base_sub_id}', f'SubscriberID={new_sub_id}')

    # Save generated script
    os.makedirs(settings.GENERATED_SCRIPTS_DIR, exist_ok=True)
    output_path = os.path.join(settings.GENERATED_SCRIPTS_DIR, f'{sheet_name}.sql')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(script)

    logger.info(f"Generated SQL script for sheet '{sheet_name}': {output_path}")
    return output_path


def get_batchupdate_connection():
    """Get a pyodbc connection to the BatchUpdate database."""
    conn_str = (
        f"DRIVER={{{settings.BATCHUPDATE_DRIVER}}};"
        f"SERVER={settings.BATCHUPDATE_SERVER};"
        f"DATABASE={settings.BATCHUPDATE_DB};"
    )
    if settings.BATCHUPDATE_TRUSTED_CONNECTION.lower() == 'yes':
        conn_str += "Trusted_Connection=yes;"
    return pyodbc.connect(conn_str)


def get_subscribers_from_batchupdate():
    """
    Return live subscriber list from BatchUpdate's Sheet1 table via the unmanaged BatchSubscriber model.
    Returns a list of dicts: [{'subscriber_id': int, 'subscriber_name': str}, ...]
    """
    from .models import BatchSubscriber
    return [
        {'subscriber_id': int(row['subscriber_id']), 'subscriber_name': row['subscriber_name']}
        for row in BatchSubscriber.objects.order_by('subscriber_name').values('subscriber_id', 'subscriber_name')
    ]


def upload_raw_to_batchupdate(df, table_name):
    """
    Upload a raw DataFrame to the BatchUpdate database as a new table.
    Table name = sheet name. Columns come from DataFrame headers.
    Drops existing table if present, then creates and bulk-inserts.
    """
    conn = get_batchupdate_connection()
    cursor = conn.cursor()
    cursor.fast_executemany = True

    safe_table = table_name.replace("'", "''")

    try:
        # Drop table if exists
        cursor.execute(f"IF OBJECT_ID('[{safe_table}]', 'U') IS NOT NULL DROP TABLE [{safe_table}]")
        conn.commit()

        # Build CREATE TABLE with all columns as NVARCHAR(MAX)
        col_defs = ', '.join(f'[{col}] NVARCHAR(MAX)' for col in df.columns)
        cursor.execute(f"CREATE TABLE [{safe_table}] ({col_defs})")
        conn.commit()

        # Bulk insert using executemany with fast_executemany enabled
        placeholders = ', '.join(['?'] * len(df.columns))
        insert_sql = f"INSERT INTO [{safe_table}] VALUES ({placeholders})"

        # Convert NaN to None for SQL NULL
        data = df.where(df.notna(), None).values.tolist()

        batch_size = 5000
        for i in range(0, len(data), batch_size):
            batch = data[i:i + batch_size]
            cursor.executemany(insert_sql, batch)
            conn.commit()

        return len(data)

    except Exception as e:
        conn.rollback()
        logger.error(f"BatchUpdate upload failed for [{safe_table}]: {e}", exc_info=True)
        raise
    finally:
        cursor.close()
        conn.close()
