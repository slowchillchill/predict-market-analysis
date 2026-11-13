import json
import base64
import hashlib
import io
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import polymarket_kaggle_social as social
import polymarket_publish_state as publishing
from polymarket_kaggle_reports import ReportPeriod
from test_polymarket_social_text import example_bundle


def bundle_for(identity):
    period = ReportPeriod.from_identity(identity)
    bundle = example_bundle()
    bundle.update(kind=period.kind, target_utc=period.target, start_date_utc=str(period.start),
                  end_date_utc_exclusive=str(period.end))
    if period.kind == "daily":
        bundle["payload"]["date_utc"] = str(period.start)
        bundle["distribution"]["date_utc"] = str(period.start)
    else:
        for item in (bundle["payload"]["current"], bundle["distribution"]):
            item.update(start_date_utc=str(period.start), end_date_utc_exclusive=str(period.end))
        bundle["distribution"]["kind"] = period.kind
    return bundle


class CloudSocialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.month_dir = self.root / "month"
        self.month_dir.mkdir()
        self.reference = {"dataset": "owner/analysis-2026-11", "version": 7}

    def snapshot(self, *identities):
        index = {"schema_version": 1, "results": {}}
        for identity in identities:
            filename = identity.replace(":", "_") + ".json"
            (self.month_dir / filename).write_text(json.dumps(bundle_for(identity)))
            index["results"][identity] = {"status": "complete", "filename": filename}
        (self.month_dir / "index.json").write_text(json.dumps(index))

    def render(self, *, now=None, month="2026-11", rollout=date(2026, 11, 10), **kwargs):
        return social.render_cloud_month(self.month_dir, month=month,
            source_reference=self.reference, rollout_date=rollout,
            now=now or datetime(2026, 11, 11, 1, tzinfo=timezone.utc), **kwargs)

    def state(self):
        return json.loads((self.month_dir / "social/state.json").read_text())

    def test_generates_due_result_preserves_pending_and_skips_historical_backfill(self):
        self.snapshot("daily:2026-11-10", "daily:2025-09-10", "daily:2026-11-12")
        result = self.render()
        self.assertTrue(result["changed"])
        self.assertEqual(result["generated"], ["daily:2026-11-10"])
        self.assertEqual(result["pending"], ["daily:2026-11-09"])
        self.assertEqual(set(self.state()["reports"]), {"daily:2026-11-09", "daily:2026-11-10"})
        self.assertFalse((self.month_dir / "social/daily/2025-09-10").exists())
        self.assertFalse((self.month_dir / "social/daily/2026-11-12").exists())
        manifest = json.loads(next(self.month_dir.glob("social/daily/*/*_publish.json")).read_text())
        self.assertEqual(manifest["source_reference"]["version"], 7)
        self.assertEqual(manifest["path_base"], "manifest_directory")
        self.assertFalse(Path(manifest["source_path"]).is_absolute())

    def test_repeat_does_not_write_or_replace_text_even_when_result_is_revised(self):
        self.snapshot("daily:2026-11-10")
        self.render()
        before = {str(p.relative_to(self.month_dir)): p.read_bytes()
                  for p in (self.month_dir / "social").rglob("*") if p.is_file()}
        path = self.month_dir / "daily_2026-11-10.json"
        result = json.loads(path.read_text())
        result["payload"]["current"]["total_markets"] += 1
        path.write_text(json.dumps(result))
        self.reference["version"] = 8
        with patch.object(social, "render_bundle", side_effect=AssertionError("Regenerated old text")):
            outcome = self.render()
        self.assertFalse(outcome["changed"])
        self.assertEqual(outcome["preserved"], ["daily:2026-11-10"])
        after = {str(p.relative_to(self.month_dir)): p.read_bytes()
                 for p in (self.month_dir / "social").rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_fresh_session_restores_package_and_missing_state_without_original_paths(self):
        self.snapshot("daily:2026-11-10")
        self.render()
        restored = self.root / "fresh-session"
        shutil.copytree(self.month_dir, restored)
        shutil.rmtree(self.month_dir)
        self.month_dir = restored
        (restored / "social/state.json").unlink()
        with patch.object(social, "render_bundle", side_effect=AssertionError("Should reuse package")):
            result = self.render()
        self.assertTrue(result["changed"])
        self.assertEqual(result["preserved"], ["daily:2026-11-10"])
        manifest_path = next(restored.glob("social/daily/*/*_publish.json"))
        manifest = publishing.load_manifest(manifest_path)
        publishing._validate_text_manifest(manifest, "x")
        self.assertTrue(Path(manifest["source_path"]).is_relative_to(restored))

    def test_weekly_and_monthly_use_confirmed_analysis_version(self):
        for identity, rollout, now, month in [
            ("weekly:2026-10-26", date(2026, 11, 2), datetime(2026, 11, 2, 3, tzinfo=timezone.utc), "2026-11"),
            ("monthly:2026-10", date(2026, 11, 1), datetime(2026, 11, 1, 4, tzinfo=timezone.utc), "2026-10"),
        ]:
            with self.subTest(identity=identity):
                self.month_dir = self.root / identity.replace(":", "_")
                self.month_dir.mkdir()
                self.snapshot(identity)
                outcome = self.render(rollout=rollout, now=now, month=month)
                self.assertIn(identity, outcome["generated"])
                body = next(self.month_dir.glob("social/*/*/*_linkedin.txt")).read_text()
                self.assertIn("owner/analysis-2026-11/versions/7", body)
                self.assertNotIn("owner/raw-day", body)

    def test_existing_rollout_date_survives_restart_and_pending_period_is_retried(self):
        self.snapshot()
        self.render()
        self.snapshot("daily:2026-11-09", "daily:2026-11-10")
        outcome = self.render(rollout=date(2026, 12, 1))
        self.assertEqual(set(outcome["generated"]), {"daily:2026-11-09", "daily:2026-11-10"})
        self.assertEqual(self.state()["rollout_date"], "2026-11-10")
        self.assertTrue(all("last_error" not in item for item in self.state()["reports"].values()))

    def test_corrupt_existing_package_is_not_overwritten_or_called_successful(self):
        self.snapshot("daily:2026-11-10")
        self.render()
        path = next(self.month_dir.glob("social/daily/*/*_x_01.txt"))
        path.write_text("operator changed this draft")
        outcome = self.render()
        self.assertIn("daily:2026-11-10", outcome["pending"])
        self.assertNotIn("daily:2026-11-10", outcome["preserved"])
        self.assertIn("Text changed", self.state()["reports"]["daily:2026-11-10"]["last_error"])
        self.assertEqual(path.read_text(), "operator changed this draft")

    def test_incomplete_result_waits_and_no_completed_package_is_exposed(self):
        self.snapshot("daily:2026-11-10")
        path = self.month_dir / "daily_2026-11-10.json"
        bundle = json.loads(path.read_text())
        bundle["status"] = "incomplete"
        path.write_text(json.dumps(bundle))
        result = self.render()
        self.assertIn("daily:2026-11-10", result["pending"])
        self.assertFalse(list(self.month_dir.glob("social/daily/*/*_publish.json")))

    def test_failure_before_atomic_install_can_retry_without_partial_package(self):
        self.snapshot("daily:2026-11-10")
        with patch.object(social, "_existing_package", side_effect=ValueError("Injected validation failure")):
            outcome = self.render()
        self.assertIn("daily:2026-11-10", outcome["pending"])
        self.assertFalse((self.month_dir / "social/daily/2026-11-10").exists())
        self.assertIn("daily:2026-11-10", self.render()["generated"])

    def test_no_due_periods_does_not_create_state_or_generate_before_schedule(self):
        self.snapshot("daily:2026-11-10")
        result = self.render(rollout=date(2026, 11, 11),
                             now=datetime(2026, 11, 11, 0, 29, tzinfo=timezone.utc))
        self.assertFalse(result["changed"])
        self.assertFalse((self.month_dir / "social").exists())

    def test_requires_confirmed_source_and_rejects_portable_path_traversal(self):
        self.snapshot("daily:2026-11-10")
        self.reference["version"] = 0
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.render()
        self.reference["version"] = 7
        self.render()
        path = next(self.month_dir.glob("social/daily/*/*_publish.json"))
        manifest = json.loads(path.read_text())
        manifest["source_path"] = "../../index.json"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "filenames"):
            publishing.load_manifest(path)

    def test_archive_roundtrip_preserves_bytes_and_state_in_fresh_session(self):
        self.snapshot("daily:2026-11-10")
        self.render()
        before = {p.relative_to(self.month_dir).as_posix(): p.read_bytes()
                  for p in (self.month_dir / "social").rglob("*") if p.is_file()}
        self.assertTrue(social.pack_cloud_social(self.month_dir))
        archive = (self.month_dir / social.SOCIAL_ARCHIVE_FILENAME).read_bytes()
        envelope = json.loads(archive)
        self.assertEqual(envelope["encoding"], "base64+zip")
        payload = base64.b64decode(envelope["payload"], validate=True)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), envelope["sha256"])
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(payload)))
        self.assertFalse(zipfile.is_zipfile(io.BytesIO(archive)))
        self.assertFalse(social.pack_cloud_social(self.month_dir))
        self.assertEqual((self.month_dir / social.SOCIAL_ARCHIVE_FILENAME).read_bytes(), archive)
        shutil.rmtree(self.month_dir / "social")
        self.assertTrue(social.restore_cloud_social(self.month_dir))
        self.assertFalse(social.restore_cloud_social(self.month_dir))
        after = {p.relative_to(self.month_dir).as_posix(): p.read_bytes()
                 for p in (self.month_dir / "social").rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.render()["changed"])

    def test_archive_rejects_traversal_links_and_conflicts_before_writing(self):
        for name, link in [("../outside.txt", False), ("other/state.json", False),
                           ("social/../outside.txt", False), ("social/link", True)]:
            with self.subTest(name=name):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as archive:
                    archive.writestr("social/valid.txt", "valid")
                    info = zipfile.ZipInfo(name)
                    if link:
                        info.external_attr = 0o120777 << 16
                    archive.writestr(info, "bad")
                (self.month_dir / social.SOCIAL_ARCHIVE_FILENAME).write_bytes(social._encode_archive(buffer.getvalue()))
                with self.assertRaises(ValueError):
                    social.restore_cloud_social(self.month_dir)
                self.assertFalse((self.month_dir / "social/valid.txt").exists())
        (self.month_dir / "social").mkdir()
        existing = self.month_dir / "social/existing.txt"
        existing.write_text("preserve")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("social/new.txt", "new")
            archive.writestr("social/existing.txt", "different")
        (self.month_dir / social.SOCIAL_ARCHIVE_FILENAME).write_bytes(social._encode_archive(buffer.getvalue()))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            social.restore_cloud_social(self.month_dir)
        self.assertEqual(existing.read_text(), "preserve")
        self.assertFalse((self.month_dir / "social/new.txt").exists())

    def test_archive_rejects_bad_container_and_checksum_before_restoring_files(self):
        self.snapshot("daily:2026-11-10")
        self.render()
        social.pack_cloud_social(self.month_dir)
        path = self.month_dir / social.SOCIAL_ARCHIVE_FILENAME
        original = json.loads(path.read_text())
        shutil.rmtree(self.month_dir / "social")
        for field, value in [("schema_version", 2), ("encoding", "zip"),
                             ("payload", "%%invalid%%"), ("sha256", "0" * 64)]:
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: value}))
                with self.assertRaises(ValueError):
                    social.restore_cloud_social(self.month_dir)
                self.assertFalse((self.month_dir / "social").exists())

    def test_seed_import_preserves_local_text_and_legacy_terminal_periods(self):
        local_output = self.root / "local-text"
        social.render_bundle(bundle_for("daily:2026-11-10"), local_output,
                             source_reference=self.reference)
        local_state = self.root / "local-state.json"
        local_state.write_text(json.dumps({"schema_version": 1, "rollout_date": "2026-11-10", "reports": {
            "daily:2026-11-09": {"status": "preserved"},
            "daily:2026-11-10": {"status": "generated", "format": "text"},
            "daily:2026-11-11": {"status": "pending"},
        }}))
        original = {p.name: p.read_bytes() for p in local_output.iterdir()}
        seed_dir = self.root / "seed"
        summary = social.build_cloud_text_seed(seed_dir, local_state_path=local_state,
                                               local_output_dir=local_output)
        self.assertEqual(set(p.name for p in seed_dir.iterdir()), {"seed.json", social.SEED_ARCHIVE_FILENAME})
        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["archive_filename"], social.SEED_ARCHIVE_FILENAME)
        self.assertEqual(summary["months"], ["2026-11"])
        result = social.import_cloud_text_state(self.month_dir, seed_dir, month="2026-11")
        self.assertEqual(result["imported"], ["daily:2026-11-10"])
        self.assertEqual(self.state()["reports"]["daily:2026-11-09"]["status"], "preserved")
        cloud_file = next(self.month_dir.glob("social/daily/*/*_x_01.txt"))
        self.assertEqual(cloud_file.read_bytes(), original[cloud_file.name])
        self.assertEqual({p.name: p.read_bytes() for p in local_output.iterdir()}, original)
        self.assertFalse(social.import_cloud_text_state(self.month_dir, seed_dir, month="2026-11")["changed"])
        self.snapshot("daily:2026-11-09", "daily:2026-11-10")
        with patch.object(social, "render_bundle", side_effect=AssertionError("Recreated migrated content")):
            outcome = self.render()
        self.assertFalse(outcome["changed"])
        self.assertEqual(set(outcome["preserved"]), {"daily:2026-11-09", "daily:2026-11-10"})


if __name__ == "__main__":
    unittest.main()
