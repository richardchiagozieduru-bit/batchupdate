"""
Centralised, read-only SQL query constants for Bureau database extraction.
All queries use WITH (NOLOCK) to avoid locking production tables.
Parameters are passed via ? placeholders for pyodbc parameterised execution.

Performance optimisation (Option B — SQL-side formatting):
All columns are CAST to VARCHAR/NVARCHAR with ISNULL handling, so Python
receives ready-to-write strings with no NULL values and no per-cell processing.
Text/ID columns are wrapped in ="value" format for Excel compatibility
(retains leading zeros, prevents scientific notation).

Databases:
- Subscriber Search: XDSNigeriaBureauAdmin..SubscriberDataLoad
- Consumer/Commercial Extraction: XDSBureauAdmin..ConsumerAccount, vconsumer, etc.
"""

# ── Subscriber Search ────────────────────────────────────────────────────────
SUBSCRIBER_SEARCH_QUERY = """
    SELECT subscribername, subscriberid
    FROM XDSNigeriaBureauAdmin..SubscriberDataLoad WITH (NOLOCK)
    WHERE subscribername LIKE ?
    ORDER BY subscribername
"""

# ══════════════════════════════════════════════════════════════════════════════
# ── Consumer Account Extraction ──────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# Fast count + ID range for parallel chunking (hits ConsumerAccount only — no vconsumer join)
CONSUMER_RANGE_QUERY = """
    SELECT MIN(consumerid), MAX(consumerid), COUNT(*)
    FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
    WHERE StatusInd = 'A' AND subscriberid = ?
"""

CONSUMER_HEADERS = [
    'Name', 'BankVerificationNumber', 'Address', 'HomeTelephoneNumber',
    'FirstCentralReferenceNo', 'customerid', 'AccountNo', 'TypeOfAccount',
    'DateAccountOpened', 'OpeningBalanceAmt', 'CurrentBalanceAmt',
    'CurrentBalanceDebitInd', 'AmountOverdue', 'InstalmentAmount',
    'Currency', 'MonthsInArrears', 'AccountStatusCode', 'ClosedDate',
    'loanclassification',
]

# ── DRY building blocks (shared between full and chunked queries) ────────────

_CONSUMER_CTE_COLUMNS = """
        consumerid, customerid, AccountNo, TypeOfAccount,
        DateAccountOpened, OpeningBalanceAmt, CurrentBalanceAmt,
        CurrentBalanceDebitInd, AmountOverdue, InstalmentAmount,
        Currency, MonthsInArrears, AccountStatusCode, ClosedDate,
        loanclassification"""

_CONSUMER_SELECT_BODY = """
    SELECT
        ISNULL(LTRIM(RTRIM(
            ISNULL(b.Surname,'') + ' ' + ISNULL(b.FirstName,'') + ' ' + ISNULL(b.OtherNames,'')
        )), ''),
        CASE WHEN b.BankVerificationNumber IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(b.BankVerificationNumber AS NVARCHAR(100)))) + '"' END,
        ISNULL(LTRIM(RTRIM(CAST(b.Address AS NVARCHAR(500)))), ''),
        CASE WHEN b.HomeTelephoneNumber IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(b.HomeTelephoneNumber AS NVARCHAR(100)))) + '"' END,
        CASE WHEN a.consumerid IS NULL THEN ''
             ELSE '="' + CAST(a.consumerid AS VARCHAR(50)) + '"' END,
        CASE WHEN a.customerid IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(a.customerid AS VARCHAR(50)))) + '"' END,
        CASE WHEN a.AccountNo IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(a.AccountNo AS NVARCHAR(100)))) + '"' END,
        ISNULL(LTRIM(RTRIM(CAST(a.TypeOfAccount AS VARCHAR(50)))), ''),
        ISNULL(CONVERT(VARCHAR(20), a.DateAccountOpened, 120), ''),
        ISNULL(CAST(a.OpeningBalanceAmt AS VARCHAR(50)), ''),
        ISNULL(CAST(a.CurrentBalanceAmt AS VARCHAR(50)), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.CurrentBalanceDebitInd AS VARCHAR(10)))), ''),
        ISNULL(CAST(a.AmountOverdue AS VARCHAR(50)), ''),
        ISNULL(CAST(a.InstalmentAmount AS VARCHAR(50)), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.Currency AS VARCHAR(10)))), ''),
        ISNULL(CAST(a.MonthsInArrears AS VARCHAR(10)), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.AccountStatusCode AS VARCHAR(10)))), ''),
        ISNULL(CONVERT(VARCHAR(20), a.ClosedDate, 120), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.loanclassification AS VARCHAR(50)))), '')
    FROM SubAccounts a
    INNER JOIN XDSBureauAdmin..vconsumer b WITH (NOLOCK)
        ON a.consumerid = b.consumerid"""

# Full query — streams entire subscriber (used for small extractions < 500K rows)
CONSUMER_QUERY = f"""
    WITH SubAccounts AS (
        SELECT {_CONSUMER_CTE_COLUMNS}
        FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
        WHERE StatusInd = 'A'
          AND subscriberid = ?
    )
    {_CONSUMER_SELECT_BODY}
"""

