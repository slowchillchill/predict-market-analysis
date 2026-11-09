import contextlib
import copy
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_bot_price_poster as distribution_poster
import polymarket_updown_poster as poster
import polymarket_updown_price_distribution as distribution

DAY = date(2026, 11, 7)
ROOT = Path(__file__).resolve().parents[1]


class DistributionTweetTests(unittest.TestCase):
    def setUp(self):
        self.overview = poster.PosterData(DAY, {
            "total_markets": 3295, "unique_wallets": 10271,
            "volume_micro_usdc": 8624333260445,
            "wallet_volume_micro_usdc": 18361844342762,
            "suspected_bot_wallets": 331,
            "suspected_bot_volume_micro_usdc": 9261996115790,
            "wallets_meeting_daily_frequency_rule": 0,
        }, None)
        self.distribution = {
            "date_utc": str(DAY), "date_basis": "market_end_utc",
            "market_scope": "all_crypto_updown_series", "series_count": 38,
            "price_basis": "up_equivalent", "scope": "buy_participation",
            "bot_scope": "buy_participation", "count_unit": "reconstructed_trades",
            "notional_basis": "up_equivalent_price_times_shares",
            "notional_share_denominator": "all_wallets_buy_notional",
            "populations": {
                "all_wallets": {"trade_count": 1257825, "shares_exact": "17755455.163855",
                                "notional_usdc_exact": "9087455.3559015617049675",
                                "notional_share_of_market_percent": "100.00"},
                "suspected_bots": {"trade_count": 961099, "shares_exact": "12288146.540417",
                                   "notional_usdc_exact": "6358008.3659104116293819",
                                   "notional_share_of_market_percent": "69.96"},
            },
        }

    def tweet(self):
        result = poster.render_tweet(self.overview, self.distribution)
        self.assertLessEqual(len(result), 280)
        self.assertTrue(result.isascii())
        self.assertTrue("Notional=Up-equivalent price*shares;" in result
                        or "Notional=Up price*shares;" in result)
        self.assertIn("of all BUY notional.", result)
        return result

    def test_reference_day_keeps_both_buy_populations_and_distinct_overview(self):
        self.assertEqual(self.tweet(), (
            "Polymarket | 2026-11-07 UTC market end\n"
            "Market vol 8.62M USDC | 10,271 wallets\n"
            "All BUY: 1.26M trades | 17.76M shares | 9.09M USDC\n"
            "Bot-buyer subset: 961.10K trades | 12.29M shares | 6.36M USDC\n"
            "Notional=Up-equivalent price*shares; bot-buyer participation=69.96% of all BUY notional.\n"
        ))
        self.assertNotIn("50.44%", self.tweet())
        self.assertNotIn("orders", self.tweet().lower())

    def test_empty_bot_population_reports_zero_notional_share(self):
        self.distribution["populations"]["suspected_bots"].update(
            trade_count=0, shares_exact="0", notional_usdc_exact="0",
            notional_share_of_market_percent="0.00")
        text = self.tweet()
        self.assertIn("Bot-buyer subset: 0 trades | 0.00 shares | 0.00 USDC", text)
        self.assertIn("bot-buyer participation=0.00% of all BUY notional", text)

    def test_zero_denominator_is_undefined_and_missing_market_volume_remains_na(self):
        for group in self.distribution["populations"].values():
            group.update(trade_count=0, shares_exact="0", notional_usdc_exact="0",
                         notional_share_of_market_percent=None)
        self.overview.current.update(volume_micro_usdc=None, unique_wallets=0)
        text = self.tweet()
        self.assertIn("Market vol N/A USDC | 0 wallets", text)
        self.assertIn("bot-buyer participation=N/A of all BUY notional", text)
        self.assertNotIn("bot-buyer participation=0.00%", text)

    def test_two_decimal_half_up_rounding_uses_raw_values(self):
        for group in self.distribution["populations"].values():
            group.update(trade_count=1, shares_exact="1.005", notional_usdc_exact="1.005")
        self.overview.current.update(volume_micro_usdc=1005000, unique_wallets=1)
        text = self.tweet()
        self.assertIn("Market vol 1.01 USDC | 1 wallets", text)
        self.assertIn("1 trades | 1.01 shares | 1.01 USDC", text)
        self.assertIn("bot-buyer participation=100.00%", text)
        self.distribution["populations"]["all_wallets"]["shares_exact"] = "20114999.999999"
        self.assertIn("20.11M shares", self.tweet())

    def test_large_counts_compact_without_dropping_metric_definitions(self):
        self.overview.current.update(unique_wallets=987654321012, volume_micro_usdc=987654321000000000)
        for key, count, amount in (("all_wallets", 987654321012, "987654321012"),
                                   ("suspected_bots", 876543210123, "876543210123")):
            self.distribution["populations"][key].update(
                trade_count=count, shares_exact=amount, notional_usdc_exact=amount)
        text = self.tweet()
        self.assertIn("987.65B wallets", text)
        self.assertIn("987.65B trades", text)
        self.assertIn("876.54B shares", text)
        self.assertIn("987.65B USDC", text)

    def test_unsqueezable_tweet_fails_instead_of_truncating_or_omitting_denominator(self):
        self.overview.current.update(unique_wallets=999999999999999,
                                     volume_micro_usdc=999999999999999999999)
        for group in self.distribution["populations"].values():
            group.update(trade_count=999999999999999, shares_exact="999999999999999",
                         notional_usdc_exact="999999999999999")
        with self.assertRaisesRegex(ValueError, "280-character"):
            poster.render_tweet(self.overview, self.distribution)

    def test_invalid_values_scope_and_unrepresentable_counts_raise(self):
        for field, value in (("trade_count", -1), ("trade_count", "1.1"),
                             ("shares_exact", "NaN"), ("notional_usdc_exact", "Infinity"),
                             ("trade_count", 10 ** 30)):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(self.distribution)
                bad["populations"]["all_wallets"][field] = value
                with self.assertRaises(ValueError):
                    poster.render_tweet(self.overview, bad)
        for field, value in (("date_utc", "2026-11-08"), ("price_basis", "down"),
                             ("notional_share_denominator", "wallet_volume"),
                             ("market_scope", "btc-up-or-down-5m")):
            with self.subTest(field=field):
                bad = copy.deepcopy(self.distribution)
                bad[field] = value
                with self.assertRaises(ValueError):
                    poster.render_tweet(self.overview, bad)


class DailyPosterDistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "source.sqlite3"
        self.output = self.root / "posters"
        db = sqlite3.connect(self.path)
        db.executescript((ROOT / "sql/btc5m_schema.sql").read_text())
        db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) "
                   "AS VALUES ('btc',1,NULL),('eth',1,NULL)")
        for slug, series in (("btc-market", "btc"), ("eth-market", "eth")):
            db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                       "VALUES (?,'2026-11-06',?,?,'2026-11-07T00:00:00Z')", (slug, slug, series))
            db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))
        for slug, outcome, side, price, size in (
            ("btc-market", "Up", "SELL", "0.2", "10"),
            ("eth-market", "Down", "BUY", "0.3", "5"),
        ):
            rows = [("buyer", "BUY", outcome, price)]
            rows.append(("counterparty", side, "Up", "0.2" if slug == "btc-market" else "0.7"))
            for index, (wallet, direction, result, raw_price) in enumerate(rows):
                db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (slug, 1, index, wallet, slug + "-tx", 1700000000,
                            direction, result, 0 if result == "Up" else 1,
                            result + "-token", raw_price, size, 0))
        db.commit()
        db.close()
        self.overview = poster.PosterData(DAY, {
            "total_markets": 2, "unique_wallets": 2,
            "volume_basis": "taker_only", "volume_micro_usdc": 100000000,
            "wallet_volume_micro_usdc": 200000000,
            "suspected_bot_wallets": 0, "suspected_bot_volume_micro_usdc": 0,
        }, None)

    def args(self, *, include=True):
        args = ["--date", str(DAY), "--db", str(self.path), "--output-dir", str(self.output)]
        return args + (["--with-distribution"] if include else [])

    def test_daily_outputs_include_full_scope_distribution_after_closing_overview_transaction(self):
        original_bytes = self.path.read_bytes()
        overview_connections = []
        real_loader = distribution.load_data
        sidecar_checks = []

        def load_overview(db, day, *, bot_first_seen=None):
            self.assertTrue(db.in_transaction)
            overview_connections.append(db)
            return self.overview

        def load_distribution(db, day, *, series_slug=None, bot_first_seen=None):
            self.assertEqual(day, DAY)
            self.assertIsNone(series_slug)
            self.assertFalse(db.in_transaction)
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                overview_connections[0].execute("SELECT 1")
            return real_loader(db, day, series_slug=series_slug, bot_first_seen=bot_first_seen)

        def write_sidecars(source):
            payload = json.loads(source.read_text())
            ref = payload["trade_distribution"]
            for key, digest_key in (("json_file", "json_sha256"), ("image_file", "image_sha256")):
                asset = source.parent / ref[key]
                self.assertTrue(asset.is_file())
                self.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(), ref[digest_key])
            sidecar_checks.append(ref)
            self.assertTrue(source.with_name(source.stem + "_tweet.md").is_file())

        with patch.object(poster, "load_data", side_effect=load_overview), \
                patch.object(distribution, "load_data", side_effect=load_distribution), \
                patch.object(poster, "render_poster", return_value=(Image.new("RGB", (40, 50)), [])), \
                patch.object(distribution_poster, "render_poster", return_value=(Image.new("RGB", (40, 50)), [])), \
                patch.object(poster, "write_sidecars", side_effect=write_sidecars), \
                contextlib.redirect_stdout(io.StringIO()):
            result = poster.main(self.args())

        self.assertEqual(result, 0)
        self.assertEqual(original_bytes, self.path.read_bytes())
        self.assertEqual(len(sidecar_checks), 1)
        source = self.output / f"updown_{DAY}.json"
        payload = json.loads(source.read_text())
        self.assertEqual(payload["current"], self.overview.current)
        self.assertEqual(payload["display"]["volume"], "100.00")
        ref = payload["trade_distribution"]
        self.assertEqual(ref["date_utc"], str(DAY))
        self.assertEqual(ref["layout_version"], "buy_price_distribution_chart_v1")
        self.assertEqual(ref["market_scope"], "all_crypto_updown_series")
        self.assertEqual(ref["series_count"], 2)
        self.assertEqual(ref["price_basis"], "up_equivalent")
        self.assertEqual(ref["notional_basis"], "up_equivalent_price_times_shares")
        self.assertEqual(ref["json_file"], f"updown_buy_trade_distribution_all_series_{DAY}.json")
        data = json.loads((self.output / ref["json_file"]).read_text())
        self.assertEqual(data["included_series_slugs"], ["btc", "eth"])
        self.assertEqual(data["populations"]["all_wallets"]["trade_count"], 2)
        self.assertEqual(data["populations"]["all_wallets"]["shares_exact"], "15")
        self.assertEqual(data["populations"]["all_wallets"]["notional_usdc_exact"], "5.5")
        self.assertNotEqual(data["populations"]["all_wallets"]["notional_usdc_exact"], payload["display"]["volume"])
        tweet = source.with_name(source.stem + "_tweet.md").read_text()
        self.assertEqual(tweet, poster.render_tweet(self.overview, data))
        self.assertIn("All BUY: 2 trades | 15.00 shares | 5.50 USDC", tweet)
        self.assertIn("Bot-buyer subset: 0 trades", tweet)
        self.assertLessEqual(len(tweet), 280)

    def test_tweet_failure_or_oversize_preserves_existing_outputs(self):
        self.output.mkdir()
        sentinel = self.output / f"updown_{DAY}.json"
        sentinel.write_text('{"existing": true}\n')
        before = sentinel.read_bytes()
        for failure in (ValueError("unrepresentable metrics"), "x" * 281):
            with self.subTest(failure=str(failure)[:30]):
                behavior = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(poster, "load_data", return_value=self.overview), \
                        patch.object(poster, "render_tweet", **behavior) as tweet, \
                        patch.object(poster, "render_poster") as render, \
                        patch.object(distribution_poster, "write_outputs") as export, \
                        patch.object(poster, "write_sidecars") as sidecars, \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(poster.main(self.args()), 2)
                self.assertEqual(tweet.call_args.args[0], self.overview)
                self.assertEqual(tweet.call_args.args[1]["populations"]["all_wallets"]["trade_count"], 2)
                self.assertIn("推文生成未完成", stderr.getvalue())
                self.assertEqual(sentinel.read_bytes(), before)
                self.assertEqual(list(self.output.iterdir()), [sentinel])
                render.assert_not_called()
                export.assert_not_called()
                sidecars.assert_not_called()

    def test_bad_distribution_prevents_new_daily_assets_and_sidecars(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE trades SET raw_size='6' WHERE market_slug='eth-market' AND row_number=1")
        with patch.object(poster, "load_data", return_value=self.overview), \
                patch.object(poster, "write_sidecars") as sidecars, \
                patch.object(poster, "render_poster") as render, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(poster.main(self.args()), 2)
        self.assertIn("交易分布统计未完成", stderr.getvalue())
        self.assertFalse(self.output.exists())
        render.assert_not_called()
        sidecars.assert_not_called()

    def test_distribution_sqlite_error_preserves_existing_outputs(self):
        self.output.mkdir()
        sentinel = self.output / f"updown_{DAY}.json"
        sentinel.write_text('{"existing": true}\n')
        before = sentinel.read_bytes()
        with patch.object(poster, "load_data", return_value=self.overview), \
                patch.object(distribution, "load_data", side_effect=sqlite3.OperationalError("locked")), \
                patch.object(poster, "write_sidecars") as sidecars, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(poster.main(self.args()), 2)
        self.assertEqual(sentinel.read_bytes(), before)
        self.assertEqual(list(self.output.iterdir()), [sentinel])
        sidecars.assert_not_called()

    def test_without_flag_does_not_compute_or_export_distribution(self):
        with patch.object(poster, "load_data", return_value=self.overview), \
                patch.object(distribution, "load_data") as load, \
                patch.object(distribution_poster, "write_outputs") as write, \
                patch.object(poster, "render_poster", return_value=(Image.new("RGB", (40, 50)), [])), \
                patch.object(poster, "write_sidecars"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(poster.main(self.args(include=False)), 0)
        load.assert_not_called()
        write.assert_not_called()
        payload = json.loads((self.output / f"updown_{DAY}.json").read_text())
        self.assertNotIn("trade_distribution", payload)
        self.assertEqual(sorted(path.suffix for path in self.output.iterdir()), [".json", ".md", ".png"])

    def test_shared_output_writer_uses_shared_statistics_export(self):
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            data = distribution.load_data(db, DAY)
        real_export = distribution.save_statistics
        with patch.object(distribution, "save_statistics", wraps=real_export) as export, \
                patch.object(distribution_poster, "render_poster", return_value=(Image.new("RGB", (40, 50)), [])):
            paths = distribution_poster.write_outputs(data, self.output)
        export.assert_called_once_with(data, self.output)
        self.assertEqual(paths["png"], paths["json"].with_suffix(".png"))
        self.assertEqual(json.loads(paths["json"].read_text()), data)
        self.assertFalse(list(self.output.glob("*.tmp")))
        with Image.open(paths["png"]) as image:
            self.assertEqual(image.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
