"""
SQL Queries and constants for the Unupdated Records (Stale Accounts) app.
All database objects are explicitly fully-qualified to avoid ambient default database bugs.
"""

EXCLUDED_ACCOUNT_STATUSES = (
    'Paidup',
    'Closed',
    'Writtenoff',
    'Paid up',
    'Paid off',
    'Written off',
)

CONSUMER_HEADERS = [
    'Name',
    'BankVerificationNumber',
    'Address',
    'HomeTelephoneNumber',
    'FirstCentralReferenceNo',
    'CustomerID',
    'AccountNo',
    'TypeOfAccount',
    'DateAccountOpened',
    'OpeningBalanceAmt',
    'CurrentBalanceAmt',
    'CurrentBalanceDebitInd',
    'AmountOverdue',
    'InstalmentAmount',
    'Currency',
    'MonthsInArrears',
    'AccountStatusCode',
    'ClosedDate',
    'LoanClassification',
]

COMMERCIAL_HEADERS = [
    'BusinessName',
    'BusinessRegistrationNumber',
    'FirstCentralReferenceNo',
    'CustomerID',
    'AccountNo',
    'TypeOfAccount',
    'DateAccountOpened',
    'OpeningBalanceAmt',
    'CurrentBalanceAmt',
    'CurrentBalanceDebitInd',
    'AmountOverdue',
    'InstalmentAmount',
    'Currency',
    'MonthsInArrears',
    'AccountStatusCode',
    'ClosedDate',
    'LoanClassification',
]

SUBSCRIBER_SEARCH_QUERY = """
SELECT DISTINCT 
    subscribername, 
    subscriberid 
FROM XDSNigeriaBureauAdmin..SubscriberDataLoad WITH (NOLOCK)
WHERE subscribername LIKE ? OR CAST(subscriberid AS VARCHAR(20)) = ?
ORDER BY subscribername
"""

ALL_SUBSCRIBERS_QUERY = """
SELECT DISTINCT 
    subscriberid, 
    subscribername 
FROM XDSNigeriaBureauAdmin..SubscriberDataLoad WITH (NOLOCK)
WHERE subscriberid IS NOT NULL AND subscribername IS NOT NULL
ORDER BY subscribername
"""

CONSUMER_MAX_PERIOD_QUERY = """
SELECT MAX(lastperiodnum)
FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
WHERE StatusInd = 'A' AND subscriberid = ?
"""

COMMERCIAL_MAX_PERIOD_QUERY = """
SELECT MAX(lastperiodnum)
FROM XDSBureauAdmin..CommercialAccount WITH (NOLOCK)
WHERE StatusInd = 'A' AND subscriberid = ?
"""

def get_loaded_subscribers_query(include_consumer=True, include_commercial=True):
    """
    Returns query to find subscribers whose file has been loaded for a specific target period.
    """
    sub_parts = []
    if include_consumer:
        sub_parts.append("""
            SELECT DISTINCT subscriberid
            FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
            WHERE StatusInd = 'A' AND LastPeriodNum = ?
        """)
    if include_commercial:
        sub_parts.append("""
            SELECT DISTINCT subscriberid
            FROM XDSBureauAdmin..CommercialAccount WITH (NOLOCK)
            WHERE StatusInd = 'A' AND LastPeriodNum = ?
        """)

    if not sub_parts:
        sub_parts.append("""
            SELECT DISTINCT subscriberid
            FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
            WHERE StatusInd = 'A' AND LastPeriodNum = ?
        """)

    union_sql = " UNION ".join(sub_parts)
    return f"""
    WITH LoadedSubs AS (
        {union_sql}
    )
    SELECT DISTINCT 
        s.subscriberid, 
        ISNULL(s.subscribername, 'Unknown Subscriber') AS subscribername
    FROM LoadedSubs l
    INNER JOIN XDSNigeriaBureauAdmin..SubscriberDataLoad s WITH (NOLOCK)
        ON l.subscriberid = s.subscriberid
    ORDER BY subscribername
    """

