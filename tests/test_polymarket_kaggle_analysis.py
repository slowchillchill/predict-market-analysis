import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_kaggle_analysis as cloud
import polymarket_updown_bot_registry as registry
import polymarket_updown_period as legacy_period
import polymarket_updown_period_distribution as legacy_distribution
import polymarket_updown_poster as legacy_daily
from polymarket_updown_distribution_assets import validate_distribution

ROOT = Path(__file__).resolve().parents[1]
START = date(2026, 11, 2)
END = START + timedelta(days=7)


def make_database(path, start=START, end=END):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript((ROOT / "sql/btc5m_schema.sql").read_text())
    db.executescript((ROOT / "sql/market_volume_schema.sql").read_text())
    db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) AS "
               "VALUES ('btc-up-or-down-daily',1,NULL),('eth-up-or-down-daily',1,NULL)")
    db.executescript((ROOT / "sql/updown_daily.sql").read_text())
    with db:
        for offset in range((end - start).days):
            day = start + timedelta(days=offset)
            for coin in ("btc", "eth"):
                slug = f"{coin}-{day}"
                db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)",
                           (slug, str(day - timedelta(days=1)), slug, f"{coin}-up-or-down-daily", f"{day}T00:00:00Z"))
                db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))
                db.execute("INSERT INTO market_volume(market_slug,completed_at) VALUES (?,'done')", (slug,))
            if offset < 3:
                slug = f"btc-{day}"
                for row, wallet, side in ((0, "bot", "BUY"), (1, f"human-{offset % 2}", "SELL")):
                    db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (slug, 1, row, wallet, "same-hash", 1700000000 + offset, side, "Up", 0, "token", ".2", "10", 2000000))
                db.execute("UPDATE market_volume SET amount_micro_usdc=2000000 WHERE market_slug=?", (slug,))
    db.close()


class KaggleAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw.sqlite3"
        self.store = self.root / "features.sqlite3"
        make_database(self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def save_days(self, first_seen, start=START, end=END):
        for offset in range((end - start).days):
            day = start + timedelta(days=offset)
            features = cloud.extract_day_features(self.raw, day)
            result = cloud.export_day(self.raw, day, first_seen,
                                     source_reference={"dataset": f"owner/raw-{day}", "version": 1})
            cloud.save_day(self.store, features, result, first_seen=first_seen)

    def test_daily_exactly_reuses_legacy_statistics(self):
        labels = registry.FirstSeen({"bot": str(START)}, metadata={"history_pending": True})
        source = {"dataset": "owner/raw", "version": 3}
        result = cloud.export_day(self.raw, START, labels, source_reference=source)
        with closing(sqlite3.connect(self.raw)) as db:
            db.row_factory = sqlite3.Row
            original = legacy_daily.load_data(db, START, bot_first_seen=labels)
        self.assertEqual(result["payload"]["current"], original.current)
        self.assertEqual(result["source_versions"], {str(START): source})
        self.assertEqual(result["payload"]["bot_history"], labels.metadata)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["distribution"]["populations"]["all_wallets"]["trade_count"], 1)
        validate_distribution(result["distribution"], str(START))
        json.dumps(result, allow_nan=False)

    def test_both_progress_modes_required_before_export(self):
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute("UPDATE market_volume SET completed_at=NULL WHERE market_slug=?", (f"btc-{START}",))
            db.commit()
        with self.assertRaises(cloud.IncompleteDay):
            cloud.export_day(self.raw, START, {})
        with self.assertRaises(cloud.IncompleteDay):
            cloud.extract_day_features(self.raw, START)
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute("UPDATE market_volume SET completed_at='done'")
            db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug=?", (f"btc-{START}",))
            db.commit()
        with self.assertRaises(ValueError):
            cloud.export_day(self.raw, START, {})

    def test_earlier_backfill_replaces_later_first_seen_without_future_labels(self):
        features = cloud.extract_day_features(self.raw, START)
        features["daily_hits"] = ["bot"]
        existing = {"bot": str(START + timedelta(days=3)), "later": str(END)}
        labels, changed = cloud.update_first_seen(existing, features,
            history_metadata={"requested_start_date_utc": "2025-01-01", "history_pending": True})
        self.assertEqual(labels["bot"], str(START))
        self.assertEqual(changed, {"bot": str(START)})
        self.assertEqual(existing["bot"], str(START + timedelta(days=3)))
        self.assertEqual(labels.metadata["requested_start_date_utc"], "2025-01-01")
        result = cloud.export_day(self.raw, START, {"bot": str(END)})
        self.assertEqual(result["payload"]["current"]["suspected_bot_wallets"], 0)
        self.assertEqual(result["distribution"]["populations"]["suspected_bots"]["trade_count"], 0)

    def test_week_exactly_matches_legacy_wallet_union_and_distribution(self):
        labels = {"bot": str(START + timedelta(days=1))}
        self.save_days(labels)
        result = cloud.export_period([self.store], START, END, "weekly", labels)
        with closing(sqlite3.connect(self.raw)) as db:
            db.row_factory = sqlite3.Row
            expected = legacy_period.load_period(db, START, END, bot_first_seen=labels)
            expected_distribution = legacy_distribution.load_period_data(db, START, END, kind="weekly", bot_first_seen=labels)
        self.assertEqual(result["payload"]["current"], expected)
        self.assertEqual(result["distribution"], expected_distribution)
        self.assertEqual(result["payload"]["current"]["unique_wallets"], 3)
        self.assertEqual(result["payload"]["current"]["suspected_bot_wallets"], 1)
        self.assertEqual(result["distribution"]["populations"]["suspected_bots"]["notional_usdc_exact"], "4")
        self.assertEqual(len(result["source_versions"]), 7)
        validate_distribution(result["distribution"])

    def test_stale_daily_classification_requires_raw_recalculation(self):
        self.save_days({"bot": str(END)})
        with self.assertRaisesRegex(cloud.IncompleteDay, "重算"):
            cloud.export_period(self.store, START, END, "weekly", {"bot": str(START)})
        self.save_days({"bot": str(START)})
        result = cloud.export_period(self.store, START, END, "weekly", {"bot": str(START)})
        self.assertEqual(result["distribution"]["populations"]["suspected_bots"]["trade_count"], 3)

    def test_affected_dates_exclude_inactive_pre_hit_and_already_classified_days(self):
        self.save_days({"bot": str(START + timedelta(days=2))})
        changed = {"bot": str(START + timedelta(days=1)), "never-active": str(START)}
        self.assertEqual(cloud.affected_dates(self.store, changed), [str(START + timedelta(days=1))])
        self.assertEqual(cloud.affected_dates(self.store, {}), [])
        self.assertEqual(cloud.affected_dates([self.root / "absent.sqlite3"], changed), [])

    def test_previous_day_comparison_updates_without_raw_database(self):
        self.save_days({})
        day = START + timedelta(days=1)
        before = cloud.load_day(self.store, day)
        previous = dict(before["payload"]["previous"])
        previous["unique_wallets"] += 100
        self.raw.unlink()
        updated = cloud.update_day_previous(self.store, day, previous)
        self.assertEqual(updated["payload"]["previous"], previous)
        self.assertEqual(updated["payload"]["current"], before["payload"]["current"])
        self.assertEqual(updated["source_versions"], before["source_versions"])
        self.assertEqual(updated["distribution"], before["distribution"])
        expected = legacy_daily.display_values(
            legacy_daily.PosterData(day, before["payload"]["current"], previous), before["distribution"])
        self.assertEqual(updated["payload"]["display"], expected)
        self.assertEqual(cloud.load_day(self.store, day), updated)
        self.assertIsNone(cloud.update_day_previous(self.store, day, previous))
        self.assertIsNone(cloud.update_day_previous(self.store, END, previous))

    def test_missing_days_and_store_upserts(self):
        features = cloud.extract_day_features(self.raw, START)
        result = cloud.export_day(self.raw, START, {})
        cloud.save_day(self.store, features, result, first_seen={})
        cloud.save_day(self.store, features, result, first_seen={})
        self.assertEqual(cloud.list_days([self.store]), [str(START)])
        self.assertEqual(cloud.load_day(self.store, START), result)
        self.assertIsNone(cloud.load_day(self.store, END))
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wallet_days").fetchone()[0], 2)
        with self.assertRaises(cloud.IncompleteDay):
            cloud.export_period(self.store, START, END, "weekly", {})

    def test_calendar_month_with_zero_trade_days_still_matches_existing_report(self):
        raw = self.root / "october.sqlite3"
        start, end = date(2026, 10, 1), date(2026, 11, 1)
        make_database(raw, start, end)
        store = self.root / "october-features.sqlite3"
        for offset in range((end-start).days):
            day = start + timedelta(days=offset)
            features = cloud.extract_day_features(raw, day)
            cloud.save_day(store, features, cloud.export_day(raw, day, {}), first_seen={})
        actual = cloud.export_period(store, start, end, "monthly", {})
        with closing(sqlite3.connect(raw)) as db:
            db.row_factory = sqlite3.Row
            expected = legacy_period.load_period(db, start, end, bot_first_seen={})
        self.assertEqual(actual["payload"]["current"], expected)
        self.assertEqual(actual["target_utc"], "2026-10")
        self.assertEqual(actual["payload"]["scope_changes"], [])
        validate_distribution(actual["distribution"])


if __name__ == "__main__":
    unittest.main()
