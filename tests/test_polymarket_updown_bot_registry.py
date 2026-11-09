import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import polymarket_updown_bot_registry as registry

ROOT = Path(__file__).resolve().parents[1]
DAY1 = date(2026, 11, 1)
DAY2 = date(2026, 11, 2)
DAY3 = date(2026, 11, 3)


class BotRegistryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript((ROOT / 'sql/btc5m_schema.sql').read_text())
        self.db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) "
                        "AS VALUES ('btc',1,NULL),('eth',1,NULL)")
        for day in (DAY1, DAY2, DAY3):
            for series in ('btc', 'eth'):
                slug = f'{day}-{series}'
                self.db.execute('INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)',
                                (slug, str(day), slug, series, f'{day}T00:00:00Z'))
                self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))
        self.db.commit()
        self.page = 0

    def tearDown(self):
        self.db.close()

    def trades(self, day, wallet, count=91, *, series='btc', start=0, end=5400, side='SELL'):
        self.page += 1
        with self.db:
            for index in range(count):
                timestamp = 1700000000 + start + (end-start)*index//(count-1) if count > 1 else 1700000000+start
                self.db.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f'{day}-{series}', self.page, index, wallet, f'tx-{self.page}-{index}', timestamp,
                     side, 'Up', 0, 'token', '0.5', '1', 500000))

    def test_carry_forward_only_from_first_hit_day(self):
        self.trades(DAY1, 'old-bot')
        self.trades(DAY2, 'old-bot', 1)
        self.trades(DAY3, 'future-bot')
        first_seen = registry.load_first_seen(self.db, DAY3)
        self.assertEqual(first_seen, {'old-bot': str(DAY1), 'future-bot': str(DAY3)})
        self.assertEqual(registry.filter_active(first_seen, DAY2, {'old-bot', 'future-bot', 'human'}), {'old-bot'})
        self.assertEqual(registry.filter_active(first_seen, DAY3, {'old-bot', 'future-bot'}), {'old-bot', 'future-bot'})
        self.assertEqual(registry.load_first_seen(self.db, DAY2), {'old-bot': str(DAY1)})
        self.assertEqual(first_seen.metadata['classification'], 'first_hit_day_onward')
        self.assertFalse(first_seen.metadata['future_labels_applied'])

    def test_daily_all_series_features_include_buy_and_sell(self):
        self.trades(DAY1, 'cross', 46, series='btc', start=0, end=2700, side='BUY')
        self.trades(DAY1, 'cross', 45, series='eth', start=2760, end=5400, side='SELL')
        self.trades(DAY1, 'too-slow', 90)
        self.trades(DAY1, 'too-short', 91, end=5399)
        self.assertEqual(registry.load_first_seen(self.db, DAY1), {'cross': str(DAY1)})

    def test_does_not_merge_multiple_days_to_make_a_new_hit(self):
        self.trades(DAY1, 'not-daily-bot', 46)
        self.trades(DAY2, 'not-daily-bot', 46)
        self.assertEqual(registry.load_first_seen(self.db, DAY2), {})

    def test_cache_reuses_matching_days_and_never_applies_future_cached_days(self):
        self.trades(DAY1, 'old-bot')
        self.trades(DAY3, 'future-bot')
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)/'registry.json'
            registry.load_first_seen(self.db, DAY3, cache_path=cache)
            with patch.object(registry, '_daily_flags', side_effect=AssertionError('unexpected rescan')):
                early = registry.load_first_seen(self.db, DAY1, cache_path=cache)
                latest = registry.load_first_seen(self.db, DAY3, cache_path=cache)
            self.assertEqual(early, {'old-bot': str(DAY1)})
            self.assertEqual(latest['future-bot'], str(DAY3))
            self.assertEqual(early.metadata['through_day_utc'], str(DAY1))

    def test_cache_invalidates_changed_source_day_and_rule_version(self):
        self.trades(DAY1, 'old-bot')
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)/'registry.json'
            registry.load_first_seen(self.db, DAY3, cache_path=cache)
            with self.db:
                self.db.execute("UPDATE collection_progress SET last_error='recollected' WHERE market_slug=?", (f'{DAY1}-btc',))
            with patch.object(registry, '_daily_flags', wraps=registry._daily_flags) as calculate:
                registry.load_first_seen(self.db, DAY3, cache_path=cache)
                self.assertEqual(calculate.call_count, 1)
            stored = json.loads(cache.read_text())
            stored['rule']['max_mean_interval_seconds'] = 999
            cache.write_text(json.dumps(stored))
            with patch.object(registry, '_daily_flags', wraps=registry._daily_flags) as calculate:
                registry.load_first_seen(self.db, DAY3, cache_path=cache)
                self.assertEqual(calculate.call_count, 3)

    def test_missing_or_incomplete_history_is_explicit_observed_coverage(self):
        self.trades(DAY1, 'observed')
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug=?", (f'{DAY1}-btc',))
            self.db.execute('DELETE FROM collection_progress WHERE market_slug LIKE ?', (f'{DAY2}-%',))
            self.db.execute('DELETE FROM markets WHERE date(end_time)=?', (str(DAY2),))
        result = registry.load_first_seen(self.db, DAY3)
        self.assertEqual(result, {'observed': str(DAY1)})
        self.assertEqual(result.metadata['history_start_utc'], str(DAY1))
        self.assertEqual(result.metadata['incomplete_history_days'], [str(DAY1), str(DAY2)])
        self.assertEqual(result.metadata['missing_market_days'], [str(DAY2)])
        self.assertEqual(result.metadata['first_seen_semantics'], 'first_observed_daily_rule_hit')

    def test_readonly_source_unchanged_and_default_cache_is_derived(self):
        self.trades(DAY1, 'bot')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/'source.sqlite3'
            writer = sqlite3.connect(path)
            self.db.backup(writer)
            writer.close()
            before = path.read_bytes()
            reader = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
            queries = []
            reader.set_trace_callback(queries.append)
            progress = []
            try:
                with patch.object(registry, 'ROOT', root):
                    result = registry.load_first_seen(reader, DAY3, progress=progress.append)
                self.assertEqual(result['bot'], str(DAY1))
                self.assertEqual(before, path.read_bytes())
                self.assertFalse(reader.in_transaction)
                self.assertTrue(all(query.lstrip().upper().startswith(('SELECT', '-- PRAGMA DATABASE_LIST'))
                                    for query in queries))
                self.assertEqual(len(progress), 3)
                self.assertEqual(len(list((root/'outputs/statistics').glob('*.json'))), 1)
                with self.assertRaises(ValueError):
                    registry.load_first_seen(reader, DAY1, cache_path=path)
            finally:
                reader.close()

    def test_outer_transaction_and_mid_read_change_rejected(self):
        self.db.execute('BEGIN')
        with self.assertRaisesRegex(ValueError, '外层数据库事务'):
            registry.load_first_seen(self.db, DAY1)
        self.db.rollback()
        original = registry.source._snapshot
        calls = 0
        def changed(db, day):
            nonlocal calls
            snapshot = original(db, day)
            calls += 1
            return snapshot if calls == 1 else (snapshot[0], snapshot[1]+(('changed',),))
        with patch.object(registry.source, '_snapshot', side_effect=changed):
            with self.assertRaises(registry.source.DataChangedError):
                registry.load_first_seen(self.db, DAY1)

    def test_change_to_earlier_day_during_later_read_rejected(self):
        original = registry._daily_flags
        def changed(db, snapshot):
            result = original(db, snapshot)
            if snapshot[1][0][0].startswith(str(DAY3)):
                with db:
                    db.execute("UPDATE collection_progress SET last_error='changed' WHERE market_slug=?", (f'{DAY1}-btc',))
            return result
        with patch.object(registry, '_daily_flags', side_effect=changed):
            with self.assertRaises(registry.source.DataChangedError):
                registry.load_first_seen(self.db, DAY3)

    def test_before_available_history_empty_without_fabricated_start(self):
        result = registry.load_first_seen(self.db, date(2026, 10, 31))
        self.assertEqual(result, {})
        self.assertIsNone(result.metadata['history_start_utc'])
        self.assertEqual(result.metadata['observed_day_count'], 0)


if __name__ == '__main__':
    unittest.main()