# Chunked query — filters by consumerid range for parallel streaming
# Parameters: (subscriberid, range_start, range_end)
CONSUMER_QUERY_CHUNKED = f"""
    WITH SubAccounts AS (
        SELECT {_CONSUMER_CTE_COLUMNS}
        FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
        WHERE StatusInd = 'A'
          AND subscriberid = ?
          AND consumerid >= ? AND consumerid < ?
    )
    {_CONSUMER_SELECT_BODY}
"""


# ══════════════════════════════════════════════════════════════════════════════
# ── Commercial Account Extraction ───────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# Fast count + ID range for parallel chunking
COMMERCIAL_RANGE_QUERY = """
    SELECT MIN(CommercialID), MAX(CommercialID), COUNT(*)
    FROM XDSBureauAdmin..CommercialAccount WITH (NOLOCK)
    WHERE StatusInd = 'A' AND subscriberid = ?
"""

COMMERCIAL_HEADERS = [
    'BusinessName', 'BusinessRegistrationNumber', 'FirstCentralReferenceNo',
    'customerid', 'AccountNo', 'TypeOfAccount', 'DateAccountOpened',
    'OpeningBalanceAmt', 'CurrentBalanceAmt', 'CurrentBalanceDebitInd',
    'AmountOverdue', 'InstalmentAmount', 'Currency', 'MonthsInArrears',
    'AccountStatusCode', 'ClosedDate', 'loanclassification',
]

# ── DRY building blocks ─────────────────────────────────────────────────────

_COMMERCIAL_CTE_COLUMNS = """
        CommercialID, customerid, AccountNo, TypeOfAccount,
        DateAccountOpened, OpeningBalanceAmt, CurrentBalanceAmt,
        CurrentBalanceDebitInd, AmountOverdue, InstalmentAmount,
        Currency, MonthsInArrears, AccountStatusCode, ClosedDate,
        loanclassification"""

_COMMERCIAL_SELECT_BODY = """
    SELECT
        ISNULL(LTRIM(RTRIM(CAST(b.BusinessName AS NVARCHAR(500)))), ''),
        CASE WHEN b.BusinessRegistrationNumber IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(b.BusinessRegistrationNumber AS NVARCHAR(100)))) + '"' END,
        CASE WHEN a.CommercialID IS NULL THEN ''
             ELSE '="' + CAST(a.CommercialID AS VARCHAR(50)) + '"' END,
        CASE WHEN a.customerid IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(a.customerid AS VARCHAR(50)))) + '"' END,
        CASE WHEN a.AccountNo IS NULL THEN ''
             ELSE '="' + LTRIM(RTRIM(CAST(a.AccountNo AS NVARCHAR(100)))) + '"' END,
        ISNULL(LTRIM(RTRIM(CAST(a.TypeOfAccount AS VARCHAR(50)))), ''),
        ISNULL(CONVERT(VARCHAR(20), a.DateAccountOpened, 120), ''),
        ISNULL(CAST(a.OpeningBalanceAmt AS VARCHAR(50)), ''),
        ISNULL(CAST(a.CurrentBalanceAmt AS VARCHAR(50)), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.CurrentBalanceDebitInd AS VARCHAR(10)))), ''),
        ISNULL(CAST(a.AmountOverdue AS VARCHAR(50)), ''),
        ISNULL(CAST(a.InstalmentAmount AS VARCHAR(50)), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.Currency AS VARCHAR(10)))), ''),
        ISNULL(CAST(a.MonthsInArrears AS VARCHAR(10)), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.AccountStatusCode AS VARCHAR(10)))), ''),
        ISNULL(CONVERT(VARCHAR(20), a.ClosedDate, 120), ''),
        ISNULL(LTRIM(RTRIM(CAST(a.loanclassification AS VARCHAR(50)))), '')
    FROM SubAccounts a
    INNER JOIN XDSBureauAdmin..Commercial b WITH (NOLOCK)
        ON a.CommercialID = b.CommercialID"""

# Full query — streams entire subscriber
COMMERCIAL_QUERY = f"""
    WITH SubAccounts AS (
        SELECT {_COMMERCIAL_CTE_COLUMNS}
        FROM XDSBureauAdmin..CommercialAccount WITH (NOLOCK)
        WHERE StatusInd = 'A'
          AND subscriberid = ?
    )
    {_COMMERCIAL_SELECT_BODY}
"""

# Chunked query — filters by CommercialID range for parallel streaming
# Parameters: (subscriberid, range_start, range_end)
COMMERCIAL_QUERY_CHUNKED = f"""
    WITH SubAccounts AS (
        SELECT {_COMMERCIAL_CTE_COLUMNS}
        FROM XDSBureauAdmin..CommercialAccount WITH (NOLOCK)
        WHERE StatusInd = 'A'
          AND subscriberid = ?
          AND CommercialID >= ? AND CommercialID < ?
    )
    {_COMMERCIAL_SELECT_BODY}
"""
