import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/polymarket_btc5m_daily.py"
spec = importlib.util.spec_from_file_location("btc5m", SCRIPT)
btc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(btc)
DAY = date(2026, 9, 15)


def trade(wallet="0xA", price="0.65", size="5", **kwargs):
    return dict(proxy_wallet=wallet, price=price, size=size, transaction_hash="same-hash",
                timestamp=1789430400, side="SELL", outcome="Up", outcome_index=0,
                token_id="token", **kwargs)


def page(rows, cursor=None):
    return {"data": rows, "pagination": {"next_cursor": cursor, "has_more": cursor is not None}}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "trades.sqlite3"
        self.db = btc.connect_database(self.path)
        btc.seed_markets(self.db, DAY, DAY + timedelta(days=1))
        self.slugs = [slug for slug, _ in btc.slots(DAY, DAY + timedelta(days=1))]
        with self.db:
            self.db.execute("UPDATE collection_progress SET query_params=?",
                            (json.dumps(btc.trade_params("condition")),))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def complete_other_slots(self):
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at='2026-09-17T00:00:00Z'")

    def test_slots_use_utc_midnight_and_exclusive_end(self):
        slots = list(btc.slots(DAY, DAY + timedelta(days=2)))
        self.assertEqual(len(slots), 576)
        self.assertEqual(slots[0][0], "btc-updown-5m-1789430400")
        self.assertEqual(slots[288][1].isoformat(), "2026-09-16T00:00:00+00:00")
        self.assertEqual(slots[-1][1].isoformat(), "2026-09-16T23:55:00+00:00")

    def test_rounding_is_per_trade_and_decimal_exact(self):
        self.assertEqual(btc.micro_usdc("0.65", "5"), 3250000)
        self.assertEqual(btc.micro_usdc("0.35", "5"), 1750000)
        self.assertEqual(btc.micro_usdc("0.0000005", "1"), 1)
        self.assertEqual(btc.micro_usdc("0.000000499999999999999999999999", "1"), 0)
        self.assertEqual(btc.micro_usdc("0.333333333333333333333333333333", "3"), 1000000)

    def test_cash_volume_wallet_case_and_trade_date_do_not_change_market_day(self):
        sell = trade()
        sell["timestamp"] = 1789430390  # 市场开始前，且属于前一日。
        buy = trade("0xa", "0.35", "5")
        buy.update(side="BUY", outcome="Down", timestamp=1789603201)  # 跨日成交。
        btc.commit_page(self.db, self.slugs[0], 1, page([sell]))
        btc.commit_page(self.db, self.slugs[1], 1, page([buy]))
        self.complete_other_slots()
        row = self.db.execute("SELECT * FROM btc5m_daily_summary").fetchone()
        self.assertEqual(row["date_utc"], "2026-09-15")
        self.assertEqual(row["unique_wallets"], 1)
        self.assertEqual(row["wallet_total_micro_usdc"], 5000000)
        self.assertEqual(row["top10_share"], 1.0)

    def test_identical_rows_and_same_hash_are_all_retained(self):
        btc.commit_page(self.db, self.slugs[0], 1, page([trade(), trade(), trade(size="7")]))
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.db.execute("SELECT SUM(amount_micro_usdc) FROM trades").fetchone()[0], 11050000)

    def test_more_than_4000_rows_and_short_nonterminal_page(self):
        client = Mock()
        client.get.side_effect = [page([trade()] * 1000, f"c{i}") for i in range(5)] + [
            page([trade()] * 3, "short-page-cursor"), page([trade()] * 4)]
        btc.collect_market(self.db, client, self.slugs[0])
        self.assertEqual(self.count(), 5007)
        self.assertEqual(client.get.call_count, 7)
        for call in client.get.call_args_list:
            self.assertEqual(call.args[1]["filter_amount"], "1e-18")
            self.assertEqual(call.args[1]["taker_only"], "false")
            self.assertEqual(call.args[1]["condition"], "condition")
        self.assertEqual(client.get.call_args_list[-1].args[1]["cursor"], "short-page-cursor")
        btc.collect_market(self.db, client, self.slugs[0])
        self.assertEqual(client.get.call_count, 7)
        self.assertEqual(self.count(), 5007)

    def test_failure_before_cursor_commit_rolls_back_rows(self):
        self.db.execute("CREATE TRIGGER fail_progress BEFORE UPDATE ON collection_progress "
                        "BEGIN SELECT RAISE(ABORT, '模拟提交前失败'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            btc.commit_page(self.db, self.slugs[0], 1, page([trade()], "c1"))
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.db.execute("SELECT committed_page FROM collection_progress LIMIT 1").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER fail_progress")
        btc.commit_page(self.db, self.slugs[0], 1, page([trade()], "c1"))
        self.assertEqual(self.count(), 1)

    def test_interrupt_after_commit_resumes_from_persisted_cursor(self):
        client = Mock()
        client.get.return_value = page([trade()], "c1")
        commit = btc.commit_page

        def interrupted(*args):
            commit(*args)
            raise KeyboardInterrupt

        with patch.object(btc, "commit_page", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            btc.collect_market(self.db, client, self.slugs[0])
        self.db.close()
        self.db = btc.connect_database(self.path)
        client.get.return_value = page([trade()])
        btc.collect_market(self.db, client, self.slugs[0])
        self.assertEqual(client.get.call_args.args[1]["cursor"], "c1")
        self.assertEqual(self.count(), 2)
        btc.collect_market(self.db, client, self.slugs[0])
        self.assertEqual(self.count(), 2)

    def test_expired_cursor_restarts_only_incomplete_market(self):
        btc.commit_page(self.db, self.slugs[0], 1, page([trade()] * 3, "expired"))
        btc.commit_page(self.db, self.slugs[1], 1, page([trade()]))
        client = Mock()
        client.get.side_effect = [btc.FetchError("invalid cursor", invalid_cursor=True), page([trade()] * 2)]
        btc.collect_market(self.db, client, self.slugs[0])
        self.assertEqual(self.count(), 3)
        self.assertNotIn("cursor", client.get.call_args.args[1])
        self.assertEqual(self.db.execute("SELECT committed_page FROM collection_progress WHERE market_slug=?",
                                         (self.slugs[0],)).fetchone()[0], 1)

    def test_non_cursor_failure_keeps_committed_data_and_cursor(self):
        btc.commit_page(self.db, self.slugs[0], 1, page([trade()], "c1"))
        client = Mock()
        client.get.side_effect = btc.FetchError("HTTP 503")
        with self.assertRaises(btc.FetchError):
            btc.collect_market(self.db, client, self.slugs[0])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.db.execute("SELECT next_cursor FROM collection_progress WHERE market_slug=?",
                                        (self.slugs[0],)).fetchone()[0], "c1")

    def test_top10_ties_are_stable_and_aggregate_across_markets(self):
        rows = [trade(f"wallet-{i:02}", "1", "2") for i in range(12)]
        btc.commit_page(self.db, self.slugs[0], 1, page(rows))
        btc.commit_page(self.db, self.slugs[1], 1, page([trade("wallet-11", "1", "2")]))
        self.complete_other_slots()
        top = list(self.db.execute("SELECT wallet FROM btc5m_wallet_ranked WHERE wallet_rank<=10 ORDER BY wallet_rank"))
        self.assertEqual([r[0] for r in top], ["wallet-11"] + [f"wallet-{i:02}" for i in range(9)])
        summary = self.db.execute("SELECT * FROM btc5m_daily_summary").fetchone()
        self.assertEqual(summary["unique_wallets"], 12)
        self.assertEqual(summary["wallet_total_micro_usdc"], 26000000)
        self.assertEqual(summary["top10_micro_usdc"], 22000000)
        self.assertAlmostEqual(summary["top10_share"], 22 / 26)

    def test_complete_zero_trade_day_and_rounded_zero_wallet(self):
        self.complete_other_slots()
        summary = self.db.execute("SELECT * FROM btc5m_daily_summary").fetchone()
        self.assertEqual(summary["unique_wallets"], 0)
        self.assertEqual(summary["wallet_total_micro_usdc"], 0)
        self.assertEqual(summary["top10_micro_usdc"], 0)
        self.assertIsNone(summary["top10_share"])
        btc.commit_page(self.db, self.slugs[0], 1, page([trade(price="1e-9", size="1")]))
        summary = self.db.execute("SELECT * FROM btc5m_daily_summary").fetchone()
        self.assertEqual(summary["unique_wallets"], 1)
        self.assertIsNone(summary["top10_share"])

    def test_incomplete_day_has_no_summary_or_top10(self):
        btc.commit_page(self.db, self.slugs[0], 1, page([trade()]))
        self.assertEqual(list(self.db.execute("SELECT * FROM btc5m_daily_summary")), [])
        output = Path(self.tmp.name) / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(btc.report_day(self.db, DAY.isoformat(), output))
        self.assertEqual(len(list(output.glob("*.csv"))), 0)
        self.assertIn("采集未完成", (output / "btc5m_daily_2026-09-15.md").read_text())

    def test_discovery_records_missing_and_wrong_series_or_start_as_gaps(self):
        def event(index):
            return dict(slug=self.slugs[index], id=str(index), seriesSlug=btc.SERIES,
                        markets=[dict(slug=self.slugs[index], id=str(index), conditionId=f"c{index}",
                                      question="BTC", eventStartTime=f"2026-09-15T00:{index*5:02}:00Z",
                                      endDate="2026-09-15T00:20:00Z")])
        valid, wrong_series, wrong_start = event(0), event(1), event(2)
        wrong_series["seriesSlug"] = "eth-up-or-down-5m"
        wrong_start["markets"][0]["eventStartTime"] = "2026-09-14T23:55:00Z"
        client = Mock()
        client.get.side_effect = [[valid, wrong_series, wrong_start]] + [[]] * 5
        btc.discover(self.db, client, DAY, DAY + timedelta(days=1))
        self.assertEqual(btc.coverage(self.db, DAY.isoformat())["discovered_markets"], 1)
        errors = {r[0]: r[1] for r in self.db.execute("SELECT market_slug,last_error FROM collection_progress")}
        self.assertIn("系列", errors[self.slugs[1]])
        self.assertIn("eventStartTime", errors[self.slugs[2]])
        self.assertIn("未返回", errors[self.slugs[3]])

    def test_offline_report_is_repeatable_with_readonly_database(self):
        btc.commit_page(self.db, self.slugs[0], 1, page([trade(), trade("0xb", "0.35", "5")]))
        self.complete_other_slots()
        output = Path(self.tmp.name) / "out"
        args = ["report", "--date", DAY.isoformat(), "--db", str(self.path), "--output-dir", str(output)]
        with patch.object(btc, "Client", side_effect=AssertionError("报告不得创建网络客户端")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(btc.main(args), 0)
            first = {p.name: p.read_bytes() for p in output.iterdir()}
            self.assertEqual(btc.main(args), 0)
            self.assertEqual(first, {p.name: p.read_bytes() for p in output.iterdir()})


class NetworkTests(unittest.TestCase):
    def response(self, status, body, headers=None):
        import requests
        response = requests.Response()
        response.status_code = status
        response._content = body.encode()
        response.headers.update(headers or {})
        return response

    def test_proxy_retries_retry_after_and_preserves_decimal_lexeme(self):
        client = btc.Client("socks5h://127.0.0.1:1234")
        self.addCleanup(client.close)
        self.assertFalse(client.session.trust_env)
        self.assertEqual(client.session.proxies["https"], "socks5h://127.0.0.1:1234")
        responses = [self.response(429, '{}', {"Retry-After": "7"}),
                     self.response(200, '{"price":0.10000000000000000001}')]
        with patch.object(client.session, "get", side_effect=responses) as get, patch.object(btc.time, "sleep") as sleep:
            self.assertEqual(client.get(btc.TRADES_URL, {})["price"], "0.10000000000000000001")
            sleep.assert_called_once_with(7.0)
            self.assertEqual(get.call_args.kwargs["timeout"], 30)

    def test_cursor_400_is_classified_but_other_400_is_not(self):
        client = btc.Client("http://127.0.0.1:1234")
        self.addCleanup(client.close)
        for message, is_cursor in [("invalid cursor", True), ("invalid condition", False)]:
            response = self.response(400, json.dumps(dict(error=message, code="invalid_request")))
            with patch.object(client.session, "get", return_value=response), self.assertRaises(btc.FetchError) as ctx:
                client.get(btc.TRADES_URL, {})
            self.assertEqual(ctx.exception.invalid_cursor, is_cursor)


if __name__ == "__main__":
    unittest.main()
