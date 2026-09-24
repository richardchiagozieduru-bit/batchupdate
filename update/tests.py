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

        for variant in ['past and watch', 'pastand watch', 'pastwatch', 'pass and watch', 'watchlist']:
            df = _make_df(loan_classification=variant)
            valid, _ = clean_dataframe(df, ALL_MAPPINGS)
            self.assertEqual(valid.iloc[0]['loan_classification'], 'Watchlist', msg=f"Failed for variant: {variant}")


class BusinessRulesRejectionTests(TestCase):
    def test_empty_balance_is_rejected(self):
        df = _make_df(CurrentBalanceAmt='', overdue_amount='')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('empty', rejected.iloc[0]['Rejection Reason'].lower())

    def test_non_numeric_balance_is_rejected(self):
        df = _make_df(CurrentBalanceAmt='N/A', overdue_amount='')
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

    def test_zero_balance_with_positive_overdue_is_valid(self):
        df = _make_df(CurrentBalanceAmt='0', overdue_amount='1000')
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(float(valid.iloc[0]['CurrentBalanceAmt']), 0)
        self.assertEqual(float(valid.iloc[0]['overdue_amount']), 1000)

    def test_zero_balance_with_positive_overdue_and_lost_exception_is_valid(self):
        df = _make_df(
            CurrentBalanceAmt='0',
            overdue_amount='1000',
            loan_classification='Lost',
            months_in_arrears='3',
            account_status_code='Open'
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['CurrentBalanceAmt']), 0)
        self.assertEqual(float(row['overdue_amount']), 1000)
        self.assertEqual(row['loan_classification'], 'Lost')

    def test_positive_balance_with_positive_overdue_and_lost_exception_is_valid(self):
        df = _make_df(
            CurrentBalanceAmt='5000',
            overdue_amount='1000',
            loan_classification='Lost',
            months_in_arrears='3',
            account_status_code='Open'
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['CurrentBalanceAmt']), 5000)
        self.assertEqual(float(row['overdue_amount']), 1000)
        self.assertEqual(row['loan_classification'], 'Lost')

    def test_zero_balance_with_positive_overdue_and_months_zero_lost_exception_is_valid(self):
        df = _make_df(
            CurrentBalanceAmt='0',
            overdue_amount='1000',
            loan_classification='Lost',
            months_in_arrears='0',
            account_status_code='Open'
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['CurrentBalanceAmt']), 0)
        self.assertEqual(float(row['overdue_amount']), 1000)
        self.assertEqual(row['loan_classification'], 'Lost')


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

    def test_balance_zero_overrides_existing_values(self):
        """balance = 0 and overdue = 0/empty should override any months, classification, or status to 0, Performing, Closed."""
        df = _make_df(
            CurrentBalanceAmt='0',
            overdue_amount='0',
            months_in_arrears='12',
            loan_classification='Substandard',
            account_status_code='Open',
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

    def test_balance_positive_overdue_zero_missing_months_in_arrears_is_rejected(self):
        df = _make_df(
            CurrentBalanceAmt='50000',
            overdue_amount='0',
            months_in_arrears='',
            loan_classification='Performing',
            account_status_code='Open',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('months_in_arrears', rejected.iloc[0]['Rejection Reason'])

    def test_balance_positive_overdue_zero_positive_months_in_arrears_preserved(self):
        df = _make_df(
            CurrentBalanceAmt='50000',
            overdue_amount='0',
            months_in_arrears='3',
            loan_classification='Sub standard',
            account_status_code='Open',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(int(row['months_in_arrears']), 3)
        self.assertEqual(row['loan_classification'], 'Sub standard')

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

    def test_invalid_loan_classification_is_rejected(self):
        """Values in loan_classification not recognized by dictionary (e.g. 'cleared off') are rejected."""
        df = _make_df(
            CurrentBalanceAmt='50000',
            overdue_amount='10000',
            months_in_arrears='3',
            loan_classification='cleared off',
            account_status_code='Open',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("Invalid LoanClassification: 'cleared off'", rejected.iloc[0]['Rejection Reason'])

    def test_invalid_account_status_code_is_rejected(self):
        """Values in account_status_code not recognized by dictionary (e.g. 'pending') are rejected."""
        df = _make_df(
            CurrentBalanceAmt='50000',
            overdue_amount='10000',
            months_in_arrears='3',
            loan_classification='Watchlist',
            account_status_code='pending',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("Invalid AccountStatusCode: 'pending'", rejected.iloc[0]['Rejection Reason'])

    def test_empty_account_status_code_after_rules_is_rejected(self):
        """Active debt where status code remains empty after all rules are applied must be rejected."""
        df = _make_df(
            CurrentBalanceAmt='50000',
            overdue_amount='10000',
            months_in_arrears='3',
            loan_classification='Lost',
            account_status_code='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected.iloc[0]['Rejection Reason'], 'AccountStatusCode is empty')

    def test_performing_closed_with_empty_balance_and_arrears_overdue_zero_or_empty_is_valid(self):
        """Performing + Closed and empty balance, arrears, and overdue are all filled with 0, row valid."""
        df = _make_df(
            CurrentBalanceAmt='',
            overdue_amount='',
            months_in_arrears='',
            loan_classification='Performing',
            account_status_code='Closed',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['CurrentBalanceAmt']), 0)
        self.assertEqual(float(row['overdue_amount']), 0)
        self.assertEqual(int(row['months_in_arrears']), 0)

    def test_performing_closed_with_zero_balance_and_some_empty_is_valid(self):
        """Performing + Closed and mix of zero and empty fields gets filled with 0, row valid."""
        df = _make_df(
            CurrentBalanceAmt='0',
            overdue_amount='',
            months_in_arrears='0',
            loan_classification='Performing',
            account_status_code='Closed',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['CurrentBalanceAmt']), 0)
        self.assertEqual(float(row['overdue_amount']), 0)
        self.assertEqual(int(row['months_in_arrears']), 0)

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

    def test_balance_positive_overdue_zero_forces_months_zero_performing_open(self):
        """balance > 0, overdue = 0 → forces months_in_arrears=0, loan_classification='Performing', account_status_code='Open'."""
        df = _make_df(
            CurrentBalanceAmt='15000',
            overdue_amount='0',
            months_in_arrears='5',
            loan_classification='Doubtful',
            account_status_code='Doubtful',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(int(row['months_in_arrears']), 0)
        self.assertEqual(row['loan_classification'], 'Performing')
        self.assertEqual(row['account_status_code'], 'Open')

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

    def test_overdue_exceeds_balance_is_valid(self):
        """overdue > balance → allowed (left as-is)."""
        df = _make_df(
            CurrentBalanceAmt='5000',
            overdue_amount='8000',
            months_in_arrears='3',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(float(valid.iloc[0]['CurrentBalanceAmt']), 5000)
        self.assertEqual(float(valid.iloc[0]['overdue_amount']), 8000)

    def test_written_off_and_open_corrected_to_lost_and_written_off(self):
        """Rule 2d sub-case: balance > 0, overdue > 0, balance == overdue, months > 0, 
        and classification = 'Writtenoff', status = 'Open' -> classification='Lost', status='Writtenoff'."""
        df = _make_df(
            CurrentBalanceAmt='10000',
            overdue_amount='10000',
            months_in_arrears='3',
            loan_classification='written off',
            account_status_code='open',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(row['loan_classification'], 'Lost')
        self.assertEqual(row['account_status_code'], 'Writtenoff')


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
        from update.services import write_parquet_as_excel
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
            write_parquet_as_excel(pq_path, "test_sheet", response_stream)

            # xlsxwriter writes and closes the stream; seek back to verify
            response_stream.seek(0)
            restored_df = pd.read_excel(response_stream, engine='openpyxl')
            self.assertEqual(len(restored_df), 1)
            self.assertEqual(str(restored_df.iloc[0]['AccountNo']), '999888')
            self.assertEqual(float(restored_df.iloc[0]['CurrentBalanceAmt']), 12345.67)
        finally:
            os.remove(pq_path)


class RecentOptimizationsTests(TestCase):
    def test_excel_to_csv_streaming_with_styled_accounts(self):
        from update.services import excel_to_csv_streaming
        from openpyxl import Workbook
        import tempfile
        import csv
        import os

        # Create a temp workbook with an account column and zero mask format
        wb = Workbook()
        ws = wb.active
        ws.title = "test_sheet"
        # Row 1: Header
        ws.append(["account_number", "other_col"])
        # Row 2: Text value (retained as is)
        ws.cell(row=2, column=1, value="001234")
        # Row 3: Numeric value with 8-character zero-mask format (padded)
        cell = ws.cell(row=3, column=1, value=5678)
        cell.number_format = "00000000"
        
        # Save temp workbook
        xls_file = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        xls_path = xls_file.name
        xls_file.close()
        wb.save(xls_path)

        csv_file = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
        csv_path = csv_file.name
        csv_file.close()

        try:
            # Run stream conversion
            excel_to_csv_streaming(xls_path, csv_path, sheet_name="test_sheet", header_row=0, account_col_name="account_number")

            # Read result CSV and verify leading zero recovery
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                rows = list(reader)

            self.assertEqual(len(rows), 3) # Header + 2 data rows
            self.assertEqual(rows[0][0], "account_number")
            # Text cell retained
            self.assertEqual(rows[1][0], "001234")
            # Numeric cell padded to 8 chars
            self.assertEqual(rows[2][0], "00005678")

        finally:
            if os.path.exists(xls_path):
                os.remove(xls_path)
            if os.path.exists(csv_path):
                os.remove(csv_path)

    def test_upload_to_db_toggle_respected_in_task(self):
        from unittest.mock import patch
        from django.conf import settings
        from update.tasks import process_file_task
        from update.models import UploadSession, ColumnMapping
        from django.contrib.auth.models import User
        from django.core.files.base import ContentFile
        import tempfile
        import os
        import pyarrow as pa
        import pyarrow.parquet as pq

        user = User.objects.create_user(username="test_user", password="password")
        
        # Create processed parquet files
        pq_path = tempfile.mktemp(suffix=".parquet")
        schema = pa.schema([
            ('AccountNo', pa.string()),
            ('CurrentBalanceAmt', pa.float64()),
            ('AmountOverdue', pa.float64()),
            ('MonthsInArrears', pa.float64()),
            ('LoanClassification', pa.string()),
            ('AccountStatusCode', pa.string()),
        ])
        table = pa.Table.from_pydict({
            'AccountNo': ['123'],
            'CurrentBalanceAmt': [100.0],
            'AmountOverdue': [0.0],
            'MonthsInArrears': [0.0],
            'LoanClassification': ['Performing'],
            'AccountStatusCode': ['Open'],
        }, schema=schema)
        pq.write_table(table, pq_path)

        # Create session with upload_to_db = False
        session = UploadSession.objects.create(
            user=user,
            original_file=ContentFile(b"dummy original", name="test.csv"),
            original_filename="test.csv",
            status="pending_mapping",
            upload_to_db=False,
            sheet_name="test_table",
        )

        # Create column mappings to satisfy mappings check
        ColumnMapping.objects.create(session=session, original_header="AccountNo", target_column="account_number")
        ColumnMapping.objects.create(session=session, original_header="CurrentBalanceAmt", target_column="CurrentBalanceAmt")
        ColumnMapping.objects.create(session=session, original_header="AmountOverdue", target_column="overdue_amount")
        ColumnMapping.objects.create(session=session, original_header="MonthsInArrears", target_column="months_in_arrears")
        ColumnMapping.objects.create(session=session, original_header="LoanClassification", target_column="loan_classification")
        ColumnMapping.objects.create(session=session, original_header="AccountStatusCode", target_column="account_status_code")

        # Dynamically calculated paths that process_file_task will use
        base_name = "test_table"
        cleaned_parquet_path = os.path.join(settings.MEDIA_ROOT, 'processed', f"processed_{base_name}.parquet")
        excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', f"processed_{base_name}.xlsx")

        try:
            # Mock the clean and load function so it copies the parquet file
            with patch('update.tasks._load_and_clean') as mock_clean, \
                 patch('update.tasks.upload_parquet_to_batchupdate') as mock_upload:
                
                def mock_clean_side_effect(file_path, mappings, header_row, session_id, cleaned_path, rejected_path):
                    import shutil
                    shutil.copy(pq_path, cleaned_path)
                    return (1, 0)
                mock_clean.side_effect = mock_clean_side_effect
                
                # Run background task
                process_file_task(session.id)
                
                # Verify session status is processed, but NOT uploaded
                session.refresh_from_db()
                self.assertEqual(session.status, "processed")
                self.assertFalse(session.batchupdate_uploaded)
                
                # Verify upload function was NOT called
            
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
        from update.services import write_parquet_as_excel
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
            write_parquet_as_excel(pq_path, "test_sheet", response_stream)

            # xlsxwriter writes and closes the stream; seek back to verify
            response_stream.seek(0)
            restored_df = pd.read_excel(response_stream, engine='openpyxl')
            self.assertEqual(len(restored_df), 1)
            self.assertEqual(str(restored_df.iloc[0]['AccountNo']), '999888')
            self.assertEqual(float(restored_df.iloc[0]['CurrentBalanceAmt']), 12345.67)
        finally:
            os.remove(pq_path)


class RecentOptimizationsTests(TestCase):
    def test_excel_to_csv_streaming_with_styled_accounts(self):
        from update.services import excel_to_csv_streaming
        from openpyxl import Workbook
        import tempfile
        import csv
        import os

        # Create a temp workbook with an account column and zero mask format
        wb = Workbook()
        ws = wb.active
        ws.title = "test_sheet"
        # Row 1: Header
        ws.append(["account_number", "other_col"])
        # Row 2: Text value (retained as is)
        ws.cell(row=2, column=1, value="001234")
        # Row 3: Numeric value with 8-character zero-mask format (padded)
        cell = ws.cell(row=3, column=1, value=5678)
        cell.number_format = "00000000"
        
        # Save temp workbook
        xls_file = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        xls_path = xls_file.name
        xls_file.close()
        wb.save(xls_path)

        csv_file = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
        csv_path = csv_file.name
        csv_file.close()

        try:
            # Run stream conversion
            excel_to_csv_streaming(xls_path, csv_path, sheet_name="test_sheet", header_row=0, account_col_name="account_number")

            # Read result CSV and verify leading zero recovery
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                rows = list(reader)

            self.assertEqual(len(rows), 3) # Header + 2 data rows
            self.assertEqual(rows[0][0], "account_number")
            # Text cell retained
            self.assertEqual(rows[1][0], "001234")
            # Numeric cell padded to 8 chars
            self.assertEqual(rows[2][0], "00005678")

        finally:
            if os.path.exists(xls_path):
                os.remove(xls_path)
            if os.path.exists(csv_path):
                os.remove(csv_path)

    def test_upload_to_db_toggle_respected_in_task(self):
        from unittest.mock import patch
        from django.conf import settings
        from update.tasks import process_file_task
        from update.models import UploadSession, ColumnMapping
        from django.contrib.auth.models import User
        from django.core.files.base import ContentFile
        import tempfile
        import os
        import pyarrow as pa
        import pyarrow.parquet as pq

        user = User.objects.create_user(username="test_user", password="password")
        
        # Create processed parquet files
        pq_path = tempfile.mktemp(suffix=".parquet")
        schema = pa.schema([
            ('AccountNo', pa.string()),
            ('CurrentBalanceAmt', pa.float64()),
            ('AmountOverdue', pa.float64()),
            ('MonthsInArrears', pa.float64()),
            ('LoanClassification', pa.string()),
            ('AccountStatusCode', pa.string()),
        ])
        table = pa.Table.from_pydict({
            'AccountNo': ['123'],
            'CurrentBalanceAmt': [100.0],
            'AmountOverdue': [0.0],
            'MonthsInArrears': [0.0],
            'LoanClassification': ['Performing'],
            'AccountStatusCode': ['Open'],
        }, schema=schema)
        pq.write_table(table, pq_path)

        # Create session with upload_to_db = False
        session = UploadSession.objects.create(
            user=user,
            original_file=ContentFile(b"dummy original", name="test.csv"),
            original_filename="test.csv",
            status="pending_mapping",
            upload_to_db=False,
            sheet_name="test_table",
        )

        # Create column mappings to satisfy mappings check
        ColumnMapping.objects.create(session=session, original_header="AccountNo", target_column="account_number")
        ColumnMapping.objects.create(session=session, original_header="CurrentBalanceAmt", target_column="CurrentBalanceAmt")
        ColumnMapping.objects.create(session=session, original_header="AmountOverdue", target_column="overdue_amount")
        ColumnMapping.objects.create(session=session, original_header="MonthsInArrears", target_column="months_in_arrears")
        ColumnMapping.objects.create(session=session, original_header="LoanClassification", target_column="loan_classification")
        ColumnMapping.objects.create(session=session, original_header="AccountStatusCode", target_column="account_status_code")

        # Dynamically calculated paths that process_file_task will use
        base_name = "test_table"
        cleaned_parquet_path = os.path.join(settings.MEDIA_ROOT, 'processed', f"processed_{base_name}.parquet")
        excel_path = os.path.join(settings.MEDIA_ROOT, 'processed', f"processed_{base_name}.xlsx")

        try:
            # Mock the clean and load function so it copies the parquet file
            with patch('update.tasks._load_and_clean') as mock_clean, \
                 patch('update.tasks.upload_parquet_to_batchupdate') as mock_upload:
                
                def mock_clean_side_effect(file_path, mappings, header_row, session_id, cleaned_path, rejected_path):
                    import shutil
                    shutil.copy(pq_path, cleaned_path)
                    return (1, 0)
                mock_clean.side_effect = mock_clean_side_effect
                
                # Run background task
                process_file_task(session.id)
                
                # Verify session status is processed, but NOT uploaded
                session.refresh_from_db()
                self.assertEqual(session.status, "processed")
                self.assertFalse(session.batchupdate_uploaded)
                
                # Verify upload function was NOT called
                mock_upload.assert_not_called()
        finally:
            if os.path.exists(pq_path):
                os.remove(pq_path)
            if os.path.exists(cleaned_parquet_path):
                os.remove(cleaned_parquet_path)
            if os.path.exists(excel_path):
                os.remove(excel_path)


class NewBusinessRulesAndSheetValidationTests(TestCase):
    def test_closed_blank_balance_zero_overdue_auto_filled(self):
        """
        Closed account with blank balance, overdue=0, arrears=0, and blank classification
        should be auto-filled with CurrentBalanceAmt=0 and loan_classification='Performing'.
        """
        df = _make_df(
            CurrentBalanceAmt='',
            overdue_amount='0',
            months_in_arrears='0',
            account_status_code='Closed',
            loan_classification='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(rejected), 0)
        row = valid.iloc[0]
        self.assertEqual(float(row['CurrentBalanceAmt']), 0.0)
        self.assertEqual(row['loan_classification'], 'Performing')
        self.assertEqual(row['account_status_code'], 'Closed')
        self.assertEqual(float(row['overdue_amount']), 0.0)
        self.assertEqual(int(row['months_in_arrears']), 0)

    def test_closed_blank_balance_positive_overdue_rejected(self):
        """
        Closed account with blank balance but overdue > 0 cannot be inferred as zero balance,
        so it must be rejected with 'Current Balance Amount is empty'.
        """
        df = _make_df(
            CurrentBalanceAmt='',
            overdue_amount='500',
            months_in_arrears='0',
            account_status_code='Closed',
            loan_classification='',
        )
        valid, rejected = clean_dataframe(df, ALL_MAPPINGS)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn('Current Balance Amount is empty', rejected.iloc[0]['Rejection Reason'])

    def test_is_sheet_usable_detection(self):
        from update.services import is_sheet_usable

        # 1. Empty or None DataFrame
        self.assertFalse(is_sheet_usable(None))
        self.assertFalse(is_sheet_usable(pd.DataFrame()))

        # 2. All-NaN DataFrame
        nan_df = pd.DataFrame({'Col1': [None, None], 'Col2': [None, None]})
        self.assertFalse(is_sheet_usable(nan_df))

        # 3. Unrecognized headers (cover sheet, notes, summary instructions)
        unrecognized_df = pd.DataFrame({
            'Cover Page Note': ['This is an instruction sheet', 'Confidential'],
            'Summary Info': ['Total records: 50', 'Verified by: John'],
        })
        self.assertFalse(is_sheet_usable(unrecognized_df))

        # 4. Recognized headers from target columns or dictionary synonyms
        usable_df = pd.DataFrame({
            'Account Number': ['123456', '789012'],
            'Outstanding Balance': [1000, 2000],
            'Overdue': [0, 50],
        })
        self.assertTrue(is_sheet_usable(usable_df))

    def test_clear_all_errors_view(self):
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from update.models import UploadSession
        from update.views import clear_all_errors_view
        from django.contrib.messages.storage.fallback import FallbackStorage

        user = User.objects.create_user(username="err_test_user", password="password")
        
        # Create 2 errored sessions and 1 uploaded session
        s1 = UploadSession.objects.create(user=user, status='error', original_filename='bad1.xlsx')
        s2 = UploadSession.objects.create(user=user, status='error', original_filename='bad2.xlsx')
        s3 = UploadSession.objects.create(user=user, status='uploaded', original_filename='good.xlsx')

        factory = RequestFactory()
        request = factory.post('/clear-errors/')
        request.user = user
        # Attach message storage
        setattr(request, 'session', {})
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)

        response = clear_all_errors_view(request)
        self.assertEqual(response.status_code, 302)

        # Verify only error sessions were deleted
        self.assertFalse(UploadSession.objects.filter(id__in=[s1.id, s2.id]).exists())
        self.assertTrue(UploadSession.objects.filter(id=s3.id).exists())

    def test_clear_all_pending_view(self):
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from update.models import UploadSession
        from update.views import clear_all_pending_view
        from django.contrib.messages.storage.fallback import FallbackStorage

        user = User.objects.create_user(username="pending_test_user", password="password")
        
        # Create 2 pending sessions and 1 uploaded session
        s1 = UploadSession.objects.create(user=user, status='pending_mapping', original_filename='pend1.xlsx')
        s2 = UploadSession.objects.create(user=user, status='processing', original_filename='pend2.xlsx')
        s3 = UploadSession.objects.create(user=user, status='uploaded', original_filename='good.xlsx')

        factory = RequestFactory()
        request = factory.post('/clear-pending/')
        request.user = user
        setattr(request, 'session', {})
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)

        response = clear_all_pending_view(request)
        self.assertEqual(response.status_code, 302)

        # Verify only pending sessions were deleted
        self.assertFalse(UploadSession.objects.filter(id__in=[s1.id, s2.id]).exists())
        self.assertTrue(UploadSession.objects.filter(id=s3.id).exists())

    def test_ajax_delete_batch(self):
        import uuid
        import json
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from update.models import UploadSession
        from update.views import delete_batch_view

        user = User.objects.create_user(username="ajax_test_user", password="password")
        b_id = uuid.uuid4()
        UploadSession.objects.create(user=user, batch_id=b_id, status='uploaded', original_filename='s1.xlsx')
        UploadSession.objects.create(user=user, batch_id=b_id, status='uploaded', original_filename='s2.xlsx')

        factory = RequestFactory()
        request = factory.post(f'/batch/{b_id}/delete/', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        request.user = user

        response = delete_batch_view(request, b_id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['total_count'], 0)

    def test_duplicate_file_upload_blocked(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from update.models import UploadSession
        from update.views import upload_view

        user = User.objects.create_user(username="dup_test_user", password="password")
        file_content = b"AccountNo,CurrentBalanceAmt\n12345,100\n"
        
        sub388, _ = Subscriber.objects.get_or_create(subscriber_id=388, defaults={'subscriber_name': 'Access Bank Plc'})
        # First upload
        uploaded_file1 = SimpleUploadedFile("test_file.csv", file_content, content_type="text/csv")
        s1 = UploadSession.objects.create(
            user=user,
            original_file=uploaded_file1,
            original_filename="test_file.csv",
            status='uploaded',
            subscriber=sub388,
        )

        # Attempt second upload of identical file
        uploaded_file2 = SimpleUploadedFile("test_file_dup.csv", file_content, content_type="text/csv")
        factory = RequestFactory()
        request = factory.post('/upload/', {'file': uploaded_file2, 'file_subscriber_0': '388'})
        request.user = user
        setattr(request, 'session', {})
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)

        response = upload_view(request)
        self.assertEqual(response.status_code, 302)

        # Ensure no new session was created
        self.assertEqual(UploadSession.objects.filter(user=user).count(), 1)
        
        # Verify the error message
        error_msgs = [m.message for m in messages if m.level_tag == 'error']
        self.assertTrue(any('Upload blocked' in m for m in error_msgs))
        self.assertFalse(any('No files could be processed' in m for m in error_msgs))

    def test_batch_duplicate_file_upload_blocked(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from update.models import UploadSession
        from update.views import upload_view

        user = User.objects.create_user(username="batch_dup_user", password="password")
        file_content = b"AccountNo,CurrentBalanceAmt\n12345,100\n"
        
        # Prior existing session
        uploaded_file1 = SimpleUploadedFile("388_31082026_access.xlsx", file_content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s1 = UploadSession.objects.create(
            user=user,
            original_file=uploaded_file1,
            original_filename="388_31082026_access.xlsx",
            status='uploaded',
        )

        # Upload duplicate in multi-file mode
        uploaded_file2 = SimpleUploadedFile("388_31082026_access.xlsx", file_content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        factory = RequestFactory()
        request = factory.post('/upload/', {'file': [uploaded_file2]})
        request.user = user
        setattr(request, 'session', {})
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)

        response = upload_view(request)
        self.assertEqual(response.status_code, 302)

        # Ensure only the blocked message exists and no generic pattern error
        error_msgs = [m.message for m in messages if m.level_tag == 'error']
        self.assertTrue(any('Upload blocked' in m for m in error_msgs))
        self.assertFalse(any('No files could be processed' in m for m in error_msgs))

    def test_multi_file_upload_with_per_file_subscribers(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from update.models import UploadSession, Subscriber
        from update.views import upload_view

        user = User.objects.create_user(username="per_file_sub_user", password="password")
        sub388, _ = Subscriber.objects.get_or_create(subscriber_id=388, defaults={'subscriber_name': 'Access Bank Plc'})
        sub446, _ = Subscriber.objects.get_or_create(subscriber_id=446, defaults={'subscriber_name': 'Guaranty Trust Bank'})

        file_content_1 = b"AccountNo,CurrentBalanceAmt\nACC001,150.50\n"
        file_content_2 = b"AccountNo,CurrentBalanceAmt\nACC002,250.75\n"
        file_content_3 = b"AccountNo,CurrentBalanceAmt\nGTB001,500.00\n"

        f1 = SimpleUploadedFile("Access_Retail.csv", file_content_1, content_type="text/csv")
        f2 = SimpleUploadedFile("Access_Corporate.csv", file_content_2, content_type="text/csv")
        f3 = SimpleUploadedFile("GTB_Consumer.csv", file_content_3, content_type="text/csv")

        factory = RequestFactory()
        post_data = {
            'excel_file': [f1, f2, f3],
            'file_subscriber_0': '388',
            'file_subscriber_1': '388',
            'file_subscriber_2': '446',
        }
        request = factory.post('/upload/', post_data)
        request.user = user
        setattr(request, 'session', {})
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)

        response = upload_view(request)
        # Should redirect to batch page
        self.assertEqual(response.status_code, 302)

        sessions = list(UploadSession.objects.filter(user=user).order_by('uploaded_at'))
        self.assertEqual(len(sessions), 3)

        # Check subscriber attribution
        self.assertEqual(sessions[0].subscriber, sub388)
        self.assertEqual(sessions[1].subscriber, sub388)
        self.assertEqual(sessions[2].subscriber, sub446)

        # Check collision-free sheet names
        self.assertTrue('access' in sessions[0].sheet_name)
        self.assertTrue('access_1' in sessions[1].sheet_name)
        self.assertTrue('guaranty' in sessions[2].sheet_name or '446' in sessions[2].sheet_name)

    def test_cleanup_old_dropped_files_task(self):
        from datetime import timedelta
        from django.utils import timezone
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile
        from update.tasks import cleanup_old_dropped_files_task

        user = User.objects.create_user(username="cleanup_test_user", password="password")

        # Create old drop (3 days ago)
        f_old = SimpleUploadedFile("old_drop.xlsx", b"dummy content", content_type="application/octet-stream")
        drop_old = DroppedFile.objects.create(
            user=user,
            file=f_old,
            original_filename="old_drop.xlsx",
            file_size_bytes=len(b"dummy content"),
        )
        old_time = timezone.now() - timedelta(days=3)
        DroppedFile.objects.filter(id=drop_old.id).update(dropped_at=old_time)

        # Create recent drop (now)
        f_recent = SimpleUploadedFile("recent_drop.xlsx", b"recent content", content_type="application/octet-stream")
        drop_recent = DroppedFile.objects.create(
            user=user,
            file=f_recent,
            original_filename="recent_drop.xlsx",
            file_size_bytes=len(b"recent content"),
        )

        old_file_path = drop_old.file.path if drop_old.file else None

        # Run cleanup task with 2-day retention
        result = cleanup_old_dropped_files_task(retention_days=2)

        # Old drop should be deleted from DB and disk
        self.assertFalse(DroppedFile.objects.filter(id=drop_old.id).exists())
        if old_file_path and os.path.exists(old_file_path):
            self.fail("Expected old dropped file on disk to be removed by cleanup task")

        # Recent drop should still exist in DB
        self.assertTrue(DroppedFile.objects.filter(id=drop_recent.id).exists())

        # Cleanup recent file
        if drop_recent.file:
            try:
                drop_recent.file.delete(save=False)
            except Exception:
                pass
        drop_recent.delete()


class FileDropBoxSubscriberTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage

        self.user = User.objects.create_user(username="dropper_user", password="password")
        self.staff_user = User.objects.create_user(username="staff_user", password="password", is_staff=True)
        self.factory = RequestFactory()

    def _setup_request(self, request, user):
        request.user = user
        setattr(request, 'session', {})
        from django.contrib.messages.storage.fallback import FallbackStorage
        messages = FallbackStorage(request)
        setattr(request, '_messages', messages)
        return messages

    def test_drop_file_without_subscriber_fails(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile
        from update.views import drop_view

        file_content = b"AccountNo,CurrentBalanceAmt\n12345,100\n"
        f = SimpleUploadedFile("drop_test.xlsx", file_content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        request = self.factory.post('/drop/', {'drop_files': [f]})
        messages = self._setup_request(request, self.user)

        with patch('update.views.get_subscribers_from_batchupdate', return_value=[{'subscriber_id': 388, 'subscriber_name': 'Access Bank Plc'}]):
            response = drop_view(request)

        self.assertEqual(response.status_code, 302)
        error_msgs = [m.message for m in messages if m.level_tag == 'error']
        self.assertTrue(any('select a subscriber institution' in m for m in error_msgs))
        self.assertEqual(DroppedFile.objects.filter(user=self.user).count(), 0)

    def test_drop_file_with_invalid_subscriber_fails(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile
        from update.views import drop_view

        file_content = b"AccountNo,CurrentBalanceAmt\n12345,100\n"
        f = SimpleUploadedFile("drop_test.xlsx", file_content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        request = self.factory.post('/drop/', {'drop_files': [f], 'subscriber': '99999'})
        messages = self._setup_request(request, self.user)

        with patch('update.views.get_subscribers_from_batchupdate', return_value=[{'subscriber_id': 388, 'subscriber_name': 'Access Bank Plc'}]):
            response = drop_view(request)

        self.assertEqual(response.status_code, 302)
        error_msgs = [m.message for m in messages if m.level_tag == 'error']
        self.assertTrue(any('Invalid subscriber selected' in m for m in error_msgs))
        self.assertEqual(DroppedFile.objects.filter(user=self.user).count(), 0)

    def test_drop_file_with_valid_subscriber_succeeds(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile
        from update.views import drop_view

        file_content = b"AccountNo,CurrentBalanceAmt\n12345,100\n"
        f = SimpleUploadedFile("drop_test.xlsx", file_content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        request = self.factory.post('/drop/', {'drop_files': [f], 'subscriber': '388', 'notes': 'Test notes'})
        messages = self._setup_request(request, self.user)

        with patch('update.views.get_subscribers_from_batchupdate', return_value=[{'subscriber_id': 388, 'subscriber_name': 'Access Bank Plc'}]):
            response = drop_view(request)

        self.assertEqual(response.status_code, 302)
        drops = list(DroppedFile.objects.filter(user=self.user))
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0].original_filename, 'drop_test.xlsx')
        self.assertIsNotNone(drops[0].subscriber)
        self.assertEqual(drops[0].subscriber.subscriber_id, 388)
        self.assertEqual(drops[0].subscriber.subscriber_name, 'Access Bank Plc')
        self.assertEqual(drops[0].status, 'pending')

        # Cleanup file on disk
        if drops[0].file:
            try:
                drops[0].file.delete(save=False)
            except Exception:
                pass

    def test_drop_multiple_files_with_distinct_subscribers(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile
        from update.views import drop_view

        f1 = SimpleUploadedFile("access_batch.xlsx", b"col1,col2\n1,2\n", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        f2 = SimpleUploadedFile("gtb_batch.xlsx", b"col1,col2\n3,4\n", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        request = self.factory.post('/drop/', {
            'drop_files': [f1, f2],
            'file_subscriber_0': '388',
            'file_subscriber_1': '446',
            'notes': 'Multi-bank drop',
        })
        messages = self._setup_request(request, self.user)

        with patch('update.views.get_subscribers_from_batchupdate', return_value=[
            {'subscriber_id': 388, 'subscriber_name': 'Access Bank Plc'},
            {'subscriber_id': 446, 'subscriber_name': 'Guaranty Trust Bank'},
        ]):
            response = drop_view(request)

        self.assertEqual(response.status_code, 302)
        drops = list(DroppedFile.objects.filter(user=self.user).order_by('id'))
        self.assertEqual(len(drops), 2)
        self.assertEqual(drops[0].original_filename, 'access_batch.xlsx')
        self.assertEqual(drops[0].subscriber.subscriber_id, 388)
        self.assertEqual(drops[0].notes, 'Multi-bank drop')

        self.assertEqual(drops[1].original_filename, 'gtb_batch.xlsx')
        self.assertEqual(drops[1].subscriber.subscriber_id, 446)
        self.assertEqual(drops[1].notes, 'Multi-bank drop')

        success_msgs = [m.message for m in messages if m.level_tag == 'success']
        self.assertTrue(any('across 2 institutions' in m for m in success_msgs))

        for d in drops:
            if d.file:
                try:
                    d.file.delete(save=False)
                except Exception:
                    pass

    def test_drop_multiple_files_missing_subscriber_for_one_file(self):
        from unittest.mock import patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile
        from update.views import drop_view

        f1 = SimpleUploadedFile("access_batch.xlsx", b"col1,col2\n1,2\n", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        f2 = SimpleUploadedFile("unassigned_batch.xlsx", b"col1,col2\n3,4\n", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        request = self.factory.post('/drop/', {
            'drop_files': [f1, f2],
            'file_subscriber_0': '388',
            'file_subscriber_1': '',
        })
        messages = self._setup_request(request, self.user)

        with patch('update.views.get_subscribers_from_batchupdate', return_value=[
            {'subscriber_id': 388, 'subscriber_name': 'Access Bank Plc'},
        ]):
            response = drop_view(request)

        self.assertEqual(response.status_code, 302)
        error_msgs = [m.message for m in messages if m.level_tag == 'error']
        self.assertTrue(any('Please select a subscriber institution for "unassigned_batch.xlsx"' in m for m in error_msgs))
        self.assertEqual(DroppedFile.objects.filter(user=self.user).count(), 0)

    def test_pending_drops_api_includes_subscriber_details(self):
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile, Subscriber
        from update.views import pending_drops_api

        sub = Subscriber.objects.create(subscriber_id=446, subscriber_name='Guaranty Trust Bank')
        f = SimpleUploadedFile("gtb_drop.xlsx", b"dummy content", content_type="application/octet-stream")
        drop = DroppedFile.objects.create(
            user=self.user,
            subscriber=sub,
            file=f,
            original_filename="gtb_drop.xlsx",
            file_size_bytes=len(b"dummy content"),
            notes="July return",
            status="pending",
        )

        request = self.factory.get('/api/pending-drops/')
        self._setup_request(request, self.staff_user)

        response = pending_drops_api(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode('utf-8'))
        self.assertIn('drops', data)
        found_drop = next((d for d in data['drops'] if d['id'] == drop.id), None)
        self.assertIsNotNone(found_drop)
        self.assertEqual(found_drop['subscriber_id'], 446)
        self.assertEqual(found_drop['subscriber_name'], 'Guaranty Trust Bank')

        # Cleanup
        if drop.file:
            try:
                drop.file.delete(save=False)
            except Exception:
                pass
        drop.delete()

    def test_dropper_can_delete_own_pending_drop(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile, Subscriber
        from update.views import delete_dropped_file_view

        sub = Subscriber.objects.create(subscriber_id=388, subscriber_name='Access Bank Plc')
        f = SimpleUploadedFile("mistake.xlsx", b"dummy content", content_type="application/octet-stream")
        drop = DroppedFile.objects.create(
            user=self.user,
            subscriber=sub,
            file=f,
            original_filename="mistake.xlsx",
            file_size_bytes=len(b"dummy content"),
            status="pending",
        )

        request = self.factory.post(f'/drop/delete/{drop.id}/')
        messages = self._setup_request(request, self.user)

        response = delete_dropped_file_view(request, drop.id)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DroppedFile.objects.filter(id=drop.id).exists())
        success_msgs = [m.message for m in messages if m.level_tag == 'success']
        self.assertTrue(any('Successfully deleted' in m for m in success_msgs))

    def test_other_user_cannot_delete_dropped_file(self):
        from django.contrib.auth.models import User
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile, Subscriber
        from update.views import delete_dropped_file_view

        other_user = User.objects.create_user(username="other_user", password="password")
        sub = Subscriber.objects.create(subscriber_id=388, subscriber_name='Access Bank Plc')
        f = SimpleUploadedFile("user1_file.xlsx", b"dummy", content_type="application/octet-stream")
        drop = DroppedFile.objects.create(
            user=self.user,
            subscriber=sub,
            file=f,
            original_filename="user1_file.xlsx",
            file_size_bytes=len(b"dummy"),
            status="pending",
        )

        request = self.factory.post(f'/drop/delete/{drop.id}/')
        messages = self._setup_request(request, other_user)

        response = delete_dropped_file_view(request, drop.id)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DroppedFile.objects.filter(id=drop.id).exists())
        error_msgs = [m.message for m in messages if m.level_tag == 'error']
        self.assertTrue(any('do not have permission' in m for m in error_msgs))

        # Cleanup
        if drop.file:
            try:
                drop.file.delete(save=False)
            except Exception:
                pass
        drop.delete()

    def test_cannot_delete_imported_drop_unless_staff(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile, Subscriber
        from update.views import delete_dropped_file_view

        sub = Subscriber.objects.create(subscriber_id=388, subscriber_name='Access Bank Plc')
        f = SimpleUploadedFile("imported_file.xlsx", b"dummy", content_type="application/octet-stream")
        drop = DroppedFile.objects.create(
            user=self.user,
            subscriber=sub,
            file=f,
            original_filename="imported_file.xlsx",
            file_size_bytes=len(b"dummy"),
            status="imported",
        )

        # Dropper attempt fails
        request = self.factory.post(f'/drop/delete/{drop.id}/')
        messages = self._setup_request(request, self.user)
        response = delete_dropped_file_view(request, drop.id)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DroppedFile.objects.filter(id=drop.id).exists())

        # Staff attempt succeeds
        staff_request = self.factory.post(f'/drop/delete/{drop.id}/')
        self._setup_request(staff_request, self.staff_user)
        staff_resp = delete_dropped_file_view(staff_request, drop.id)
        self.assertEqual(staff_resp.status_code, 302)
        self.assertFalse(DroppedFile.objects.filter(id=drop.id).exists())

    def test_ajax_delete_returns_json(self):
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile
        from update.models import DroppedFile, Subscriber
        from update.views import delete_dropped_file_view

        sub = Subscriber.objects.create(subscriber_id=388, subscriber_name='Access Bank Plc')
        f = SimpleUploadedFile("ajax_file.xlsx", b"dummy", content_type="application/octet-stream")
        drop = DroppedFile.objects.create(
            user=self.user,
            subscriber=sub,
            file=f,
            original_filename="ajax_file.xlsx",
            file_size_bytes=len(b"dummy"),
            status="pending",
        )

        request = self.factory.post(f'/drop/delete/{drop.id}/', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self._setup_request(request, self.user)
        response = delete_dropped_file_view(request, drop.id)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode('utf-8'))
        self.assertTrue(data.get('success'))
        self.assertEqual(data.get('drop_id'), drop.id)
        self.assertFalse(DroppedFile.objects.filter(id=drop.id).exists())



class AutoMapHistoricalTargetGateTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from update.models import Subscriber

        self.user = User.objects.create_user(username="automap_user", password="password")
        self.factory = RequestFactory()
        self.request = self.factory.get('/upload/')
        self.request.user = self.user

        self.subscriber = Subscriber.objects.create(
            subscriber_id=101,
            subscriber_name="Test Bank 101"
        )

    def test_auto_map_halts_when_historical_column_missing(self):
        """
        When a subscriber has historically provided columns (e.g. AccountNo, Balance, MonthsInArrears),
        and an incoming file has a typo (e.g. 'monthinarrearsdays') resulting in a missing historical target,
        auto-mapping must halt (is_complete=False), list missing targets, and NOT save an incomplete template.
        """
        import json
        import pandas as pd
        from update.models import UploadSession, ColumnMapping, MappingTemplate
        from update.views import _try_auto_map

        # 1. Historical successful session for subscriber 101
        prev_session = UploadSession.objects.create(
            user=self.user,
            original_filename="prev_101.xlsx",
            status="completed",
            subscriber=self.subscriber,
        )
        ColumnMapping.objects.create(session=prev_session, original_header="AccountNo", target_column="account_number")
        ColumnMapping.objects.create(session=prev_session, original_header="Balance", target_column="CurrentBalanceAmt")
        ColumnMapping.objects.create(session=prev_session, original_header="MonthsInArrears", target_column="months_in_arrears")

        # 2. New upload with misspelled header 'monthinarrearsdays'
        new_session = UploadSession.objects.create(
            user=self.user,
            original_filename="new_101.xlsx",
            status="pending_mapping",
            subscriber=self.subscriber,
        )
        df = pd.DataFrame(columns=["AccountNo", "Balance", "monthinarrearsdays"])

        res = _try_auto_map(self.request, new_session, df)

        # 3. Assertions
        self.assertFalse(res['is_complete'])
        self.assertIn('months_in_arrears', res['missing_historical_targets'])
        self.assertTrue(res['has_acct'])
        # Recognized columns must still be mapped in DB for user convenience
        self.assertTrue(new_session.mappings.filter(target_column='account_number').exists())
        self.assertTrue(new_session.mappings.filter(target_column='CurrentBalanceAmt').exists())
        # Incomplete mapping template MUST NOT be saved
        sig = json.dumps(sorted(list(df.columns)))
        self.assertFalse(MappingTemplate.objects.filter(header_signature=sig).exists())

    def test_auto_map_advances_when_all_historical_columns_match(self):
        """
        When all historical columns are recognized, auto-mapping advances (is_complete=True)
        and persists the template.
        """
        import json
        import pandas as pd
        from update.models import UploadSession, ColumnMapping, MappingTemplate
        from update.views import _try_auto_map

        # Historical session
        prev_session = UploadSession.objects.create(
            user=self.user,
            original_filename="prev_101.xlsx",
            status="completed",
            subscriber=self.subscriber,
        )
        ColumnMapping.objects.create(session=prev_session, original_header="AccountNo", target_column="account_number")
        ColumnMapping.objects.create(session=prev_session, original_header="Balance", target_column="CurrentBalanceAmt")
        ColumnMapping.objects.create(session=prev_session, original_header="MIA", target_column="months_in_arrears")

        # New upload where all 3 historical targets match dictionary synonyms
        new_session = UploadSession.objects.create(
            user=self.user,
            original_filename="new_101.xlsx",
            status="pending_mapping",
            subscriber=self.subscriber,
        )
        # 'acct no' -> account_number, 'current balance' -> CurrentBalanceAmt, 'months in arrears' -> months_in_arrears
        df = pd.DataFrame(columns=["acct no", "current balance", "months in arrears"])

        res = _try_auto_map(self.request, new_session, df)

        self.assertTrue(res['is_complete'])
        self.assertEqual(len(res['missing_historical_targets']), 0)
        sig = json.dumps(sorted(list(df.columns)))
        self.assertTrue(MappingTemplate.objects.filter(header_signature=sig).exists())

    def test_first_time_subscriber_with_unmapped_header_halts(self):
        """
        When a subscriber uploads for the first time and the file contains an unmapped column,
        auto-mapping halts (is_complete=False) so a human operator establishes the baseline.
        """
        import json
        import pandas as pd
        from update.models import Subscriber, UploadSession, MappingTemplate
        from update.views import _try_auto_map

        new_sub = Subscriber.objects.create(subscriber_id=999, subscriber_name="First Time Bank")
        session = UploadSession.objects.create(
            user=self.user,
            original_filename="first_999.xlsx",
            status="pending_mapping",
            subscriber=new_sub,
        )
        df = pd.DataFrame(columns=["AccountNo", "Balance", "UnrecognizedColXYZ"])

        res = _try_auto_map(self.request, session, df)

        self.assertFalse(res['is_complete'])
        sig = json.dumps(sorted(list(df.columns)))
        self.assertFalse(MappingTemplate.objects.filter(header_signature=sig).exists())

    def test_first_time_subscriber_with_all_headers_mapped_advances(self):
        """
        When a first-time subscriber uploads a file where all headers are recognized,
        auto-mapping completes and saves the initial template.
        """
        import json
        import pandas as pd
        from update.models import Subscriber, UploadSession, MappingTemplate
        from update.views import _try_auto_map

        clean_sub = Subscriber.objects.create(subscriber_id=888, subscriber_name="Clean First Time Bank")
        session = UploadSession.objects.create(
            user=self.user,
            original_filename="clean_888.xlsx",
            status="pending_mapping",
            subscriber=clean_sub,
        )
        df = pd.DataFrame(columns=["AccountNo", "Balance"])

        res = _try_auto_map(self.request, session, df)

        self.assertTrue(res['is_complete'])
        sig = json.dumps(sorted(list(df.columns)))
        self.assertTrue(MappingTemplate.objects.filter(header_signature=sig).exists())

    def test_get_subscriber_historical_targets_deduplicates_and_ignores_empty(self):
        """
        get_subscriber_historical_targets returns a deduplicated set of non-empty mapped targets.
        """
        from update.models import UploadSession, ColumnMapping
        from update.services import get_subscriber_historical_targets

        s1 = UploadSession.objects.create(user=self.user, original_filename="s1.xlsx", subscriber=self.subscriber)
        ColumnMapping.objects.create(session=s1, original_header="h1", target_column="account_number")
        ColumnMapping.objects.create(session=s1, original_header="h2", target_column="CurrentBalanceAmt")

        s2 = UploadSession.objects.create(user=self.user, original_filename="s2.xlsx", subscriber=self.subscriber)
        ColumnMapping.objects.create(session=s2, original_header="h1", target_column="account_number")
        ColumnMapping.objects.create(session=s2, original_header="h3", target_column="overdue_amount")
        ColumnMapping.objects.create(session=s2, original_header="h4", target_column="")  # empty string should be ignored

        targets = get_subscriber_historical_targets(self.subscriber.id)
        self.assertEqual(targets, {"account_number", "CurrentBalanceAmt", "overdue_amount"})

        targets_excl_s2 = get_subscriber_historical_targets(self.subscriber.id, exclude_session_id=s2.id)
        self.assertEqual(targets_excl_s2, {"account_number", "CurrentBalanceAmt"})


class BuildSheetNameTests(TestCase):
    def test_the_alternative_bank_produces_thealternative(self):
        from update.models import Subscriber
        from update.services import build_sheet_name

        sub = Subscriber.objects.create(subscriber_id=388, subscriber_name="The Alternative Bank")
        sheet_name = build_sheet_name(sub, date="14092026")
        self.assertEqual(sheet_name, "388_14092026_thealternative")
        self.assertLessEqual(len(sheet_name), 31)

    def test_access_bank_plc_produces_access(self):
        from update.models import Subscriber
        from update.services import build_sheet_name

        sub = Subscriber.objects.create(subscriber_id=446, subscriber_name="Access Bank Plc")
        sheet_name = build_sheet_name(sub, date="14092026")
        self.assertEqual(sheet_name, "446_14092026_access")
        self.assertLessEqual(len(sheet_name), 31)

    def test_leading_number_in_name_is_skipped(self):
        from update.models import Subscriber
        from update.services import build_sheet_name

        sub = Subscriber.objects.create(subscriber_id=388, subscriber_name="388 - Access Bank")
        sheet_name = build_sheet_name(sub, date="14092026")
        self.assertEqual(sheet_name, "388_14092026_access")

    def test_sheet_name_never_exceeds_31_characters(self):
        from update.models import Subscriber
        from update.services import build_sheet_name

        sub = Subscriber.objects.create(
            subscriber_id=1234,
            subscriber_name="First City Monument Bank International Commercial Holdings Plc"
        )
        sheet_name = build_sheet_name(sub, date="14092026")
        self.assertLessEqual(len(sheet_name), 31)

        sheet_name_indexed = build_sheet_name(sub, date="14092026", index=1)
        self.assertLessEqual(len(sheet_name_indexed), 31)







