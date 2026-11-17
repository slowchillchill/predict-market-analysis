import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import date, datetime, timezone, timedelta


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import polymarket_kaggle_scheduler as scheduler
from prepare_kaggle_notebook import prepare_notebook


KERNEL = scheduler.OWNER + "/qa-external-scheduler"
NOW = datetime(2026, 11, 16, 0, 5, tzinfo=timezone.utc)
FIRST = date(2026, 11, 16)


def fixture_bundle():
    metadata = {"id": KERNEL, "title": "QA External Scheduler", "code_file": "notebook.ipynb",
                "language": "python", "kernel_type": "notebook", "is_private": True,
                "enable_gpu": False, "enable_tpu": False, "enable_internet": True,
                "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": []}
    notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "code", "source": "print('QA')\n", "metadata": {},
         "execution_count": None, "outputs": []}]}
    return scheduler.Bundle(metadata, notebook)


class FakeBackend:
    def __init__(self, bundle, state_path):
        self.bundle, self.state_path = bundle, state_path
        self.versions = {7: scheduler.Snapshot(7, "COMPLETE", scheduler.canonical(bundle.notebook))}
        self.current = 7
        self.writes = 0
        self.identity_checks = 0
        self.fail_identity = False
        self.lose_response = False
        self.readback_fails = False
        self.submit_fails_before_acceptance = False
        self.source_mismatch = False
        self.accepted_status = "QUEUED"
        self.change_on_recheck = False
        self.latest_reads = 0
        self.assertion = None

    def verify_identity(self):
        self.identity_checks += 1
        if self.fail_identity:
            raise RuntimeError("PRIVATE_TOKEN identity failed")

    def snapshot(self, version=None):
        if version is None:
            self.latest_reads += 1
            if self.change_on_recheck and self.latest_reads == 2:
                return scheduler.Snapshot(8, "RUNNING", "{}")
            return self.versions[self.current]
        if self.readback_fails:
            raise TimeoutError("PRIVATE_TOKEN readback")
        return self.versions[version]

    def submit(self, source):
        self.writes += 1
        persisted = json.loads(self.state_path.read_text())
        assert persisted["pending"] is not None
        assert persisted["pending"]["version"] is None
        assert persisted["pending"]["marker"] == scheduler.source_marker(source)
        if self.assertion:
            self.assertion(source)
        if self.submit_fails_before_acceptance:
            raise TimeoutError("PRIVATE_TOKEN upload")
        self.current += 1
        self.versions[self.current] = scheduler.Snapshot(
            self.current, self.accepted_status,
            scheduler.canonical(self.bundle.notebook) if self.source_mismatch else source)
        if self.lose_response:
            raise TimeoutError("PRIVATE_TOKEN accepted response lost")
        return self.current


class SchedulerStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state.json"
        self.bundle = fixture_bundle()
        self.backend = FakeBackend(self.bundle, self.state)

    def run_scheduler(self, now=NOW, first_date=FIRST, check_only=False):
        return scheduler.run_scheduler(self.backend, self.bundle, self.state, first_date, now,
                                       check_only=check_only)

    def test_confirmed_submission_is_durable_and_same_day_does_not_repeat(self):
        result = self.run_scheduler()
        self.assertEqual(result["action"], "submitted")
        self.assertEqual(result["submitted_version"], 8)
        state = json.loads(self.state.read_text())
        self.assertIsNone(state["pending"])
        self.assertEqual(state["last_submission"]["version"], 8)
        self.assertEqual(self.run_scheduler()["action"], "already_submitted")
        self.assertEqual(self.backend.writes, 1)

    def test_next_utc_day_submits_after_terminal_result(self):
        self.run_scheduler()
        self.backend.versions[8].status = "ERROR"
        self.assertEqual(self.run_scheduler()["action"], "already_submitted")
        self.assertEqual(self.run_scheduler(NOW + timedelta(days=1))["submitted_version"], 9)
        self.assertEqual(self.backend.writes, 2)

    def test_missing_local_state_still_detects_todays_remote_submission(self):
        self.run_scheduler()
        self.backend.versions[8].status = "COMPLETE"
        self.state.unlink()
        self.assertEqual(self.run_scheduler()["action"], "already_submitted_remote")
        self.assertEqual(self.backend.writes, 1)

    def test_started_or_queued_or_cancelling_remote_is_not_duplicated(self):
        for status in scheduler.ACTIVE_STATUSES:
            with self.subTest(status=status):
                self.backend.versions[7].status = status
                self.assertEqual(self.run_scheduler()["action"], "remote_active")
                self.assertEqual(self.backend.writes, 0)
                self.assertFalse(self.state.exists())

    def test_unknown_status_stops_before_write(self):
        self.backend.versions[7].status = "FUTURE_UNKNOWN"
        with self.assertRaisesRegex(scheduler.SchedulerError, "unknown_remote_status"):
            self.run_scheduler()
        self.assertEqual(self.backend.writes, 0)

    def test_first_date_blocks_deployment_day_catchup(self):
        result = self.run_scheduler(NOW - timedelta(days=1))
        self.assertEqual(result["action"], "before_first_date")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.backend.writes, 0)

    def test_before_utc_0005_waits_and_jst_input_is_converted(self):
        self.assertEqual(self.run_scheduler(NOW - timedelta(seconds=1))["action"], "before_daily_time")
        jst = timezone(timedelta(hours=9))
        instant = datetime(2026, 11, 16, 9, 5, tzinfo=jst)
        self.assertEqual(self.run_scheduler(instant)["action"], "submitted")
        self.assertEqual(json.loads(self.state.read_text())["last_submission"]["marker"]["date_utc"], "2026-11-16")

    def test_check_only_reads_identity_and_status_without_state_or_write(self):
        result = self.run_scheduler(check_only=True)
        self.assertEqual(result["action"], "would_submit")
        self.assertEqual(self.backend.identity_checks, 1)
        self.assertEqual(self.backend.writes, 0)
        self.assertFalse(self.state.exists())

    def test_lost_success_response_recovers_exact_marker_without_repeating_write(self):
        self.backend.lose_response = True
        result = self.run_scheduler()
        self.assertEqual(result["submitted_version"], 8)
        self.assertEqual(self.backend.writes, 1)
        self.assertIsNone(json.loads(self.state.read_text())["pending"])

    def test_restart_recovers_pending_fixed_version(self):
        self.backend.readback_fails = True
        with self.assertRaisesRegex(scheduler.SchedulerError, "submission_unconfirmed"):
            self.run_scheduler()
        saved = self.state.read_bytes()
        self.assertEqual(json.loads(saved)["pending"]["version"], 8)
        self.backend.readback_fails = False
        result = self.run_scheduler()
        self.assertEqual(result["action"], "already_submitted")
        self.assertEqual(result["recovered_version"], 8)
        self.assertEqual(self.backend.writes, 1)

    def test_check_only_can_verify_pending_but_does_not_change_state(self):
        self.backend.readback_fails = True
        with self.assertRaises(scheduler.SchedulerError):
            self.run_scheduler()
        saved = self.state.read_bytes()
        self.backend.readback_fails = False
        self.assertEqual(self.run_scheduler(check_only=True)["action"], "already_submitted")
        self.assertEqual(saved, self.state.read_bytes())
        self.assertEqual(self.backend.writes, 1)

    def test_restart_after_lost_response_uses_baseline_plus_one(self):
        self.backend.lose_response = True
        self.backend.readback_fails = True
        with self.assertRaises(scheduler.SchedulerError):
            self.run_scheduler()
        self.assertIsNone(json.loads(self.state.read_text())["pending"]["version"])
        self.backend.readback_fails = False
        self.assertEqual(self.run_scheduler()["recovered_version"], 8)
        self.assertEqual(self.backend.writes, 1)

    def test_cross_day_pending_active_run_is_not_duplicated_when_latest_is_stale(self):
        self.backend.readback_fails = True
        with self.assertRaises(scheduler.SchedulerError):
            self.run_scheduler()
        self.backend.readback_fails = False
        self.backend.current = 7  # latest endpoint still exposes old COMPLETE v7
        result = self.run_scheduler(NOW + timedelta(days=1))
        self.assertEqual(result["action"], "remote_active")
        self.assertEqual(result["active_version"], 8)
        self.assertEqual(self.backend.writes, 1)
        self.assertIsNone(json.loads(self.state.read_text())["pending"])

    def test_cross_day_recorded_active_run_is_not_duplicated_when_latest_is_stale(self):
        self.run_scheduler()
        self.backend.current = 7
        for status in scheduler.ACTIVE_STATUSES:
            with self.subTest(status=status):
                self.backend.versions[8].status = status
                result = self.run_scheduler(NOW + timedelta(days=1))
                self.assertEqual(result["action"], "remote_active")
                self.assertEqual(result["active_version"], 8)
                self.assertEqual(self.backend.writes, 1)

    def test_cross_day_terminal_recorded_version_still_requires_monotonic_latest(self):
        self.run_scheduler()
        self.backend.current = 7
        self.backend.versions[8].status = "COMPLETE"
        with self.assertRaisesRegex(scheduler.SchedulerError, "remote_version_regressed"):
            self.run_scheduler(NOW + timedelta(days=1))
        self.assertEqual(self.backend.writes, 1)

    def test_unaccepted_or_unknown_write_blocks_future_days(self):
        self.backend.submit_fails_before_acceptance = True
        with self.assertRaisesRegex(scheduler.SchedulerError, "submission_unconfirmed"):
            self.run_scheduler()
        saved = self.state.read_bytes()
        with self.assertRaisesRegex(scheduler.SchedulerError, "pending_unresolved"):
            self.run_scheduler(NOW + timedelta(days=1))
        self.assertEqual(self.backend.writes, 1)
        self.assertEqual(saved, self.state.read_bytes())

    def test_wrong_marker_never_confirms_pending(self):
        self.backend.source_mismatch = True
        with self.assertRaisesRegex(scheduler.SchedulerError, "submission_unconfirmed"):
            self.run_scheduler()
        self.assertIsNotNone(json.loads(self.state.read_text())["pending"])
        with self.assertRaisesRegex(scheduler.SchedulerError, "pending_unresolved"):
            self.run_scheduler()
        self.assertEqual(self.backend.writes, 1)

    def test_remote_changes_between_checks_stop_submission(self):
        self.backend.change_on_recheck = True
        self.assertEqual(self.run_scheduler()["action"], "remote_changed")
        self.assertEqual(self.backend.writes, 0)
        self.assertFalse(self.state.exists())

    def test_identity_failure_and_corrupt_state_do_not_submit(self):
        self.backend.fail_identity = True
        with self.assertRaises(RuntimeError):
            self.run_scheduler()
        self.backend.fail_identity = False
        self.state.write_text('{"schema_version":1,"kernel":"other/kernel"}')
        with self.assertRaisesRegex(scheduler.SchedulerError, "invalid_state"):
            self.run_scheduler()
        self.assertEqual(self.backend.writes, 0)

    def test_file_lock_excludes_second_scheduler(self):
        with scheduler.state_lock(self.directory) as first:
            self.assertTrue(first)
            with scheduler.state_lock(self.directory) as second:
                self.assertFalse(second)

    def test_state_persistence_failure_prevents_remote_write(self):
        with patch.object(scheduler, "atomic_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_scheduler()
        self.assertEqual(self.backend.writes, 0)


class BundleAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bundle = fixture_bundle()
        self.write_bundle()

    def write_bundle(self):
        (self.directory / "kernel-metadata.json").write_text(json.dumps(self.bundle.metadata))
        (self.directory / "notebook.ipynb").write_text(json.dumps(self.bundle.notebook))

    def test_accepts_existing_production_packaging(self):
        prepare_notebook(ROOT / "kaggle", self.directory)
        kernel = json.loads((ROOT / "kaggle/kernel-metadata.json").read_text())["id"]
        loaded = scheduler.load_bundle(self.directory, kernel)
        self.assertEqual(len(loaded.digest), 64)

    def test_rejects_different_kernel_gpu_outputs_extra_metadata_and_existing_marker(self):
        for mutate in [lambda: self.bundle.metadata.update(id="other/kernel"),
                       lambda: self.bundle.metadata.update(enable_gpu=True),
                       lambda: self.bundle.metadata.update(machine_shape="NvidiaTeslaT4"),
                       lambda: self.bundle.notebook["cells"][0].update(outputs=[{"data": "secret"}]),
                       lambda: self.bundle.notebook["cells"][0].update(source=scheduler.MARKER_PREFIX + "{}")]:
            with self.subTest(mutate=mutate):
                self.bundle = fixture_bundle()
                mutate()
                self.write_bundle()
                with self.assertRaises(scheduler.SchedulerError):
                    scheduler.load_bundle(self.directory, KERNEL)

    def test_rejects_path_escape_duplicate_json_and_oversized_file(self):
        self.bundle.metadata["code_file"] = "../private"
        self.write_bundle()
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.load_bundle(self.directory, KERNEL)
        (self.directory / "kernel-metadata.json").write_text('{"id":"one","id":"two"}')
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.load_bundle(self.directory, KERNEL)
        (self.directory / "kernel-metadata.json").write_text(" " * (scheduler.MAX_FILE_BYTES + 1))
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.load_bundle(self.directory, KERNEL)

    def test_main_never_prints_sensitive_exception_text(self):
        output = io.StringIO()
        arguments = ["--bundle-dir", str(self.directory), "--state-dir", str(self.directory / "state"),
                     "--expected-kernel", KERNEL, "--first-date-utc", "2026-11-16", "--check-only"]
        with patch.object(scheduler, "KaggleBackend", side_effect=RuntimeError("PRIVATE_TOKEN")), \
                patch("sys.stdout", output):
            self.assertEqual(scheduler.main(arguments), 1)
        self.assertNotIn("PRIVATE_TOKEN", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "operation_failed")


class OfficialSdkTransportTests(unittest.TestCase):
    def setUp(self):
        try:
            import kagglesdk
            import requests
        except ImportError:
            self.skipTest("official Kaggle SDK not installed in this interpreter")
        self.bundle = fixture_bundle()
        self.backend = scheduler.KaggleBackend(KERNEL, self.bundle, "PRIVATE_TOKEN")
        self.addCleanup(self.backend.close)

    def metadata(self):
        return SimpleNamespace(ref=KERNEL, title=self.bundle.metadata["title"],
                               language="python", kernel_type="notebook", enable_gpu=False,
                               enable_tpu=False, enable_internet=True, is_private=True,
                               dataset_data_sources=[], kernel_data_sources=[],
                               competition_data_sources=[], model_data_sources=[],
                               category_ids=["scheduled"], current_version_number=7)

    def test_remote_metadata_drift_stops_without_writes(self):
        api = self.backend.client.kernels.kernels_api_client
        changes = {"title": "Changed title", "language": "r", "kernel_type": "script",
                   "dataset_data_sources": ["owner/data"], "kernel_data_sources": ["owner/code"],
                   "competition_data_sources": ["competition"], "model_data_sources": ["owner/model"]}
        for name, value in changes.items():
            with self.subTest(field=name), tempfile.TemporaryDirectory() as directory:
                metadata = self.metadata()
                setattr(metadata, name, value)
                response = SimpleNamespace(metadata=metadata, blob=SimpleNamespace(
                    source=scheduler.canonical(self.bundle.notebook)))
                with patch.object(self.backend, "verify_identity"), \
                        patch.object(api, "get_kernel", return_value=response), \
                        patch.object(api, "save_kernel") as save:
                    with self.assertRaisesRegex(scheduler.SchedulerError, "remote_kernel_metadata_drift"):
                        scheduler.run_scheduler(self.backend, self.bundle, Path(directory) / "state.json",
                                                FIRST, NOW)
                    save.assert_not_called()

    def test_existing_remote_categories_are_preserved_on_submission(self):
        api = self.backend.client.kernels.kernels_api_client
        response = SimpleNamespace(metadata=self.metadata(), blob=SimpleNamespace(
            source=scheduler.canonical(self.bundle.notebook)))
        with patch.object(api, "get_kernel", return_value=response), \
                patch.object(api, "get_kernel_session_status", return_value=SimpleNamespace(
                    status=SimpleNamespace(name="COMPLETE"))):
            self.backend.snapshot()
        calls = self.adapter(payload={"versionNumber": 8, "ref": KERNEL,
                                      "url": "https://www.kaggle.com/code/" + KERNEL})
        self.backend.submit(scheduler.canonical(self.bundle.notebook))
        self.assertEqual(json.loads(calls[0][0].body)["categoryIds"], ["scheduled"])

    def adapter(self, *, code=200, payload=None, error=None):
        import requests
        from requests.adapters import BaseAdapter
        calls = []

        class RecordingAdapter(BaseAdapter):
            def send(self, request, **kwargs):
                calls.append((request, kwargs))
                if error:
                    raise error
                response = requests.Response()
                response.status_code = code
                response.headers["Content-Type"] = "application/json"
                response.headers["Location"] = "https://api.kaggle.com/v1/repeated-write"
                response._content = json.dumps(payload or {}).encode()
                response.request = request
                return response

            def close(self):
                pass

        self.backend.client.http_client()._session.mount("https://", RecordingAdapter())
        return calls

    def test_official_save_request_is_single_bounded_call_and_cpu_only(self):
        calls = self.adapter(payload={"versionNumber": 8, "ref": KERNEL,
                                      "url": "https://www.kaggle.com/code/" + KERNEL})
        self.assertEqual(self.backend.submit(scheduler.canonical(self.bundle.notebook)), 8)
        self.assertEqual(len(calls), 1)
        request, settings = calls[0]
        self.assertEqual(settings["timeout"], (10, 60))
        body = json.loads(request.body)
        self.assertEqual(body["slug"], KERNEL)
        self.assertFalse(body["enableGpu"])
        self.assertFalse(body["enableTpu"])
        self.assertTrue(body["enableInternet"])
        self.assertEqual(body["kernelExecutionType"], "SAVE_AND_RUN_ALL")
        self.assertEqual(body["sessionTimeoutSeconds"], 43200)

    def test_save_response_identity_mismatch_is_not_accepted(self):
        self.adapter(payload={"versionNumber": 8, "ref": "other/kernel",
                              "url": "https://www.kaggle.com/code/" + KERNEL})
        with self.assertRaisesRegex(scheduler.SchedulerError, "submission_identity_mismatch"):
            self.backend.submit("{}")

    def test_transport_does_not_retry_a_timeout_or_follow_307_redirect(self):
        import requests
        calls = self.adapter(error=requests.Timeout("PRIVATE_TOKEN"))
        with self.assertRaises(requests.Timeout):
            self.backend.submit("{}")
        self.assertEqual(len(calls), 1)
        calls = self.adapter(code=307)
        with self.assertRaisesRegex(scheduler.SchedulerError, "api_redirect_rejected"):
            self.backend.submit("{}")
        self.assertEqual(len(calls), 1)

    def test_authenticated_identity_is_checked_with_official_sdk(self):
        calls = self.adapter(payload={"active": True, "username": scheduler.OWNER})
        self.backend.verify_identity()
        self.assertTrue(calls[0][0].url.endswith("security.OAuthService/IntrospectToken"))
        self.adapter(payload={"active": True, "username": "another-owner"})
        with self.assertRaisesRegex(scheduler.SchedulerError, "credential_owner_mismatch"):
            self.backend.verify_identity()

    def test_expired_credentials_stop_and_valid_expiry_is_reported_without_token(self):
        self.adapter(payload={"active": True, "username": scheduler.OWNER, "exp": 1})
        with self.assertRaisesRegex(scheduler.SchedulerError, "credential_expired"):
            self.backend.verify_identity()
        self.adapter(payload={"active": True, "username": scheduler.OWNER, "exp": 4102444800})
        result = self.backend.verify_identity()
        self.assertEqual(result["credential_expires_at_utc"], "2100-01-01T00:00:00+00:00")
        self.assertNotIn("PRIVATE_TOKEN", str(result))
        self.adapter(payload={"active": True})
        with self.assertRaisesRegex(scheduler.SchedulerError, "credential_owner_mismatch"):
            self.backend.verify_identity()


if __name__ == "__main__":
    unittest.main()
