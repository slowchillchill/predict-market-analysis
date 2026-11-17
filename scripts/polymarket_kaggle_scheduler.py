"""Submit one small Kaggle Notebook per UTC day; never retry an uncertain write.

This process coordinates one external scheduler host. The native Kaggle schedule
must be disabled before enabling its systemd timer. It does not lock out a human
or a different host. Only the official SDK's SaveKernel method writes remotely.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import fcntl
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
import uuid


OWNER = "qiuqiuslowchillchill"
MARKER_PREFIX = "# polymarket_external_scheduler: "
MAX_FILE_BYTES = 256 * 1024
ACTIVE_STATUSES = {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}
TERMINAL_STATUSES = {"COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED", "NEW_SCRIPT"}
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


class SchedulerError(RuntimeError):
    """A fixed, credential-free diagnostic code."""


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SchedulerError("duplicate_json_key")
        result[key] = value
    return result


def read_json(path: Path):
    if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise SchedulerError("invalid_file")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_strict_object)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_kernel(kernel: str) -> None:
    if not re.fullmatch(OWNER + r"/[a-z0-9][a-z0-9-]{2,99}", kernel):
        raise SchedulerError("unexpected_kernel")


@dataclass
class Bundle:
    metadata: dict
    notebook: dict

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical([self.metadata, self.notebook]).encode()).hexdigest()

    def marked_source(self, marker: dict) -> str:
        notebook = deepcopy(self.notebook)
        code = next(cell for cell in notebook["cells"] if cell["cell_type"] == "code")
        code["source"] = MARKER_PREFIX + canonical(marker) + "\n" + code["source"]
        return canonical(notebook)


def load_bundle(directory: Path, expected_kernel: str) -> Bundle:
    validate_kernel(expected_kernel)
    metadata = read_json(directory / "kernel-metadata.json")
    required = {"id", "title", "code_file", "language", "kernel_type", "is_private",
                "enable_gpu", "enable_tpu", "enable_internet", "dataset_sources",
                "competition_sources", "kernel_sources", "model_sources"}
    if not isinstance(metadata, dict) or set(metadata) != required:
        raise SchedulerError("invalid_bundle_metadata_fields")
    if metadata["id"] != expected_kernel or metadata["code_file"] != "notebook.ipynb":
        raise SchedulerError("bundle_identity_mismatch")
    if metadata["language"] != "python" or metadata["kernel_type"] != "notebook":
        raise SchedulerError("invalid_bundle_type")
    if (metadata["enable_gpu"] is not False or metadata["enable_tpu"] is not False
            or metadata["enable_internet"] is not True or type(metadata["is_private"]) is not bool):
        raise SchedulerError("invalid_bundle_resources")
    if not isinstance(metadata["title"], str) or not 5 <= len(metadata["title"]) <= 100:
        raise SchedulerError("invalid_bundle_title")
    for name in ("dataset_sources", "competition_sources", "kernel_sources", "model_sources"):
        if metadata[name] != []:
            raise SchedulerError("unexpected_bundle_inputs")
    notebook = read_json(directory / "notebook.ipynb")
    if not isinstance(notebook, dict) or notebook.get("nbformat") != 4:
        raise SchedulerError("invalid_notebook")
    cells = notebook.get("cells")
    if not isinstance(cells, list) or not cells or len(cells) > 20:
        raise SchedulerError("invalid_notebook_cells")
    code_count = 0
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") not in {"code", "markdown"}:
            raise SchedulerError("invalid_notebook_cell")
        source = cell.get("source")
        if isinstance(source, list) and all(isinstance(line, str) for line in source):
            source = "".join(source)
            cell["source"] = source
        if not isinstance(source, str) or MARKER_PREFIX in source:
            raise SchedulerError("invalid_notebook_source")
        if cell["cell_type"] == "code":
            code_count += 1
            if cell.get("outputs") != [] or cell.get("execution_count") is not None:
                raise SchedulerError("notebook_contains_execution_output")
    if not code_count:
        raise SchedulerError("notebook_has_no_code")
    return Bundle(metadata, notebook)


def source_marker(source: str) -> dict | None:
    if not isinstance(source, str) or len(source.encode()) > MAX_FILE_BYTES:
        raise SchedulerError("invalid_remote_source")
    notebook = json.loads(source, object_pairs_hook=_strict_object)
    matches = []
    for cell in notebook.get("cells", []):
        text = cell.get("source", "")
        if isinstance(text, list):
            text = "".join(text)
        if cell.get("cell_type") == "code":
            for line in text.splitlines():
                if line.startswith(MARKER_PREFIX):
                    matches.append(json.loads(line[len(MARKER_PREFIX):], object_pairs_hook=_strict_object))
    if len(matches) > 1:
        raise SchedulerError("multiple_submission_markers")
    return matches[0] if matches else None


@dataclass
class Snapshot:
    version: int
    status: str
    source: str


class KaggleBackend:
    """Official generated SDK with explicit zero-retry, bounded HTTP transport.

    SDK 0.1.38 has no request timeout. Its private session is pinned here and
    tested, rather than allowing a POST redirect or a network retry to duplicate
    SaveKernel. No CLI authentication fallback or verbose logging is enabled.
    """

    def __init__(self, kernel: str, bundle: Bundle, token: str):
        if package_version("kagglesdk") != "0.1.38":
            raise SchedulerError("unsupported_sdk_version")
        if not token or token != token.strip():
            raise SchedulerError("missing_api_token")
        from kagglesdk import KaggleClient
        from kagglesdk.kaggle_env import KaggleEnv
        from requests.adapters import HTTPAdapter

        self.kernel, self.bundle = kernel, bundle
        self.owner, self.slug = kernel.split("/")
        self._category_ids = []
        self.client = KaggleClient(env=KaggleEnv.PROD, api_token=token, verbose=False,
                                   user_agent="polymarket-kaggle-scheduler/1")
        http = self.client.http_client()
        http._init_session()
        session = http._session
        session.trust_env = False
        session.mount("https://", HTTPAdapter(max_retries=0))
        send = session.send

        def bounded_send(request, **kwargs):
            if not request.url.startswith("https://api.kaggle.com/v1/"):
                raise SchedulerError("unexpected_api_endpoint")
            kwargs.update(timeout=(10, 60), allow_redirects=False)
            response = send(request, **kwargs)
            if 300 <= response.status_code < 400:
                response.close()
                raise SchedulerError("api_redirect_rejected")
            return response

        session.send = bounded_send
        self._token = token

    def close(self):
        self.client.http_client()._session.close()

    def verify_identity(self):
        from kagglesdk.security.types.oauth_service import IntrospectTokenRequest
        request = IntrospectTokenRequest()
        request.token = self._token
        response = self.client.security.oauth_client.introspect_token(request)
        if (response.active is not True or not isinstance(response.username, str)
                or response.username.casefold() != self.owner.casefold()):
            raise SchedulerError("credential_owner_mismatch")
        expiry = response.exp
        if expiry:
            if type(expiry) is not int or expiry <= datetime.now(timezone.utc).timestamp():
                raise SchedulerError("credential_expired")
            try:
                expires_at = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                raise SchedulerError("credential_expiry_invalid") from None
        else:
            expires_at = None
        return {"owner": self.owner, "credential_expires_at_utc": expires_at}

    def snapshot(self, version: int | None = None) -> Snapshot:
        from kagglesdk.kernels.types.kernels_api_service import (
            ApiGetKernelRequest, ApiGetKernelSessionStatusRequest)
        request = ApiGetKernelRequest()
        request.user_name, request.kernel_slug = self.owner, self.slug
        if version is not None:
            request.version_label = f"v{version}"
        response = self.client.kernels.kernels_api_client.get_kernel(request)
        metadata = response.metadata
        if metadata.ref != self.kernel:
            raise SchedulerError("remote_kernel_identity_mismatch")
        if (metadata.enable_gpu or metadata.enable_tpu or not metadata.enable_internet
                or metadata.is_private != self.bundle.metadata["is_private"]):
            raise SchedulerError("remote_kernel_settings_mismatch")
        if (metadata.title != self.bundle.metadata["title"] or metadata.language != "python"
                or metadata.kernel_type != "notebook"
                or metadata.dataset_data_sources != [] or metadata.kernel_data_sources != []
                or metadata.competition_data_sources != [] or metadata.model_data_sources != []):
            raise SchedulerError("remote_kernel_metadata_drift")
        if (not isinstance(metadata.category_ids, list)
                or not all(isinstance(tag, str) for tag in metadata.category_ids)):
            raise SchedulerError("invalid_remote_categories")
        self._category_ids = list(metadata.category_ids)
        selected_version = version if version is not None else metadata.current_version_number
        if type(selected_version) is not int or selected_version < 1:
            raise SchedulerError("invalid_remote_version")
        status_request = ApiGetKernelSessionStatusRequest()
        status_request.user_name, status_request.kernel_slug = self.owner, self.slug
        status_request.version_label = f"v{selected_version}"
        status = self.client.kernels.kernels_api_client.get_kernel_session_status(status_request).status.name
        if status not in KNOWN_STATUSES:
            raise SchedulerError("unknown_remote_status")
        return Snapshot(selected_version, status, response.blob.source)

    def submit(self, source: str) -> int:
        from kagglesdk.kernels.types.kernels_api_service import ApiSaveKernelRequest
        from kagglesdk.kernels.types.kernels_enums import KernelExecutionType
        request = ApiSaveKernelRequest()
        metadata = self.bundle.metadata
        request.slug, request.new_title, request.text = self.kernel, metadata["title"], source
        request.language, request.kernel_type = "python", "notebook"
        request.is_private = metadata["is_private"]
        request.enable_gpu, request.enable_tpu, request.enable_internet = False, False, True
        request.dataset_data_sources = []
        request.competition_data_sources = []
        request.kernel_data_sources = []
        request.model_data_sources = []
        request.category_ids = list(self._category_ids)
        request.kernel_execution_type = KernelExecutionType.SAVE_AND_RUN_ALL
        request.session_timeout_seconds = 43200
        response = self.client.kernels.kernels_api_client.save_kernel(request)
        if (response.error or response.invalid_tags or response.invalid_dataset_sources
                or response.invalid_competition_sources or response.invalid_kernel_sources
                or response.invalid_model_sources):
            raise SchedulerError("submission_response_rejected")
        if type(response.version_number) is not int or response.version_number < 1:
            raise SchedulerError("submission_version_missing")
        url = urlsplit(response.url)
        if (response.ref != self.kernel or url.scheme != "https"
                or url.netloc not in {"www.kaggle.com", "kaggle.com"}
                or url.path.rstrip("/") != "/code/" + self.kernel):
            raise SchedulerError("submission_identity_mismatch")
        return response.version_number


def atomic_state(path: Path, state: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".scheduler-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(state) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path: Path, kernel: str) -> dict:
    if not path.exists():
        return {"schema_version": 1, "kernel": kernel, "last_submission": None, "pending": None}
    state = read_json(path)
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or state.get("kernel") != kernel
            or set(state) != {"schema_version", "kernel", "last_submission", "pending"}):
        raise SchedulerError("invalid_state")
    for name in ("pending", "last_submission"):
        record = state[name]
        if record is None:
            continue
        if not isinstance(record, dict):
            raise SchedulerError("invalid_state_record")
        marker = record.get("marker")
        if (not isinstance(marker, dict) or set(marker) != {"date_utc", "submission_id", "bundle_sha256"}
                or not re.fullmatch(r"[a-f0-9]{32}", marker.get("submission_id", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", marker.get("bundle_sha256", ""))
                or date.fromisoformat(marker["date_utc"]).isoformat() != marker["date_utc"]
                or type(record.get("baseline_version")) is not int or record["baseline_version"] < 1):
            raise SchedulerError("invalid_state_record")
        if record.get("version") is not None and (type(record["version"]) is not int
                                                   or record["version"] <= record["baseline_version"]):
            raise SchedulerError("invalid_state_version")
        if name == "last_submission" and record.get("version") is None:
            raise SchedulerError("invalid_state_version")
    return state


@contextmanager
def state_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / "scheduler.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def reconcile_pending(backend, record: dict) -> dict:
    expected_version = record.get("version") or record["baseline_version"] + 1
    snapshot = backend.snapshot(expected_version)
    if source_marker(snapshot.source) != record["marker"]:
        raise SchedulerError("pending_marker_mismatch")
    if snapshot.status not in KNOWN_STATUSES:
        raise SchedulerError("unknown_remote_status")
    return {**record, "version": expected_version, "confirmed_status": snapshot.status}


def run_scheduler(backend, bundle: Bundle, state_path: Path, first_date: date,
                  now: datetime, *, check_only: bool = False) -> dict:
    """Run under state_lock. Tests inject time and backend; CLI uses real UTC only."""
    if now.tzinfo is None:
        raise SchedulerError("timezone_required")
    now = now.astimezone(timezone.utc)
    today = now.date()
    kernel = bundle.metadata["id"]
    state = load_state(state_path, kernel)
    identity = backend.verify_identity()
    latest = backend.snapshot()
    if latest.status not in KNOWN_STATUSES:
        raise SchedulerError("unknown_remote_status")
    result = {"date_utc": today.isoformat(), "target_date_utc": date.fromordinal(today.toordinal() - 1).isoformat(),
              "kernel": kernel, "version": latest.version, "status": latest.status,
              "check_only": check_only, "bundle_sha256": bundle.digest}
    if identity is not None:
        result.update(identity)
    if state["pending"] is not None:
        try:
            confirmed = reconcile_pending(backend, state["pending"])
        except Exception:
            raise SchedulerError("pending_unresolved") from None
        state["last_submission"], state["pending"] = confirmed, None
        if not check_only:
            atomic_state(state_path, state)
        result["recovered_version"] = confirmed["version"]
    previous = state["last_submission"]
    if previous is not None and date.fromisoformat(previous["marker"]["date_utc"]) > today:
        raise SchedulerError("clock_before_saved_submission")
    if today < first_date:
        return {**result, "action": "before_first_date"}
    if now.time().replace(tzinfo=None) < time(0, 5):
        return {**result, "action": "before_daily_time"}
    if previous is not None and previous["marker"]["date_utc"] == today.isoformat():
        return {**result, "action": "already_submitted", "submitted_version": previous["version"]}
    if previous is not None:
        # Latest metadata can lag behind an accepted version. An older COMPLETE
        # snapshot does not prove the recorded run has finished. Always inspect
        # our exact prior submission before allowing a later UTC day to write.
        prior = reconcile_pending(backend, previous)
        if prior["confirmed_status"] in ACTIVE_STATUSES:
            return {**result, "action": "remote_active", "active_version": prior["version"],
                    "active_status": prior["confirmed_status"]}
        if latest.version < prior["version"]:
            raise SchedulerError("remote_version_regressed")
    remote_marker = source_marker(latest.source)
    if remote_marker is not None:
        if not isinstance(remote_marker, dict) or not isinstance(remote_marker.get("date_utc"), str):
            raise SchedulerError("invalid_remote_marker")
        remote_day = date.fromisoformat(remote_marker["date_utc"])
        if remote_day > today:
            raise SchedulerError("clock_before_remote_submission")
        if remote_day == today:
            return {**result, "action": "already_submitted_remote", "submitted_version": latest.version}
    if latest.status in ACTIVE_STATUSES:
        return {**result, "action": "remote_active"}
    if check_only:
        return {**result, "action": "would_submit"}
    # Narrow the race with other writers. The server offers no atomic global
    # compare-and-submit operation; deployment therefore requires a single writer.
    rechecked = backend.snapshot()
    if rechecked.version != latest.version or rechecked.status in ACTIVE_STATUSES:
        return {**result, "action": "remote_changed"}
    if rechecked.status not in TERMINAL_STATUSES:
        raise SchedulerError("unknown_remote_status")
    marker = {"date_utc": today.isoformat(), "submission_id": uuid.uuid4().hex,
              "bundle_sha256": bundle.digest}
    record = {"marker": marker, "baseline_version": latest.version, "version": None,
              "prepared_at_utc": now.isoformat()}
    state["pending"] = record
    atomic_state(state_path, state)  # durable intent BEFORE the only remote write
    try:
        submitted = backend.submit(bundle.marked_source(marker))
        if type(submitted) is not int or submitted <= latest.version:
            raise SchedulerError("invalid_submitted_version")
        record["version"] = submitted
        atomic_state(state_path, state)
    except Exception:
        # The write may have reached Kaggle. Preserve pending and only read back.
        pass
    try:
        confirmed = reconcile_pending(backend, record)
    except Exception:
        raise SchedulerError("submission_unconfirmed") from None
    state["last_submission"], state["pending"] = confirmed, None
    atomic_state(state_path, state)
    return {**result, "action": "submitted", "submitted_version": confirmed["version"],
            "submitted_status": confirmed["confirmed_status"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--expected-kernel", required=True)
    parser.add_argument("--first-date-utc", type=date.fromisoformat, required=True)
    parser.add_argument("--check-only", action="store_true")
    arguments = parser.parse_args(argv)
    backend = None
    try:
        bundle = load_bundle(arguments.bundle_dir, arguments.expected_kernel)
        with state_lock(arguments.state_dir) as locked:
            if not locked:
                emit("scheduler", action="locked", check_only=arguments.check_only)
                return 0
            backend = KaggleBackend(arguments.expected_kernel, bundle,
                                    os.environ.get("KAGGLE_API_TOKEN", ""))
            result = run_scheduler(backend, bundle, arguments.state_dir / "state.json",
                                   arguments.first_date_utc, datetime.now(timezone.utc),
                                   check_only=arguments.check_only)
            emit("scheduler", **result)
        return 0
    except Exception as error:
        # Never expose exception text or traceback from HTTP/authentication code.
        code = str(error) if isinstance(error, SchedulerError) else "operation_failed"
        emit("scheduler_error", code=code, check_only=arguments.check_only)
        return 1
    finally:
        if backend is not None:
            backend.close()


if __name__ == "__main__":
    sys.exit(main())
