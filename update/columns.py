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
    'CurrentBalanceAmt':  'FLOAT',
    'AmountOverdue':      'FLOAT',
    'MonthsInArrears':    'INT',
    'LoanClassification': 'NVARCHAR(50)',
    'AccountStatusCode':  'NVARCHAR(50)',
    # Internal names (fallback in case display rename was skipped)
    'account_number':        'NVARCHAR(100)',
    'overdue_amount':        'FLOAT',
    'months_in_arrears':     'INT',
    'loan_classification':   'NVARCHAR(50)',
    'account_status_code':   'NVARCHAR(50)',
}

# Synonyms for auto-mapping incoming column headers to target columns
HEADER_MAPPING_DICTIONARY = {
    'account_number': [
        'account_number', 'account number', 'accountno', 'account_no', 'acct_no', 'acct no', 'acct_num', 'acctno', 
        'account id', 'account_id', 'acctid', 'customer account', 'customer account number', 
        'customer_acct_no', 'acc_no', 'acc no', 'acc_num', 'account', 'acct', 'agreement_no', 
        'agreement number', 'agreementno', 'contract_no', 'contract number', 'contractno','nuban'
    ],
    'CurrentBalanceAmt': [
        'currentbalanceamt', 'current balance amt', 'current balance', 'current_balance', 
        'outstanding balance', 'outstanding_balance', 'outstanding amt', 'outstanding amount', 
        'balance', 'balance amount', 'currentbal', 'current_bal', 'outstandingbal', 'outstanding_bal', 
        'amount outstanding', 'current balance amount', 'outstanding', 'total balance','outstanding balance','outstanding_balance',
        'total_balance', 'bal', 'current_balance_amount', 'currentbalance', 'current_balance_amt', 
        'currentbalance_amt', 'current balance_amt','currentbalanceamount','balamt','bal','bal amt','balanceamount','balance_amt','bal_amt'
    ],
    'overdue_amount': [
        'overdue_amount', 'overdue amount', 'amountoverdue', 'amount_overdue', 'overdue_amt', 
        'overdue amt', 'overdue', 'arrears amount', 'arrears_amount', 'arrears amt', 'arrears_amt', 
        'amount in arrears', 'amount_in_arrears', 'past due amount', 'past_due_amount', 
        'past due amt', 'past_due_amt',''
    ],
    'months_in_arrears': [
        'months_in_arrears', 'months in arrears', 'monthsinarrears', 'months_in_arrear', 
        'months in arrear', 'arrears months', 'arrears_months', 'days in arrears', 'days_in_arrears', 
        'daysinarrears', 'no of days in arrears', 'number of days in arrears', 'arrear days', 
        'dpd', 'days past due', 'days_past_due', 'arrears_days', 'arrears days', 'months_arrears', 
        'months arrears','ndod','overdue_days','overdue days','nodaysoverdue','no_days_overdue',
        'days_overdue','daysoverdue','arrears',
    ],
    'loan_classification': [
        'loan_classification', 'loan classification', 'loanclass', 'loan_class', 'classification', 
        'class', 'status classification', 'credit classification', 'performance classification', 
        'performance status', 'performance_status', 'asset classification', 'asset_classification', 
        'classification status','account classification', 'account_classification','loanclassification'
    ],
    'account_status_code': [
        'account_status_code', 'account status code', 'accountstatuscode', 'account_status', 
        'account status', 'status code', 'status_code', 'status', 'acct status', 'acct_status', 
        'account_status_desc', 'account status desc','accountstatus','accoutstatuscode','acctstatus','acctstatuscode'
    ]
}

