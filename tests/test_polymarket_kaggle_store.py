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
        self.store.download_file = Mock(side_effect=lambda slug, filename, version, destination:
            destination.write_text((folder / cloud.PUBLICATION_FILE).read_text()))
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.store.publish("research", folder, title="Research dataset",
                                                description="Data science research", message="Initial"), 1)
        create = self.store._run.call_args.args
        self.assertIn("create", create)
        self.assertIn("--public", create)
        self.assertIn("--keep-tabular", create)
        meta = json.loads((folder / "dataset-metadata.json").read_text())
        self.assertEqual(meta["id"], "owner/research")
        marker = json.loads((folder / cloud.PUBLICATION_FILE).read_text())
        self.assertEqual(marker["dataset"], "owner/research")
        self.assertTrue(marker["publication_id"])
        self.assertEqual(self.store.download_file.call_args.args[:3], ("research", cloud.PUBLICATION_FILE, 1))

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

    def test_publish_initial_status_503_recovers_before_any_upload(self):
        folder = self.publication_folder()
        commands = []
        status_attempts = 0

        def run(arguments, **kwargs):
            nonlocal status_attempts
            commands.append(arguments[2])
            if arguments[2] == "status":
                status_attempts += 1
                if status_attempts == 1:
                    return subprocess.CompletedProcess(arguments, 1, "", "HTTP 503 Service Unavailable")
                version = 4 if status_attempts == 2 else 5
                return subprocess.CompletedProcess(arguments, 0, json.dumps(status(version=version)), "")
            self.assertEqual(arguments[2], "version")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        self.store.runner = Mock(side_effect=run)
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 5)
        self.assertEqual(commands, ["status", "status", "version", "status"])

    def test_read_connection_errors_retry_without_changing_commands(self):
        runner = Mock(side_effect=[ConnectionResetError(),
                                  subprocess.CompletedProcess([], 1, "", "ConnectionError: connection aborted"),
                                  subprocess.CompletedProcess([], 0, "No datasets found", "")])
        self.store.runner = runner
        with patch.object(cloud.time, "sleep") as sleep:
            self.assertEqual(self.store.list_slugs("pm-updown-analysis-"), [])
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(sleep.call_args_list[0].args, (2,))
        self.assertEqual(sleep.call_args_list[1].args, (4,))
        self.assertEqual(runner.call_args_list[0].args, runner.call_args_list[2].args)

    def test_read_permanent_errors_do_not_retry(self):
        for code in (400, 401, 403):
            with self.subTest(code=code):
                runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", f"HTTP {code}: connection error"))
                self.store.runner = runner
                with self.assertRaisesRegex(cloud.KaggleError, str(code)):
                    self.store._run("datasets", "status", "owner/research")
                self.assertEqual(runner.call_count, 1)

    def test_read_retry_stops_at_same_operation_deadline(self):
        clock = [100.0]
        self.store.deadline = 101
        self.store.runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", "HTTP 503"))
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(cloud.KaggleDeadlineError):
                self.store._run("datasets", "status", "owner/research")
        self.assertEqual(self.store.runner.call_count, 1)

    def test_cli_printed_creation_error_is_failure_even_with_zero_exit_code(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "Dataset creation error: quota exceeded\n", ""))
        store = cloud.KaggleStore("owner", self.root / "printed-error", runner=runner)
        with self.assertRaisesRegex(cloud.KaggleError, "quota exceeded"):
            store._run("datasets", "create", "-p", "folder")

    def publication_folder(self):
        folder = self.root / "publication-input"
        folder.mkdir()
        (folder / "result.json").write_text('{"data":1}')
        return folder

    def matching_marker(self, folder):
        def download(slug, filename, version, destination):
            self.assertEqual(filename, cloud.PUBLICATION_FILE)
            destination.write_text((folder / filename).read_text())
            return destination
        self.store.download_file = Mock(side_effect=download)

    def publish(self, folder):
        return self.store.publish("research", folder, title="Research dataset",
                                  description="Data science research", message="Update")

    def test_create_499_after_server_commit_accepts_matching_processed_version(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, status("processing", 1), status("ready", 1)])
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 1)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertIn("create", self.store._run.call_args.args)

    def test_version_connection_error_after_server_commit_does_not_upload_again(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[status(version=4), status(version=5)])
        self.store._run = Mock(side_effect=cloud.KaggleError("ConnectionError: connection aborted"))
        self.matching_marker(folder)
        self.assertEqual(self.publish(folder), 5)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertIn("version", self.store._run.call_args.args)

    def test_unaccepted_create_retries_with_one_stable_marker(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, None, None, status(version=1)])
        identifiers = []

        def upload(*args):
            identifiers.append(json.loads((folder / cloud.PUBLICATION_FILE).read_text())["publication_id"])
            if len(identifiers) == 1:
                raise cloud.KaggleError("HTTP 503 Service Unavailable")
            return ""

        self.store._run = Mock(side_effect=upload)
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 1)
        self.assertEqual(len(identifiers), 2)
        self.assertEqual(identifiers[0], identifiers[1])

    def test_create_conflict_after_499_reconciles_delayed_original_publication(self):
        messages = (
            'Dataset creation error: The requested slug "research" is already in use by a dataset. '
            'Please choose another slug.',
            "HTTP 409 Conflict",
            "Dataset already exists",
        )
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                folder = self.root / f"delayed-publication-{index}"
                folder.mkdir()
                self.store.status = Mock(side_effect=[None, None, None, status(version=1)])
                self.store._run = Mock(side_effect=[cloud.KaggleError("499 Client Error"),
                                                    cloud.KaggleError(message)])
                self.matching_marker(folder)
                with patch.object(cloud.time, "sleep"):
                    self.assertEqual(self.publish(folder), 1)
                self.assertEqual(self.store._run.call_count, 2)
                self.assertEqual(self.store.download_file.call_count, 1)

    def test_conflict_reconciliation_waits_through_more_stale_statuses(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, None, None, None, None,
                                             status("processing", 1), status(version=1)])
        self.store._run = Mock(side_effect=[cloud.KaggleError("HTTP 503"),
                                            cloud.KaggleError("HTTP 409 Conflict")])
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 1)
        self.assertEqual(self.store._run.call_count, 2)

    def test_conflict_after_unknown_write_does_not_accept_other_publication(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, None, None, status(version=1)])
        self.store._run = Mock(side_effect=[cloud.KaggleError("499 Client Error"),
                                            cloud.KaggleError("Dataset already exists")])
        self.store.download_file = Mock(side_effect=lambda slug, filename, version, destination:
            destination.write_text('{"publication_id":"other"}'))
        with patch.object(cloud.time, "sleep"), self.assertRaisesRegex(cloud.KaggleError, "different publication marker"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 2)

    def test_first_conflict_without_uncertain_write_still_fails(self):
        folder = self.publication_folder()
        self.store.status = Mock(return_value=None)
        self.store._run = Mock(side_effect=cloud.KaggleError("HTTP 409 Conflict"))
        with self.assertRaisesRegex(cloud.KaggleError, "409"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertEqual(self.store.status.call_count, 1)

    def test_unaccepted_version_retries_only_after_two_unchanged_statuses(self):
        folder = self.publication_folder()
        events = []
        responses = iter([status(version=4), status(version=4), status(version=4), status(version=5)])

        def remote_status(slug):
            events.append("status")
            return next(responses)

        def upload(*args):
            events.append("upload")
            if events.count("upload") == 1:
                raise cloud.KaggleError("HTTP 500 Internal Server Error")
            return ""

        self.store.status = Mock(side_effect=remote_status)
        self.store._run = Mock(side_effect=upload)
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 5)
        self.assertEqual(events, ["status", "upload", "status", "status", "upload", "status"])

    def test_newer_version_without_this_marker_is_not_success_or_safe_to_retry(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[status(version=4), status(version=6)])
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        self.store.download_file = Mock(side_effect=lambda slug, filename, version, destination:
            destination.write_text('{"schema_version":1,"dataset":"owner/research","publication_id":"other"}'))
        with self.assertRaisesRegex(cloud.KaggleError, "different publication marker"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertTrue((folder / "result.json").exists())

    def test_matching_marker_at_higher_version_accepts_official_cli_retry_result(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[status(version=4), status(version=6)])
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        self.matching_marker(folder)
        self.assertEqual(self.publish(folder), 6)
        self.assertEqual(self.store._run.call_count, 1)

    def test_permissions_parameters_and_quota_fail_without_retry(self):
        for message in ("HTTP 403 Forbidden", "HTTP 400 Invalid parameter", "quota exceeded"):
            with self.subTest(message=message):
                folder = self.root / message.split()[0]
                folder.mkdir(exist_ok=True)
                self.store.status = Mock(return_value=None)
                self.store._run = Mock(side_effect=cloud.KaggleError(message))
                with self.assertRaisesRegex(cloud.KaggleError, message):
                    self.publish(folder)
                self.assertEqual(self.store._run.call_count, 1)
                self.assertEqual(self.store.status.call_count, 1)

    def test_unknown_status_does_not_repeat_upload(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None] + [cloud.KaggleError("HTTP 503")] * 3)
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        with patch.object(cloud.time, "sleep"), self.assertRaisesRegex(cloud.KaggleError, "Cannot determine"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 1)

    def test_missing_marker_after_commit_does_not_repeat_upload(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, status(version=1)])
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        self.store.download_file = Mock(side_effect=cloud.KaggleError("HTTP 404"))
        with self.assertRaisesRegex(cloud.KaggleError, "Cannot verify"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 1)

    def test_repeated_unaccepted_upload_is_bounded(self):
        folder = self.publication_folder()
        self.store.status = Mock(return_value=None)
        self.store._run = Mock(side_effect=cloud.KaggleError("HTTP 503"))
        with patch.object(cloud.time, "sleep"), self.assertRaisesRegex(cloud.KaggleError, "three attempts"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 3)

    def test_successful_submission_waits_through_temporarily_stale_status(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, None, None, status("processing", 1), status(version=1)])
        self.store._run = Mock(return_value="")
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 1)
        self.assertEqual(self.store._run.call_count, 1)

    def test_mutation_does_not_use_read_command_outer_429_retry(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", "HTTP 429 Too many requests"))
        store = cloud.KaggleStore("owner", self.root / "mutation-rate", runner=runner)
        with self.assertRaises(cloud.KaggleError):
            store._run("datasets", "version", "-p", "folder")
        self.assertEqual(runner.call_count, 1)

    def test_command_receives_remaining_deadline_as_timeout(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ready", ""))
        store = cloud.KaggleStore("owner", self.root / "bounded-run", runner=runner, deadline=110)
        with patch.object(cloud.time, "monotonic", return_value=100):
            self.assertEqual(store._run("datasets", "status", "owner/research"), "ready")
        self.assertEqual(runner.call_args.kwargs["timeout"], 10)

    def test_expired_deadline_does_not_start_a_command(self):
        runner = Mock()
        self.store.runner = runner
        self.store.deadline = 99
        with patch.object(cloud.time, "monotonic", return_value=100), self.assertRaises(cloud.KaggleDeadlineError):
            self.store._run("datasets", "status", "owner/research")
        runner.assert_not_called()

    def test_subprocess_timeout_becomes_reconcilable_unknown_result(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired(["kaggle"], 10))
        self.store.runner = runner
        with self.assertRaises(cloud.KaggleError) as failure:
            self.store._run("datasets", "create", "-p", "folder")
        self.assertTrue(failure.exception.retryable)
        self.assertEqual(runner.call_count, 1)

    def test_processing_poll_respects_explicit_deadline(self):
        clock = [100.0]
        self.store.deadline = 110
        self.store.status = Mock(return_value=status("processing", 1))
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)) as sleep:
            with self.assertRaises(cloud.KaggleDeadlineError):
                self.store.wait_ready("research")
        sleep.assert_called_once_with(10)

    def test_processing_poll_is_bounded_even_without_caller_deadline(self):
        clock = [100.0]
        self.store.status = Mock(return_value=status("processing", 1))
        with patch.object(cloud, "OPERATION_TIMEOUT_SECONDS", 30), \
             patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(cloud.KaggleDeadlineError):
                self.store.wait_ready("research")
        self.assertEqual(clock[0], 130)

    def test_unknown_publication_poll_stops_at_deadline_and_keeps_input(self):
        folder = self.publication_folder()
        clock = [100.0]
        self.store.deadline = 110
        self.store.status = Mock(side_effect=[None, status("processing", 1)])
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(cloud.KaggleDeadlineError):
                self.publish(folder)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertTrue((folder / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
