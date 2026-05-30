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

    def test_balance_positive_overdue_zero_months_missing_autofills_months(self):
        """balance > 0, overdue = 0, months_in_arrears missing → auto-fill months = 0, row is valid."""
        df = _make_df(
            CurrentBalanceAmt='20000',
            overdue_amount='0',
            months_in_arrears='',
            loan_classification='Performing',
            account_status_code='Open',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(int(valid.iloc[0]['months_in_arrears']), 0)

    def test_balance_positive_overdue_positive_months_missing_is_rejected(self):
        """balance > 0, overdue > 0, months_in_arrears missing → rejected."""
        df = _make_df(
            CurrentBalanceAmt='20000',
            overdue_amount='5000',
            months_in_arrears='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('months_in_arrears', rejected.iloc[0]['Rejection Reason'])

    def test_missing_account_number_is_rejected(self):
        """Empty account_number → rejected with 'AccountNo is missing'."""
        df = _make_df(account_number='')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('AccountNo is missing', rejected.iloc[0]['Rejection Reason'])

    def test_balance_zero_overdue_zero_months_positive_corrected_to_zero(self):
        """balance=0, overdue=0, months>0 → auto-corrected to months=0, row is valid."""
        df = _make_df(
            CurrentBalanceAmt='0',
            overdue_amount='0',
            months_in_arrears='3',
            loan_classification='Performing',
            account_status_code='Closed',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(int(valid.iloc[0]['months_in_arrears']), 0)

    def test_balance_equals_overdue_months_missing_is_rejected(self):
        """balance == overdue (both same non-zero) and months_in_arrears missing → rejected."""
        df = _make_df(
            CurrentBalanceAmt='10000',
            overdue_amount='10000',
            months_in_arrears='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('MonthsInArrears is missing', rejected.iloc[0]['Rejection Reason'])

    def test_overdue_exceeds_balance_is_rejected(self):
        """overdue > balance → rejected."""
        df = _make_df(
            CurrentBalanceAmt='5000',
            overdue_amount='8000',
            months_in_arrears='3',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('exceeds', rejected.iloc[0]['Rejection Reason'])


class HybridAndParquetPipelineTests(TestCase):
    def test_should_use_chunking_routing(self):
        from update.services import should_use_chunking
        import tempfile
        import os

        # Verify Excel routing
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(b'\0' * (16 * 1024 * 1024)) # 16 MB
            xlsx_path = f.name
        
        try:
            self.assertTrue(should_use_chunking(xlsx_path))
        finally:
            os.remove(xlsx_path)

        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            f.write(b'\0' * (5 * 1024 * 1024)) # 5 MB
            xlsx_small_path = f.name
        
        try:
            self.assertFalse(should_use_chunking(xlsx_small_path))
        finally:
            os.remove(xlsx_small_path)

        # Verify CSV routing
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            f.write(b'\0' * (31 * 1024 * 1024)) # 31 MB
            csv_path = f.name
        
        try:
            self.assertTrue(should_use_chunking(csv_path))
        finally:
            os.remove(csv_path)

    def test_parquet_generation_and_loading(self):
        from update.tasks import _load_and_clean
        import tempfile
        import os
        import pyarrow.parquet as pq

        df = _make_df(account_number='111222')
        with tempfile.NamedTemporaryFile(suffix='.csv', mode='w+', delete=False, newline='') as f:
            df.to_csv(f, index=False)
            csv_path = f.name

        cleaned_pq = tempfile.mktemp(suffix='.parquet')
        rejected_pq = tempfile.mktemp(suffix='.parquet')

        try:
            c_count, r_count = _load_and_clean(
                csv_path, ALL_MAPPINGS, 0, 9999, cleaned_pq, rejected_pq
            )
            self.assertEqual(c_count, 1)
            self.assertEqual(r_count, 0)
            self.assertTrue(os.path.exists(cleaned_pq))
            self.assertTrue(os.path.exists(rejected_pq))

            # Verify contents
            table = pq.read_table(cleaned_pq)
            self.assertEqual(table.num_rows, 1)
            self.assertEqual(table.column('AccountNo').to_pylist(), ['111222'])
        finally:
            if os.path.exists(csv_path):
                os.remove(csv_path)
            if os.path.exists(cleaned_pq):
                os.remove(cleaned_pq)
            if os.path.exists(rejected_pq):
                os.remove(rejected_pq)

    def test_excel_streaming_from_parquet(self):
        from update.views import _stream_parquet_as_excel
        import tempfile
        import os
        import pyarrow as pa
        import pyarrow.parquet as pq
        import io

        # Create a small Parquet file
        schema = pa.schema([
            ('AccountNo', pa.string()),
            ('CurrentBalanceAmt', pa.float64()),
            ('AmountOverdue', pa.float64()),
            ('MonthsInArrears', pa.float64()),
            ('LoanClassification', pa.string()),
            ('AccountStatusCode', pa.string()),
        ])
        
        table = pa.Table.from_pydict({
            'AccountNo': ['999888'],
            'CurrentBalanceAmt': [12345.67],
            'AmountOverdue': [0.0],
            'MonthsInArrears': [0.0],
            'LoanClassification': ['Performing'],
            'AccountStatusCode': ['Open'],
        }, schema=schema)

        pq_file = tempfile.NamedTemporaryFile(suffix='.parquet', delete=False)
        pq_path = pq_file.name
        pq_file.close()

        try:
            pq.write_table(table, pq_path)

            # Stream as Excel into a BytesIO buffer
            response_stream = io.BytesIO()
            _stream_parquet_as_excel(pq_path, "test_sheet", response_stream)

            # xlsxwriter writes and closes the stream; seek back to verify
            response_stream.seek(0)
            restored_df = pd.read_excel(response_stream, engine='openpyxl')
            self.assertEqual(len(restored_df), 1)
            self.assertEqual(str(restored_df.iloc[0]['AccountNo']), '999888')
            self.assertEqual(float(restored_df.iloc[0]['CurrentBalanceAmt']), 12345.67)
        finally:
            os.remove(pq_path)


