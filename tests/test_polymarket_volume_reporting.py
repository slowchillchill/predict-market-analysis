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
from polymarket_market_volume_query import load_market_volume

ROOT = Path(__file__).resolve().parents[1]


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

    def test_declared_complete_empty_legacy_day_is_zero_but_missing_day_is_not(self):
        self.db.execute("INSERT INTO updown_coverage VALUES ('2026-10-06',1,0)")
        empty = load_market_volume(self.db, date(2026, 10, 6), date(2026, 10, 7))
        self.assertEqual(empty['volume_micro_usdc'], 0)
        self.assertEqual(empty['daily_volume_micro_usdc'], {'2026-10-06': 0})
        missing = load_market_volume(self.db, date(2026, 10, 6), date(2026, 10, 8))
        self.assertEqual(missing['daily_volume_micro_usdc'], {'2026-10-06': 0, '2026-10-07': None})
        self.assertIsNone(missing['volume_micro_usdc'])

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


class DerivedVolumeReportingTests(unittest.TestCase):
    """新原始格式无金额实体表，旧哈希格式仍可只读统计或作 API 对照。"""

    DAY = date(2026, 10, 5)
    END = date(2026, 10, 6)

    def database(self, *, legacy=False):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        schema = ROOT / ("tests/fixtures/btc5m_legacy_schema.sql" if legacy else "sql/btc5m_schema.sql")
        db.executescript(schema.read_text())
        db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) AS "
                   "VALUES ('btc-up-or-down-daily',1,NULL),('eth-up-or-down-daily',1,NULL)")
        views = (ROOT / "sql/updown_daily.sql").read_text()
        db.executescript(views.replace("group_id", "transaction_hash") if legacy else views)
        with db:
            for coin in ("btc", "eth"):
                db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)",
                    (coin, "2026-10-04", coin, f"{coin}-up-or-down-daily", "2026-10-05T00:00:00Z"))
                db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (coin,))
        return db

    def add_pairs(self, db, *, legacy=False):
        with db:
            # 组号相同但市场不同；互补 BUY 的现金金额不能通过双方合计除二得到。
            for slug, row, wallet, side, outcome, price, amount in [
                ("btc", 0, "anchor", "BUY", "Down", ".8", 8000000),
                ("btc", 1, "maker", "BUY", "Up", ".2", 2000000),
                ("eth", 0, "anchor", "BUY", "Up", ".3", 3000000),
                ("eth", 1, "maker", "SELL", "Up", ".3", 3000000),
            ]:
                db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, 1, row, wallet, "same-hash" if legacy else 1, 1700000000,
                     side, outcome, 0 if outcome == "Up" else 1, "token", price, "10", amount))

    def test_group_first_amount_preserves_market_scope_and_differs_from_wallet_half(self):
        db = self.database()
        self.add_pairs(db)
        result = load_market_volume(db, self.DAY, self.END)
        self.assertEqual(result["volume_micro_usdc"], 11000000)
        self.assertEqual(result["asset_volume_micro_usdc"], {"BTC": 8000000, "ETH": 3000000})
        self.assertEqual(db.execute("SELECT SUM(amount_micro_usdc) FROM trades").fetchone()[0], 16000000)
        self.assertEqual(db.execute("SELECT market_volume_micro_usdc FROM updown_market_volume_daily").fetchone()[0], 11000000)
        self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone())
        self.assertNotIn("market_volume_is_complete", dict(db.execute("SELECT * FROM updown_coverage").fetchone()))

    def test_incomplete_wallet_day_is_missing_and_complete_empty_day_is_zero(self):
        db = self.database()
        self.assertEqual(load_market_volume(db, self.DAY, self.END)["volume_micro_usdc"], 0)
        with db:
            db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='eth'")
        result = load_market_volume(db, self.DAY, self.END)
        self.assertIsNone(result["volume_micro_usdc"])
        self.assertIsNone(result["asset_volume_micro_usdc"])
        self.assertIsNone(db.execute("SELECT market_volume_micro_usdc FROM updown_market_volume_daily").fetchone()[0])

    def test_new_group_id_format_ignores_any_leftover_api_cache(self):
        db = self.database()
        self.add_pairs(db)
        db.execute("CREATE TABLE market_volume(market_slug TEXT,amount_micro_usdc INTEGER,completed_at TEXT)")
        db.executemany("INSERT INTO market_volume VALUES (?,999999999,'done')", [("btc",), ("eth",)])
        self.assertEqual(load_market_volume(db, self.DAY, self.END)["volume_micro_usdc"], 11000000)

    def test_old_hash_without_api_table_derives_same_amount(self):
        old = self.database(legacy=True)
        new = self.database()
        self.add_pairs(old, legacy=True)
        self.add_pairs(new)
        self.assertEqual(load_market_volume(old, self.DAY, self.END), load_market_volume(new, self.DAY, self.END))

    def test_old_hash_with_api_table_preserves_reference_amount(self):
        db = self.database(legacy=True)
        self.add_pairs(db, legacy=True)
        db.execute("CREATE TABLE market_volume(market_slug TEXT,amount_micro_usdc INTEGER,completed_at TEXT)")
        db.executemany("INSERT INTO market_volume VALUES (?,?,'done')", [("btc", 8000001), ("eth", 3000000)])
        self.assertEqual(load_market_volume(db, self.DAY, self.END)["volume_micro_usdc"], 11000001)

    def test_derived_amount_uses_first_page_row_and_existing_micro_rounding(self):
        db = self.database()
        with db:
            # 插入顺序故意倒置；首行金额保持已采样的666667微USDC，
            # 不改为明细分别舍入后的333333+333333。
            for page, row, amount, size in [(2, 0, 333333, "1"), (1, 0, 666667, "2"), (2, 1, 333333, "1")]:
                db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("btc", page, row, "wallet", 1, 1700000000, "BUY", "Up", 0,
                     "token", ".3333334", size, amount))
        self.assertEqual(load_market_volume(db, self.DAY, self.END)["volume_micro_usdc"], 666667)
        self.assertEqual(db.execute("SELECT market_volume_micro_usdc FROM updown_market_volume_daily").fetchone()[0], 666667)

    def test_missing_calendar_day_is_not_filled_with_zero(self):
        db = self.database()
        result = load_market_volume(db, self.DAY, date(2026, 10, 7))
        self.assertEqual(result["daily_volume_micro_usdc"], {"2026-10-05": 0, "2026-10-06": None})
        self.assertIsNone(result["volume_micro_usdc"])


if __name__ == '__main__':
    unittest.main()
