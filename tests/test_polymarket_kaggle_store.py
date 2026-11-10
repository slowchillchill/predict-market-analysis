import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_kaggle_store as cloud


def status(state="ready", version=1):
    return {"status": state, "current_version_number": version}


class KaggleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = cloud.KaggleStore("owner", self.root / "download")

    def test_absent_dataset_requires_successful_listing_after_403(self):
        self.store._run = Mock(side_effect=[cloud.KaggleError("HTTP 403 Forbidden"),
                                           "ref,title\nowner/other,Other\n"])
        self.assertIsNone(self.store.status("missing"))
        self.assertEqual(self.store._run.call_count, 2)

    def test_403_authentication_failure_is_not_absence(self):
        self.store._run = Mock(side_effect=cloud.KaggleError("HTTP 403 Forbidden"))
        with self.assertRaises(cloud.KaggleError):
            self.store.status("private-or-forbidden")

    def test_forbidden_existing_dataset_does_not_become_absent(self):
        self.store._run = Mock(side_effect=[cloud.KaggleError("HTTP 403 Forbidden"),
                                           "ref,title\nowner/exists,Existing\n"])
        with self.assertRaises(cloud.KaggleError):
            self.store.status("exists")

    def test_single_file_download_pins_version_and_keeps_only_requested_file(self):
        destination = self.root / "result.json"
        calls = []

        def run(*args):
            calls.append(args)
            folder = Path(args[args.index("-p") + 1])
            (folder / "reports").mkdir()
            (folder / "reports/day.json").write_text('{"ready":true}')
            return ""

        self.store._run = run
        self.store.download_file("analysis", "reports/day.json", 7, destination)
        self.assertEqual(json.loads(destination.read_text()), {"ready": True})
        self.assertIn("owner/analysis/7", calls[0])
        self.assertIn("-f", calls[0])
        self.assertEqual(list((self.root / "download").iterdir()), [])

    def test_restore_waits_for_processing_month_and_preserves_full_snapshot(self):
        folder = self.root / "month"
        self.store.status = Mock(side_effect=[status("processing", 3), status("ready", 3)])

        def run(*args):
            folder.mkdir(exist_ok=True)
            (folder / "old-daily.json").write_text('{"old":true}')
            (folder / "progress.json").write_text('{"raw_days":{"2026-11-01":{}}}')
            return ""

        self.store._run = Mock(side_effect=run)
        with patch.object(cloud.time, "sleep"):
            version = self.store.restore("analysis", folder)
        self.assertEqual(version, 3)
        self.assertTrue((folder / "old-daily.json").is_file())
        self.assertIn("2026-11-01", json.loads((folder / "progress.json").read_text())["raw_days"])
        self.assertIn("owner/analysis/3", self.store._run.call_args.args)

    def test_publish_is_public_and_waits_for_expected_processed_version(self):
        folder = self.root / "publish"
        folder.mkdir()
        (folder / "result.json").write_text('{"data":1}')
        self.store.status = Mock(side_effect=[None, status("processing", 1), status("ready", 1)])
        self.store._run = Mock(return_value="")
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.store.publish("research", folder, title="Research dataset",
                                                description="Data science research", message="Initial"), 1)
        create = self.store._run.call_args.args
        self.assertIn("create", create)
        self.assertIn("--public", create)
        self.assertIn("--keep-tabular", create)
        meta = json.loads((folder / "dataset-metadata.json").read_text())
        self.assertEqual(meta["id"], "owner/research")

    def test_processing_failure_does_not_report_publish_success(self):
        self.store.status = Mock(return_value=status("failed", 2))
        with self.assertRaises(cloud.KaggleError):
            self.store.wait_ready("research", 2)

    def test_kaggle_rate_limit_retries_do_not_repeat_successful_call(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 1, "", "HTTP 429 Too many requests"),
                                   subprocess.CompletedProcess([], 0, "result\n", "")])
        store = cloud.KaggleStore("owner", self.root / "rate", runner=runner)
        with patch.object(cloud.time, "sleep") as sleep:
            self.assertEqual(store._run("datasets", "status", "owner/research"), "result")
        self.assertEqual(runner.call_count, 2)
        sleep.assert_called_once_with(60)

    def test_cli_printed_creation_error_is_failure_even_with_zero_exit_code(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "Dataset creation error: quota exceeded\n", ""))
        store = cloud.KaggleStore("owner", self.root / "printed-error", runner=runner)
        with self.assertRaisesRegex(cloud.KaggleError, "quota exceeded"):
            store._run("datasets", "create", "-p", "folder")


if __name__ == "__main__":
    unittest.main()
