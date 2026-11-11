import contextlib
import copy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_kaggle_migration as migration
import polymarket_kaggle_pipeline as producer
from polymarket_kaggle_store import KaggleError, write_json
import test_polymarket_kaggle_pipeline as fixtures


ROOT = Path(__file__).resolve().parents[1]
DAY = fixtures.YESTERDAY


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = fixtures.MemoryStore()
        self.client = fixtures.PublicAPI([fixtures.TODAY])
        catalog = patch.dict(producer.collector.TARGET_SERIES, {fixtures.SERIES: 1}, clear=True)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.pipeline = producer.Pipeline(self.store, self.root / "work", client=self.client,
                                         today=lambda: fixtures.TODAY)

    def seed_legacy(self, *, api_amount=2000000):
        folder = self.root / "legacy"
        folder.mkdir()
        path = folder / "data.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            db.executescript((ROOT / "tests/fixtures/btc5m_legacy_schema.sql").read_text())
            db.executescript("""CREATE TABLE market_volume (
                market_slug TEXT PRIMARY KEY REFERENCES markets(slug),
                amount_micro_usdc INTEGER NOT NULL DEFAULT 0,
                committed_page INTEGER NOT NULL DEFAULT 0,
                next_cursor TEXT, completed_at TEXT, last_error TEXT)""")
            producer.collector.set_series_first_end(db)
            event = self.client.market(DAY)
            market = event["markets"][0]
            slug = market["slug"]
            # Seed the historical nine-column schema independently of the
            # current writer, which now also records optional rule metadata.
            db.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?)", (
                slug, str(producer.collector.utc_time(market["eventStartTime"]).date()),
                market["id"], market["conditionId"], event["id"], market["question"],
                fixtures.SERIES, market["eventStartTime"], market["endDate"]))
            db.execute("INSERT INTO collection_progress(market_slug,query_params) VALUES (?,?)", (
                slug, json.dumps(producer.core.trade_params(market["conditionId"]), sort_keys=True)))
            for index, (wallet, side) in enumerate((("buyer", "BUY"), ("seller", "SELL"))):
                db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    slug, 1, index, wallet, "original-long-chain-hash", 100, side,
                    "Up", 0, "token-up", "0.2", "10", 2000000))
            db.execute("UPDATE collection_progress SET committed_page=1,completed_at='done' WHERE market_slug=?", (slug,))
            db.execute("INSERT INTO market_volume VALUES (?,?,1,NULL,'done',NULL)", (slug, api_amount))
            db.commit()
            db.executescript((ROOT / "tests/fixtures/updown_legacy_views.sql").read_text())
            coverage = producer.collector.coverage(db, str(DAY))
        write_json(folder / "collection_status.json", {"date_utc": str(DAY), "status": "complete", "coverage": coverage})
        (folder / "README.md").write_text("old source notice")
        raw_slug = f"pm-updown-raw-{DAY}"
        self.store.publish(raw_slug, folder)
        self.pipeline.state["raw_days"][str(DAY)] = {
            "slug": raw_slug, "version": 1, "status": "complete", "coverage": coverage}
        self.pipeline.state["first_seen"] = {"buyer": str(DAY)}
        self.pipeline.state["history"] = {
            "start_date_utc": "2026-01-01", "target_end_date_utc": "2026-11-08",
            "next_date_utc": "2026-02-03", "series_first_end": {},
            "gaps": {"2026-02-01": "retry later"}, "finished": False}
        self.pipeline.save()
        return raw_slug

    def quiet(self):
        return contextlib.redirect_stdout(io.StringIO())

    def test_conversion_preserves_business_values_and_progress_without_api_calls(self):
        slug = self.seed_legacy()
        history = copy.deepcopy(self.pipeline.state["history"])
        labels = copy.deepcopy(self.pipeline.state["first_seen"])
        with self.quiet():
            result, = migration.migrate_existing(self.pipeline)
        self.assertEqual(result["status"], "passed")
        self.assertEqual((result["wallet_rows"], result["transaction_groups"]), (2, 1))
        self.assertEqual(result["api_amount_mismatched_markets"], 0)
        self.assertEqual(result["tables"], ["collection_progress", "markets", "trades"])
        self.assertEqual(self.store.version(slug), 2)
        self.assertEqual(self.pipeline.state["history"], history)
        self.assertEqual(self.pipeline.state["first_seen"], labels)
        self.assertEqual(self.client.trades, [])
        raw = self.store.datasets[slug][2]
        self.assertEqual(json.loads(raw["collection_status.json"])["schema_version"], producer.RAW_SCHEMA_VERSION)
        file = self.root / "read.sqlite3"
        file.write_bytes(raw["data.sqlite3"])
        with contextlib.closing(sqlite3.connect(file)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            self.assertIn("group_id", columns)
            self.assertNotIn("transaction_hash", columns)
            self.assertEqual(db.execute("SELECT group_id,wallet FROM trades ORDER BY page_number,row_number").fetchall(),
                             [(1, "buyer"), (1, "seller")])
        month = self.store.datasets["pm-updown-analysis-2026-11"][2]
        bundle = json.loads(month[f"daily_{DAY}.json"])
        self.assertEqual(bundle["source_versions"][str(DAY)]["version"], 2)
        self.assertEqual(bundle["payload"]["current"]["volume_micro_usdc"], 2000000)
        self.assertEqual(bundle["raw_schema_version"], producer.RAW_SCHEMA_VERSION)
        self.assertFalse((self.pipeline.work_dir / "migration" / str(DAY)).exists())
        self.assertEqual(migration.migrate_existing(self.pipeline), [])
        self.assertEqual(self.store.version(slug), 2)

    def test_changed_legacy_api_amount_is_rejected_before_publication(self):
        slug = self.seed_legacy(api_amount=3000000)
        with self.quiet(), self.assertRaisesRegex(ValueError, "changed report business values"):
            migration.migrate_existing(self.pipeline)
        self.assertEqual(self.store.version(slug), 1)
        self.assertEqual(self.pipeline.state["raw_days"][str(DAY)]["version"], 1)
        self.assertEqual(self.client.trades, [])

    def test_new_global_history_bounds_do_not_change_original_day_catalog(self):
        self.seed_legacy()
        self.pipeline.state["history"]["series_first_end"] = {fixtures.SERIES: "2026-01-01T00:00:00Z"}
        with self.quiet():
            result, = migration.migrate_existing(self.pipeline)
        self.assertTrue(result["business_values_unchanged"])
        self.assertEqual(self.pipeline.state["history"]["series_first_end"],
                         {fixtures.SERIES: "2026-01-01T00:00:00Z"})

    def test_resume_reuses_published_v2_when_monthly_publication_failed(self):
        slug = self.seed_legacy()
        publish = self.store.publish
        failed = [False]
        def interrupted(target, directory, **kwargs):
            if target == "pm-updown-analysis-2026-11" and not failed[0]:
                failed[0] = True
                raise KaggleError("simulated monthly publication failure")
            return publish(target, directory, **kwargs)
        with patch.object(self.store, "publish", side_effect=interrupted), self.quiet(), self.assertRaises(KaggleError):
            migration.migrate_existing(self.pipeline)
        self.assertEqual(self.store.version(slug), 2)
        resumed = producer.Pipeline(self.store, self.root / "new-session", client=self.client,
                                    today=lambda: fixtures.TODAY)
        self.assertEqual(resumed.state["raw_days"][str(DAY)]["version"], 1)
        with self.quiet():
            migration.migrate_existing(resumed)
        self.assertEqual(self.store.version(slug), 2)
        self.assertEqual(resumed.state["raw_days"][str(DAY)]["version"], 2)
        month = self.store.datasets["pm-updown-analysis-2026-11"][self.store.version("pm-updown-analysis-2026-11")]
        self.assertEqual(json.loads(month[f"daily_{DAY}.json"])["source_versions"][str(DAY)]["version"], 2)
        self.assertEqual(self.client.trades, [])

    def test_existing_grouped_v2_is_reused_without_claiming_v3_or_republishing_raw(self):
        slug = self.seed_legacy()
        original_reference = copy.deepcopy(self.pipeline.state["raw_days"][str(DAY)])
        with self.quiet():
            migration.migrate_existing(self.pipeline)
        raw = self.store.datasets[slug][2]
        source = self.root / "grouped.sqlite3"
        source.write_bytes(raw["data.sqlite3"])
        with contextlib.closing(sqlite3.connect(source)) as db:
            script = "\n".join(db.iterdump()).replace("date_utc TEXT,", "date_utc TEXT NOT NULL,")
        old = self.root / "actual-v2.sqlite3"
        with contextlib.closing(sqlite3.connect(old)) as db:
            db.executescript(script)
            db.execute("PRAGMA user_version=2")
        raw["data.sqlite3"] = old.read_bytes()
        status = json.loads(raw["collection_status.json"])
        status["schema_version"] = 2
        raw["collection_status.json"] = json.dumps(status).encode()
        raw.pop("migration_summary.json", None)
        self.pipeline.state["raw_days"][str(DAY)] = original_reference
        with self.quiet():
            migration.migrate_existing(self.pipeline)
        self.assertEqual(self.store.version(slug), 2)
        self.assertEqual(self.pipeline.state["raw_days"][str(DAY)]["raw_schema_version"], 2)
        month = self.latest_month()
        self.assertEqual(json.loads(month[f"daily_{DAY}.json"])["raw_schema_version"], 2)

    def latest_month(self):
        slug = "pm-updown-analysis-2026-11"
        return self.store.datasets[slug][self.store.version(slug)]


if __name__ == "__main__":
    unittest.main()
