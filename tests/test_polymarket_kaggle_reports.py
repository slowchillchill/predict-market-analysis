import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import polymarket_kaggle_reports as reports
import polymarket_updown_distribution_assets as assets
import polymarket_updown_price_distribution as distribution
import test_polymarket_updown_poster_distribution as daily_fixtures
import test_polymarket_updown_period_package as period_fixtures

DAY = daily_fixtures.DAY


class KaggleReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def daily_bundle(self):
        fixture = daily_fixtures.DailyPosterDistributionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with contextlib.closing(sqlite3.connect(fixture.path)) as db:
            stats = distribution.load_data(db, DAY, bot_first_seen={})
        current = {**fixture.overview.current, "wallets_meeting_daily_frequency_rule": 0}
        return {"schema_version": 1, "status": "complete", "kind": "daily", "target_utc": str(DAY),
                "start_date_utc": str(DAY), "end_date_utc_exclusive": "2026-11-08",
                "source_versions": {str(DAY): {"dataset": "owner/raw-day", "version": 1}},
                "payload": {"date_utc": str(DAY), "date_basis": "market_end_utc", "current": current,
                            "previous": None, "previous_date_utc": "2026-11-06", "bot_history": {},
                            "bot_rule": stats["bot_rule"], "volume_basis": "taker_only",
                            "bot_volume_basis": "wallet_buys_only"}, "distribution": stats}

    def period_bundle(self, kind):
        fixture = period_fixtures.PeriodPackageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, start, end, _, stats, current = fixture.fixture(kind)
        target = str(start) if kind == "weekly" else start.strftime("%Y-%m")
        return {"schema_version": 1, "status": "complete", "kind": kind, "target_utc": target,
                "start_date_utc": str(start), "end_date_utc_exclusive": str(end),
                "source_versions": {str(start): {"dataset": "owner/raw-day", "version": 2}},
                "payload": {"date_basis": "market_end_utc", "current": current, "previous": None,
                            "scope_changes": [], "bot_history": {}, "bot_rule": stats["bot_rule"],
                            "volume_basis": "taker_only", "bot_volume_basis": "wallet_buys_only"},
                "distribution": stats}

    def test_render_all_kinds_from_json_without_opening_raw_database(self):
        for kind in reports.KINDS:
            with self.subTest(kind=kind):
                bundle = self.daily_bundle() if kind == "daily" else self.period_bundle(kind)
                output = self.root / kind
                with patch("sqlite3.connect", side_effect=AssertionError("Local renderer opened SQLite")):
                    paths = reports.render_bundle(bundle, output, source_reference={"dataset": "owner/analysis", "version": 3})
                payload = json.loads(paths["source"].read_text())
                self.assertEqual(payload["current"], bundle["payload"]["current"])
                self.assertEqual(payload["kaggle_source"]["version"], 3)
                manifest = json.loads(paths["manifest"].read_text())
                self.assertEqual(Path(manifest["source_path"]), paths["source"])
                self.assertTrue(all(Path(path).exists() for item in manifest["platforms"].values()
                                    for path in item["image_paths"]))
                self.assertTrue(all(Path(path).parent == output for item in manifest["platforms"].values()
                                    for path in item["image_paths"]))
                self.assertLessEqual(len(paths["tweet"].read_text()), 280)
                stats = assets.load_distribution(paths["source"], payload)
                self.assertEqual(stats, bundle["distribution"])

    def test_existing_assets_are_never_overwritten(self):
        bundle = self.daily_bundle()
        period = reports.validate_bundle(bundle)
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / (period.stem + "_publish.json")
        sentinel.write_text('{"published":true}')
        before = sentinel.read_bytes()
        with self.assertRaises(reports.ExistingArtifacts):
            reports.render_bundle(bundle, output)
        state_path = self.root / "state.json"
        now = datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc)
        with patch.object(reports, "fetch_result", side_effect=reports.ReportNotReady("Old network failure")), \
                contextlib.redirect_stderr(io.StringIO()):
            state = reports.run_pending(None, state_path, output, kind="daily", now=now)
        self.assertEqual(state["reports"][period.identity]["last_error"], "Old network failure")
        with patch.object(reports, "fetch_result", return_value=(bundle, {"version": 7})), \
                contextlib.redirect_stdout(io.StringIO()):
            state = reports.run_pending(None, state_path, output, kind="daily", now=now)
        self.assertEqual(state["reports"][period.identity]["status"], "preserved")
        self.assertNotIn("last_error", state["reports"][period.identity])
        self.assertEqual(json.loads(state_path.read_text()), state)
        self.assertEqual(sentinel.read_bytes(), before)
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_existing_terminal_states_clear_stale_error_without_network_or_regeneration(self):
        state_path = self.root / "state.json"
        original = {"schema_version": 1, "rollout_date": "2026-11-07", "reports": {
            "daily:2026-11-06": {"status": "generated", "last_error": "Old failure",
                                 "manifest": "existing_publish.json", "generated_at": "original timestamp"},
            "daily:2026-11-07": {"status": "preserved", "last_error": "Old missing dataset",
                                 "reason": "Existing local assets"},
        }}
        state_path.write_text(json.dumps(original))
        with patch.object(reports, "fetch_result") as fetch, patch.object(reports, "render_bundle") as render:
            state = reports.run_pending(None, state_path, self.root / "output", kind="daily",
                rollout_date=date(2026, 12, 1), now=datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc))
        fetch.assert_not_called()
        render.assert_not_called()
        for item in original["reports"].values():
            item.pop("last_error")
        self.assertEqual(state, original)
        self.assertEqual(json.loads(state_path.read_text()), original)

    def test_incomplete_and_wrong_period_results_are_rejected(self):
        bundle = self.daily_bundle()
        bundle["status"] = "incomplete"
        with self.assertRaises(reports.ReportNotReady):
            reports.render_bundle(bundle, self.root / "incomplete")
        bundle["status"] = "complete"
        with self.assertRaisesRegex(ValueError, "requested period"):
            reports.validate_bundle(bundle, reports.ReportPeriod.from_identity("daily:2026-11-08"))
        self.assertFalse((self.root / "incomplete").exists())

    def test_schedule_boundaries_and_cross_month_week_dataset(self):
        before = datetime(2026, 11, 2, 0, 29, tzinfo=timezone.utc)
        at = datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(reports.due_periods(date(2026, 11, 2), before), [])
        self.assertEqual([p.identity for p in reports.due_periods(date(2026, 11, 2), at)],
                         ["daily:2026-11-01"])
        weekly_at = datetime(2026, 11, 2, 2, 30, tzinfo=timezone.utc)
        self.assertEqual([p.identity for p in reports.due_periods(date(2026, 11, 2), weekly_at)],
                         ["daily:2026-11-01", "weekly:2026-10-26"])
        cross = reports.ReportPeriod.from_identity("weekly:2026-10-26")
        self.assertEqual(cross.month, "2026-11")
        month_at = datetime(2026, 12, 1, 3, 30, tzinfo=timezone.utc)
        self.assertEqual([p.identity for p in reports.due_periods(date(2026, 12, 1), month_at)],
                         ["daily:2026-11-30", "monthly:2026-11"])

    def test_exact_version_index_and_file_downloads(self):
        bundle = self.daily_bundle()
        calls = []
        class Store:
            owner = "owner"
            def version(self, slug):
                calls.append(("version", slug))
                return 7
            def download_file(self, slug, filename, version, destination):
                calls.append((filename, version))
                destination.write_text(json.dumps(
                    {"results": {"daily:2026-11-07": {"filename": "daily-07.json", "status": "complete"}}}
                    if filename == "index.json" else bundle))
                return destination
        period = reports.validate_bundle(bundle)
        result, ref = reports.fetch_result(Store(), period, self.root)
        self.assertEqual(result, bundle)
        self.assertEqual(ref["version"], 7)
        self.assertEqual(calls, [("version", "pm-updown-analysis-2026-11"), ("index.json", 7), ("daily-07.json", 7)])

    def test_pending_target_survives_midnight_and_only_new_cutover_periods_are_queued(self):
        state_path = self.root / "state.json"
        output = self.root / "output"
        def unavailable(store, period, directory):
            raise reports.ReportNotReady("Not ready")
        with patch.object(reports, "fetch_result", side_effect=unavailable), \
                contextlib.redirect_stderr(io.StringIO()):
            reports.run_pending(None, state_path, output, kind="daily", rollout_date=date(2026, 11, 10),
                                now=datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc))
            state = reports.run_pending(None, state_path, output, kind="daily",
                                        now=datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(set(state["reports"]), {"daily:2026-11-09", "daily:2026-11-10"})
        self.assertEqual(state["reports"]["daily:2026-11-09"]["status"], "pending")
        self.assertEqual(state["rollout_date"], "2026-11-10")


if __name__ == "__main__":
    unittest.main()
