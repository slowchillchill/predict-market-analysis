import json
import io
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

    def test_exception_chain_redacts_credentials_and_stops_at_cycles(self):
        root = RuntimeError("HTTP 403 env-secret Authorization: Bearer bearer-secret "
                            "Basic basic-secret https://files.example/file?X-Goog-Signature=url-secret&Expires=1 "
                            "https://user:password-secret@example.test/path api_key=key-secret")
        outer = cloud.KaggleError("Cannot determine publication")
        outer.__cause__ = root
        root.__cause__ = outer
        with patch.dict(cloud.os.environ, {"KAGGLE_API_TOKEN": "env-secret"}):
            detail = cloud.describe_exception(outer)
        self.assertIn("KaggleError: Cannot determine publication", detail)
        self.assertIn("RuntimeError: HTTP 403", detail)
        self.assertEqual(detail.count("KaggleError:"), 1)
        for secret in ("env-secret", "bearer-secret", "basic-secret", "url-secret", "password-secret", "key-secret"):
            self.assertNotIn(secret, detail)
        self.assertIn("https://files.example/file", detail)

    def test_read_command_diagnostics_include_operation_and_exit_code_without_secrets(self):
        self.store.runner = Mock(return_value=subprocess.CompletedProcess(
            [], 7, "", "HTTP 503 Bearer retry-secret https://host/path?signature=query-secret"))
        output = io.StringIO()
        with patch.object(cloud.time, "sleep"), patch("sys.stdout", output), \
                self.assertRaises(cloud.KaggleError) as failure:
            self.store._run("datasets", "status", "owner/research")
        detail = cloud.describe_exception(failure.exception) + output.getvalue()
        self.assertIn("datasets status exited 7", detail)
        self.assertIn("HTTP 503", detail)
        self.assertNotIn("retry-secret", detail)
        self.assertNotIn("query-secret", detail)
        self.assertEqual(self.store.runner.call_count, 3)

    def test_malformed_status_is_a_diagnostic_error_not_dataset_absence(self):
        responses = ["not JSON", "[]", "null", "{}",
                     json.dumps(status("", 1)), json.dumps(status("ready", 0)),
                     json.dumps(status(version=True)), json.dumps(status(version=-1)),
                     json.dumps(status(version=1.5)), json.dumps(status(version="abc")),
                     json.dumps({"status": "ready"})]
        for response in responses:
            with self.subTest(response=response):
                self.store._run = Mock(return_value=response)
                with self.assertRaisesRegex(cloud.KaggleError, "Invalid Dataset status response for owner/research") as failure:
                    self.store.status("research")
                self.assertIsNotNone(failure.exception.__cause__)
                self.assertEqual(self.store._run.call_count, 1)

    def test_processing_status_accepts_unassigned_version(self):
        for version in (None, 0, "0"):
            with self.subTest(version=version):
                self.store._run = Mock(return_value=json.dumps(status("processing", version)))
                self.assertEqual(self.store.status("research"), status("processing", 0))

    def test_malformed_status_response_is_redacted(self):
        self.store._run = Mock(return_value="Bearer status-secret not JSON")
        with self.assertRaises(cloud.KaggleError) as failure:
            self.store.status("research")
        self.assertNotIn("status-secret", cloud.describe_exception(failure.exception))

    def test_successful_status_resets_consecutive_confirmation_failures(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None,
            cloud.KaggleError("HTTP 503"), status("processing", 1),
            cloud.KaggleError("HTTP 503"), status("processing", 1),
            cloud.KaggleError("HTTP 503"), status("ready", 1)])
        self.store._run = Mock(return_value="")
        self.matching_marker(folder)
        with patch.object(cloud.time, "sleep"):
            self.assertEqual(self.publish(folder), 1)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertEqual(list((self.store.work_dir / "pending-publications").iterdir()), [])

    def test_permanent_confirmation_failure_preserves_cause_and_accepted_journal(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[status(version=4), cloud.KaggleError("HTTP 401 Unauthorized")])
        self.store._run = Mock(return_value="")
        with self.assertRaises(cloud.KaggleError) as failure:
            self.publish(folder)
        marker = json.loads((folder / cloud.PUBLICATION_FILE).read_text())
        detail = cloud.describe_exception(failure.exception)
        for text in ("Cannot determine", "HTTP 401 Unauthorized", "dataset=owner/research",
                     "expected_version=5", "submitted=True", "consecutive_read_failures=1",
                     marker["publication_id"]):
            self.assertIn(text, detail)
        journals = list((self.store.work_dir / "pending-publications").glob("*.json"))
        self.assertEqual(len(journals), 1)
        journal = json.loads(journals[0].read_text())
        self.assertEqual(journal["publication"], marker)
        self.assertEqual(journal["phase"], "accepted")
        self.assertTrue(journal["submitted"])
        self.assertEqual(journal["expected_version"], 5)
        self.assertEqual(journal["source_directory"], str(folder.resolve()))
        self.assertTrue((folder / "result.json").exists())
        self.assertEqual(self.store._run.call_count, 1)

    def test_unknown_write_keeps_journal_without_repeating_upload(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, cloud.KaggleError("HTTP 403 Forbidden")])
        self.store._run = Mock(side_effect=cloud.KaggleError("499 Client Error"))
        with self.assertRaisesRegex(cloud.KaggleError, "Cannot determine"):
            self.publish(folder)
        journals = list((self.store.work_dir / "pending-publications").glob("*.json"))
        self.assertEqual(len(journals), 1)
        journal = json.loads(journals[0].read_text())
        self.assertEqual(journal["phase"], "unknown")
        self.assertIsNone(journal["submitted"])
        self.assertEqual(journal["attempt"], 1)
        self.assertEqual(self.store._run.call_count, 1)

    def test_journal_exists_before_upload_and_is_not_in_upload_directory(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, status(version=1)])

        def upload(*args):
            journal_paths = list((self.store.work_dir / "pending-publications").glob("*.json"))
            self.assertEqual(len(journal_paths), 1)
            self.assertEqual(json.loads(journal_paths[0].read_text())["phase"], "submitting")
            self.assertFalse(journal_paths[0].is_relative_to(folder))
            self.assertEqual(Path(args[args.index("-p") + 1]), folder)
            return ""

        self.store._run = Mock(side_effect=upload)
        self.matching_marker(folder)
        self.assertEqual(self.publish(folder), 1)
        self.assertEqual(list((self.store.work_dir / "pending-publications").iterdir()), [])

    def test_journal_write_failure_before_submission_prevents_upload(self):
        folder = self.publication_folder()
        self.store.status = Mock(return_value=None)
        self.store._run = Mock(return_value="")
        write_json = cloud.write_json

        def write(path, value):
            if path.parent.name == "pending-publications":
                raise OSError("disk full")
            write_json(path, value)

        with patch.object(cloud, "write_json", side_effect=write), self.assertRaisesRegex(cloud.KaggleError, "Cannot save publication journal"):
            self.publish(folder)
        self.store._run.assert_not_called()
        self.assertTrue((folder / "result.json").exists())

    def test_journal_update_failure_after_submission_keeps_prior_evidence(self):
        folder = self.publication_folder()
        self.store.status = Mock(return_value=None)
        self.store._run = Mock(return_value="")
        write_json = cloud.write_json

        def write(path, value):
            if path.parent.name == "pending-publications" and value["phase"] == "accepted":
                raise OSError("disk full after upload")
            write_json(path, value)

        with patch.object(cloud, "write_json", side_effect=write), self.assertRaises(cloud.KaggleError) as failure:
            self.publish(folder)
        self.assertIn("disk full after upload", cloud.describe_exception(failure.exception))
        journal = json.loads(next((self.store.work_dir / "pending-publications").glob("*.json")).read_text())
        self.assertEqual(journal["phase"], "submitting")
        self.assertEqual(self.store._run.call_count, 1)
        self.assertTrue((folder / cloud.PUBLICATION_FILE).exists())

    def test_official_terminal_states_stop_waiting_and_publication(self):
        for state in ("processing_error", "deleted"):
            with self.subTest(state=state):
                self.store.status = Mock(return_value=status(state, 1))
                with self.assertRaisesRegex(cloud.KaggleError, "Dataset processing failed"):
                    self.store.wait_ready("research")
                self.assertEqual(self.store.status.call_count, 1)
                self.store.status = Mock(return_value=status(state, 1))
                with self.assertRaisesRegex(cloud.KaggleError, "Dataset processing failed"):
                    self.store._confirm_publication("research", {"publication_id": "id"}, 1, submitted=True)
                self.assertEqual(self.store.status.call_count, 1)

    def test_accepted_create_403_then_processing_and_ready_uploads_only_once(self):
        folder = self.publication_folder()
        commands = []
        clock = [100.0]
        responses = iter(["403 Client Error: Forbidden", "403 Client Error: Forbidden",
                          status("processing", 1), status("ready", 1)])
        created = False

        def runner(command, **kwargs):
            nonlocal created
            commands.append((command[2], kwargs["timeout"]))
            if command[2] == "create":
                self.assertFalse(created, "create must never repeat during confirmation")
                created = True
                return subprocess.CompletedProcess(command, 0, "Created", "")
            if command[2] == "list":
                value = "ref,title\nowner/research,Research\n" if created else "No datasets found"
                return subprocess.CompletedProcess(command, 0, value, "")
            self.assertEqual(command[2], "status")
            response = next(responses)
            if isinstance(response, str):
                return subprocess.CompletedProcess(command, 1, "", response)
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

        self.store.runner = runner
        self.matching_marker(folder)
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            self.assertEqual(self.publish(folder), 1)
        self.assertEqual([operation for operation, _ in commands],
                         ["status", "list", "create", "status", "list", "status", "status"])
        self.assertEqual(commands[5][1], 105, "403 retry read must fit inside the remaining grace window")
        self.assertEqual(self.store.download_file.call_args.args[2], 1)
        self.assertFalse(list((self.store.work_dir / "pending-publications").glob("*.json")))

    @staticmethod
    def forbidden_status(message="HTTP 403 Forbidden"):
        return cloud.KaggleError(message, http_status=403, operation="datasets status")

    def test_persistent_create_403_is_bounded_and_keeps_accepted_journal(self):
        folder = self.publication_folder()
        clock = [100.0]
        self.store.status = Mock(side_effect=[None] + [self.forbidden_status()] * 9)
        self.store._run = Mock(return_value="Created")
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaisesRegex(cloud.KaggleError, "confirmation grace expired") as failure:
                self.publish(folder)
        self.assertEqual(clock[0], 220)
        self.assertEqual(self.store.status.call_count, 9)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertIn("HTTP 403 Forbidden", cloud.describe_exception(failure.exception))
        journal = json.loads(next((self.store.work_dir / "pending-publications").glob("*.json")).read_text())
        self.assertEqual(journal["phase"], "accepted")
        self.assertEqual(journal["attempt"], 1)
        self.assertEqual(journal["publication"], json.loads((folder / cloud.PUBLICATION_FILE).read_text()))

    def test_create_403_grace_does_not_extend_session_deadline(self):
        folder = self.publication_folder()
        clock = [100.0]
        self.store.deadline = 110
        self.store.status = Mock(side_effect=[None, self.forbidden_status()])
        self.store._run = Mock(return_value="Created")
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(cloud.KaggleDeadlineError):
                self.publish(folder)
        self.assertEqual(clock[0], 110)
        self.assertEqual(self.store._run.call_count, 1)
        self.assertEqual(len(list((self.store.work_dir / "pending-publications").glob("*.json"))), 1)

    def test_version_upload_does_not_receive_create_403_grace(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[status(version=1), self.forbidden_status(), status(version=2)])
        self.store._run = Mock(return_value="Uploaded")
        with patch.object(cloud.time, "sleep") as sleep, self.assertRaisesRegex(cloud.KaggleError, "Cannot determine"):
            self.publish(folder)
        sleep.assert_not_called()
        self.assertEqual(self.store.status.call_count, 2)
        self.assertEqual(self.store._run.call_count, 1)

    def test_unknown_create_does_not_receive_403_grace(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, self.forbidden_status()])
        self.store._run = Mock(side_effect=cloud.KaggleError("HTTP 499 Client Error"))
        with patch.object(cloud.time, "sleep") as sleep, self.assertRaisesRegex(cloud.KaggleError, "Cannot determine"):
            self.publish(folder)
        sleep.assert_not_called()
        self.assertEqual(self.store._run.call_count, 1)

    def test_create_grace_rejects_other_owners_and_non_status_auth_errors(self):
        cases = [
            ("other/research", self.forbidden_status()),
            ("research", cloud.KaggleError("HTTP 403 Forbidden", http_status=403, operation="datasets list")),
            ("research", cloud.KaggleError("HTTP 401 Unauthorized", http_status=401, operation="datasets status")),
            ("research", self.forbidden_status("HTTP 403 Forbidden: quota exceeded")),
            ("research", cloud.KaggleError("403 mentioned without HTTP evidence")),
        ]
        for slug, error in cases:
            with self.subTest(slug=slug, error=str(error)):
                self.store.status = Mock(side_effect=error)
                with patch.object(cloud.time, "sleep") as sleep, self.assertRaisesRegex(cloud.KaggleError, "Cannot determine"):
                    self.store._confirm_publication(slug, {"publication_id": "id"}, 1,
                                                    submitted=True, accepted_create_at=cloud.time.monotonic())
                sleep.assert_not_called()
                self.assertEqual(self.store.status.call_count, 1)

    def test_status_list_403_does_not_enter_create_grace(self):
        folder = self.publication_folder()
        created = False

        def runner(command, **kwargs):
            nonlocal created
            if command[2] == "create":
                created = True
                return subprocess.CompletedProcess(command, 0, "Created", "")
            if command[2] == "list" and not created:
                return subprocess.CompletedProcess(command, 0, "No datasets found", "")
            return subprocess.CompletedProcess(command, 1, "", "HTTP 403 Forbidden")

        self.store.runner = Mock(side_effect=runner)
        with patch.object(cloud.time, "sleep") as sleep, self.assertRaisesRegex(cloud.KaggleError, "Cannot determine") as failure:
            self.publish(folder)
        sleep.assert_not_called()
        self.assertIn("datasets list", cloud.describe_exception(failure.exception))
        self.assertEqual(self.store.runner.call_count, 5)

    def test_matching_state_after_create_403_still_requires_own_marker(self):
        folder = self.publication_folder()
        self.store.status = Mock(side_effect=[None, self.forbidden_status(), status(version=1)])
        self.store._run = Mock(return_value="Created")
        self.store.download_file = Mock(side_effect=lambda slug, filename, version, destination:
            destination.write_text('{"publication_id":"other"}'))
        with patch.object(cloud.time, "sleep"), self.assertRaisesRegex(cloud.KaggleError, "different publication marker"):
            self.publish(folder)
        self.assertEqual(self.store._run.call_count, 1)

    def test_successful_processing_read_does_not_restart_create_grace(self):
        folder = self.publication_folder()
        clock = [100.0]
        self.store.status = Mock(side_effect=[None, self.forbidden_status(), status("processing", 1), self.forbidden_status()])
        self.store._run = Mock(return_value="Created")
        def sleep(seconds):
            clock[0] += seconds if clock[0] == 100 else 120
        with patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(cloud.time, "sleep", side_effect=sleep), self.assertRaisesRegex(cloud.KaggleError, "Cannot determine"):
            self.publish(folder)
        self.assertEqual(self.store.status.call_count, 4)
        self.assertEqual(self.store._run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
