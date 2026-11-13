"""Opt-in two-session Kaggle acceptance with synthetic data and private QA Datasets.

Run phase 1 and phase 2 in separate clean Kaggle Notebook sessions using the
same --dataset-prefix. Phase 1 accepts a raw snapshot, then deliberately stops
before publishing its monthly reference. Phase 2 uses only cloud Datasets to
recover that snapshot, preserve its version, and verify portable social text.
No production market requests or production Dataset access are permitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import polymarket_kaggle_pipeline as producer
import polymarket_kaggle_social as social
import polymarket_kaggle_store as cloud
from polymarket_kaggle_auth import kaggle_auth
from test_polymarket_kaggle_pipeline import PublicAPI, SERIES, TODAY, YESTERDAY


DAY = YESTERDAY
MONTH = str(DAY)[:7]
RAW_SLUG = f"pm-updown-raw-{DAY}"
MONTH_SLUG = f"pm-updown-analysis-{MONTH}"
EVIDENCE_SLUG = "pm-updown-acceptance"
IDENTITY = f"daily:{DAY}"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class InjectedMonthlyFailure(cloud.KaggleError):
    """Expected phase-1 interruption after the raw upload was confirmed."""


class NamespacedStore(cloud.KaggleStore):
    """Map logical producer names while rejecting commands outside private QA."""

    def __init__(self, prefix: str, work_dir: Path):
        require(re.fullmatch(r"qa-cloud-20261113-[a-z0-9][a-z0-9-]{0,12}", prefix),
                "Use a new qa-cloud-20261113-* Dataset namespace")
        self.prefix = prefix
        self.mutations = []
        self.fail_month_publish = False
        super().__init__(owner=cloud.OWNER, work_dir=work_dir, runner=self._guarded_runner,
                         deadline=time.monotonic() + 20 * 60)

    def _map(self, slug: str) -> str:
        if "/" in slug:
            owner, slug = slug.split("/", 1)
            require(owner == self.owner, "QA Dataset owner changed")
        if slug.startswith("pm-updown-"):
            slug = self.prefix + "-" + slug.removeprefix("pm-updown-")
        require(slug.startswith(self.prefix + "-"), "Dataset escaped the QA namespace")
        require(re.fullmatch(r"[a-z0-9-]+", slug), "Invalid QA Dataset slug")
        return slug

    def ref(self, slug: str) -> str:
        return f"{self.owner}/{self._map(slug)}"

    def list_slugs(self, prefix: str) -> list[str]:
        mapped = self._map(prefix)
        return ["pm-updown-" + slug.removeprefix(self.prefix + "-")
                for slug in super().list_slugs(mapped)]

    def _guarded_runner(self, command, **kwargs):
        command = list(command)
        require(command[0] == "kaggle" and command[1] == "datasets", "Unexpected QA command")
        operation = command[2]
        allowed_ref = f"{self.owner}/{self.prefix}-"
        if operation in {"create", "version"}:
            directory = Path(command[command.index("-p") + 1])
            metadata = json.loads((directory / "dataset-metadata.json").read_text())
            marker = json.loads((directory / cloud.PUBLICATION_FILE).read_text())
            require(metadata["id"].startswith(allowed_ref), "Production Dataset write refused")
            require(marker["dataset"] == metadata["id"], "QA marker does not match its Dataset")
            command = [part for part in command if part != "--public"]
            self.mutations.append({"operation": operation, "dataset": metadata["id"],
                                   "publication_id": marker["publication_id"]})
        elif operation == "list":
            require(command[command.index("--user") + 1] == self.owner, "QA listing owner changed")
            require(command[command.index("--search") + 1].startswith(self.prefix + "-"),
                    "QA listing escaped its namespace")
        elif operation in {"status", "download", "metadata"}:
            require(command[3].startswith(allowed_ref), "Production Dataset access refused")
        else:
            raise AssertionError(f"Unsupported QA Dataset command: {operation}")
        return subprocess.run(command, **kwargs)

    def publish(self, slug, directory, **kwargs):
        mapped = self._map(slug)
        if self.fail_month_publish and mapped == self._map(MONTH_SLUG):
            raise InjectedMonthlyFailure("Injected monthly interruption after accepted raw upload")
        return super().publish(slug, directory, **kwargs)


def download_json(store, slug: str, filename: str, version: int, destination: Path):
    store.download_file(slug, filename, version, destination)
    return json.loads(destination.read_text())


def raw_summary(path: Path) -> dict:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT * FROM trades ORDER BY page_number,row_number").fetchall()
        return {"schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                "trade_columns": [row[1] for row in db.execute("PRAGMA table_info(trades)")],
                "tables": [row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
                "trades": rows, "database_digest": producer.Pipeline._database_digest(path)}


def seed_history(pipeline):
    pipeline.state["history"] = {
        "start_date_utc": str(DAY), "target_end_date_utc": str(DAY), "next_date_utc": str(DAY),
        "series_first_end": {SERIES: next(producer.collector.expected_slots(SERIES, DAY)).isoformat()},
        "gaps": {}, "finished": False,
    }


def publish_evidence(store, root: Path, result: dict, phase: int):
    folder = root / "published-evidence"
    folder.mkdir()
    cloud.write_json(folder / f"phase{phase}.json", result)
    return store.publish(EVIDENCE_SLUG, folder, title="Private cloud recovery acceptance",
                         description="Synthetic two-session recovery and social text acceptance. No production data.",
                         message=f"Phase {phase}: synthetic acceptance evidence")


def phase_one(store, root: Path) -> dict:
    for slug in (RAW_SLUG, MONTH_SLUG, EVIDENCE_SLUG):
        require(store.version(slug) is None, "Phase 1 requires a new, empty QA namespace")
    client = PublicAPI([TODAY])
    pipeline = producer.Pipeline(store, root / "pipeline", today=lambda: TODAY, client=client)
    seed_history(pipeline)
    pipeline.save()
    seeded_version = store.version(MONTH_SLUG)
    require(seeded_version == 1, "Initial cloud progress must be version 1")
    store.fail_month_publish = True
    try:
        pipeline.process_day(DAY, historical=True)
    except InjectedMonthlyFailure:
        pass
    else:
        raise AssertionError("The injected monthly publication interruption did not occur")
    finally:
        store.fail_month_publish = False
    require(client.trades == [(str(DAY), "false", None)], "Synthetic first page was not collected exactly once")
    require(store.version(RAW_SLUG) == 1, "Raw publication must have exactly one accepted version")
    require(store.version(MONTH_SLUG) == seeded_version, "Injected failure unexpectedly advanced monthly progress")
    restored = root / "verified-raw"
    require(store.restore(RAW_SLUG, restored) == 1, "Could not verify the accepted raw version")
    marker = json.loads((restored / cloud.PUBLICATION_FILE).read_text())
    metadata = json.loads((restored / "collection_status.json").read_text())
    require(metadata["status"] == "complete", "Synthetic raw sample did not complete")
    require(marker["dataset"] == store.ref(RAW_SLUG), "Accepted raw marker belongs to another Dataset")
    progress = download_json(store, MONTH_SLUG, "progress.json", seeded_version, root / "seeded-progress.json")
    require(str(DAY) not in progress["raw_days"], "Cloud progress already references the interrupted raw date")
    require(progress["history"]["next_date_utc"] == str(DAY), "Cloud history cursor advanced after failure")
    summary = raw_summary(restored / "data.sqlite3")
    require(len(summary["trades"]) == 2, "Unexpected number of synthetic wallet rows")
    return {"status": "passed", "phase": 1, "date_utc": str(DAY),
            "raw": {"dataset": store.ref(RAW_SLUG), "version": 1, "publication": marker,
                    "collection_status": metadata, "summary": summary},
            "monthly_seed_version": seeded_version, "monthly_publication_failure_injected": True,
            "trade_requests": client.trades, "production_writes": 0,
            "mutations": list(store.mutations)}


def phase_two(store, root: Path) -> dict:
    baseline = download_json(store, EVIDENCE_SLUG, "phase1.json", 1, root / "phase1-cloud-evidence.json")
    require(baseline["status"] == "passed" and baseline["phase"] == 1, "Phase-1 evidence is not valid")
    require(baseline["raw"]["dataset"] == store.ref(RAW_SLUG), "Phase-1 evidence has another namespace")
    require(store.version(RAW_SLUG) == 1, "Raw version changed before clean-session recovery")
    require(store.version(MONTH_SLUG) == baseline["monthly_seed_version"],
            "Monthly progress changed before clean-session recovery")
    client = PublicAPI([TODAY])
    pipeline = producer.Pipeline(store, root / "pipeline", today=lambda: TODAY, client=client)
    require(str(DAY) not in pipeline.state["raw_days"], "Recovery did not start from stale cloud progress")
    require(pipeline.state["history"]["next_date_utc"] == str(DAY), "Wrong initial recovery cursor")
    pipeline.process_day(DAY, historical=True)
    require(client.trades == [], "Recovery requested already committed wallet trades")
    require(store.version(RAW_SLUG) == 1, "Recovery republished an unchanged raw snapshot")
    require(not any(item["dataset"] == store.ref(RAW_SLUG) for item in store.mutations),
            "Recovery attempted another raw upload")
    raw = root / "verified-raw"
    require(store.restore(RAW_SLUG, raw) == 1, "Could not reread the fixed raw version")
    marker = json.loads((raw / cloud.PUBLICATION_FILE).read_text())
    require(marker == baseline["raw"]["publication"], "Raw publication identity changed across sessions")
    # JSON normalizes SQLite tuple rows to lists in the cross-session evidence.
    summary = json.loads(json.dumps(raw_summary(raw / "data.sqlite3")))
    require(summary == baseline["raw"]["summary"], "Raw rows, group IDs or persisted SQL values changed")
    monthly_version = store.version(MONTH_SLUG)
    require(monthly_version > baseline["monthly_seed_version"], "Monthly recovery was not published")
    progress = download_json(store, MONTH_SLUG, "progress.json", monthly_version, root / "recovered-progress.json")
    require(progress["raw_days"][str(DAY)]["version"] == 1, "Monthly progress references a different raw version")
    require(progress["raw_days"][str(DAY)]["status"] == "complete", "Complete raw did not produce a formal result")
    require(progress["history"]["next_date_utc"] == str(DAY + timedelta(days=1)), "History cursor did not recover")
    # Download into a second empty folder. This also verifies that social.zip
    # survives Kaggle's flat file upload/download and restores portable paths.
    clean_client = PublicAPI([TODAY])
    reloaded = producer.Pipeline(store, root / "cloud-only-reload", today=lambda: TODAY, client=clean_client)
    folder = reloaded.month(MONTH)
    require((folder / "social.zip").is_file(), "Cloud monthly snapshot has no social.zip")
    state = json.loads((folder / "social/state.json").read_text())
    require(state["reports"][IDENTITY]["status"] == "generated", "Cloud text did not reach generated state")
    package = folder / "social" / "daily" / str(DAY)
    require(list(package.glob("*_x_01.txt")) and list(package.glob("*_linkedin.txt")), "Cloud text files are missing")
    before = {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (folder / "social").rglob("*") if path.is_file()}
    mutations_before = len(store.mutations)
    with patch.object(social, "render_bundle", side_effect=AssertionError("Existing cloud text was regenerated")):
        reloaded.save()
    after = {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (folder / "social").rglob("*") if path.is_file()}
    require(before == after, "Restored text or publication state changed on an idle save")
    require(len(store.mutations) == mutations_before, "Idle restored state caused another publication")
    require(clean_client.trades == [], "Cloud text restoration called the trade API")
    return {"status": "passed", "phase": 2, "date_utc": str(DAY),
            "raw": {"dataset": store.ref(RAW_SLUG), "version": 1, "publication": marker},
            "monthly": {"dataset": store.ref(MONTH_SLUG), "version": monthly_version},
            "cloud_only_recovery": True, "raw_version_reused": True,
            "trade_requests": client.trades, "social_archive_restored": True,
            "social_state_idempotent": True, "social_files_sha256": before,
            "production_writes": 0, "mutations": list(store.mutations)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, choices=(1, 2), required=True)
    parser.add_argument("--dataset-prefix", default="qa-cloud-20261113-autonomy")
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    cloud_root = Path("/kaggle/working")
    require(cloud_root.is_dir(), "This acceptance runs only in a Kaggle Notebook")
    root = args.work_dir.resolve()
    require(root.is_relative_to(cloud_root.resolve()), "QA outputs must remain in /kaggle/working")
    root.mkdir(parents=True, exist_ok=False)
    with kaggle_auth("platform") as authentication, \
            patch.dict(producer.collector.TARGET_SERIES, {SERIES: 1}, clear=True):
        store = NamespacedStore(args.dataset_prefix, root / "downloads")
        result = (phase_one if args.phase == 1 else phase_two)(store, root)
        result.update(authentication=authentication, dataset_prefix=args.dataset_prefix,
                      source_commit=os.environ.get("POLYMARKET_SOURCE_COMMIT"),
                      completed_at_utc=datetime.now(timezone.utc).isoformat(), private_requested=True)
        evidence_version = publish_evidence(store, root, result, args.phase)
        result["evidence"] = {"dataset": store.ref(EVIDENCE_SLUG), "version": evidence_version}
        cloud.write_json(root / f"acceptance_phase{args.phase}.json", result)
        print(json.dumps({"status": result["status"], "phase": args.phase,
                          "raw_version": result["raw"]["version"], "evidence": result["evidence"],
                          "production_writes": 0}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
