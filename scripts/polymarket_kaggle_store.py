"""Small versioned Kaggle Dataset adapter shared by the producer and reports."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

OWNER = "qiuqiuslowchillchill"
TITLE = "Polymarket Crypto Updown Markets Analysis"
OPERATION_TIMEOUT_SECONDS = 30 * 60
COMMAND_TIMEOUT_SECONDS = 15 * 60
POLL_SECONDS = 15
CREATE_CONFIRMATION_GRACE_SECONDS = 120
PUBLICATION_FILE = "publication.json"
FAILED_STATES = {"error", "failed", "processing_error", "deleted"}


class KaggleError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False,
                 http_status: int | None = None, operation: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.operation = operation


class KaggleDeadlineError(KaggleError):
    """The caller's deadline or this operation's timeout has expired."""


def redact_diagnostic(message: str) -> str:
    """Keep error evidence while excluding credentials from logs and artifacts."""
    for name in ("KAGGLE_API_TOKEN", "KAGGLE_KEY"):
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "[REDACTED]")
    message = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;\"']+", r"\1 [REDACTED]", message)
    # Signed downloads can put several credentials in the query. Keep the
    # endpoint for diagnosis, but never retain any URL query or fragment.
    message = re.sub(r"(https?://[^\s?\#\"'<>]+)[?\#][^\s\"'<>]*", r"\1?[REDACTED]", message)
    message = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", message)
    message = re.sub(
        r"(?i)(\b(?:kaggle_api_token|kaggle_key|access_token|api_key|token|password|signature)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}&]+", r"\1[REDACTED]", message)
    return message


def describe_exception(exc: BaseException) -> str:
    """Format the causal chain without exposing tokens or looping on cycles."""
    parts = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(redact_diagnostic(f"{type(exc).__name__}: {exc}"))
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return " <- ".join(parts)


def bounded_operation(method):
    @wraps(method)
    def bounded(self, *args, **kwargs):
        with self._operation():
            return method(self, *args, **kwargs)
    return bounded


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class KaggleStore:
    def __init__(self, owner: str = OWNER, work_dir: Path | str = "/tmp/polymarket-kaggle", *, runner=None,
                 deadline: float | None = None):
        self.owner = owner
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner or subprocess.run
        self.deadline = deadline
        self._operation_deadline = None

    @contextmanager
    def _operation(self):
        previous = self._operation_deadline
        self._operation_deadline = min(previous if previous is not None else float("inf"),
                                       time.monotonic() + OPERATION_TIMEOUT_SECONDS)
        try:
            self._remaining()
            yield
        finally:
            self._operation_deadline = previous

    def _remaining(self) -> float:
        limit = min(self.deadline if self.deadline is not None else float("inf"),
                    self._operation_deadline if self._operation_deadline is not None else float("inf"))
        remaining = limit - time.monotonic()
        if remaining <= 0:
            raise KaggleDeadlineError("Kaggle operation deadline reached; keep unpublished working files")
        return remaining

    def _sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, self._remaining()))
        self._remaining()

    @staticmethod
    def _retryable(error: KaggleError) -> bool:
        if isinstance(error, KaggleDeadlineError):
            return False
        message = str(error).lower()
        if any(word in message for word in ("quota", "unauthorized", "forbidden", "permission denied")):
            return False
        if any(code not in {"429", "499"} for code in re.findall(r"\b4\d\d\b", message)):
            return False
        return error.retryable or bool(re.search(r"\b(?:429|499|5\d\d)\b", message)) or any(
            word in message for word in ("connectionerror", "connection error", "connection reset",
                "connection aborted", "remotedisconnected", "readtimeout", "timed out",
                "max retries exceeded", "connection refused", "connection broken", "too many requests"))

    @staticmethod
    def _publication_conflict(error: KaggleError) -> bool:
        message = str(error).lower()
        return bool(re.search(r"\b409\b", message)) or any(
            text in message for text in ("already exists", "already-exists", "already in use"))

    def ref(self, slug: str) -> str:
        return slug if "/" in slug else f"{self.owner}/{slug}"

    @bounded_operation
    def _run(self, *arguments: str) -> str:
        mutation = arguments[:2] in (("datasets", "create"), ("datasets", "version"))
        operation = " ".join(arguments[:2])
        for attempt in range(3):
            cause = None
            try:
                result = self.runner(["kaggle", *map(str, arguments)], text=True, capture_output=True,
                                     timeout=min(COMMAND_TIMEOUT_SECONDS, self._remaining()))
            except subprocess.TimeoutExpired as exc:
                error = KaggleError(f"Kaggle {operation} command timed out; its remote result may be unknown",
                                    retryable=True)
                cause = exc
            except OSError as exc:
                error = KaggleError(f"Kaggle {operation} command could not run: {type(exc).__name__}",
                                    retryable=isinstance(exc, (ConnectionError, TimeoutError)))
                cause = exc
            else:
                output = (result.stdout or "") + (result.stderr or "")
                # These CLI wrappers print API failures but still exit zero.
                printed_error = "Dataset creation error:" in output or "Dataset version creation error:" in output
                if result.returncode == 0 and not printed_error:
                    return result.stdout.strip()
                # Only recognize explicit HTTP diagnostics, not arbitrary
                # three-digit numbers in Dataset names or upload progress.
                matches = re.finditer(r"\bHTTP(?:Error)?:?\s+(\d{3})\b|\b(\d{3})\s+(?:Client|Server) Error\b",
                                      output, re.IGNORECASE)
                codes = {int(next(value for value in match.groups() if value)) for match in matches}
                http_status = codes.pop() if len(codes) == 1 else None
                error = KaggleError(redact_diagnostic(
                    f"Kaggle {operation} exited {result.returncode}: {output.strip() or 'no output'}"),
                    http_status=http_status, operation=operation)
            if not mutation and self._retryable(error) and attempt < 2:
                limited = "429" in str(error) or "too many requests" in str(error).lower()
                delay = 60 * (attempt + 1) if limited else 2 ** (attempt + 1)
                print(f"Kaggle temporary read error: retrying in {delay}s; {describe_exception(error)}", flush=True)
                self._sleep(delay)
                continue
            raise error from cause
        raise AssertionError("unreachable")

    def _exists(self, slug: str) -> bool:
        # Kaggle returns 403 for nonexistent datasets, too. A successful owner
        # listing distinguishes absence from an authentication failure.
        ref = self.ref(slug)
        page = 1
        while True:
            output = self._run("datasets", "list", "--user", ref.split("/")[0],
                               "--search", ref.split("/")[1], "--page", str(page), "--csv")
            if not output or output == "No datasets found":
                return False
            rows = list(csv.DictReader(io.StringIO(output)))
            if any(row.get("ref") == ref for row in rows):
                return True
            if len(rows) < 20:
                return False
            page += 1

    @bounded_operation
    def list_slugs(self, prefix: str) -> list[str]:
        slugs = []
        page = 1
        while True:
            output = self._run("datasets", "list", "--user", self.owner,
                               "--search", prefix, "--page", str(page), "--csv")
            if not output or output == "No datasets found":
                break
            rows = list(csv.DictReader(io.StringIO(output)))
            for row in rows:
                ref = row.get("ref", "")
                if ref.startswith(f"{self.owner}/{prefix}"):
                    slugs.append(ref.split("/", 1)[1])
            if len(rows) < 20:
                break
            page += 1
        return sorted(slugs)

    @bounded_operation
    def status(self, slug: str) -> dict | None:
        try:
            output = self._run("datasets", "status", self.ref(slug), "--format", "json")
        except KaggleError as exc:
            if ("403" in str(exc) or "404" in str(exc)) and not self._exists(slug):
                return None
            raise
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("status response must be an object")
            state = result.get("status")
            if not isinstance(state, str) or not state.strip():
                raise ValueError("status response needs a nonempty status string")
            version = result["current_version_number"]
            if version is None and state.lower() != "ready":
                version = 0
            if isinstance(version, bool) or not isinstance(version, (int, str)):
                raise ValueError("current_version_number must be a nonnegative integer")
            number = int(version)
            if number < 0 or (state.lower() == "ready" and number == 0):
                raise ValueError("current_version_number is invalid for the Dataset state")
            return {**result, "status": state.lower(), "current_version_number": number}
        except (ValueError, KeyError, TypeError) as exc:
            # A successful CLI exit with malformed output is not proof of
            # Dataset absence and must never lead to another upload.
            detail = redact_diagnostic(output)[:1000]
            raise KaggleError(f"Invalid Dataset status response for {self.ref(slug)}: {detail}") from exc

    def version(self, slug: str) -> int | None:
        status = self.status(slug)
        if status is None or status["status"].lower() != "ready":
            return None
        return int(status["current_version_number"])

    @bounded_operation
    def download_file(self, slug: str, filename: str, version: int, destination: Path | str) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="download-", dir=self.work_dir) as temporary:
            self._run("datasets", "download", f"{self.ref(slug)}/{version}",
                      "-f", filename, "-p", temporary, "--unzip", "--quiet")
            source = Path(temporary) / filename
            if not source.is_file():
                raise KaggleError(f"Missing file in Dataset version: {filename}")
            shutil.move(str(source), destination)
        return destination

    @bounded_operation
    def restore(self, slug: str, destination: Path | str) -> int | None:
        """Restore a complete monthly snapshot, including files not changed today."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        status = self.status(slug)
        version = None
        if status is not None:
            version = (int(status["current_version_number"]) if status["status"].lower() == "ready"
                       else self.wait_ready(slug, int(status["current_version_number"] or 1)))
            self._run("datasets", "download", f"{self.ref(slug)}/{version}",
                      "-p", str(destination), "--unzip", "--quiet")
        return version

    @bounded_operation
    def publish(self, slug: str, directory: Path | str, *, title: str, description: str,
                message: str) -> int:
        directory = Path(directory)
        previous = self.status(slug)
        if previous and previous["status"].lower() != "ready":
            self.wait_ready(slug)
            previous = self.status(slug)
        write_json(directory / "dataset-metadata.json", {
            "id": self.ref(slug), "title": title, "subtitle": "Public research data and reproducible analysis",
            "description": description, "licenses": [{"name": "CC0-1.0"}],
            "keywords": ["finance", "cryptocurrency"],
        })
        publication = {"schema_version": 1, "dataset": self.ref(slug), "publication_id": uuid.uuid4().hex}
        write_json(directory / PUBLICATION_FILE, publication)
        expected = int(previous["current_version_number"]) + 1 if previous else 1
        arguments = (("datasets", "version", "-p", str(directory), "-m", message,
                      "--keep-tabular", "--quiet") if previous else
                     ("datasets", "create", "-p", str(directory), "--public", "--keep-tabular", "--quiet"))
        journal_path = self.work_dir / "pending-publications" / f"{publication['publication_id']}.json"
        if journal_path.resolve().is_relative_to(directory.resolve()):
            raise KaggleError("Publication journal must be outside the uploaded directory")
        journal = {"schema_version": 1, "dataset": self.ref(slug), "expected_version": expected,
                   "publication": publication, "source_directory": str(directory.resolve())}

        def record(phase: str, submitted: bool | None) -> None:
            journal.update(phase=phase, submitted=submitted)
            try:
                write_json(journal_path, journal)
            except OSError as exc:
                raise KaggleError(f"Cannot save publication journal {journal_path}; keep working files") from exc

        def confirmed(version: int) -> int:
            try:
                journal_path.unlink()
            except OSError as exc:
                # Marker confirmation is already complete. A cleanup failure
                # must not turn a confirmed publication into an unknown result.
                print(f"Confirmed Dataset version {version}, but journal cleanup failed: "
                      f"{describe_exception(exc)}; journal={journal_path}", flush=True)
            return version

        uncertain_write = False
        try:
            for attempt in range(3):
                journal["attempt"] = attempt + 1
                record("submitting", None)
                try:
                    self._run(*arguments)
                except KaggleError as exc:
                    record("unknown", None)
                    if uncertain_write and self._publication_conflict(exc):
                        # An earlier write may have committed before it became
                        # visible. A conflict is not proof that it is ours.
                        version = self._confirm_publication(slug, publication, expected, submitted=True)
                        return confirmed(version)
                    if not self._retryable(exc):
                        raise
                    uncertain_write = True
                    version = self._confirm_publication(slug, publication, expected, submitted=False)
                    if version is not None:
                        return confirmed(version)
                    if attempt == 2:
                        raise KaggleError("Dataset publication was not accepted after three attempts") from exc
                    self._sleep(60 * (attempt + 1) if "429" in str(exc) else 2 ** (attempt + 1))
                    continue
                accepted_create_at = time.monotonic() if previous is None else None
                record("accepted", True)
                version = self._confirm_publication(
                    slug, publication, expected, submitted=True,
                    accepted_create_at=accepted_create_at)
                return confirmed(version)
        except KaggleError as exc:
            context = (f"dataset={self.ref(slug)} expected_version={expected} "
                       f"publication_id={publication['publication_id']} phase={journal.get('phase')} "
                       f"submitted={journal.get('submitted')} journal={journal_path}")
            raise type(exc)(f"{exc}; {context}", retryable=exc.retryable,
                            http_status=exc.http_status, operation=exc.operation) from exc
        raise AssertionError("unreachable")

    def _confirm_publication(self, slug: str, publication: dict, expected: int, *, submitted: bool,
                             accepted_create_at: float | None = None) -> int | None:
        """Only a pinned version with this operation's marker proves acceptance."""
        # This exception is restricted to a successful create by this owner.
        # It does not apply to version uploads, unknown writes or list/auth
        # failures. Once accepted, this method can only read; it cannot upload.
        grace_until = (accepted_create_at + CREATE_CONFIRMATION_GRACE_SECONDS
                       if accepted_create_at is not None and submitted and expected == 1
                       and self.ref(slug).split("/", 1)[0] == self.owner else None)
        last_forbidden = None
        unchanged = 0
        read_failures = 0
        while True:
            self._remaining()
            if last_forbidden is not None and time.monotonic() >= grace_until:
                raise KaggleError("Cannot determine Dataset publication result; new Dataset confirmation "
                                  f"grace expired; keep working files; dataset={self.ref(slug)} "
                                  f"expected_version={expected} publication_id={publication['publication_id']}") from last_forbidden
            previous_deadline = self._operation_deadline
            try:
                if last_forbidden is not None:
                    # Bound subsequent CLI calls as well as polling sleeps.
                    self._operation_deadline = min(
                        previous_deadline if previous_deadline is not None else float("inf"), grace_until)
                try:
                    self._remaining()
                    status = self.status(slug)
                finally:
                    self._operation_deadline = previous_deadline
            except KaggleDeadlineError:
                raise
            except KaggleError as exc:
                read_failures += 1
                grace_remaining = grace_until - time.monotonic() if grace_until is not None else 0
                if (grace_remaining > 0 and exc.http_status == 403 and exc.operation == "datasets status"
                        and not any(word in str(exc).lower() for word in ("quota", "unauthorized", "permission denied"))):
                    last_forbidden = exc
                    print(f"Accepted Dataset create is not readable yet; dataset={self.ref(slug)} "
                          f"read_only_confirmation_remaining={grace_remaining:.1f}s; "
                          f"{describe_exception(exc)}", flush=True)
                    self._sleep(min(POLL_SECONDS, grace_remaining))
                    continue
                if not self._retryable(exc) or read_failures >= 3:
                    raise KaggleError("Cannot determine Dataset publication result; keep working files; "
                                      f"dataset={self.ref(slug)} expected_version={expected} "
                                      f"publication_id={publication['publication_id']} submitted={submitted} "
                                      f"consecutive_read_failures={read_failures}") from exc
                print(f"Dataset confirmation read failed ({read_failures}/3); "
                      f"dataset={self.ref(slug)} retrying in {POLL_SECONDS}s; {describe_exception(exc)}", flush=True)
                self._sleep(POLL_SECONDS)
                continue
            last_forbidden = None
            read_failures = 0
            if status:
                state = status["status"].lower()
                version = int(status["current_version_number"] or 0)
                if state in FAILED_STATES:
                    raise KaggleError(f"Dataset processing failed: {self.ref(slug)} ({state})")
                if state == "ready" and version >= expected:
                    with tempfile.TemporaryDirectory(prefix="publication-", dir=self.work_dir) as temporary:
                        marker = Path(temporary) / PUBLICATION_FILE
                        try:
                            self.download_file(slug, PUBLICATION_FILE, version, marker)
                            actual = json.loads(marker.read_text())
                        except KaggleDeadlineError:
                            raise
                        except (KaggleError, ValueError, OSError) as exc:
                            raise KaggleError("Cannot verify Dataset publication marker; keep working files") from exc
                    if actual != publication:
                        raise KaggleError(f"Dataset version {version} has a different publication marker; keep working files")
                    return version
                not_submitted = state == "ready" and version < expected
            else:
                not_submitted = True
            # A successful submission may not be visible yet. After an uncertain
            # failure, require two unchanged observations before retrying upload.
            unchanged = unchanged + 1 if not_submitted else 0
            if not submitted and unchanged >= 2:
                return None
            print(f"Checking Dataset publication: {self.ref(slug)} version {expected}", flush=True)
            self._sleep(POLL_SECONDS)

    @bounded_operation
    def wait_ready(self, slug: str, expected: int = 1) -> int:
        while True:
            status = self.status(slug)
            if status:
                state = status["status"].lower()
                version = int(status["current_version_number"] or 0)
                if state == "ready" and version >= expected:
                    return version
                if state in FAILED_STATES:
                    raise KaggleError(f"Dataset processing failed: {self.ref(slug)} ({state})")
            print(f"Waiting for Dataset processing: {self.ref(slug)} version {expected}", flush=True)
            self._sleep(POLL_SECONDS)