def get_consumer_query():
    placeholders = ', '.join(['?'] * len(EXCLUDED_ACCOUNT_STATUSES))
    return f"""
    WITH StaleAccounts AS (
        SELECT 
            consumerid,
            customerid,
            AccountNo,
            TypeOfAccount,
            DateAccountOpened,
            OpeningBalanceAmt,
            CurrentBalanceAmt,
            CurrentBalanceDebitInd,
            AmountOverdue,
            InstalmentAmount,
            Currency,
            MonthsInArrears,
            AccountStatusCode,
            ClosedDate,
            loanclassification
        FROM XDSBureauAdmin..ConsumerAccount WITH (NOLOCK)
        WHERE StatusInd = 'A'
          AND subscriberid = ?
          AND (
              ISNULL(CurrentBalanceAmt, 0) > 0 OR 
              ISNULL(AmountOverdue, 0) > 0 OR 
              ISNULL(MonthsInArrears, 0) > 0
          )
          AND ISNULL(AccountStatusCode, 'A') NOT IN ({placeholders})
          AND (ISNULL(changedOnDate, '2020-01-01') < ? OR LTRIM(RTRIM(changedOnDate)) = '')
          AND LastPeriodNum != ?
    )
    SELECT 
        LTRIM(RTRIM(ISNULL(b.Surname, '') + ' ' + ISNULL(b.FirstName, '') + ' ' + ISNULL(b.OtherNames, ''))) AS [Name],
        b.BankVerificationNumber,
        b.Address,
        b.HomeTelephoneNumber,
        a.consumerid AS FirstCentralReferenceNo,
        a.customerid,
        a.AccountNo,
        a.TypeOfAccount,
        a.DateAccountOpened,
        a.OpeningBalanceAmt,
        a.CurrentBalanceAmt,
        a.CurrentBalanceDebitInd,
        a.AmountOverdue,
        a.InstalmentAmount,
        a.Currency,
        a.MonthsInArrears,
        a.AccountStatusCode,
        a.ClosedDate,
        a.loanclassification
    FROM StaleAccounts a
    INNER JOIN XDSBureauAdmin..vconsumer b WITH (NOLOCK) 
        ON a.consumerid = b.consumerid
    ORDER BY a.consumerid
    """

def get_commercial_query():
    placeholders = ', '.join(['?'] * len(EXCLUDED_ACCOUNT_STATUSES))
    return f"""
    WITH StaleCommercialAccounts AS (
        SELECT 
            CommercialID,
            customerid,
            AccountNo,
            TypeOfAccount,
            DateAccountOpened,
            OpeningBalanceAmt,
            CurrentBalanceAmt,
            CurrentBalanceDebitInd,
            AmountOverdue,
            InstalmentAmount,
            Currency,
            MonthsInArrears,
            AccountStatusCode,
            ClosedDate,
            loanclassification
        FROM XDSBureauAdmin..CommercialAccount WITH (NOLOCK)
        WHERE StatusInd = 'A'
          AND subscriberid = ?
          AND (
              ISNULL(CurrentBalanceAmt, 0) > 0 OR 
              ISNULL(AmountOverdue, 0) > 0 OR 
              ISNULL(MonthsInArrears, 0) > 0
          )
          AND ISNULL(AccountStatusCode, 'A') NOT IN ({placeholders})
          AND (ISNULL(Updatedondate, '2020-01-01') < ? OR LTRIM(RTRIM(Updatedondate)) = '')
          AND LastPeriodNum != ?
    )
    SELECT 
        b.BusinessName,
        b.BusinessRegistrationNumber,
        a.CommercialID AS FirstCentralReferenceNo,
        a.customerid,
        a.AccountNo,
        a.TypeOfAccount,
        a.DateAccountOpened,
        a.OpeningBalanceAmt,
        a.CurrentBalanceAmt,
        a.CurrentBalanceDebitInd,
        a.AmountOverdue,
        a.InstalmentAmount,
        a.Currency,
        a.MonthsInArrears,
        a.AccountStatusCode,
        a.ClosedDate,
        a.loanclassification
    FROM StaleCommercialAccounts a
    INNER JOIN XDSBureauAdmin..Commercial b WITH (NOLOCK) 
        ON a.CommercialID = b.CommercialID
    ORDER BY a.CommercialID
    """
