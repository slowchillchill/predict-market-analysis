import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_daily as daily
import polymarket_updown_price_distribution as distribution

ROOT = Path(__file__).resolve().parents[1]


class DistributionCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "source.sqlite3"
        db = sqlite3.connect(self.path)
        # Preserve the hash-based fixture to cover read-only legacy statistics.
        db.executescript((ROOT / "tests/fixtures/btc5m_legacy_schema.sql").read_text())
        db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) "
                   "AS VALUES ('btc-up-or-down-5m',1,NULL),('eth-up-or-down-5m',1,NULL)")
        for coin in ("btc", "eth"):
            db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                       "VALUES (?,?,?,?,?)", (coin, "2026-11-06", coin,
                                              f"{coin}-up-or-down-5m", "2026-11-07T00:00:00Z"))
            db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (coin,))
        for i in range(91):
            stamp = 1700000000 + i * 60
            for offset, wallet, side in ((0, "bot", "BUY"), (1, f"human-{i}", "SELL")):
                db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("btc", 1, i * 2 + offset, wallet, f"tx-{i}", stamp, side,
                            "Up", 0, "btc-up", "0.1", "2", 200000))
        db.commit()
        db.close()

    def run_command(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(daily.core, "Client", side_effect=AssertionError("must not collect")):
            return daily.main(["distribution", "--date", "2026-11-07", "--db", str(self.path), *extra])

    def test_exports_all_registered_series_exact_statistics_without_database_writes(self):
        before = self.path.read_bytes()
        folder = self.root / "stats"
        self.assertEqual(self.run_command("--output-dir", str(folder)), 0)
        self.assertEqual(self.path.read_bytes(), before)
        paths = list(folder.iterdir())
        self.assertEqual([path.name for path in paths],
                         ["updown_buy_trade_distribution_all_series_2026-11-07.json"])
        payload = json.loads(paths[0].read_text())
        self.assertEqual(payload["market_scope"], "all_crypto_updown_series")
        self.assertEqual(payload["series_count"], 2)
        self.assertEqual(payload["total_markets"], 2)
        self.assertEqual(payload["suspected_bot_wallets"], 1)
        for pop in payload["populations"].values():
            self.assertEqual(pop["trade_count"], 91)
            self.assertEqual(pop["shares_exact"], "182")
            self.assertEqual(pop["notional_usdc_exact"], "18.2")
            self.assertEqual(pop["bins"][1]["trade_count"], 91)
            self.assertEqual(pop["bins"][1]["interval_cents"], "[10, 20)")

    def test_incomplete_or_unbalanced_data_returns_two_without_export(self):
        for mode in ("incomplete", "unbalanced"):
            with self.subTest(mode=mode):
                db = sqlite3.connect(self.path)
                if mode == "incomplete":
                    db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='eth'")
                else:
                    db.execute("UPDATE collection_progress SET completed_at='done'")
                    db.execute("UPDATE trades SET raw_size='3' WHERE market_slug='btc' AND row_number=1")
                db.commit()
                db.close()
                output = self.root / mode
                self.assertEqual(self.run_command("--output-dir", str(output)), 2)
                self.assertFalse(output.exists())

    def test_default_statistics_output_directory(self):
        with patch.object(daily.core, "ROOT", self.root):
            self.assertEqual(self.run_command(), 0)
        path = self.root / "outputs/updown/updown_buy_trade_distribution_all_series_2026-11-07.json"
        self.assertTrue(path.exists())

    def test_missing_database_is_not_created(self):
        missing = self.root / "missing.sqlite3"
        with contextlib.redirect_stderr(io.StringIO()):
            code = daily.main(["distribution", "--date", "2026-11-07", "--db", str(missing),
                               "--output-dir", str(self.root / "stats")])
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())

    def test_statistics_command_imports_and_runs_without_pillow_or_network_dependencies(self):
        # -S hides installed site packages; this verifies the data-only path is standard-library-only.
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/polymarket_updown_daily.py"),
             "distribution", "--date", "2026-11-07", "--db", str(self.path),
             "--output-dir", str(self.root / "stdlib")],
            text=True, capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "stdlib/updown_buy_trade_distribution_all_series_2026-11-07.json").exists())

    def test_failed_atomic_export_preserves_existing_json_and_cleans_temporary_file(self):
        payload = {"date_utc": "2026-11-07", "series_slug": None, "precision": "123.000000000000000001"}
        folder = self.root / "atomic"
        target = distribution.save_statistics(payload, folder)
        original = target.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("simulated export failure")):
            with self.assertRaises(OSError):
                distribution.save_statistics({**payload, "precision": "different"}, folder)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(folder.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
