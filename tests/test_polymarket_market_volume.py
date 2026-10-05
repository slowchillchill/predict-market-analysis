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


if __name__ == "__main__":
    unittest.main()
