"""分别检验市场单边金额、钱包双方活动，以及新旧数据过渡。"""

import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_poster as daily
import polymarket_updown_period as period


class VolumeReportingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE updown_coverage(date_utc TEXT,is_complete INTEGER,completed_markets INTEGER);
            CREATE TABLE updown_target_series(series_slug TEXT);
            CREATE TABLE markets(slug TEXT PRIMARY KEY,series_slug TEXT,end_time TEXT);
            CREATE TABLE trades(market_slug TEXT,wallet TEXT,timestamp INTEGER,amount_micro_usdc INTEGER);
            CREATE TABLE market_volume(market_slug TEXT PRIMARY KEY,amount_micro_usdc INTEGER,completed_at TEXT);
            INSERT INTO updown_target_series VALUES ('btc-up-or-down-5m');
            INSERT INTO updown_coverage VALUES ('2026-10-04',1,1),('2026-10-05',1,1);
            INSERT INTO markets VALUES ('old','btc-up-or-down-5m','2026-10-04T00:00:00Z'),
                                       ('new','btc-up-or-down-5m','2026-10-05T00:00:00Z');
            INSERT INTO trades VALUES ('old','a',0,1000000),
                                      ('new','a',0,4995000),('new','b',0,4995000);
            INSERT INTO market_volume VALUES ('new',4995000,'done');
        """)

    def tearDown(self):
        self.db.close()

    def test_same_fill_counts_once_but_keeps_both_wallets(self):
        data = daily.load_data(self.db, date(2026, 10, 5))
        self.assertEqual(data.current['volume_micro_usdc'], 4_995_000)
        self.assertEqual(data.current['wallet_volume_micro_usdc'], 9_990_000)
        self.assertEqual(data.current['unique_wallets'], 2)
        self.assertEqual(data.current['volume_basis'], 'taker_only')
        self.assertIsNone(data.previous['volume_micro_usdc'])
        values = daily.display_values(data)
        self.assertEqual(values['volume_change'], 'N/A')
        self.assertEqual(values['wallet_change'], '+100.00%')

    def test_partial_period_does_not_mix_or_sum_only_available_days(self):
        result = period.load_period(self.db, date(2026, 10, 4), date(2026, 10, 6))
        self.assertIsNone(result['volume_micro_usdc'])
        self.assertIsNone(result['asset_volume_micro_usdc'])
        self.assertEqual(result['daily_volume_micro_usdc'], {'2026-10-04': None, '2026-10-05': 4_995_000})
        self.assertEqual(result['wallet_volume_micro_usdc'], 10_990_000)
        self.assertEqual(result['unique_wallets'], 2)

    def test_incomplete_page_is_missing_and_completed_empty_market_is_zero(self):
        self.db.execute("UPDATE market_volume SET completed_at=NULL")
        self.assertIsNone(daily.load_data(self.db, date(2026, 10, 5)).current['volume_micro_usdc'])
        self.db.execute("UPDATE market_volume SET amount_micro_usdc=0,completed_at='done'")
        self.assertEqual(daily.load_data(self.db, date(2026, 10, 5)).current['volume_micro_usdc'], 0)

    def test_legacy_database_without_new_table_remains_readable(self):
        self.db.execute('DROP TABLE market_volume')
        data = daily.load_data(self.db, date(2026, 10, 5))
        self.assertIsNone(data.current['volume_micro_usdc'])
        self.assertEqual(data.current['wallet_volume_micro_usdc'], 9_990_000)
        self.assertEqual(daily.display_values(data)['volume'], 'N/A')

    def test_bot_share_uses_wallet_denominator_even_when_market_data_missing(self):
        self.db.execute('DELETE FROM trades')
        self.db.executemany("INSERT INTO trades VALUES ('new','bot',?,1000000)",
                            [(i * 60,) for i in range(91)])
        self.db.execute("INSERT INTO trades VALUES ('new','human',0,9000000)")
        self.db.execute("DELETE FROM market_volume")
        data = daily.load_data(self.db, date(2026, 10, 5))
        self.assertEqual(data.current['suspected_bot_wallets'], 1)
        self.assertEqual(data.current['wallet_volume_micro_usdc'], 100_000_000)
        self.assertEqual(daily.display_values(data)['bot_volume_share'], '91.00%')
        result = period.load_period(self.db, date(2026, 10, 5), date(2026, 10, 6))
        self.assertEqual(result['suspected_bot_volume_micro_usdc'], 91_000_000)
        self.assertEqual(period.bot_share_change(result, result, 'week')[0], '0.00 pp')


if __name__ == '__main__':
    unittest.main()
