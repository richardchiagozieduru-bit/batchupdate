"""
Single source of truth for all target column metadata.

Any module that needs to know the set of columns, their types, validation rules,
or display names should import from here rather than maintaining its own copy.
"""

# ── Column type sets ──────────────────────────────────────────────────────────
# Used by clean_value() to determine cleaning/validation strategy

NUMERIC_COLUMNS = ['CurrentBalanceAmt', 'overdue_amount', 'months_in_arrears']
TEXT_COLUMNS    = ['loan_classification', 'account_status_code']
STRING_COLUMNS  = ['account_number']

# Ordered list of all target internal names
ALL_TARGET_COLUMNS = STRING_COLUMNS + ['CurrentBalanceAmt'] + [
    'overdue_amount', 'months_in_arrears',
] + TEXT_COLUMNS

# ── Validation rules ──────────────────────────────────────────────────────────
# max only — negative values are stripped during cleaning
VALIDATION_RULES = {
    'CurrentBalanceAmt': {'max': 999_999_999_999},
    'overdue_amount':    {'max': 999_999_999_999},
    'months_in_arrears': {'max': 9_999},
}

# ── Display headers (internal name → Excel/SQL column name) ──────────────────
DISPLAY_HEADERS = {
    'account_number':       'AccountNo',
    'CurrentBalanceAmt':    'CurrentBalanceAmt',
    'overdue_amount':       'AmountOverdue',
    'months_in_arrears':    'MonthsInArrears',
    'loan_classification':  'LoanClassification',
    'account_status_code':  'AccountStatusCode',
}

# ── Django model choices (internal name, display name) ───────────────────────
# Imported directly by ColumnMapping.target_column choices.
TARGET_COLUMN_CHOICES = [(k, v) for k, v in DISPLAY_HEADERS.items()]

# ── SQL Server column types for the BatchUpdate destination table ─────────────
# Imported by upload_raw_to_batchupdate() to avoid NVARCHAR(MAX) for everything.
COLUMN_SQL_TYPES = {
    'AccountNo':          'NVARCHAR(100)',
    'CurrentBalanceAmt':  'DECIMAL(18, 2)',
    'AmountOverdue':      'DECIMAL(18, 2)',
    'MonthsInArrears':    'INT',
    'LoanClassification': 'NVARCHAR(50)',
    'AccountStatusCode':  'NVARCHAR(50)',
    # Internal names (fallback in case display rename was skipped)
    'account_number':        'NVARCHAR(100)',
    'overdue_amount':        'DECIMAL(18, 2)',
    'months_in_arrears':     'INT',
    'loan_classification':   'NVARCHAR(50)',
    'account_status_code':   'NVARCHAR(50)',
}
