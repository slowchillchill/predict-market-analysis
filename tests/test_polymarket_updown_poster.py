import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_poster as poster

DAY = date(2026, 9, 17)


class PosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data.sqlite3"
        self.db = poster.daily.connect_database(self.path)
        self.seed_day(DAY)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def seed_day(self, day):
        with self.db:
            for series, count in poster.daily.TARGET_SERIES.items():
                for i in range(count):
                    slug = f"{series}-{day}-{i}"
                    self.db.execute(
                        "INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)",
                        (slug, (day-timedelta(days=1)).isoformat(), slug, series, f"{day}T00:00:00Z"))
                    self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))
                    self.db.execute("INSERT INTO market_volume(market_slug,completed_at) VALUES (?,'done')", (slug,))

    def add_trades(self, wallet, count, span, *, day=DAY, series="btc-up-or-down-5m", page=1, price="1"):
        slug = f"{series}-{day}-0"
        # 故意使用目标 UTC 日之外的成交时间，防止误改为成交日筛选。
        first = 1789510000
        rows = [dict(proxy_wallet=wallet, transaction_hash="same", timestamp=first+(span*i//(count-1) if count>1 else 0),
                     side="BUY", outcome="Up", outcome_index=0, token_id="token", price=price, size="1")
                for i in range(count)]
        poster.daily.core.commit_page(self.db, slug, page, {"data": rows, "pagination": {"next_cursor": None}})
        self.db.execute("UPDATE market_volume SET amount_micro_usdc=amount_micro_usdc+? WHERE market_slug=?",
                        (sum(poster.daily.core.micro_usdc(r['price'], r['size']) for r in rows), slug))
        self.db.commit()

    def test_classification_boundaries_and_cross_market_wallets(self):
        self.seed_day(DAY-timedelta(days=1))
        self.add_trades("boundary", 91, 5400, page=1)  # 60 秒、90 分钟，包含两个等号。
        self.add_trades("too-slow", 90, 5400, page=2)
        self.add_trades("too-short", 100, 5399, page=3)
        self.add_trades("cross-market", 46, 5401, page=4)
        self.add_trades("cross-market", 46, 5401, series="eth-up-or-down-5m")
        self.add_trades("single", 1, 0, page=5)
        prev = DAY-timedelta(days=1)
        self.add_trades("prior-a", 1, 0, day=prev, page=1, price="200")
        self.add_trades("prior-b", 1, 0, day=prev, page=2, price="200")
        data = poster.load_data(self.db, DAY)
        self.assertEqual(data.current["total_markets"], 3295)
        self.assertEqual(data.current["unique_wallets"], 5)
        self.assertEqual(data.current["volume_micro_usdc"], 374000000)
        self.assertEqual(data.current["suspected_bot_wallets"], 2)
        self.assertEqual(data.current["suspected_bot_volume_micro_usdc"], 183000000)
        values = poster.display_values(data)
        self.assertEqual(values["volume_change"], "-6.50%")
        self.assertEqual(values["wallet_change"], "+150.00%")
        self.assertEqual(values["bot_volume_share"], "48.93%")

    def test_end_day_upper_boundary_is_excluded(self):
        self.add_trades("in-scope", 1, 0)
        with self.db:
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                            "VALUES ('next','2026-09-17','next','btc-up-or-down-5m','2026-09-18T00:00:00Z')")
            self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES ('next','done')")
            self.db.execute("INSERT INTO trades VALUES ('next',1,0,'excluded','hash',1789650000,'BUY','Up',0,'token','1000','1',1000000000)")
        self.assertEqual(poster.load_data(self.db, DAY).current["volume_micro_usdc"], 1000000)

    def test_partial_previous_day_does_not_create_false_growth(self):
        previous = DAY-timedelta(days=1)
        self.seed_day(previous)
        self.add_trades("current", 1, 0, price="100")
        self.add_trades("previous", 1, 0, day=previous)
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug=?",
                            (f"eth-up-or-down-5m-{previous}-0",))
        data = poster.load_data(self.db, DAY)
        self.assertIsNone(data.previous)
        values = poster.display_values(data)
        self.assertEqual(values["volume"], "100.00")
        self.assertEqual(values["volume_change"], "N/A")
        self.assertEqual(values["wallet_change"], "N/A")
        self.assertEqual(values["volume_change_caption"], "prior day unavailable")

    def test_complete_zero_day_has_zero_amounts_and_undefined_ratios(self):
        self.seed_day(DAY-timedelta(days=1))
        data = poster.load_data(self.db, DAY)
        self.assertEqual(data.current["unique_wallets"], 0)
        self.assertEqual(data.current["suspected_bot_wallets"], 0)
        values = poster.display_values(data)
        self.assertEqual(values["volume"], "0.00")
        self.assertEqual(values["bot_volume"], "0.00")
        self.assertEqual(values["volume_change"], "N/A")
        self.assertEqual(values["volume_change_caption"], "prior day = 0")
        self.assertEqual(values["bot_volume_share"], "N/A")

    def test_incomplete_current_day_returns_two_without_poster(self):
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug=?",
                            (f"btc-up-or-down-daily-{DAY}-0",))
        out = Path(self.tmp.name) / "poster"
        with contextlib.redirect_stderr(io.StringIO()):
            code = poster.main(["--date", str(DAY), "--db", str(self.path), "--output-dir", str(out)])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_two_decimal_rounding_and_no_negative_zero(self):
        self.assertEqual(poster.amount_text(1005000), "1.01")
        self.assertEqual(poster.amount_text(1004999), "1.00")
        self.assertEqual(poster.percent_text(201, 20000), "1.01%")
        self.assertEqual(poster.percent_text(-1, 1000000, change=True), "0.00%")
        self.assertEqual(poster.percent_text(0, 1, change=True), "0.00%")

    def test_cli_is_offline_read_only_and_outputs_opaque_png(self):
        from PIL import Image
        self.add_trades("somebody", 1, 0)
        before = self.path.read_bytes()
        out = Path(self.tmp.name) / "poster"
        with contextlib.redirect_stdout(io.StringIO()):
            code = poster.main(["--date", str(DAY), "--db", str(self.path), "--output-dir", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(before, self.path.read_bytes())
        with Image.open(out/f"updown_{DAY}.png") as im:
            self.assertEqual(im.size, (1600, 2000))
            self.assertEqual(im.mode, "RGB")
        result = json.loads((out/f"updown_{DAY}.json").read_text())
        self.assertEqual(result["date_basis"], "market_end_utc")
        self.assertEqual(result["display"]["volume"], "1.00")
        self.assertEqual(result["display"]["volume_change"], "N/A")
        tweet = (out/f"updown_{DAY}_tweet.md").read_text(encoding="utf-8")
        self.assertIn("1.00 USDC market volume (N/A; prior day unavailable)", tweet)
        self.assertIn("1 trading wallets (N/A; prior day unavailable)", tweet)
        self.assertTrue(tweet.endswith("#Polymarket\n"))

    def test_tweet_matches_reference_metrics_and_updates_trend(self):
        current = dict(total_markets=3295, unique_wallets=14774, volume_micro_usdc=20111703726273,
                       wallet_volume_micro_usdc=20111703726273,
                       suspected_bot_wallets=293, suspected_bot_volume_micro_usdc=11084715613058)
        previous = dict(unique_wallets=12917, volume_micro_usdc=20367207857854)
        text = poster.render_tweet(poster.PosterData(DAY, current, previous))
        self.assertEqual(text, (
            "Polymarket Crypto Up/Down | Sep 17, 2026 UTC\n\n"
            "20.11M USDC market volume (-1.25%)\n"
            "14,774 trading wallets (+14.38%)\n"
            "293 suspected bot wallets: 55.12% of wallet volume\n\n"
            "More wallets, lower volume.\n"
            "Markets ending that day; taker side once. Bot criteria in chart.\n"
            "#Polymarket\n"
        ))
        current.update(unique_wallets=11731, volume_micro_usdc=22763114385171)
        previous.update(unique_wallets=14774, volume_micro_usdc=20111703726273)
        text = poster.render_tweet(poster.PosterData(DAY+timedelta(days=1), current, previous))
        self.assertIn("Sep 18, 2026 UTC", text)
        self.assertIn("22.76M USDC market volume (+13.18%)", text)
        self.assertIn("11,731 trading wallets (-20.60%)", text)
        self.assertIn("Fewer wallets, higher volume.", text)

    def test_tweet_missing_zero_and_rounded_changes_do_not_claim_a_trend(self):
        current = dict(total_markets=3295, unique_wallets=0, volume_micro_usdc=0, wallet_volume_micro_usdc=0,
                       suspected_bot_wallets=0, suspected_bot_volume_micro_usdc=0)
        for previous, reason in ((None, "prior day unavailable"),
                                 (dict(unique_wallets=0, volume_micro_usdc=0), "prior day = 0")):
            with self.subTest(reason=reason):
                text = poster.render_tweet(poster.PosterData(DAY, current, previous))
                self.assertIn(f"0.00 USDC market volume (N/A; {reason})", text)
                self.assertIn("0 suspected bot wallets: N/A of wallet volume", text)
                self.assertNotIn("More wallets", text)
                self.assertNotIn("Fewer wallets", text)

        current.update(unique_wallets=100001, volume_micro_usdc=20000000000001)
        previous = dict(unique_wallets=100000, volume_micro_usdc=20000000000000)
        text = poster.render_tweet(poster.PosterData(DAY, current, previous))
        self.assertIn("20.00M USDC market volume (0.00%)", text)
        self.assertIn("100,001 trading wallets (0.00%)", text)
        self.assertNotIn("More wallets", text)

    def test_missing_market_volume_keeps_wallet_metrics(self):
        current = dict(total_markets=1, unique_wallets=2, volume_micro_usdc=None,
                       wallet_volume_micro_usdc=20_000_000, suspected_bot_wallets=1,
                       suspected_bot_volume_micro_usdc=5_000_000)
        previous = dict(unique_wallets=1, volume_micro_usdc=10_000_000)
        data = poster.PosterData(DAY, current, previous)
        values = poster.display_values(data)
        self.assertEqual(values["volume"], "N/A")
        self.assertEqual(values["volume_change"], "N/A")
        self.assertEqual(values["wallet_change"], "+100.00%")
        self.assertEqual(values["bot_volume_share"], "25.00%")
        self.assertIn("N/A USDC market volume", poster.render_tweet(data))
        current["volume_micro_usdc"] = 10_000_000
        previous["volume_micro_usdc"] = None
        self.assertEqual(poster.display_values(data)["volume_change"], "N/A")
        self.assertEqual(poster.display_values(data)["bot_volume_share"], "25.00%")

    def test_tweet_millions_round_from_raw_amount_without_double_rounding(self):
        current = dict(total_markets=3295, unique_wallets=1, volume_micro_usdc=20114999999999,
                       wallet_volume_micro_usdc=20114999999999,
                       suspected_bot_wallets=0, suspected_bot_volume_micro_usdc=0)
        text = poster.render_tweet(poster.PosterData(DAY, current, None))
        self.assertIn("20.11M USDC market volume", text)
        current["volume_micro_usdc"] = 20115000000000
        text = poster.render_tweet(poster.PosterData(DAY, current, None))
        self.assertIn("20.12M USDC market volume", text)

    def test_long_values_and_missing_baseline_do_not_overlap_or_overflow(self):
        values = poster.display_values(poster.load_data(self.db, DAY))
        values.update(volume="9,223,372,036,854.78", bot_volume="9,223,372,036,854.78",
                      total_markets="9,999,999", unique_wallets="999,999,999",
                      bot_wallets="999,999,999", bot_volume_share="100.00%",
                      volume_change="-100.00%", volume_change_caption="vs. previous day")
        image, boxes = poster.render_poster(values)
        for item in boxes:
            left, top, right, bottom = item["bounds"]
            self.assertGreaterEqual(left, 100)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, 1485)
            self.assertLessEqual(bottom, image.height)
        for a, b in combinations(boxes, 2):
            x1,y1,x2,y2 = a["bounds"]
            u1,v1,u2,v2 = b["bounds"]
            self.assertFalse(min(x2,u2)>max(x1,u1) and min(y2,v2)>max(y1,v1),
                             f"文本相交：{a['text']} / {b['text']}")
        for index, item in enumerate(boxes):
            if item["text"] == "USDC":
                self.assertLessEqual(abs(item["baseline"]-boxes[index-1]["baseline"]), 1)


if __name__ == "__main__":
    unittest.main()
