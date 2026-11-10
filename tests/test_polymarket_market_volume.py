import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_daily as up
import polymarket_market_volume as volume


def payload(rows, cursor=None):
    return {"data": rows, "pagination": {"next_cursor": cursor}}


def trade(price, size="1", side="BUY", outcome="Up"):
    return {"price": price, "size": size, "side": side, "outcome": outcome,
            "transaction_hash": "same-hash"}


def wallet_trade(price, size="1", side="BUY", outcome="Up", tx="same-hash", wallet="wallet"):
    return {**trade(price, size, side, outcome), "transaction_hash": tx,
            "proxy_wallet": wallet, "timestamp": 1, "outcome_index": int(outcome == "Down"),
            "token_id": outcome.lower()}


class MarketVolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        self.db = up.connect_database(self.path)
        with self.db:
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id) VALUES ('market','2026-10-05','condition')")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def state(self):
        return dict(self.db.execute("SELECT * FROM market_volume WHERE market_slug='market'").fetchone())

    def test_taker_amount_and_completed_rerun_skip(self):
        with self.db:
            self.db.execute("INSERT INTO collection_progress(market_slug) VALUES ('market')")
        wallet_rows = [{**trade("0.999", "5", side), "proxy_wallet": wallet, "timestamp": 1,
                        "outcome_index": 0, "token_id": "up"}
                       for side, wallet in (("BUY", "0xbuyer"), ("SELL", "0xseller"))]
        up.core.commit_page(self.db, "market", 1, payload(wallet_rows))
        client = Mock()
        client.get.return_value = payload([trade("0.999", "5", "SELL")])
        volume.collect_market(self.db, client, "market")
        self.assertEqual(self.state()["amount_micro_usdc"], 4995000)
        self.assertEqual(self.db.execute("SELECT SUM(amount_micro_usdc) FROM trades").fetchone()[0], 9990000)
        self.assertEqual(self.db.execute("SELECT COUNT(DISTINCT wallet) FROM trades").fetchone()[0], 2)
        self.assertEqual(client.get.call_args.args[1]["taker_only"], "true")
        volume.collect_market(self.db, client, "market")
        client.get.assert_called_once()
        self.assertEqual(self.state()["amount_micro_usdc"], 4995000)

    def test_partial_failure_resumes_without_doubling_and_keeps_same_hash_fills(self):
        client = Mock()
        client.get.side_effect = [payload([trade("0.999", "1000")], "page-2"), up.core.FetchError("network")]
        with self.assertRaises(up.core.FetchError):
            volume.collect_market(self.db, client, "market")
        self.assertEqual(self.state()["amount_micro_usdc"], 999000000)
        self.assertIsNone(self.state()["completed_at"])
        # 重开数据库模拟进程退出；双买及同一哈希中的不同成交均不能合并或除以二。
        self.db.close()
        self.db = up.connect_database(self.path)
        client.get.side_effect = [payload([trade("0.001", "1000", outcome="Down")])]
        volume.collect_market(self.db, client, "market")
        self.assertEqual(client.get.call_args.args[1]["cursor"], "page-2")
        self.assertEqual(self.state()["amount_micro_usdc"], 1000000000)
        self.assertEqual(self.state()["committed_page"], 2)

    def test_page_failure_does_not_advance_amount_or_cursor(self):
        client = Mock()
        client.get.side_effect = [payload([trade("0.2")], "page-2"), payload([trade("0.3"), {}])]
        with self.assertRaises(KeyError):
            volume.collect_market(self.db, client, "market")
        self.assertEqual(self.state()["amount_micro_usdc"], 200000)
        self.assertEqual(self.state()["committed_page"], 1)
        self.assertEqual(self.state()["next_cursor"], "page-2")

    def test_expired_cursor_resets_only_current_market_single_side(self):
        with self.db:
            self.db.execute("INSERT INTO market_volume(market_slug,amount_micro_usdc,committed_page,next_cursor) "
                            "VALUES ('market',900000,1,'expired')")
            self.db.execute("INSERT INTO collection_progress(market_slug,committed_page,completed_at) VALUES ('market',7,'done')")
        original = tuple(self.db.execute("SELECT * FROM collection_progress").fetchone())
        client = Mock()
        client.get.side_effect = [up.core.FetchError("invalid cursor", invalid_cursor=True), payload([trade("0.1")])]
        volume.collect_market(self.db, client, "market")
        self.assertEqual(self.state()["amount_micro_usdc"], 100000)
        self.assertEqual(self.state()["committed_page"], 1)
        self.assertNotIn("cursor", client.get.call_args.args[1])
        self.assertEqual(tuple(self.db.execute("SELECT * FROM collection_progress").fetchone()), original)

    def test_rounds_each_row_and_empty_complete_is_zero(self):
        client = Mock()
        client.get.return_value = payload([trade("0.0000005"), trade("0.0000005")])
        volume.collect_market(self.db, client, "market")
        self.assertEqual(self.state()["amount_micro_usdc"], 2)
        with self.db:
            self.db.execute("DELETE FROM market_volume")
        client.get.return_value = payload([])
        volume.collect_market(self.db, client, "market")
        self.assertEqual(self.state()["amount_micro_usdc"], 0)
        self.assertIsNotNone(self.state()["completed_at"])

    def test_cooperative_pause_preserves_single_side_and_wallet_progress(self):
        with self.db:
            self.db.execute("INSERT INTO collection_progress(market_slug,committed_page,completed_at) "
                            "VALUES ('market',7,'wallet-done')")
        wallet = tuple(self.db.execute("SELECT * FROM collection_progress").fetchone())
        client = Mock()
        client.get.return_value = payload([trade("0.2")], "page-2")
        with self.assertRaises(up.core.CollectionPaused):
            volume.collect_market(self.db, client, "market", should_yield=lambda: self.state()["committed_page"] == 1)
        client.get.assert_called_once()
        self.assertEqual(self.state()["amount_micro_usdc"], 200000)
        self.assertEqual(self.state()["next_cursor"], "page-2")
        self.assertIsNone(self.state()["last_error"])
        self.assertEqual(tuple(self.db.execute("SELECT * FROM collection_progress").fetchone()), wallet)
        client.get.return_value = payload([trade("0.3")])
        volume.collect_market(self.db, client, "market")
        self.assertEqual(client.get.call_args.args[1]["cursor"], "page-2")
        self.assertEqual(self.state()["amount_micro_usdc"], 500000)

    def seed_wallet_progress(self):
        with self.db:
            self.db.execute("INSERT INTO collection_progress(market_slug) VALUES ('market')")

    def test_derived_volume_uses_first_row_for_pair_and_preserves_all_wallet_rows(self):
        self.seed_wallet_progress()
        up.core.commit_page(self.db, "market", 1, payload([
            wallet_trade(".7", "10", "SELL", wallet="taker"),
            wallet_trade(".7", "10", "BUY", wallet="maker")]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 7000000)
        self.assertEqual(self.state()["amount_micro_usdc"], 7000000)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)

    def test_derived_volume_handles_multifill_complementary_buys_and_page_boundary(self):
        self.seed_wallet_progress()
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

    def test_derived_volume_uses_stored_per_row_decimal_rounding(self):
        self.seed_wallet_progress()
        precise = "0.333333333333333333333333333333"
        up.core.commit_page(self.db, "market", 1, payload([
            wallet_trade("0.0000005", tx="micro"),
            wallet_trade("0.0000005", tx="micro"),
            wallet_trade(precise, "3", tx="precise"),
            wallet_trade(precise, "3", tx="precise")]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 1000001)
        self.assertEqual(self.db.execute("SELECT raw_price FROM trades WHERE transaction_hash='precise' LIMIT 1").fetchone()[0], precise)

    def test_derived_volume_requires_completed_wallet_and_keeps_previous_volume_on_failure(self):
        with self.assertRaises(ValueError):
            volume.derive_market_volume(self.db, "market")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM market_volume").fetchone()[0], 0)
        self.seed_wallet_progress()
        with self.db:
            self.db.execute("INSERT INTO market_volume(market_slug,amount_micro_usdc,next_cursor) "
                            "VALUES ('market',900000,'api-cursor')")
        before = self.state()
        with self.assertRaises(ValueError):
            volume.derive_market_volume(self.db, "market")
        self.assertEqual(self.state(), before)

    def test_derived_volume_replaces_old_summary_idempotently_and_resets_api_progress(self):
        self.seed_wallet_progress()
        up.core.commit_page(self.db, "market", 1, payload([wallet_trade(".3")]))
        with self.db:
            self.db.execute("INSERT INTO market_volume VALUES ('market',999999,5,'old-cursor',NULL,'old-error')")
        volume.derive_market_volume(self.db, "market")
        volume.derive_market_volume(self.db, "market")
        state = self.state()
        self.assertEqual(state["amount_micro_usdc"], 300000)
        self.assertEqual(state["committed_page"], 0)
        self.assertIsNone(state["next_cursor"])
        self.assertIsNone(state["last_error"])
        self.assertIsNotNone(state["completed_at"])

    def test_empty_completed_wallet_derives_zero_and_hashes_are_scoped_to_market(self):
        self.seed_wallet_progress()
        up.core.commit_page(self.db, "market", 1, payload([]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 0)
        with self.db:
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id) VALUES ('other','2026-10-05','other')")
            self.db.execute("INSERT INTO collection_progress(market_slug) VALUES ('other')")
        up.core.commit_page(self.db, "market", 2, payload([wallet_trade(".1")]))
        up.core.commit_page(self.db, "other", 1, payload([wallet_trade(".9")]))
        self.assertEqual(volume.derive_market_volume(self.db, "market"), 100000)
        self.assertEqual(volume.derive_market_volume(self.db, "other"), 900000)


if __name__ == "__main__":
    unittest.main()
