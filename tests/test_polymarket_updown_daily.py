import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_daily as up

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
        with patch.object(up, "discover_series"), contextlib.redirect_stdout(io.StringIO()):
            up.collect(self.db, client, START, END)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.get.call_args.args[1]["condition"], "condition-new")
        self.assertNotIn("start", client.get.call_args.args[1])
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
        # 缺少一个完整系列时，不能把其成交当作零并声称完整。
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug LIKE 'zec-up-or-down-4h-%'")
        self.assertFalse(up.coverage(self.db, "2026-09-17")["is_complete"])
        self.assertIsNone(self.db.execute("SELECT * FROM updown_daily_summary").fetchone())

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
        output = Path(self.tmp.name) / "output"
        with patch.object(up.core, "Client", side_effect=AssertionError("离线报告不能联网")), contextlib.redirect_stdout(io.StringIO()):
            result = up.main(["report", "--date", "2026-09-17", "--db", str(self.path), "--output-dir", str(output)])
        self.assertEqual(result, 2)
        self.assertIn("zec-up-or-down-4h", (output / "updown_daily_2026-09-17.md").read_text())
        self.assertFalse((output / "updown_daily_2026-09-17.csv").exists())


if __name__ == "__main__":
    unittest.main()
