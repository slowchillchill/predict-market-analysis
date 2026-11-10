import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from decimal import localcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_bot_price as prices


DAY = date(2026, 11, 7)
ROOT = Path(__file__).resolve().parents[1]


class BotPriceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        # Preserve the hash-based fixture to cover read-only legacy statistics.
        self.db.executescript((ROOT / "tests/fixtures/btc5m_legacy_schema.sql").read_text())
        self.db.execute("CREATE VIEW updown_target_series(series_slug, expected_markets, first_end_time) "
                        "AS VALUES ('btc',1,NULL), ('eth',1,NULL)")
        for slug, series in (("m-btc", "btc"), ("m-eth", "eth")):
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                            "VALUES (?,'2026-11-06',?,?, '2026-11-07T00:00:00Z')", (slug, slug, series))
            self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))
        self.db.commit()
        self.pages = 0

    def tearDown(self):
        self.db.close()

    def trades(self, wallet, records, *, slug="m-btc", span=5400):
        self.pages += 1
        for i, record in enumerate(records):
            side, price, outcome = record
            stamp = 1700000000 + (span * i // (len(records) - 1) if len(records) > 1 else 0)
            self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (slug, self.pages, i, wallet, "same-hash", stamp, side, outcome,
                             0 if outcome == "Up" else 1, f"token-{outcome}", price, "1", 0))
        self.db.commit()

    def test_decimal_boundaries_and_endpoint_one(self):
        cases = [("0", 0), ("0.09999999999999999999999999999999999999999", 0)]
        cases += [(f"0.{i}", i) for i in range(1, 10)]
        cases += [("0.99999999999999999999999999", 9), ("1", 9)]
        with localcontext() as context:
            context.prec = 2
            for value, expected in cases:
                with self.subTest(value=value):
                    self.assertEqual(prices._price_bin(value, "market"), expected)

    def test_uses_all_sides_for_classification_then_counts_buy_only(self):
        records = [("BUY", "0.1", "Up"), ("BUY", "0.7", "Down")]
        records += [("SELL", "0.2", "Up")] * 89
        self.trades("bot", records)
        self.trades("slow", [("BUY", "0.3", "Up")] * 90)
        self.trades("short", [("BUY", "0.4", "Up")] * 100, span=5399)
        result = prices.load_data(self.db, DAY)
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["buy_record_count"], 2)
        self.assertEqual([row["record_count"] for row in result["bins"]], [0, 1, 0, 0, 0, 0, 0, 1, 0, 0])
        self.assertEqual(result["bins"][1]["share_percent"], "50.00")

    def test_cross_market_wallet_and_market_end_date(self):
        self.trades("cross", [("BUY", "0.2", "Up")] * 46)
        self.trades("cross", [("BUY", "0.2", "Down")] * 46, slug="m-eth")
        # 成交时间故意远离目标日，markets.date_utc 也属于前一日。
        result = prices.load_data(self.db, DAY)
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["buy_record_count"], 92)
        self.assertEqual(result["bins"][2]["record_count"], 92)
        self.assertEqual(result["bins"][2]["share_percent"], "100.00")

    def test_selected_series_reclassifies_bots_and_excludes_other_trades(self):
        self.trades("cross", [("BUY", "0.2", "Up")] * 46)
        self.trades("cross", [("BUY", "0.2", "Down")] * 46, slug="m-eth")
        self.trades("btc-bot", [("BUY", "0.3", "Up")] * 91)
        self.trades("btc-bot", [("BUY", "0.9", "Down")] * 5, slug="m-eth")
        selected = prices.load_data(self.db, DAY, series_slug="btc")
        self.assertEqual(selected["total_markets"], 1)
        self.assertEqual(selected["suspected_bot_wallets"], 1)
        self.assertEqual(selected["buy_record_count"], 91)
        self.assertEqual(selected["bins"][3]["record_count"], 91)
        self.assertEqual(sum(row["record_count"] for row in selected["bins"]), 91)
        self.assertEqual(selected["series_slug"], "btc")
        self.assertEqual(selected["bot_classification_scope"], "selected_series")
        full = prices.load_data(self.db, DAY)
        self.assertEqual(full["suspected_bot_wallets"], 2)
        self.assertEqual(full["buy_record_count"], 188)
        self.assertNotIn("series_slug", full)
        self.assertNotIn("bot_classification_scope", full)

    def test_selected_series_ignores_unrelated_incompleteness(self):
        self.trades("bot", [("BUY", "0.2", "Up")] * 91)
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='m-eth'")
        self.assertEqual(prices.load_data(self.db, DAY, series_slug="btc")["buy_record_count"], 91)
        with self.assertRaises(prices.IncompleteDay):
            prices.load_data(self.db, DAY)
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='m-btc'")
        with self.assertRaises(prices.IncompleteDay):
            prices.load_data(self.db, DAY, series_slug="btc")

    def test_unknown_series_rejected(self):
        for unknown in ("unknown", "", "btc' OR 1=1 --"):
            with self.subTest(series=unknown):
                with self.assertRaisesRegex(ValueError, "未知市场系列"):
                    prices.load_data(self.db, DAY, series_slug=unknown)

    def test_same_hash_and_identical_records_are_preserved(self):
        self.trades("bot", [("BUY", "0.2", "Up")] * 91)
        with self.db:
            self.db.execute("UPDATE trades SET timestamp=1700000000 WHERE row_number=1")
            self.db.execute("UPDATE trades SET raw_size='200000' WHERE row_number=3")
        result = prices.load_data(self.db, DAY)
        self.assertEqual(result["buy_record_count"], 91)
        self.assertEqual(result["bins"][2]["record_count"], 91)
        self.assertEqual(sum(row["record_count"] for row in result["bins"]), 91)
        self.assertEqual(result["date_basis"], "market_end_utc")
        self.assertEqual(result["trade_basis"], "suspected_bot_wallet_buy_records")

    def test_no_bot_and_no_bot_buys(self):
        self.trades("single", [("BUY", "0.5", "Up")])
        result = prices.load_data(self.db, DAY)
        self.assertEqual(result["suspected_bot_wallets"], 0)
        self.assertEqual(result["buy_record_count"], 0)
        self.assertTrue(all(row["share_percent"] is None for row in result["bins"]))
        self.trades("seller", [("SELL", "0.3", "Up")] * 91)
        result = prices.load_data(self.db, DAY)
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["buy_record_count"], 0)
        self.assertTrue(all(row["record_count"] == 0 for row in result["bins"]))

    def test_invalid_price_is_not_silently_excluded(self):
        self.trades("bot", [("BUY", "0.2", "Up")] * 91)
        for price in ("-0.01", "1.0001", "NaN", "Infinity", "-Infinity", "not-a-price", "sNaN"):
            with self.subTest(price=price):
                with self.db:
                    self.db.execute("UPDATE trades SET raw_price=? WHERE row_number=0", (price,))
                with self.assertRaises(prices.InvalidTradePrice):
                    prices.load_data(self.db, DAY)

    def test_incomplete_missing_condition_and_unexpected_market_rejected(self):
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='m-btc'")
        with self.assertRaises(prices.IncompleteDay):
            prices.load_data(self.db, DAY)
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at='done' WHERE market_slug='m-btc'")
            self.db.execute("UPDATE markets SET condition_id=NULL WHERE slug='m-btc'")
        with self.assertRaises(prices.IncompleteDay):
            prices.load_data(self.db, DAY)
        with self.db:
            self.db.execute("UPDATE markets SET condition_id='m-btc',end_time='2026-11-08T00:00:00Z' WHERE slug='m-btc'")
        with self.assertRaises(prices.IncompleteDay):
            prices.load_data(self.db, DAY)

    def test_series_launch_expected_count(self):
        for day, expected in ((date(2026, 8, 3), 0), (date(2026, 8, 4), 28), (date(2026, 8, 5), 288)):
            with self.subTest(day=day):
                self.assertEqual(prices._expected_markets(day, 288, "2026-08-04T21:40:00+00:00"), expected)
        self.assertEqual(prices._expected_markets(date(2026, 8, 5), 6, "2026-08-05T00:00:00Z"), 6)

    def test_outer_transaction_is_rejected(self):
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "外层数据库事务"):
            prices.load_data(self.db, DAY)
        self.db.rollback()

    def test_metadata_change_is_rejected(self):
        original = prices._snapshot
        calls = 0

        def changed(db, day, series_slug=None):
            nonlocal calls
            result = original(db, day, series_slug)
            calls += 1
            return result if calls == 1 else (result[0], result[1] + (("changed",),))

        with mock.patch.object(prices, "_snapshot", side_effect=changed):
            with self.assertRaises(prices.DataChangedError):
                prices.load_data(self.db, DAY)

    def test_read_only_database_without_market_volume_and_released_locks(self):
        self.trades("bot", [("BUY", "0.2", "Up")] * 91)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.sqlite3"
            writer = sqlite3.connect(path, timeout=0.1)
            self.db.backup(writer)
            before = path.read_bytes()
            reader = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                reader.execute("PRAGMA query_only=ON")
                result = prices.load_data(reader, DAY)
                self.assertEqual(result["buy_record_count"], 91)
                self.assertEqual(before, path.read_bytes())
                self.assertFalse(reader.in_transaction)
                with writer:
                    writer.execute("UPDATE collection_progress SET last_error='a change' WHERE market_slug='m-btc'")
            finally:
                reader.close()
                writer.close()

    def test_percent_round_half_up_and_zero_denominator(self):
        self.assertEqual(prices._share_percent(201, 20000), "1.01")
        self.assertEqual(prices._share_percent(0, 1), "0.00")
        self.assertIsNone(prices._share_percent(0, 0))


if __name__ == "__main__":
    unittest.main()
