from django.test import TestCase
import pandas as pd

from update.services import clean_dataframe, apply_business_rules


def _make_df(**kwargs):
    """Build a single-row DataFrame with all required target columns."""
    defaults = {
        'account_number': '1234567890',
        'CurrentBalanceAmt': '10000',
        'overdue_amount': '500',
        'months_in_arrears': '3',
        'loan_classification': 'Performing',
        'account_status_code': 'Open',
    }
    defaults.update(kwargs)
    return pd.DataFrame([defaults])


# ── Mappings used across tests ──────────────────────────────────────────────
ALL_MAPPINGS = {col: col for col in [
    'account_number', 'CurrentBalanceAmt', 'overdue_amount',
    'months_in_arrears', 'loan_classification', 'account_status_code',
]}


class CleanDataframeBasicTests(TestCase):
    def test_valid_row_passes_through(self):
        df = _make_df()
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)

    def test_numeric_columns_are_cleaned(self):
        df = _make_df(CurrentBalanceAmt='$10,000.50', overdue_amount='N/A 200', months_in_arrears='6 months')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(float(valid.iloc[0]['CurrentBalanceAmt']), 10000.50)
        self.assertEqual(float(valid.iloc[0]['overdue_amount']), 200.0)
        self.assertEqual(int(valid.iloc[0]['months_in_arrears']), 6)

    def test_duplicate_rows_are_deduplicated(self):
        row = _make_df()
        df = pd.concat([row, row], ignore_index=True)
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)

    def test_fully_empty_row_is_silently_dropped(self):
        """All-None mapped columns → dropped without appearing in rejected."""
        df = _make_df(
            account_number='', CurrentBalanceAmt='', overdue_amount='',
            months_in_arrears='', loan_classification='', account_status_code='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 0)

    def test_account_status_normalisation(self):
        for variant in ['001', 'open', '01', 'active']:
            df = _make_df(account_status_code=variant)
            valid, _ = clean_dataframe(df, ALL_MAPPINGS)
            self.assertEqual(valid.iloc[0]['account_status_code'], 'Open', msg=f"Failed for variant: {variant}")

    def test_loan_classification_normalisation(self):
        for variant in ['001', 'performing', 'perform', 'performimg']:
            df = _make_df(loan_classification=variant)
            valid, _ = clean_dataframe(df, ALL_MAPPINGS)
            self.assertEqual(valid.iloc[0]['loan_classification'], 'Performing', msg=f"Failed for variant: {variant}")


class BusinessRulesRejectionTests(TestCase):
    def test_empty_balance_is_rejected(self):
        df = _make_df(CurrentBalanceAmt='')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('empty', rejected.iloc[0]['Rejection Reason'].lower())

    def test_non_numeric_balance_is_rejected(self):
        df = _make_df(CurrentBalanceAmt='N/A')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('not a valid number', rejected.iloc[0]['Rejection Reason'].lower())

    def test_balance_positive_missing_overdue_is_rejected(self):
        df = _make_df(CurrentBalanceAmt='5000', overdue_amount='', months_in_arrears='2')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('overdue_amount', rejected.iloc[0]['Rejection Reason'])

    def test_zero_balance_with_positive_overdue_is_rejected(self):
        df = _make_df(CurrentBalanceAmt='0', overdue_amount='1000')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('AmountOverdue > 0', rejected.iloc[0]['Rejection Reason'])


class BusinessRulesAutoFillTests(TestCase):
    def test_balance_zero_autofills_missing_fields(self):
        df = _make_df(
            CurrentBalanceAmt='0',
            overdue_amount='',
            months_in_arrears='',
            loan_classification='',
            account_status_code='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['overdue_amount']), 0)
        self.assertEqual(int(row['months_in_arrears']), 0)
        self.assertEqual(row['loan_classification'], 'Performing')
        self.assertEqual(row['account_status_code'], 'Closed')

    def test_balance_positive_months_zero_overdue_autofilled_and_forced_performing(self):
        df = _make_df(
            CurrentBalanceAmt='50000',
            overdue_amount='',
            months_in_arrears='0',
            loan_classification='',
            account_status_code='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['overdue_amount']), 0)
        self.assertEqual(row['loan_classification'], 'Performing')
        self.assertEqual(row['account_status_code'], 'Open')

    def test_closed_in_loan_classification_remapped(self):
        """'Closed'/'Cloed' in loan_classification should become Performing + account_status_code=Closed."""
        for variant in ['closed', 'Closed', 'cloed']:
            df = _make_df(
                CurrentBalanceAmt='0',
                overdue_amount='0',
                months_in_arrears='0',
                loan_classification=variant,
                account_status_code='',
            )
            valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
            self.assertEqual(len(valid), 1, msg=f"Expected valid row for variant: {variant}")
            row = valid.iloc[0]
            self.assertEqual(row['loan_classification'], 'Performing', msg=f"Failed for variant: {variant}")
            self.assertEqual(row['account_status_code'], 'Closed', msg=f"Failed for variant: {variant}")
