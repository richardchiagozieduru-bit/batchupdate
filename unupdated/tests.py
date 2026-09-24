import datetime
from unittest.mock import MagicMock, patch
from django.test import TestCase
from django.urls import reverse
from unupdated.services import (
    compute_period_and_cutoff,
    sanitize_value,
    clean_filename,
    format_period_label,
    check_subscriber_readiness,
    get_ungenerated_ready_subscribers,
)
from unupdated.queries import (
    get_consumer_query,
    get_commercial_query,
    EXCLUDED_ACCOUNT_STATUSES,
    CONSUMER_HEADERS,
    COMMERCIAL_HEADERS,
    CONSUMER_MAX_PERIOD_QUERY,
    COMMERCIAL_MAX_PERIOD_QUERY,
)


class UnupdatedServiceTests(TestCase):
    def test_compute_period_and_cutoff_standard_month(self):
        rep_month, last_period, cutoff = compute_period_and_cutoff('2026-07')
        self.assertEqual(rep_month, '2026-07')
        self.assertEqual(last_period, '20260731')
        self.assertEqual(cutoff, '2026-08-01')

    def test_compute_period_and_cutoff_february_leap(self):
        # 2024 is leap year
        rep_month, last_period, cutoff = compute_period_and_cutoff('2024-02')
        self.assertEqual(rep_month, '2024-02')
        self.assertEqual(last_period, '20240229')
        self.assertEqual(cutoff, '2024-03-01')

    def test_compute_period_and_cutoff_year_rollover(self):
        rep_month, last_period, cutoff = compute_period_and_cutoff('2026-12')
        self.assertEqual(rep_month, '2026-12')
        self.assertEqual(last_period, '20261231')
        self.assertEqual(cutoff, '2027-01-01')

    def test_compute_period_and_cutoff_with_override(self):
        rep_month, last_period, cutoff = compute_period_and_cutoff('2026-07', cutoff_date_override='2026-08-15')
        self.assertEqual(rep_month, '2026-07')
        self.assertEqual(last_period, '20260731')
        self.assertEqual(cutoff, '2026-08-15')

    def test_sanitize_value_text_column(self):
        # Leading zero account number with whitespace/newlines
        val = "  0012345678\r\n  "
        clean = sanitize_value(val, is_text_col=True)
        self.assertEqual(clean, "0012345678")

    def test_sanitize_value_date(self):
        d = datetime.date(2026, 7, 31)
        self.assertEqual(sanitize_value(d), "2026-07-31")

    def test_clean_filename(self):
        name = "Access Bank Plc / Retail & Corporate"
        clean = clean_filename(name)
        self.assertNotIn("/", clean)
        self.assertNotIn("&", clean)

    def test_queries_have_correct_placeholder_count(self):
        c_query = get_consumer_query()
        comm_query = get_commercial_query()

        # Placeholders: 1 (sub_id) + len(statuses) + 1 (cutoff) + 1 (last_period)
        expected_params = 1 + len(EXCLUDED_ACCOUNT_STATUSES) + 1 + 1
        self.assertEqual(c_query.count('?'), expected_params)
        self.assertEqual(comm_query.count('?'), expected_params)

    def test_header_counts(self):
        self.assertEqual(len(CONSUMER_HEADERS), 19)
        self.assertEqual(len(COMMERCIAL_HEADERS), 17)

    def test_max_period_queries_structure(self):
        self.assertIn("MAX(lastperiodnum)", CONSUMER_MAX_PERIOD_QUERY)
        self.assertIn("WITH (NOLOCK)", CONSUMER_MAX_PERIOD_QUERY)
        self.assertIn("StatusInd = 'A'", CONSUMER_MAX_PERIOD_QUERY)
        self.assertIn("subscriberid = ?", CONSUMER_MAX_PERIOD_QUERY)
        self.assertIn("MAX(lastperiodnum)", COMMERCIAL_MAX_PERIOD_QUERY)
        self.assertIn("WITH (NOLOCK)", COMMERCIAL_MAX_PERIOD_QUERY)
        self.assertIn("StatusInd = 'A'", COMMERCIAL_MAX_PERIOD_QUERY)
        self.assertIn("subscriberid = ?", COMMERCIAL_MAX_PERIOD_QUERY)

    def test_format_period_label(self):
        self.assertEqual(format_period_label('20260831'), 'August 2026 (20260831)')
        self.assertEqual(format_period_label('20260731'), 'July 2026 (20260731)')
        self.assertEqual(format_period_label(''), '')
        self.assertEqual(format_period_label(None), '')

    def test_check_subscriber_readiness_both_ready(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # 1st fetchone for consumer, 2nd for commercial
        mock_cursor.fetchone.side_effect = [('20260831',), ('20260831',)]

        res = check_subscriber_readiness(
            subscriber_id=464,
            target_last_period_num='20260831',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )
        self.assertTrue(res['is_ready'])
        self.assertEqual(res['consumer_max'], '20260831')
        self.assertEqual(res['commercial_max'], '20260831')
        self.assertIn('duration_seconds', res)
        self.assertIn("File verified", res['message'])

    def test_check_subscriber_readiness_unuploaded_less_than_target(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # Consumer is July (20260731), Commercial is July (20260731)
        mock_cursor.fetchone.side_effect = [('20260731',), ('20260731',)]

        res = check_subscriber_readiness(
            subscriber_id=464,
            target_last_period_num='20260831',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )
        self.assertFalse(res['is_ready'])
        self.assertIn("Kindly confirm that the subscriber has uploaded file", res['message'])
        self.assertIn("August 2026", res['message'])
        self.assertIn("July 2026", res['message'])

    def test_check_subscriber_readiness_commercial_unuploaded(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # Consumer is August, but Commercial is July
        mock_cursor.fetchone.side_effect = [('20260831',), ('20260731',)]

        res = check_subscriber_readiness(
            subscriber_id=464,
            target_last_period_num='20260831',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )
        self.assertFalse(res['is_ready'])
        self.assertIn("Commercial (Latest: July 2026 (20260731))", res['message'])

    def test_check_subscriber_readiness_consumer_only_with_no_commercial_records(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # Consumer is August, Commercial returns None (bank only has consumer loans)
        mock_cursor.fetchone.side_effect = [('20260831',), (None,)]

        res = check_subscriber_readiness(
            subscriber_id=464,
            target_last_period_num='20260831',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )
        self.assertTrue(res['is_ready'])
        self.assertEqual(res['consumer_max'], '20260831')
        self.assertIsNone(res['commercial_max'])

    def test_check_subscriber_readiness_historical_audit(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # DB has August (20260831), auditing July (20260731)
        mock_cursor.fetchone.side_effect = [('20260831',), ('20260831',)]

        res = check_subscriber_readiness(
            subscriber_id=464,
            target_last_period_num='20260731',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )
        self.assertTrue(res['is_ready'])

    def test_check_subscriber_readiness_no_records_found(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(None,), (None,)]

        res = check_subscriber_readiness(
            subscriber_id=9999,
            target_last_period_num='20260831',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )
        self.assertFalse(res['is_ready'])
        self.assertIn("No account records found", res['message'])

    def test_get_ungenerated_ready_subscribers_indexed(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # 1. fetchall for ALL_SUBSCRIBERS_QUERY returns 2 subscribers
        mock_cursor.fetchall.return_value = [(101, 'Bank A'), (102, 'Bank B')]

        # 2. fetchone calls:
        # Sub 101: Consumer=20260831, Commercial=20260831 (Ready)
        # Sub 102: Consumer=20260731, Commercial=20260731 (Not Ready)
        mock_cursor.fetchone.side_effect = [
            ('20260831',), ('20260831',),  # Sub 101
            ('20260731',), ('20260731',),  # Sub 102
        ]

        from django.core.cache import cache
        cache.clear()

        ready = get_ungenerated_ready_subscribers(
            reporting_month_str='2026-08',
            include_consumer=True,
            include_commercial=True,
            conn=mock_conn,
        )

        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]['subscriber_id'], 101)
        self.assertEqual(ready[0]['subscriber_name'], 'Bank A')
        self.assertEqual(ready[0]['consumer_max'], '20260831')

    def test_get_ungenerated_ready_subscribers_24h_cache_and_resync(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        mock_cursor.fetchall.return_value = [(101, 'Bank A')]
        mock_cursor.fetchone.side_effect = [('20260831',), ('20260831',)]

        from django.core.cache import cache
        cache.clear()

        # Call 1: Misses cache -> queries DB
        ready1, meta1 = get_ungenerated_ready_subscribers(
            reporting_month_str='2026-08',
            conn=mock_conn,
            return_meta=True,
        )
        self.assertEqual(len(ready1), 1)
        self.assertFalse(meta1['is_cached'])
        self.assertEqual(mock_cursor.execute.call_count, 3)  # 1 for ALL_SUBSCRIBERS, 2 for MAX period queries

        # Reset mock call count
        mock_cursor.reset_mock()

        # Call 2: Within 24h -> returns from local RAM cache without touching DB
        ready2, meta2 = get_ungenerated_ready_subscribers(
            reporting_month_str='2026-08',
            conn=mock_conn,
            return_meta=True,
        )
        self.assertEqual(len(ready2), 1)
        self.assertTrue(meta2['is_cached'])
        self.assertEqual(meta2['synced_at'], meta1['synced_at'])
        mock_cursor.execute.assert_not_called()  # 0 DB calls!

        # Call 3: With force_refresh=True -> queries DB again (Resync Bureau)
        mock_cursor.fetchall.return_value = [(101, 'Bank A')]
        mock_cursor.fetchone.side_effect = [('20260831',), ('20260831',)]
        ready3, meta3 = get_ungenerated_ready_subscribers(
            reporting_month_str='2026-08',
            conn=mock_conn,
            force_refresh=True,
            return_meta=True,
        )
        self.assertEqual(len(ready3), 1)
        self.assertFalse(meta3['is_cached'])
        self.assertGreater(mock_cursor.execute.call_count, 0)

    def test_get_ungenerated_ready_subscribers_dynamic_completion_filter(self):
        from django.contrib.auth.models import User
        from unupdated.models import UnupdatedBatch, UnupdatedSession
        from django.core.cache import cache

        user = User.objects.create_user(username='testcache', password='pw')
        cache.clear()

        # Seed local RAM cache with Sub 101 and Sub 102
        cache_key = 'unupdated_ready_subs_2026-08_True_True'
        cache.set(cache_key, {
            'synced_at': '2026-09-10 12:00 PM',
            'subscribers': [
                {'subscriber_id': 101, 'subscriber_name': 'Bank A'},
                {'subscriber_id': 102, 'subscriber_name': 'Bank B'},
            ]
        }, timeout=86400)

        # Before any batch completed, both are returned from cache
        subs_before = get_ungenerated_ready_subscribers('2026-08')
        self.assertEqual(len(subs_before), 2)

        # Now complete Sub 101 in local DB
        batch = UnupdatedBatch.objects.create(
            user=user,
            reporting_month='2026-08',
            last_period_num='20260831',
            cutoff_date='2026-09-01',
        )
        UnupdatedSession.objects.create(
            batch=batch,
            subscriber_id=101,
            subscriber_name='Bank A',
            status='completed',
        )

        # Read from cache again: Sub 101 is dynamically excluded, Sub 102 remains
        subs_after = get_ungenerated_ready_subscribers('2026-08')
        self.assertEqual(len(subs_after), 1)
        self.assertEqual(subs_after[0]['subscriber_id'], 102)




from django.contrib.auth.models import User
from unupdated.models import UnupdatedBatch, UnupdatedSession


class UnupdatedModelAndApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='password123')

    def test_batch_and_session_creation(self):
        batch = UnupdatedBatch.objects.create(
            user=self.user,
            reporting_month='2026-07',
            last_period_num='20260731',
            cutoff_date='2026-08-01',
        )
        session = UnupdatedSession.objects.create(
            batch=batch,
            subscriber_id=387,
            subscriber_name='Guaranty Trust Bank',
            status='completed',
            consumer_rows=150,
            commercial_rows=50,
        )
        self.assertEqual(batch.sessions.count(), 1)
        self.assertEqual(session.subscriber_name, 'Guaranty Trust Bank')
        self.assertEqual(session.consumer_rows, 150)

    def test_session_readiness_fields(self):
        batch = UnupdatedBatch.objects.create(
            user=self.user,
            reporting_month='2026-08',
            last_period_num='20260831',
            cutoff_date='2026-09-01',
        )
        session = UnupdatedSession.objects.create(
            batch=batch,
            subscriber_id=464,
            subscriber_name='AMCON',
            status='pending_upload',
            is_file_ready=False,
            consumer_max_period='20260731',
            commercial_max_period='20260731',
            error_message='Kindly confirm that the subscriber has uploaded file for August 2026.',
        )
        self.assertEqual(session.status, 'pending_upload')
        self.assertFalse(session.is_file_ready)
        self.assertEqual(session.consumer_max_period, '20260731')
        self.assertEqual(session.commercial_max_period, '20260731')

    @patch('unupdated.tasks.check_subscriber_readiness')
    @patch('unupdated.tasks.extract_unupdated_for_subscriber')
    def test_extract_unupdated_subscriber_task_not_ready_halts(self, mock_extract, mock_readiness):
        from unupdated.tasks import extract_unupdated_subscriber_task

        mock_readiness.return_value = {
            'is_ready': False,
            'consumer_max': '20260731',
            'commercial_max': '20260731',
            'message': 'Kindly confirm that the subscriber has uploaded file for August 2026.',
        }

        batch = UnupdatedBatch.objects.create(
            user=self.user,
            reporting_month='2026-08',
            last_period_num='20260831',
            cutoff_date='2026-09-01',
        )
        session = UnupdatedSession.objects.create(
            batch=batch,
            subscriber_id=464,
            subscriber_name='AMCON',
            status='pending',
        )

        extract_unupdated_subscriber_task(session.id)

        session.refresh_from_db()
        self.assertEqual(session.status, 'pending_upload')
        self.assertFalse(session.is_file_ready)
        self.assertEqual(session.consumer_max_period, '20260731')
        self.assertIn("Kindly confirm that the subscriber has uploaded file", session.error_message)
        mock_extract.assert_not_called()

    @patch('unupdated.tasks.check_subscriber_readiness')
    @patch('unupdated.tasks.extract_unupdated_for_subscriber')
    def test_extract_unupdated_subscriber_task_ready_proceeds(self, mock_extract, mock_readiness):
        from unupdated.tasks import extract_unupdated_subscriber_task

        mock_readiness.return_value = {
            'is_ready': True,
            'consumer_max': '20260831',
            'commercial_max': '20260831',
            'message': 'File verified.',
        }
        mock_extract.return_value = ('/tmp/test_report.xlsx', 50, 10)

        batch = UnupdatedBatch.objects.create(
            user=self.user,
            reporting_month='2026-08',
            last_period_num='20260831',
            cutoff_date='2026-09-01',
        )
        session = UnupdatedSession.objects.create(
            batch=batch,
            subscriber_id=464,
            subscriber_name='AMCON',
            status='pending',
        )

        extract_unupdated_subscriber_task(session.id)

        session.refresh_from_db()
        self.assertEqual(session.status, 'completed')
        self.assertTrue(session.is_file_ready)
        self.assertEqual(session.consumer_rows, 50)
        self.assertEqual(session.commercial_rows, 10)
        mock_extract.assert_called_once()

    @patch('unupdated.views.get_ungenerated_ready_subscribers')
    def test_check_ungenerated_subscribers_api(self, mock_get_ungenerated):
        mock_get_ungenerated.return_value = (
            [
                {
                    'subscriber_id': 101,
                    'subscriber_name': 'First Bank',
                    'consumer_max': '20260831',
                    'commercial_max': '20260831',
                }
            ],
            {'synced_at': '2026-09-10 03:00 PM', 'is_cached': True}
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('unupdated:check_ungenerated_subscribers_api') + '?month=2026-08')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['reporting_month'], '2026-08')
        self.assertEqual(data['last_period_num'], '20260831')
        self.assertEqual(data['subscribers'][0]['subscriber_name'], 'First Bank')
        self.assertEqual(data['last_synced'], '2026-09-10 03:00 PM')
        self.assertTrue(data['is_cached'])
        mock_get_ungenerated.assert_called_once_with(
            reporting_month_str='2026-08',
            include_consumer=True,
            include_commercial=True,
            force_refresh=False,
            return_meta=True,
        )

    @patch('unupdated.views.get_ungenerated_ready_subscribers')
    def test_check_ungenerated_subscribers_api_force_refresh(self, mock_get_ungenerated):
        mock_get_ungenerated.return_value = (
            [],
            {'synced_at': '2026-09-10 03:05 PM', 'is_cached': False}
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('unupdated:check_ungenerated_subscribers_api') + '?month=2026-08&refresh=true')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['is_cached'])
        mock_get_ungenerated.assert_called_once_with(
            reporting_month_str='2026-08',
            include_consumer=True,
            include_commercial=True,
            force_refresh=True,
            return_meta=True,
        )

    @patch('unupdated.views.async_task')
    @patch('unupdated.views.get_ungenerated_ready_subscribers')
    def test_dispatch_ungenerated_batch_all(self, mock_get_ungenerated, mock_async_task):
        mock_get_ungenerated.return_value = [
            {'subscriber_id': 101, 'subscriber_name': 'First Bank'},
            {'subscriber_id': 102, 'subscriber_name': 'Second Bank'},
        ]
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('unupdated:dispatch_ungenerated_batch'),
            {'generate_all': 'true', 'reporting_month': '2026-08'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['enqueued_count'], 2)
        self.assertEqual(mock_async_task.call_count, 2)

        batch = UnupdatedBatch.objects.get(batch_id=data['batch_id'])
        self.assertEqual(batch.sessions.count(), 2)

    @patch('unupdated.views.async_task')
    def test_dispatch_ungenerated_batch_selected(self, mock_async_task):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('unupdated:dispatch_ungenerated_batch'),
            {
                'generate_all': 'false',
                'reporting_month': '2026-08',
                'subscriber_ids': ['101'],
                'subscriber_names': ['First Bank'],
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['enqueued_count'], 1)
        self.assertEqual(mock_async_task.call_count, 1)

    def test_dispatch_ungenerated_batch_empty(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('unupdated:dispatch_ungenerated_batch'),
            {'generate_all': 'false', 'reporting_month': '2026-08'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)

    def test_dispatch_ungenerated_batch_get_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('unupdated:dispatch_ungenerated_batch'))
        self.assertEqual(response.status_code, 405)

    def test_index_page_contains_ungenerated_subscribers_section(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('unupdated:index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Check for Ungenerated Subscribers')
        self.assertContains(response, 'Subscribers that their files are loaded but unupdated not ran')
        self.assertContains(response, 'Resync Bureau')
        self.assertContains(response, 'Generate Unupdated for All')
        self.assertContains(response, 'Generate Selected')
        # Ensure original manual form is intact
        self.assertContains(response, 'unupdatedForm')
        self.assertContains(response, 'subSearch')

