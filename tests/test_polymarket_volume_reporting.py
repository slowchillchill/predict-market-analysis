"""分别检验市场单边金额、钱包 BUY 金额及原活动特征保留。"""

import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_poster as daily
import polymarket_updown_period as period


class VolumeReportingTests(unittest.TestCase):
    def setUp(self):
        registry = patch('polymarket_updown_bot_registry.load_first_seen', return_value={'bot':'2026-10-05'})
        registry.start()
        self.addCleanup(registry.stop)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE updown_coverage(date_utc TEXT,is_complete INTEGER,completed_markets INTEGER);
            CREATE TABLE updown_target_series(series_slug TEXT);
            CREATE TABLE markets(slug TEXT PRIMARY KEY,series_slug TEXT,end_time TEXT);
            CREATE TABLE trades(market_slug TEXT,wallet TEXT,timestamp INTEGER,side TEXT,amount_micro_usdc INTEGER);
            CREATE TABLE market_volume(market_slug TEXT PRIMARY KEY,amount_micro_usdc INTEGER,completed_at TEXT);
            INSERT INTO updown_target_series VALUES ('btc-up-or-down-5m');
            INSERT INTO updown_coverage VALUES ('2026-10-04',1,1),('2026-10-05',1,1);
            INSERT INTO markets VALUES ('old','btc-up-or-down-5m','2026-10-04T00:00:00Z'),
                                       ('new','btc-up-or-down-5m','2026-10-05T00:00:00Z');
            INSERT INTO trades VALUES ('old','a',0,'BUY',1000000),
                                      ('new','a',0,'BUY',4995000),('new','b',0,'SELL',4995000);
            INSERT INTO market_volume VALUES ('new',4995000,'done');
        """)

    def tearDown(self):
        self.db.close()

    def test_same_fill_counts_once_but_keeps_both_wallets(self):
        data = daily.load_data(self.db, date(2026, 10, 5))
        self.assertEqual(data.current['volume_micro_usdc'], 4_995_000)
        self.assertEqual(data.current['wallet_volume_micro_usdc'], 4_995_000)
        self.assertEqual(data.current['unique_wallets'], 2)
        self.assertEqual(data.current['volume_basis'], 'taker_only')
        self.assertEqual(data.current['wallet_volume_basis'], 'wallet_buys_only')
        self.assertEqual(data.current['bot_volume_basis'], 'wallet_buys_only')
        self.assertIsNone(data.previous['volume_micro_usdc'])
        values = daily.display_values(data)
        self.assertEqual(values['volume_change'], 'N/A')
        self.assertEqual(values['wallet_change'], '+100.00%')

    def test_partial_period_does_not_mix_or_sum_only_available_days(self):
        result = period.load_period(self.db, date(2026, 10, 4), date(2026, 10, 6))
        self.assertIsNone(result['volume_micro_usdc'])
        self.assertIsNone(result['asset_volume_micro_usdc'])
        self.assertEqual(result['daily_volume_micro_usdc'], {'2026-10-04': None, '2026-10-05': 4_995_000})
        self.assertEqual(result['wallet_volume_micro_usdc'], 5_995_000)
        self.assertEqual(result['unique_wallets'], 2)
        self.assertEqual(result['wallet_volume_basis'], 'wallet_buys_only')
        self.assertEqual(result['bot_volume_basis'], 'wallet_buys_only')

    def test_incomplete_page_is_missing_and_completed_empty_market_is_zero(self):
        self.db.execute("UPDATE market_volume SET completed_at=NULL")
        self.assertIsNone(daily.load_data(self.db, date(2026, 10, 5)).current['volume_micro_usdc'])
        self.db.execute("UPDATE market_volume SET amount_micro_usdc=0,completed_at='done'")
        self.assertEqual(daily.load_data(self.db, date(2026, 10, 5)).current['volume_micro_usdc'], 0)

    def test_legacy_database_without_new_table_remains_readable(self):
        self.db.execute('DROP TABLE market_volume')
        data = daily.load_data(self.db, date(2026, 10, 5))
        self.assertIsNone(data.current['volume_micro_usdc'])
        self.assertEqual(data.current['wallet_volume_micro_usdc'], 4_995_000)
        self.assertEqual(daily.display_values(data)['volume'], 'N/A')

    def test_bot_share_uses_wallet_denominator_even_when_market_data_missing(self):
        self.db.execute('DELETE FROM trades')
        self.db.executemany("INSERT INTO trades VALUES ('new','bot',?,'BUY',1000000)",
                            [(i * 60,) for i in range(91)])
        self.db.execute("INSERT INTO trades VALUES ('new','human',0,'BUY',9000000)")
        self.db.execute("INSERT INTO trades VALUES ('new','bot',6000,'SELL',700000000)")
        self.db.execute("INSERT INTO trades VALUES ('new','human',6000,'SELL',900000000)")
        self.db.execute("DELETE FROM market_volume")
        data = daily.load_data(self.db, date(2026, 10, 5))
        self.assertEqual(data.current['suspected_bot_wallets'], 1)
        self.assertEqual(data.current['wallet_volume_micro_usdc'], 100_000_000)
        self.assertEqual(daily.display_values(data)['bot_volume_share'], '91.00%')
        result = period.load_period(self.db, date(2026, 10, 5), date(2026, 10, 6))
        self.assertEqual(result['suspected_bot_volume_micro_usdc'], 91_000_000)
        self.assertEqual(period.bot_share_change(result, result, 'week')[0], '0.00 pp')

    def test_sell_rows_keep_wallets_and_features_without_entering_buy_amounts(self):
        self.db.execute('DELETE FROM trades')
        self.db.executemany('INSERT INTO trades VALUES (?,?,?,?,?)', [
            ('new', 'bot', 0, 'BUY', 1_000_000),
            ('new', 'bot', 5400, 'SELL', 9_000_000),
            ('new', 'sell-only-bot', 1, 'SELL', 80_000_000),
            ('new', 'human', 0, 'BUY', 4_000_000),
            ('new', 'human', 5400, 'SELL', 20_000_000),
        ])
        labels = {'bot': '2026-10-05', 'sell-only-bot': '2026-10-05'}
        result = daily.load_data(self.db, date(2026, 10, 5), bot_first_seen=labels).current
        self.assertEqual(result['unique_wallets'], 3)
        self.assertEqual(result['suspected_bot_wallets'], 2)
        self.assertEqual(result['wallet_volume_micro_usdc'], 5_000_000)
        self.assertEqual(result['suspected_bot_volume_micro_usdc'], 1_000_000)
        period_result = period.load_period(self.db, date(2026, 10, 5), date(2026, 10, 6), bot_first_seen=labels)
        self.assertEqual(period_result['unique_wallets'], 3)
        self.assertEqual(period_result['suspected_bot_wallets'], 2)
        self.assertEqual(period_result['wallet_volume_micro_usdc'], 5_000_000)
        self.assertEqual(period_result['suspected_bot_volume_micro_usdc'], 1_000_000)
        query = (daily.ROOT / 'sql/updown_period.sql').read_text()
        features = {r['wallet']: dict(r) for r in self.db.execute(query, {'start': '2026-10-05', 'end': '2026-10-06'})}
        self.assertEqual(features['bot']['trade_count'], 2)
        self.assertEqual((features['bot']['first_timestamp'], features['bot']['last_timestamp']), (0, 5400))
        self.assertEqual(features['bot']['amount_micro_usdc'], 1_000_000)

    def test_sell_only_day_has_zero_buy_amount_and_undefined_buy_share(self):
        self.db.execute('DELETE FROM trades')
        self.db.executemany('INSERT INTO trades VALUES (?,?,?,?,?)', [
            ('new', 'bot', 0, 'SELL', 2_000_000),
            ('new', 'human', 1, 'SELL', 3_000_000),
        ])
        result = daily.load_data(self.db, date(2026, 10, 5)).current
        self.assertEqual(result['unique_wallets'], 2)
        self.assertEqual(result['suspected_bot_wallets'], 1)
        self.assertEqual(result['wallet_volume_micro_usdc'], 0)
        self.assertEqual(result['suspected_bot_volume_micro_usdc'], 0)
        self.assertEqual(daily.percent_text(result['suspected_bot_volume_micro_usdc'], result['wallet_volume_micro_usdc']), 'N/A')


if __name__ == '__main__':
    unittest.main()
