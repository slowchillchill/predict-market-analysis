"""Small versioned Kaggle Dataset adapter shared by the producer and reports."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

OWNER = "qiuqiuslowchillchill"
TITLE = "Polymarket Crypto Updown Markets Analysis"


class KaggleError(RuntimeError):
    pass


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class KaggleStore:
    def __init__(self, owner: str = OWNER, work_dir: Path | str = "/tmp/polymarket-kaggle", *, runner=None):
        self.owner = owner
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner or subprocess.run

    def ref(self, slug: str) -> str:
        return slug if "/" in slug else f"{self.owner}/{slug}"

    def _run(self, *arguments: str) -> str:
        for attempt in range(3):
            result = self.runner(["kaggle", *map(str, arguments)], text=True, capture_output=True)
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode == 0:
                return result.stdout.strip()
            if ("429" in output or "Too many requests" in output) and attempt < 2:
                delay = 60 * (attempt + 1)
                print(f"Kaggle rate limit: retrying in {delay}s", flush=True)
                time.sleep(delay)
                continue
            raise KaggleError(output.strip())
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
        if previous is None:
            self._run("datasets", "create", "-p", str(directory), "--public", "--keep-tabular", "--quiet")
            expected = 1
        else:
            expected = int(previous["current_version_number"]) + 1
            self._run("datasets", "version", "-p", str(directory), "-m", message,
                      "--keep-tabular", "--quiet")
        return self.wait_ready(slug, expected)

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
            time.sleep(15)
