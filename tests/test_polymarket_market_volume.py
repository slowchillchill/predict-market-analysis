import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_daily as up
import polymarket_market_volume as volume


def payload(rows, cursor=None):
    return {"data": rows, "pagination": {"next_cursor": cursor}}


def wallet_trade(price, size="1", side="BUY", outcome="Up", tx="same-hash", wallet="wallet"):
    return {"price": price, "size": size, "side": side, "outcome": outcome,
            "transaction_hash": tx, "proxy_wallet": wallet, "timestamp": 1,
            "outcome_index": int(outcome == "Down"), "token_id": outcome.lower()}


class MarketVolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        self.db = up.connect_database(self.path)
        with self.db:
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id) VALUES ('market','2026-10-05','condition')")
            self.db.execute("INSERT INTO collection_progress(market_slug) VALUES ('market')")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_first_row_pair_amount_is_read_only_and_preserves_wallet_rows(self):
        up.core.commit_page(self.db, "market", 1, payload([
            wallet_trade(".7", "10", "SELL", wallet="taker"),
            wallet_trade(".7", "10", "BUY", wallet="maker")]))
        before = self.db.total_changes
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 7000000)
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 7000000)
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone())

    def test_multifill_complementary_buys_and_cross_page_groups(self):
        up.core.commit_page(self.db, "market", 1, payload([
            wallet_trade(".8", "10", "SELL", tx="multi"),
            wallet_trade(".8", "4", "BUY", tx="multi")], "page-2"))
        up.core.commit_page(self.db, "market", 2, payload([
            wallet_trade(".8", "6", "BUY", tx="multi"),
            wallet_trade(".2", "10", "BUY", "Down", tx="complement"),
            wallet_trade(".8", "10", "BUY", "Up", tx="complement")]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 10000000)
        amounts = self.db.execute("SELECT SUM(amount_micro_usdc),"
                                  "SUM(CASE WHEN side='BUY' THEN amount_micro_usdc ELSE 0 END) FROM trades").fetchone()
        self.assertEqual(tuple(amounts), (26000000, 18000000))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 5)

    def test_high_precision_uses_existing_per_row_rounding(self):
        precise = "0.333333333333333333333333333333"
        up.core.commit_page(self.db, "market", 1, payload([
            wallet_trade("0.0000005", tx="micro"), wallet_trade("0.0000005", tx="micro"),
            wallet_trade(precise, "3", tx="precise"), wallet_trade(precise, "3", tx="precise")]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 1000001)
        self.assertEqual(self.db.execute("SELECT raw_price FROM trades WHERE group_id=2 LIMIT 1").fetchone()[0], precise)

    def test_incomplete_wallet_is_not_treated_as_zero(self):
        with self.assertRaises(ValueError):
            volume.derive_market_volume(self.db, "market")
        up.core.commit_page(self.db, "market", 1, payload([wallet_trade(".2")], "next"))
        with self.assertRaises(ValueError):
            volume.derive_market_volume(self.db, "market")

    def test_zero_completed_market_and_group_numbers_scoped_by_market(self):
        up.core.commit_page(self.db, "market", 1, payload([]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 0)
        up.core.commit_page(self.db, "market", 2, payload([wallet_trade(".1")]))
        with self.db:
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id) VALUES ('other','2026-10-05','other')")
            self.db.execute("INSERT INTO collection_progress(market_slug) VALUES ('other')")
        up.core.commit_page(self.db, "other", 1, payload([wallet_trade(".9")]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 100000)
        self.assertEqual(volume.derive_market_volume(self.db, "other"), 900000)

    def test_calculation_supports_readonly_connection(self):
        up.core.commit_page(self.db, "market", 1, payload([wallet_trade(".3")]))
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
            self.assertEqual(volume.derive_market_volume(db, "market"), 300000)
            self.assertIsNone(volume.legacy_market_volume(db, "market"))

    def test_legacy_api_value_is_available_only_as_completed_reference(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        root = Path(__file__).resolve().parents[1]
        with closing(sqlite3.connect(path)) as db:
            db.executescript((root / "tests/fixtures/btc5m_legacy_schema.sql").read_text())
            db.executescript("CREATE TABLE market_volume(market_slug TEXT PRIMARY KEY, amount_micro_usdc INTEGER, completed_at TEXT);"
                             "INSERT INTO market_volume VALUES ('complete',123,'done'),('incomplete',999,NULL);")
            self.assertEqual(volume.legacy_market_volume(db, "complete"), 123)
            self.assertIsNone(volume.legacy_market_volume(db, "incomplete"))
            self.assertIsNone(volume.legacy_market_volume(db, "missing"))


if __name__ == "__main__":
    unittest.main()
