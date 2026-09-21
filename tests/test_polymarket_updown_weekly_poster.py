import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_weekly_poster as weekly


class WeeklyTests(unittest.TestCase):
    def test_previous_complete_week_on_monday_sunday_and_year_boundary(self):
        for today, expected in [(date(2026,9,21),date(2026,9,14)),
                                (date(2026,9,28),date(2026,9,21)),
                                (date(2026,9,27),date(2026,9,14)),
                                (date(2026,1,1),date(2025,12,22)),
                                (date(2026,1,5),date(2025,12,29))]:
            with self.subTest(today=today):
                self.assertEqual(weekly.previous_week_start(today),expected)

    def setUp(self):
        self.start = date(2026, 9, 14)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE updown_coverage(date_utc TEXT, is_complete INTEGER, completed_markets INTEGER);
            CREATE TABLE updown_target_series(series_slug TEXT);
            CREATE TABLE markets(slug TEXT, series_slug TEXT, end_time TEXT);
            CREATE TABLE trades(market_slug TEXT, wallet TEXT, timestamp INTEGER, amount_micro_usdc INTEGER);
        """)
        for i in range(7):
            self.db.execute("INSERT INTO updown_coverage VALUES (?,1,3)", (str(self.start+timedelta(days=i)),))
        for series in ("btc-up-or-down-5m", "eth-up-or-down-15m", "solana-up-or-down-hourly"):
            self.db.execute("INSERT INTO updown_target_series VALUES (?)", (series,))
        self.market_index = 0

    def tearDown(self):
        self.db.close()

    def trades(self, offset, wallet, count, span, *, series="btc-up-or-down-5m", amount=1_000_000):
        self.market_index += 1
        slug = str(self.market_index)
        day = self.start + timedelta(days=offset)
        self.db.execute("INSERT INTO markets VALUES (?,?,?)", (slug, series, f"{day}T00:00:00Z"))
        # 成交发生于统计周之外：归属必须由市场结束日决定。
        self.db.executemany("INSERT INTO trades VALUES (?,?,?,?)", [
            (slug, wallet, 100000 + (span*i//(count-1) if count > 1 else 0), amount)
            for i in range(count)])

    def test_week_union_daily_bot_classification_and_end_boundary(self):
        self.trades(0, "repeated", 46, 5400)
        self.trades(0, "repeated", 45, 5400, series="eth-up-or-down-15m")
        self.trades(1, "repeated", 1, 0, amount=9_000_000)
        self.trades(2, "repeated", 91, 5400)
        self.trades(1, "second", 1, 0, series="solana-up-or-down-hourly", amount=2_000_000)
        self.trades(3, "slow", 90, 5400)
        self.trades(3, "short", 91, 5399)
        self.trades(7, "next-week", 1, 0, amount=999_000_000)
        self.trades(-1, "last-week", 1, 0, amount=999_000_000)
        self.trades(1, "unrelated", 1, 0, series="outside-series", amount=999_000_000)
        result = weekly.load_week(self.db, self.start)
        self.assertEqual(result["unique_wallets"], 4)
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["suspected_bot_volume_micro_usdc"], 182_000_000)
        self.assertEqual(result["volume_micro_usdc"], 374_000_000)
        self.assertEqual(result["asset_volume_micro_usdc"], {"BTC":327_000_000,"ETH":45_000_000,"SOL":2_000_000})
        self.assertEqual(result["daily_volume_micro_usdc"]["2026-09-15"], 11_000_000)
        self.assertEqual(len(result["daily_volume_micro_usdc"]), 7)

    def test_missing_day_and_prior_week(self):
        self.db.execute("DELETE FROM updown_coverage WHERE date_utc='2026-09-20'")
        with self.assertRaises(weekly.daily.IncompleteDay):
            weekly.load_week(self.db, self.start)
        self.assertIsNone(weekly.load_week(self.db, self.start, required=False))
        self.assertEqual(weekly.changes({}, None, "unique_wallets"), ("N/A", "prior week unavailable"))

    def test_percentage_points_use_unrounded_ratios(self):
        current = {"volume_micro_usdc": 3, "suspected_bot_volume_micro_usdc": 1}
        prior = {"volume_micro_usdc": 7, "suspected_bot_volume_micro_usdc": 2}
        self.assertEqual(weekly.bot_share_change(current, prior)[0], "+4.76 pp")
        self.assertEqual(weekly.changes({"v":150}, {"v":125}, "v")[0], "+20.00%")
        self.assertEqual(weekly.changes({"v":1}, {"v":0}, "v")[0], "N/A")
        current["volume_micro_usdc"] = 0
        self.assertEqual(weekly.bot_share_change(current, prior)[0], "N/A")

    def test_tweet_values_and_no_observation(self):
        current = {
            "start_date_utc": "2026-09-14", "end_date_utc_exclusive": "2026-09-21",
            "volume_micro_usdc": 150_000_000_000_000, "unique_wallets": 48600,
            "suspected_bot_wallets": 840, "suspected_bot_volume_micro_usdc": 78_000_000_000_000,
        }
        prior = {"volume_micro_usdc": 125_000_000_000_000, "unique_wallets": 45000,
                 "suspected_bot_volume_micro_usdc": 61_250_000_000_000}
        self.assertEqual(weekly.render_tweet(current, prior),
            "#Polymarket Crypto Up/Down | Weekly\n"
            "2026-09-14/2026-09-20 UTC\n"
            "150.00M USDC wallet volume (+20.00% WoW)\n"
            "48,600 unique wallets (+8.00% WoW)\n"
            "840 suspected bot wallets: 52.00% of volume\n"
            "Market end dates; buys+sells. Bot rules in chart.\n"
            )

    def test_tweet_missing_baseline_zero_volume_and_cross_year(self):
        current = {"start_date_utc":"2025-12-29", "end_date_utc_exclusive":"2026-01-05",
                   "volume_micro_usdc": 1_005_000, "unique_wallets": 1,
                   "suspected_bot_wallets":0, "suspected_bot_volume_micro_usdc":0}
        text = weekly.render_tweet(current, None)
        self.assertIn("2025-12-29/2026-01-04 UTC", text)
        self.assertIn("1.01 USDC wallet volume (N/A: no prior week)", text)
        zero = {**current, "volume_micro_usdc":0, "unique_wallets":0}
        text = weekly.render_tweet(zero, zero)
        self.assertIn("0.00 USDC wallet volume (N/A: zero base)", text)
        self.assertIn("0 unique wallets (N/A: zero base)", text)
        self.assertIn("N/A (zero volume)", text)

    def test_cli_creates_tweet_alongside_poster_and_json(self):
        self.trades(0, "wallet", 1, 0, amount=1_000_000_000_000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.sqlite3"
            target = sqlite3.connect(path)
            self.db.commit()
            self.db.backup(target)
            target.close()
            original = path.read_bytes()
            output = Path(folder) / "posters"
            with contextlib.redirect_stdout(io.StringIO()):
                result = weekly.main(["--week-start", str(self.start), "--db", str(path),
                                      "--output-dir", str(output)])
            self.assertEqual(result, 0)
            stem = "updown_weekly_2026-09-14_2026-09-20"
            self.assertEqual({p.name for p in output.iterdir()},
                             {f"{stem}.png", f"{stem}.json", f"{stem}_tweet.md"})
            text = (output / f"{stem}_tweet.md").read_text(encoding="utf-8")
            self.assertIn("1.00M USDC wallet volume (N/A: no prior week)", text)
            self.assertIn("1 unique wallets (N/A: no prior week)", text)
            data = json.loads((output / f"{stem}.json").read_text())
            self.assertEqual(data["current"]["volume_micro_usdc"], 1_000_000_000_000)
            self.assertEqual(path.read_bytes(), original)

    def test_tweet_fits_x_with_missing_zero_and_large_values(self):
        # ASCII 模板不含链接，按 X 官方规则每个字符（包括换行）权重为 1。
        current = {"start_date_utc":"2026-09-14", "end_date_utc_exclusive":"2026-09-21",
                   "volume_micro_usdc":144675200934478, "unique_wallets":29458,
                   "suspected_bot_wallets":697, "suspected_bot_volume_micro_usdc":79480660514454}
        prior = {"volume_micro_usdc":147290101021781, "unique_wallets":25280,
                 "suspected_bot_volume_micro_usdc":83393159647792}
        zero = {**current, "volume_micro_usdc":0, "unique_wallets":0,
                "suspected_bot_wallets":0, "suspected_bot_volume_micro_usdc":0}
        large = {**current, "volume_micro_usdc":2**63-1, "unique_wallets":2**63-1,
                 "suspected_bot_wallets":2**63-1, "suspected_bot_volume_micro_usdc":2**63-1}
        small = {"volume_micro_usdc":1,"unique_wallets":1,"suspected_bot_volume_micro_usdc":0}
        for now, before in [(current,prior),(current,None),(current,zero),(zero,zero),
                            (large,small),(large,None)]:
            with self.subTest(current=now, previous=before):
                text = weekly.render_tweet(now,before)
                self.assertTrue(text.isascii())
                self.assertNotIn("http", text)
                self.assertLessEqual(len(text),280)
                self.assertIn("wallet volume", text)
                self.assertIn("unique wallets", text)
                self.assertIn("of volume", text)


if __name__ == "__main__":
    unittest.main()
