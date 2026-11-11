"""Recovery scenarios: cloud snapshots survive a new process, local work does not."""

import contextlib
import io
import json
import shutil
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import test_polymarket_kaggle_pipeline as fixtures

producer = fixtures.producer


class PowerLoss(BaseException):
    pass


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    quiet = fixtures.PipelineTests.quiet
    seed_history = fixtures.PipelineTests.seed_history
    latest_files = fixtures.PipelineTests.latest_files
    inspect_raw = fixtures.PipelineTests.inspect_raw
    assert_complete_raw_shape = fixtures.PipelineTests.assert_complete_raw_shape

    def test_collection_deadline_leaves_publication_time(self):
        self.assertEqual(self.pipeline.client.deadline, self.pipeline.stop_at)
        self.assertEqual(self.store.deadline, self.pipeline.deadline)
        self.assertLess(self.pipeline.client.deadline, self.store.deadline)

    def test_existing_reconstruction_gap_keeps_records_and_continues_next_historical_day(self):
        from polymarket_updown_price_distribution import InvalidReconstruction
        first = fixtures.YESTERDAY - timedelta(days=2)
        second = first + timedelta(days=1)
        self.seed_history(first)
        self.pipeline.state['history']['target_end_date_utc'] = str(second)
        self.pipeline.state['raw_days'][str(fixtures.YESTERDAY)] = {'status': 'complete'}
        export = producer.analysis.export_day

        def checked(path, day, *args, **kwargs):
            if day == first:
                raise InvalidReconstruction('Existing reconstruction condition not satisfied')
            return export(path, day, *args, **kwargs)

        with patch.object(producer.analysis, 'export_day', side_effect=checked), self.quiet():
            self.pipeline.run(max_days=2)
        first_raw = json.loads(self.latest_files(f'pm-updown-raw-{first}')['collection_status.json'])
        self.assertEqual(first_raw['coverage']['is_complete'], 1)
        self.assertEqual(first_raw['status'], 'incomplete')
        self.assertIn('InvalidReconstruction', first_raw['error'])
        self.assertIn(str(first), self.pipeline.state['history']['gaps'])
        files = self.latest_files('pm-updown-analysis-2026-11')
        self.assertNotIn(f'daily_{first}.json', files)
        self.assertEqual(json.loads(files[f'daily_{second}.json'])['status'], 'complete')

    def elapsed_clock(self, *, stop=100, checkpoint=10):
        elapsed = [0.0]
        self.pipeline.clock = lambda: elapsed[0]
        self.pipeline.stop_at = stop
        self.pipeline.deadline = stop + 100
        self.pipeline.checkpoint_seconds = checkpoint
        self.pipeline._last_checkpoint = 0
        return elapsed

    def test_periodic_checkpoint_survives_entire_session_loss_and_keeps_group_ids(self):
        day = fixtures.YESTERDAY - timedelta(days=1)
        self.seed_history(day)
        self.pipeline.attempted.add(str(fixtures.YESTERDAY))
        elapsed = self.elapsed_clock()
        original = self.client.get
        work = self.root / 'work/raw' / str(day) / 'data.sqlite3'

        def paged(url, params):
            if url == producer.core.TRADES_URL and params.get('cursor'):
                self.assertTrue(work.exists())
                self.assertEqual(self.pipeline.state['history']['next_date_utc'], str(day))
                self.assertEqual(self.store.version(f'pm-updown-raw-{day}'), 1)
                raise PowerLoss()
            payload = original(url, params)
            if url == producer.core.TRADES_URL:
                payload['data'] = payload['data'][:1]
                payload['pagination']['next_cursor'] = 'page-2'
                elapsed[0] = 11
            return payload

        self.client.get = paged
        with self.quiet(), self.assertRaises(PowerLoss):
            self.pipeline.process_day(day, historical=True)
        slug = f'pm-updown-raw-{day}'
        files = self.latest_files(slug)
        self.assertEqual(json.loads(files['collection_status.json'])['status'], 'incomplete')
        with contextlib.closing(self.inspect_raw(files)) as db:
            self.assertEqual(db.execute('SELECT group_id FROM trades').fetchall(), [(1,)])
            self.assertEqual(db.execute('SELECT transaction_hash,group_id FROM pending_group_map').fetchall(), [('pair', 1)])
            self.assertEqual(db.execute('SELECT next_cursor FROM collection_progress').fetchone()[0], 'page-2')
        self.assertNotIn(f'daily_{day}.json', self.latest_files('pm-updown-analysis-2026-11'))
        shutil.rmtree(self.root / 'work')

        def finish(url, params):
            payload = original(url, params)
            if url == producer.core.TRADES_URL:
                self.assertEqual(params.get('cursor'), 'page-2')
                payload['data'] = payload['data'][1:]
            return payload

        self.client.get = finish
        resumed = producer.Pipeline(self.store, self.root / 'new-session', today=lambda: fixtures.TODAY,
                                    client=self.client)
        resumed.attempted.add(str(fixtures.YESTERDAY))
        with self.quiet():
            resumed.process_day(day, historical=True)
        self.assert_complete_raw_shape(self.latest_files(slug))
        with contextlib.closing(self.inspect_raw(self.latest_files(slug))) as db:
            self.assertEqual(db.execute('SELECT group_id FROM trades ORDER BY page_number,row_number').fetchall(), [(1,), (1,)])
        self.assertEqual(self.client.trades, [(str(day), 'false', None), (str(day), 'false', 'page-2')])

    def test_unchanged_active_checkpoint_does_not_publish_another_version(self):
        day = fixtures.YESTERDAY
        folder = self.root / 'work/raw' / str(day)
        db = producer.collector.connect_database(folder / 'data.sqlite3')
        result = {'discovery_errors': [], 'structural_errors': []}
        self.pipeline._active_day = {'day': day, 'db': db, 'result': result,
                                    'last_signature': (db.total_changes, json.dumps(result, sort_keys=True))}
        try:
            self.assertFalse(self.pipeline._checkpoint_active())
            producer.collector.discover_series(db, self.client, fixtures.SERIES, day, fixtures.TODAY)
            self.assertTrue(self.pipeline._checkpoint_active())
            publications = len(self.store.published)
            self.assertFalse(self.pipeline._checkpoint_active())
            self.assertEqual(len(self.store.published), publications)
            self.assertTrue((folder / 'data.sqlite3').exists())
            self.assertIsNone(db.execute('SELECT completed_at FROM collection_progress').fetchone()[0])
        finally:
            self.pipeline._active_day = None
            db.close()

    def test_soft_deadline_publishes_raw_without_starting_statistics(self):
        elapsed = self.elapsed_clock(stop=10, checkpoint=100)
        self.client.after_wallet = lambda _: elapsed.__setitem__(0, 10)
        with patch.object(producer.analysis, 'extract_day_features') as stats, self.quiet(), \
                self.assertRaises(producer.core.CollectionPaused):
            self.pipeline.process_day(fixtures.YESTERDAY)
        stats.assert_not_called()
        raw = json.loads(self.latest_files(f'pm-updown-raw-{fixtures.YESTERDAY}')['collection_status.json'])
        self.assertEqual(raw['status'], 'incomplete')
        self.assertEqual(raw['coverage']['completed_markets'], 1)
        self.assertTrue(self.pipeline.stopping)
        self.assertNotIn('index.json', self.latest_files('pm-updown-analysis-2026-11'))

    def test_repeated_save_without_state_change_does_not_publish(self):
        self.pipeline.save()
        count = len(self.store.published)
        self.pipeline.save()
        self.pipeline.save()
        self.assertEqual(len(self.store.published), count)
        self.pipeline.state['history_metadata'] = {'history_pending': True}
        self.pipeline.save()
        self.assertEqual(len(self.store.published), count + 1)

    def test_incomplete_day_publishes_monthly_progress_only_once(self):
        self.client.wallet_error = producer.core.FetchError('HTTP 400', status_code=400)
        with self.quiet(), contextlib.redirect_stderr(io.StringIO()):
            self.pipeline.process_day(fixtures.YESTERDAY)
        self.assertEqual(sum(slug.startswith('pm-updown-analysis-') for slug, _, _ in self.store.published), 1)

    def test_normal_history_return_across_midnight_still_collects_new_day(self):
        self.seed_history(fixtures.YESTERDAY - timedelta(days=2))
        self.pipeline.state['history']['next_date_utc'] = str(fixtures.YESTERDAY)
        self.pipeline.state['raw_days'][str(fixtures.YESTERDAY)] = {'status': 'complete'}
        calls = []

        def discovery():
            calls.append(True)
            self.clock[0] = fixtures.TODAY + timedelta(days=1)

        with patch.object(self.pipeline, 'discover_history', side_effect=discovery), self.quiet():
            self.pipeline.run(max_days=1)
        self.assertEqual(self.pipeline.state['raw_days'][str(fixtures.TODAY)]['status'], 'complete')
        self.assertTrue(calls)

    def test_no_work_save_crossing_midnight_rechecks_fresh_day(self):
        self.seed_history(fixtures.YESTERDAY - timedelta(days=2))
        self.pipeline.state['history']['next_date_utc'] = str(fixtures.YESTERDAY)
        self.pipeline.state['raw_days'][str(fixtures.YESTERDAY)] = {'status': 'complete'}
        original = self.pipeline.save
        moved = []

        def save():
            original()
            if not moved:
                self.clock[0] = fixtures.TODAY + timedelta(days=1)
                moved.append(True)

        with patch.object(self.pipeline, 'save', side_effect=save), self.quiet():
            self.pipeline.run(max_days=1)
        self.assertEqual(self.pipeline.state['raw_days'][str(fixtures.TODAY)]['status'], 'complete')

    def test_repair_queue_runs_before_forward_scan_without_rewinding_cursor(self):
        old = fixtures.YESTERDAY - timedelta(days=4)
        cursor = fixtures.YESTERDAY - timedelta(days=1)
        self.seed_history(old)
        self.pipeline.state['history'].update(next_date_utc=str(cursor), target_end_date_utc=str(cursor),
                                              gaps={str(old): 'metadata format'})
        self.pipeline.state['repair_queue'] = [str(old)]
        self.pipeline.state['raw_days'][str(fixtures.YESTERDAY)] = {'status': 'complete'}
        with self.quiet():
            self.pipeline.run(max_days=1)
        self.assertEqual(self.pipeline.state['raw_days'][str(old)]['status'], 'complete')
        self.assertEqual(self.pipeline.state['history']['next_date_utc'], str(cursor))
        self.assertEqual(self.pipeline.state['repair_queue'], [])

    def test_structural_metadata_error_is_saved_and_does_not_advance_history(self):
        day = fixtures.YESTERDAY - timedelta(days=1)
        self.seed_history(day)
        self.pipeline.attempted.add(str(fixtures.YESTERDAY))
        original = self.client.get

        def bad(url, params):
            if url.startswith(producer.collector.GAMMA_EVENT_SLUG):
                raise producer.core.FetchError('HTTP 404', status_code=404)
            payload = original(url, params)
            if url == producer.collector.GAMMA_EVENTS_KEYSET:
                del payload['events'][0]['markets'][0]['conditionId']
            return payload

        self.client.get = bad
        with self.quiet(), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(producer.DiscoveryBlocked):
            self.pipeline.process_day(day, historical=True)
        self.assertEqual(self.pipeline.state['history']['next_date_utc'], str(day))
        raw = json.loads(self.latest_files(f'pm-updown-raw-{day}')['collection_status.json'])
        self.assertEqual(raw['status'], 'incomplete')
        self.assertTrue(raw['structural_errors'])
        self.assertIsNotNone(raw['error'])
        self.assertNotIn('index.json', self.latest_files('pm-updown-analysis-2026-11'))

    def test_uncertain_checkpoint_failure_does_not_blindly_publish_again(self):
        elapsed = self.elapsed_clock()
        self.client.after_wallet = lambda _: elapsed.__setitem__(0, 11)
        original = self.store.publish
        calls = []

        def publish(slug, directory, **kwargs):
            if slug.startswith('pm-updown-raw-'):
                calls.append(slug)
                raise producer.KaggleError('Publication outcome remains unknown')
            return original(slug, directory, **kwargs)

        self.store.publish = publish
        with self.quiet(), self.assertRaises(producer.KaggleError):
            self.pipeline.process_day(fixtures.YESTERDAY)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.root / 'work/raw' / str(fixtures.YESTERDAY) / 'data.sqlite3').exists())
        self.assertNotIn(str(fixtures.YESTERDAY), self.pipeline.state['raw_days'])

    def test_setup_elapsed_time_is_included_before_collection(self):
        pipeline = producer.Pipeline(self.store, self.root / 'late-start', today=lambda: fixtures.TODAY,
                                     client=self.client, clock=lambda: 15, started_at=0,
                                     stop_after_seconds=10, session_seconds=20)
        with self.quiet():
            pipeline.run()
        self.assertTrue(pipeline.stopping)
        self.assertEqual(self.client.trades, [])

    def test_incomplete_legacy_progress_builds_one_time_repair_queue(self):
        old = fixtures.YESTERDAY - timedelta(days=4)
        cursor = fixtures.YESTERDAY - timedelta(days=1)
        self.seed_history(old)
        self.pipeline.state['history'].update(next_date_utc=str(cursor), target_end_date_utc=str(cursor),
                                              gaps={str(old): 'old failure'})
        self.pipeline.state['raw_days'][str(old)] = {'status': 'incomplete'}
        self.pipeline.state.pop('recovery_schema_version')
        self.pipeline.save()
        resumed = producer.Pipeline(self.store, self.root / 'v3-resumed', today=lambda: fixtures.TODAY,
                                    client=self.client)
        self.assertEqual(resumed.state['repair_queue'], [str(old), str(cursor)])
        resumed.state['repair_queue'].clear()
        resumed.save()
        again = producer.Pipeline(self.store, self.root / 'again', today=lambda: fixtures.TODAY,
                                  client=self.client)
        self.assertEqual(again.state['repair_queue'], [])


if __name__ == '__main__':
    unittest.main()
