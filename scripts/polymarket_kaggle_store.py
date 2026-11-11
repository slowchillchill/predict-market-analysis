"""Small versioned Kaggle Dataset adapter shared by the producer and reports."""

from __future__ import annotations

import csv
import io
import json
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
PUBLICATION_FILE = "publication.json"


class KaggleError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class KaggleDeadlineError(KaggleError):
    """The caller's deadline or this operation's timeout has expired."""


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
        for attempt in range(3):
            cause = None
            try:
                result = self.runner(["kaggle", *map(str, arguments)], text=True, capture_output=True,
                                     timeout=min(COMMAND_TIMEOUT_SECONDS, self._remaining()))
            except subprocess.TimeoutExpired as exc:
                error = KaggleError("Kaggle command timed out; its remote result may be unknown", retryable=True)
                cause = exc
            except OSError as exc:
                error = KaggleError(f"Kaggle command could not run: {type(exc).__name__}",
                                    retryable=isinstance(exc, (ConnectionError, TimeoutError)))
                cause = exc
            else:
                output = (result.stdout or "") + (result.stderr or "")
                # These CLI wrappers print API failures but still exit zero.
                printed_error = "Dataset creation error:" in output or "Dataset version creation error:" in output
                if result.returncode == 0 and not printed_error:
                    return result.stdout.strip()
                error = KaggleError(output.strip())
            if not mutation and self._retryable(error) and attempt < 2:
                limited = "429" in str(error) or "too many requests" in str(error).lower()
                delay = 60 * (attempt + 1) if limited else 2 ** (attempt + 1)
                print(f"Kaggle temporary read error: retrying in {delay}s", flush=True)
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
            return json.loads(self._run("datasets", "status", self.ref(slug), "--format", "json"))
        except KaggleError as exc:
            if ("403" in str(exc) or "404" in str(exc)) and not self._exists(slug):
                return None
            raise

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
        uncertain_write = False
        for attempt in range(3):
            try:
                self._run(*arguments)
            except KaggleError as exc:
                if uncertain_write and self._publication_conflict(exc):
                    # The earlier request may have committed before its new
                    # version became visible. A conflict proves neither failure
                    # nor ownership: keep waiting for this operation's marker.
                    return self._confirm_publication(slug, publication, expected, submitted=True)
                if not self._retryable(exc):
                    raise
                uncertain_write = True
                version = self._confirm_publication(slug, publication, expected, submitted=False)
                if version is not None:
                    return version
                if attempt == 2:
                    raise KaggleError("Dataset publication was not accepted after three attempts") from exc
                self._sleep(60 * (attempt + 1) if "429" in str(exc) else 2 ** (attempt + 1))
                continue
            return self._confirm_publication(slug, publication, expected, submitted=True)
        raise AssertionError("unreachable")

    def _confirm_publication(self, slug: str, publication: dict, expected: int, *, submitted: bool) -> int | None:
        """Only a pinned version with this operation's marker proves acceptance."""
        unchanged = 0
        read_failures = 0
        while True:
            self._remaining()
            try:
                status = self.status(slug)
            except KaggleDeadlineError:
                raise
            except KaggleError as exc:
                if not self._retryable(exc) or read_failures >= 2:
                    raise KaggleError("Cannot determine Dataset publication result; keep working files") from exc
                read_failures += 1
                self._sleep(POLL_SECONDS)
                continue
            if status:
                state = status["status"].lower()
                version = int(status["current_version_number"] or 0)
                if state in {"error", "failed"}:
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
                if state in {"error", "failed"}:
                    raise KaggleError(f"Dataset processing failed: {self.ref(slug)} ({state})")
            print(f"Waiting for Dataset processing: {self.ref(slug)} version {expected}", flush=True)
            self._sleep(POLL_SECONDS)
