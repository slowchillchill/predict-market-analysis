import contextlib
import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_daily as up
from polymarket_market_volume_query import load_market_volume

START, END = date(2026, 9, 17), date(2026, 9, 18)


def event(slug, ended, began="2026-09-16T23:55:00Z", series="btc-up-or-down-5m"):
    return dict(id=slug, seriesSlug=series, markets=[dict(
        id=slug, slug=slug, conditionId="condition-" + slug, question=slug,
        eventStartTime=began, endDate=ended)])


def trade(wallet, timestamp, amount="1"):
    return dict(proxy_wallet=wallet, transaction_hash="same-hash", timestamp=timestamp,
                side="BUY", outcome="Up", outcome_index=0, token_id="token", price=amount, size="1")


class UpdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "trades.sqlite3"
        self.db = up.connect_database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def discover(self, events, series="btc-up-or-down-5m"):
        client = Mock()
        client.get.side_effect = [[dict(slug=series, id="123")], {"events": events}]
        up.discover_series(self.db, client, series, START, END)
        return client

    def fill_complete_day(self):
        # 按文档的 38 个系列分别建数据，测试跨系列统计及缺少整类时的覆盖情况。
        with self.db:
            for series, count in up.TARGET_SERIES.items():
                for i in range(count):
                    slug = f"{series}-{i}"
                    self.db.execute(
                        "INSERT INTO markets(slug,date_utc,condition_id,series_slug,event_start_time,end_time) "
                        "VALUES (?,'2026-09-16',?,?,'2026-09-16T23:55:00Z','2026-09-17T00:00:00Z')",
                        (slug, slug, series))
                    self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))

    def single_market_catalog(self):
        self.db.executescript(
            "DROP VIEW updown_target_series; "
            "CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) "
            "AS VALUES ('btc-up-or-down-5m',1,NULL);")

    def test_catalog_matches_documented_scope(self):
        self.assertEqual(len(up.TARGET_SERIES), 38)
        self.assertEqual(sum(up.TARGET_SERIES.values()), 3295)
        self.assertEqual(sum(s.endswith("-5m") for s in up.TARGET_SERIES), 8)
        self.assertNotIn("zec-up-or-down-hourly", up.TARGET_SERIES)
        self.assertIn("dogecoin-up-or-down-daily", up.TARGET_SERIES)
        self.assertIn("solana-up-or-down-hourly", up.TARGET_SERIES)

    def test_end_day_boundaries_offsets_and_series_filter(self):
        self.discover([
            event("midnight", "2026-09-17T00:00:00Z"),
            event("before", "2026-09-16T23:59:59Z"),
            event("upper", "2026-09-18T00:00:00Z"),
            event("offset", "2026-09-18T08:59:59+09:00"),
            event("wrong-series", "2026-09-17T12:00:00Z", series="another-series"),
        ])
        rows = self.db.execute("SELECT * FROM markets ORDER BY slug").fetchall()
        self.assertEqual([r["slug"] for r in rows], ["midnight", "offset"])
        self.assertEqual(rows[0]["date_utc"], "2026-09-16")
        self.assertEqual(rows[1]["end_time"], "2026-09-17T23:59:59+00:00")

    def test_discovery_paginates_and_keeps_complete_and_incomplete_progress(self):
        self.discover([event("keep", "2026-09-17T00:00:00Z"), event("resume", "2026-09-17T00:05:00Z")])
        up.core.commit_page(self.db, "keep", 1, {"data": [trade("0xA", 1)], "pagination": {"next_cursor": None}})
        up.core.commit_page(self.db, "resume", 1, {"data": [trade("0xB", 2)], "pagination": {"next_cursor": "opaque"}})
        before = [tuple(row) for row in self.db.execute("SELECT * FROM collection_progress ORDER BY market_slug")]
        client = Mock()
        client.get.side_effect = [[dict(slug="btc-up-or-down-5m", id="123")],
                                  {"events": [event("keep", "2026-09-17T00:00:00Z"), event("resume", "2026-09-17T00:05:00Z")],
                                   "next_cursor": "server-issued-cursor"},
                                  {"events": [event("third", "2026-09-17T00:10:00Z")], "next_cursor": None}]
        # 未满页仍有游标时必须继续；终页可省略游标或返回 null。
        with patch.object(up, "EVENT_PAGE_SIZE", 100):
            up.discover_series(self.db, client, "btc-up-or-down-5m", START, END)
        self.assertEqual(client.get.call_count, 3)
        first, second = client.get.call_args_list[1:]
        self.assertEqual(first.args[0], "https://gamma-api.polymarket.com/events/keyset")
        self.assertNotIn("offset", first.args[1])
        self.assertNotIn("after_cursor", first.args[1])
        self.assertEqual(second.args[1], {**first.args[1], "after_cursor": "server-issued-cursor"})
        self.assertEqual(first.args[1]["series_id"], "123")
        self.assertEqual(first.args[1]["end_date_min"], "2026-09-17T00:00:00+00:00")
        self.assertEqual(first.args[1]["end_date_max"], "2026-09-18T00:00:00+00:00")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM markets WHERE slug='third'").fetchone())
        self.assertEqual(before, [tuple(row) for row in self.db.execute(
            "SELECT * FROM collection_progress WHERE market_slug IN ('keep','resume') ORDER BY market_slug")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)

    def test_collect_reuses_completed_market_and_does_not_filter_trade_timestamps(self):
        self.discover([event("keep", "2026-09-17T00:00:00Z"), event("new", "2026-09-17T23:55:00Z")])
        up.core.commit_page(self.db, "keep", 1, {"data": [trade("0xA", 1)], "pagination": {"next_cursor": None}})
        client = Mock()
        client.get.return_value = {"data": [trade("0xB", 0), trade("0xB", 9999999999)],
                                   "pagination": {"next_cursor": None}}
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), contextlib.redirect_stdout(io.StringIO()):
            up.collect(self.db, client, START, END)
        client.get.assert_called_once()
        params = client.get.call_args.args[1]
        self.assertEqual(params["taker_only"], "false")
        self.assertEqual(params["condition"], "condition-new")
        self.assertNotIn("start", params)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 3)

    def test_complete_end_day_aggregates_and_top200_rank(self):
        self.fill_complete_day()
        first = "btc-up-or-down-5m-0"
        other = "eth-up-or-down-daily-0"
        rows = [trade(f"0x{i:04x}", 0, str(i + 1)) for i in range(201)]
        up.core.commit_page(self.db, first, 1, {"data": rows, "pagination": {"next_cursor": None}})
        up.core.commit_page(self.db, other, 1, {"data": [trade("0x0000", 9999999999, "1")],
                                             "pagination": {"next_cursor": None}})
        summary = self.db.execute("SELECT * FROM updown_daily_summary").fetchone()
        self.assertEqual(summary["date_utc"], "2026-09-17")
        self.assertEqual(summary["unique_wallets"], 201)
        self.assertEqual(summary["wallet_total_micro_usdc"], 20302000000)
        self.assertEqual(summary["top200_micro_usdc"], 20300000000)
        self.assertAlmostEqual(summary["top200_share"], 20300 / 20302)
        self.assertEqual(summary["market_volume_micro_usdc"], 2000000)
        # 缺少一个完整系列时，不能把其成交当作零并声称完整。
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug LIKE 'zec-up-or-down-4h-%'")
        self.assertFalse(up.coverage(self.db, "2026-09-17")["is_complete"])
        self.assertIsNone(self.db.execute("SELECT * FROM updown_daily_summary").fetchone())

    def test_market_volume_is_derived_only_when_all_wallet_markets_are_complete(self):
        self.fill_complete_day()
        slug = 'btc-up-or-down-5m-0'
        buyer = trade("buyer", 1, "4.995")
        seller = {**trade("seller", 1, "4.995"), "side": "SELL"}
        up.core.commit_page(self.db, slug, 1, {"data": [buyer, seller], "pagination": {"next_cursor": None}})
        summary = self.db.execute("SELECT * FROM updown_daily_summary").fetchone()
        self.assertEqual(summary["market_volume_micro_usdc"], 4995000)
        self.assertEqual(summary["wallet_total_micro_usdc"], 9990000)
        self.assertEqual(load_market_volume(self.db, START, END)["volume_micro_usdc"], 4995000)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone())
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug=?", (slug,))
        self.assertIsNone(self.db.execute("SELECT * FROM updown_daily_summary").fetchone())
        self.assertFalse(up.coverage(self.db, str(START))["is_complete"])
        self.assertIsNone(load_market_volume(self.db, START, END)["volume_micro_usdc"])

    def test_report_legacy_summary_exposes_missing_volume_without_modifying_database(self):
        self.fill_complete_day()
        self.db.executescript("DROP VIEW updown_daily_summary; CREATE VIEW updown_daily_summary AS "
                            "SELECT '2026-09-17' AS date_utc,0 AS unique_wallets,0 AS wallet_total_micro_usdc,"
                            "0 AS top200_micro_usdc,NULL AS top200_share;")
        self.db.executescript("DROP VIEW updown_coverage; CREATE VIEW updown_coverage AS "
                            "SELECT '2026-09-17' AS date_utc,3295 AS expected_markets,3295 AS discovered_markets,"
                            "3295 AS completed_markets,3295 AS committed_pages,1 AS is_complete;")
        output = Path(self.tmp.name) / "legacy"
        with contextlib.redirect_stdout(io.StringIO()):
            result = up.main(["report", "--date", str(START), "--db", str(self.path), "--output-dir", str(output)])
        self.assertEqual(result, 0)
        self.assertIn("市场成交额：缺失", (output / f"updown_daily_{START}.md").read_text())
        with (output / f"updown_daily_{START}.csv").open() as handle:
            summary = next(csv.DictReader(handle))
        self.assertEqual(summary["market_volume_usdc"], "")
        self.assertEqual(summary["market_volume_micro_usdc"], "")

    def test_collect_only_fetches_wallet_pages_for_requested_range(self):
        self.discover([event("yesterday", "2026-09-17T00:00:00Z")])
        with self.db:
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                            "VALUES ('old','2026-09-15','old','btc-up-or-down-5m','2026-09-16T00:00:00Z')")
        client = Mock()
        client.get.return_value = {"data": [], "pagination": {"next_cursor": None}}
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), contextlib.redirect_stdout(io.StringIO()):
            up.collect(self.db, client, START, END)
        client.get.assert_called_once()
        self.assertEqual(client.get.call_args.args[1]["condition"], "condition-yesterday")
        self.assertEqual(client.get.call_args.args[1]["taker_only"], "false")
        self.assertIsNotNone(self.db.execute("SELECT completed_at FROM collection_progress WHERE market_slug='yesterday'").fetchone()[0])
        self.assertIsNone(self.db.execute("SELECT 1 FROM collection_progress WHERE market_slug='old'").fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM trades WHERE market_slug='old'").fetchone())

    def test_targeted_retry_skips_completed_markets_and_successful_wallet_pages(self):
        self.discover([event("keep", "2026-09-17T00:00:00Z"),
                       event("failed", "2026-09-17T00:05:00Z"),
                       event("other", "2026-09-17T00:10:00Z")])
        up.core.commit_page(self.db, "keep", 1, {"data": [], "pagination": {"next_cursor": None}})
        calls = {}

        def request(url, params):
            key = params["condition"], params["taker_only"]
            calls[key] = calls.get(key, 0) + 1
            if key == ("condition-failed", "false") and calls[key] == 1:
                raise up.core.FetchError("HTTP 503", status_code=503)
            return {"data": [], "pagination": {"next_cursor": None}}

        client = Mock()
        client.get.side_effect = request
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            up.collect(self.db, client, START, END)
        self.assertNotIn(("condition-keep", "false"), calls)
        self.assertEqual(calls[("condition-failed", "false")], 2)
        self.assertEqual(calls[("condition-other", "false")], 1)
        self.assertTrue(all(mode == "false" for _, mode in calls))
        state = self.db.execute("SELECT completed_at,last_error FROM collection_progress WHERE market_slug='failed'").fetchone()
        self.assertIsNotNone(state["completed_at"])
        self.assertIsNone(state["last_error"])

    def test_failed_page_rolls_back_new_groups_then_resumes_only_that_page(self):
        self.discover([event("keep", "2026-09-17T00:00:00Z"),
                       event("resume", "2026-09-17T00:05:00Z")])
        up.core.commit_page(self.db, "keep", 1, {"data": [], "pagination": {"next_cursor": None}})
        up.core.commit_page(self.db, "resume", 1, {"data": [trade("first", 1, ".2")],
                                                   "pagination": {"next_cursor": "page-2"}})
        invalid = {**trade("new-wallet", 3, "invalid-price"), "transaction_hash": "new-group"}
        client = Mock()
        client.get.return_value = {"data": [trade("same-group", 2, ".2"), invalid],
                                   "pagination": {"next_cursor": None}}
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            up.collect(self.db, client, START, END)
        client.get.assert_called_once()
        self.assertEqual(client.get.call_args.args[1]["condition"], "condition-resume")
        self.assertEqual(client.get.call_args.args[1]["cursor"], "page-2")
        state = self.db.execute("SELECT * FROM collection_progress WHERE market_slug='resume'").fetchone()
        self.assertEqual((state["committed_page"], state["next_cursor"]), (1, "page-2"))
        self.assertIsNone(state["completed_at"])
        self.assertTrue(state["last_error"])
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT transaction_hash,group_id FROM pending_group_map")],
                         [("same-hash", 1)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades WHERE market_slug='resume'").fetchone()[0], 1)

        client.reset_mock()
        client.get.return_value = {"data": [trade("same-group", 2, ".2"),
                                           {**trade("new-wallet", 3, ".3"), "transaction_hash": "new-group"}],
                                   "pagination": {"next_cursor": None}}
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()):
            up.collect(self.db, client, START, END)
        client.get.assert_called_once()
        self.assertEqual(client.get.call_args.args[1]["cursor"], "page-2")
        self.assertEqual(client.get.call_args.args[1]["taker_only"], "false")
        rows = [tuple(row) for row in self.db.execute(
            "SELECT page_number,row_number,group_id,typeof(group_id) FROM trades "
            "WHERE market_slug='resume' ORDER BY page_number,row_number")]
        self.assertEqual(rows, [(1, 0, 1, "integer"), (2, 0, 1, "integer"), (2, 1, 2, "integer")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pending_group_map").fetchone()[0], 0)
        state = self.db.execute("SELECT completed_at,last_error FROM collection_progress WHERE market_slug='resume'").fetchone()
        self.assertIsNotNone(state["completed_at"])
        self.assertIsNone(state["last_error"])

    def test_nonretryable_market_error_is_not_repeated_in_retry_sweep(self):
        self.discover([event("bad", "2026-09-17T00:00:00Z")])
        client = Mock()

        def request(url, params):
            if params["taker_only"] == "false":
                raise up.core.FetchError("HTTP 400: bad condition", status_code=400)
            return {"data": [], "pagination": {"next_cursor": None}}

        client.get.side_effect = request
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            up.collect(self.db, client, START, END)
        client.get.assert_called_once()
        self.assertEqual(client.get.call_args.args[1]["taker_only"], "false")
        state = self.db.execute("SELECT * FROM collection_progress WHERE market_slug='bad'").fetchone()
        self.assertIsNone(state["completed_at"])
        self.assertIn("400", state["last_error"])

    def test_persistent_rate_limit_stops_subsequent_markets_and_preserves_error(self):
        self.discover([event("first", "2026-09-17T00:00:00Z"),
                       event("second", "2026-09-17T00:05:00Z")])
        client = Mock()
        client.get.side_effect = up.core.RateLimitError("HTTP 429", status_code=429, retry_after=5)
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(up.core.RateLimitError):
            up.collect(self.db, client, START, END)
        client.get.assert_called_once()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pending_group_map").fetchone()[0], 0)
        self.assertEqual(client.get.call_args.args[1]["taker_only"], "false")
        self.assertEqual(self.db.execute("SELECT last_error FROM collection_progress WHERE market_slug='first'").fetchone()[0],
                         "HTTP 429")

    def test_frozen_launch_boundaries_drive_coverage_and_missing_discovery(self):
        bounds = {series: "2026-09-18T00:00:00Z" for series in up.TARGET_SERIES}
        bounds["btc-up-or-down-5m"] = "2026-09-17T23:55:00Z"
        up.set_series_first_end(self.db, bounds)
        slots = list(up.expected_slots("btc-up-or-down-5m", START, first_ends=up.series_first_end(self.db)))
        self.assertEqual(len(slots), 1)
        self.assertEqual(up.coverage(self.db, str(START))["expected_markets"], 1)
        client = Mock()
        slug = up.market_slug("btc-up-or-down-5m", slots[0])
        client.get.return_value = event(slug, "2026-09-17T23:55:00Z", "2026-09-17T23:50:00Z")
        with contextlib.redirect_stdout(io.StringIO()):
            up.discover_missing_markets(self.db, client, START, END)
        client.get.assert_called_once_with(up.GAMMA_EVENT_SLUG + slug, {})
        up.core.commit_page(self.db, slug, 1, {"data": [], "pagination": {"next_cursor": None}})
        self.assertTrue(up.coverage(self.db, str(START))["is_complete"])
        self.assertEqual(up.SERIES_FIRST_END_UTC.get("btc-up-or-down-5m"), None)

    def test_collect_cli_can_use_direct_network_without_proxy(self):
        with patch.object(up, "collect"), patch.object(up.core, "Client") as client, \
             patch.object(up, "coverage", return_value={"is_complete": 1}), \
             contextlib.redirect_stdout(io.StringIO()):
            result = up.main(["collect", "--start-date", str(START), "--end-date", str(END), "--db", str(self.path)])
        self.assertEqual(result, 0)
        client.assert_called_once_with(None)

    def test_complete_wallets_derive_amount_without_more_requests_or_summary_table(self):
        self.discover([event("done-wallet", "2026-09-17T00:00:00Z")])
        self.single_market_catalog()
        up.core.commit_page(self.db, "done-wallet", 1, {"data": [trade("wallet", 1, ".2")],
                                                      "pagination": {"next_cursor": None}})
        client = Mock()
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()):
            up.collect(self.db, client, START, END)
        client.get.assert_not_called()
        self.assertEqual(load_market_volume(self.db, START, END)["volume_micro_usdc"], 200000)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone())
        state = self.db.execute("SELECT * FROM collection_progress WHERE market_slug='done-wallet'").fetchone()
        self.assertEqual(state["committed_page"], 1)
        self.assertIsNotNone(state["completed_at"])

    def test_derived_volume_after_wallet_retry_skips_no_market_and_calls_only_false_route(self):
        self.discover([event("retry-wallet", "2026-09-17T00:00:00Z")])
        self.single_market_catalog()
        client = Mock()
        client.get.side_effect = [up.core.FetchError("HTTP 503", status_code=503),
                                  {"data": [trade("wallet", 1, ".4")], "pagination": {"next_cursor": None}}]
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            up.collect(self.db, client, START, END)
        self.assertEqual(client.get.call_count, 2)
        self.assertTrue(all(call.args[1]["taker_only"] == "false" for call in client.get.call_args_list))
        self.assertEqual(load_market_volume(self.db, START, END)["volume_micro_usdc"], 400000)

    def test_pause_after_wallet_page_commit_preserves_group_map_without_market_error(self):
        self.discover([event("done-wallet", "2026-09-17T00:00:00Z")])

        def pause_after_page_commit():
            return self.db.execute("SELECT 1 FROM pending_group_map").fetchone() is not None

        client = Mock()
        client.get.return_value = {"data": [trade("wallet", 1, ".2")], "pagination": {"next_cursor": "continue"}}
        with patch.object(up, "discover_series"), patch.object(up, "discover_missing_markets"), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(up.core.CollectionPaused):
            up.collect(self.db, client, START, END, should_yield=pause_after_page_commit)
        client.get.assert_called_once()
        state = self.db.execute("SELECT * FROM collection_progress WHERE market_slug='done-wallet'").fetchone()
        self.assertEqual(state["committed_page"], 1)
        self.assertEqual(state["next_cursor"], "continue")
        self.assertIsNone(state["completed_at"])
        self.assertIsNone(state["last_error"])
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT transaction_hash,group_id FROM pending_group_map")],
                         [("same-hash", 1)])
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT group_id,typeof(group_id) FROM trades")],
                         [(1, "integer")])

    def test_collect_exit_failure_when_wallet_coverage_is_incomplete(self):
        with patch.object(up, "collect"), patch.object(up.core, "Client"), patch.object(up, "coverage", return_value={
                "is_complete": 0}), contextlib.redirect_stdout(io.StringIO()):
            result = up.main(["collect", "--start-date", str(START), "--end-date", str(END),
                              "--proxy", "http://proxy.invalid", "--db", str(self.path)])
        self.assertEqual(result, 2)

    def test_legacy_btc_start_day_survives_shared_database(self):
        up.core.seed_markets(self.db, START, END)
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at='done'")
        btc_slug = next(up.core.slots(START, END))[0]
        up.core.commit_page(self.db, btc_slug, 1, {"data": [trade("0xa", 1)], "pagination": {"next_cursor": None}})
        self.discover([event("eth-market", "2026-09-17T23:00:00Z", "2026-09-17T22:00:00Z", "eth-up-or-down-hourly")],
                      "eth-up-or-down-hourly")
        up.core.commit_page(self.db, "eth-market", 1, {"data": [trade("0xb", 1, "500")], "pagination": {"next_cursor": None}})
        self.assertEqual(up.core.coverage(self.db, "2026-09-17")["completed_markets"], 288)
        self.assertEqual(self.db.execute("SELECT wallet_total_micro_usdc FROM btc5m_daily_summary").fetchone()[0], 1000000)

    def test_report_is_read_only_and_missing_series_is_visible(self):
        self.discover([event("one", "2026-09-17T00:00:00Z")])
        before = self.path.read_bytes()
        output = Path(self.tmp.name) / "output"
        with patch.object(up.core, "Client", side_effect=AssertionError("离线报告不能联网")), contextlib.redirect_stdout(io.StringIO()):
            result = up.main(["report", "--date", "2026-09-17", "--db", str(self.path), "--output-dir", str(output)])
        self.assertEqual(result, 2)
        self.assertIn("zec-up-or-down-4h", (output / "updown_daily_2026-09-17.md").read_text())
        self.assertFalse((output / "updown_daily_2026-09-17.csv").exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_historical_coverage_and_existing_database_view_upgrade(self):
        for day in (date(2026,8,1), date(2026,8,4), date(2026,8,5)):
            with self.db:
                for series in up.TARGET_SERIES:
                    for ended in up.expected_slots(series, day):
                        slug = up.market_slug(series, ended)
                        self.db.execute(
                            "INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)",
                            (slug,str(day),slug,series,ended.isoformat()))
                        self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')",(slug,))
        self.assertEqual(up.coverage(self.db,'2026-08-01')['expected_markets'],2905)
        self.assertEqual(up.coverage(self.db,'2026-08-04')['expected_markets'],2942)
        self.assertEqual(up.coverage(self.db,'2026-08-05')['expected_markets'],3295)
        self.assertTrue(all(up.coverage(self.db,str(day))['is_complete'] for day in
                            (date(2026,8,1),date(2026,8,4),date(2026,8,5))))
        # 已存在的旧视图也必须被初始化更新。
        self.db.executescript("DROP VIEW updown_coverage; CREATE VIEW updown_coverage AS SELECT 1 AS old;")
        self.db.close()
        self.db=up.connect_database(self.path)
        self.assertEqual(up.coverage(self.db,'2026-08-04')['expected_markets'],2942)
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='zec-updown-5m-1785879300'")
        self.assertFalse(up.coverage(self.db,'2026-08-04')['is_complete'])

    def test_detail_discovery_repairs_inactive_market_without_touching_completed_pages(self):
        day=date(2026,9,3)
        missing='btc-updown-5m-1788448800'
        with self.db:
            for series in up.TARGET_SERIES:
                for ended in up.expected_slots(series,day):
                    slug=up.market_slug(series,ended)
                    if slug==missing:continue
                    self.db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)",
                                    (slug,str(day),slug,series,ended.isoformat()))
                    self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')",(slug,))
        client=Mock()
        client.get.return_value={**event(missing,'2026-09-03T15:25:00Z','2026-09-03T15:20:00Z'),
                                 'active':False,'closed':False}
        with contextlib.redirect_stdout(io.StringIO()):
            up.discover_missing_markets(self.db,client,day,day+timedelta(days=1))
        client.get.assert_called_once_with(up.GAMMA_EVENT_SLUG+missing,{})
        self.assertEqual(up.coverage(self.db,str(day))['discovered_markets'],3295)
        self.assertEqual(up.coverage(self.db,str(day))['completed_markets'],3294)
        self.assertIsNone(self.db.execute('SELECT completed_at FROM collection_progress WHERE market_slug=?',(missing,)).fetchone()[0])

    def test_slots_and_slug_use_end_day_and_new_york_dst(self):
        first=list(up.expected_slots('zec-up-or-down-5m',date(2026,8,4)))
        self.assertEqual(len(first),28)
        self.assertEqual(first[0].isoformat(),'2026-08-04T21:40:00+00:00')
        self.assertEqual(list(up.expected_slots('zec-up-or-down-4h',date(2026,8,4))),[])
        self.assertEqual(up.market_slug('btc-up-or-down-hourly',datetime(2026,9,5,17,tzinfo=timezone.utc)),
                         'bitcoin-up-or-down-september-5-2026-12pm-et')
        self.assertEqual(up.market_slug('btc-up-or-down-hourly',datetime(2026,12,1,0,tzinfo=timezone.utc)),
                         'bitcoin-up-or-down-november-30-2026-6pm-et')
        self.assertEqual(next(up.expected_slots('btc-up-or-down-daily',date(2026,12,1))).hour,17)
        self.assertEqual(up.coverage(self.db,'2026-08-01')['expected_markets'],2905)

    def test_four_hour_slots_follow_new_york_winter_and_summer_phases(self):
        series = 'btc-up-or-down-4h'
        for day, hours in ((date(2026, 11, 9), [1, 5, 9, 13, 17, 21]),
                           (date(2026, 9, 15), [0, 4, 8, 12, 16, 20])):
            with self.subTest(day=day):
                slots = list(up.expected_slots(series, day))
                self.assertEqual([stamp.hour for stamp in slots], hours)
                self.assertTrue(all(stamp.date() == day and stamp.tzinfo == timezone.utc for stamp in slots))
        # 直接核对 Gamma 详情返回的真实开始/结束时间，不能把冬令时市场平移一小时。
        self.assertEqual(up.market_slug(series, datetime(2026, 11, 9, 21, tzinfo=timezone.utc)),
                         'btc-updown-4h-1794243600')
        self.assertEqual(up.market_slug(series, datetime(2026, 9, 15, 20, tzinfo=timezone.utc)),
                         'btc-updown-4h-1789488000')

    def test_four_hour_dst_boundaries_convert_each_new_york_wall_time(self):
        series = 'btc-up-or-down-4h'
        self.assertEqual([stamp.hour for stamp in up.expected_slots(series, date(2026, 3, 8))],
                         [1, 5, 8, 12, 16, 20])
        self.assertEqual([stamp.hour for stamp in up.expected_slots(series, date(2026, 11, 1))],
                         [0, 4, 9, 13, 17, 21])
        # 转换日的真实历史市场可有重叠/缺口；slug 仍使用 Gamma 证实的四个经过小时。
        self.assertEqual(up.market_slug(series, datetime(2026, 3, 8, 9, tzinfo=timezone.utc)),
                         'btc-updown-4h-1772946000')

    def test_complete_series_does_not_lookup_predicted_missing_slots(self):
        day = date(2026, 11, 9)
        series = 'btc-up-or-down-4h'
        # 即使实际时间不匹配推算网格，已达到预期市场数量也不能继续猜 slug。
        with self.db:
            for index, hour in enumerate((0, 4, 8, 12, 16, 20)):
                self.db.execute('INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) '
                                'VALUES (?,?,?,?,?)',
                                (f'listed-{index}', str(day), f'condition-{index}', series,
                                 datetime(2026, 11, 9, hour, tzinfo=timezone.utc).isoformat()))
        client = Mock()
        with patch.dict(up.TARGET_SERIES, {series: 6}, clear=True):
            up.discover_missing_markets(self.db, client, day, day + timedelta(days=1))
        client.get.assert_not_called()

    def test_four_hour_winter_gap_queries_only_correct_missing_slug(self):
        day = date(2026, 11, 9)
        series = 'btc-up-or-down-4h'
        missing_end = datetime(2026, 11, 9, 21, tzinfo=timezone.utc)
        with self.db:
            for ended in up.expected_slots(series, day):
                if ended == missing_end:
                    continue
                slug = up.market_slug(series, ended)
                self.db.execute('INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) '
                                'VALUES (?,?,?,?,?)',
                                (slug, str(day), slug, series, ended.isoformat()))
        slug = up.market_slug(series, missing_end)
        client = Mock()
        client.get.return_value = event(slug, '2026-11-09T21:00:00Z', '2026-11-09T17:00:00Z', series)
        with patch.dict(up.TARGET_SERIES, {series: 6}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            up.discover_missing_markets(self.db, client, day, day + timedelta(days=1))
        client.get.assert_called_once_with(up.GAMMA_EVENT_SLUG + 'btc-updown-4h-1794243600', {})


if __name__ == "__main__":
    unittest.main()
