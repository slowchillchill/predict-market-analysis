#!/usr/bin/env python3
"""Read completed Kaggle statistics and generate X and LinkedIn text without raw data."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
TOKYO = ZoneInfo("Asia/Tokyo")
DEFAULT_OWNER = "qiuqiuslowchillchill"
KINDS = ("daily", "weekly", "monthly")


class ReportNotReady(ValueError):
    """A completed cloud result for this fixed period is not available yet."""


class ExistingArtifacts(ValueError):
    """Existing local assets are preserved, including assets awaiting publication."""


@dataclass(frozen=True)
class ReportPeriod:
    kind: str
    start: date
    end: date

    @property
    def target(self) -> str:
        return self.start.strftime("%Y-%m") if self.kind == "monthly" else str(self.start)

    @property
    def identity(self) -> str:
        return self.kind + ":" + self.target

    @property
    def month(self) -> str:
        return (self.end - timedelta(days=1)).strftime("%Y-%m")

    @property
    def stem(self) -> str:
        if self.kind == "daily":
            return "updown_" + self.target
        if self.kind == "weekly":
            return f"updown_weekly_{self.start}_{self.end - timedelta(days=1)}"
        return "updown_monthly_" + self.target

    @classmethod
    def from_identity(cls, identity: str) -> "ReportPeriod":
        kind, target = identity.split(":", 1)
        if kind == "monthly":
            start = date.fromisoformat(target + "-01")
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        elif kind in ("daily", "weekly"):
            start = date.fromisoformat(target)
            if kind == "weekly" and start.weekday() != 0:
                raise ValueError("Weekly period must start on a UTC Monday")
            end = start + timedelta(days=1 if kind == "daily" else 7)
        else:
            raise ValueError("Unknown report kind: " + kind)
        return cls(kind, start, end)


def due_periods(rollout_date: date, now: datetime, kind: str = "all") -> list[ReportPeriod]:
    """Enumerate original schedule occurrences, retaining each occurrence's UTC target."""
    local = now.astimezone(TOKYO)
    periods = []
    day = rollout_date
    while day <= local.date():
        if kind in ("all", "daily") and local >= datetime.combine(day, time(9, 30), TOKYO):
            # The 09:30 JST check belongs to the previous UTC calendar day.
            target = day - timedelta(days=1)
            periods.append(ReportPeriod("daily", target, day))
        if (kind in ("all", "weekly") and day.weekday() == 0
                and local >= datetime.combine(day, time(11, 30), TOKYO)):
            periods.append(ReportPeriod("weekly", day - timedelta(days=7), day))
        if (kind in ("all", "monthly") and day.day == 1
                and local >= datetime.combine(day, time(12, 30), TOKYO)):
            prior = (day - timedelta(days=1)).replace(day=1)
            periods.append(ReportPeriod("monthly", prior, day))
        day += timedelta(days=1)
    return periods


def validate_bundle(bundle: dict, expected: ReportPeriod | None = None) -> ReportPeriod:
    if bundle.get("schema_version") != 1 or bundle.get("status") != "complete":
        raise ReportNotReady("Cloud result is not a completed schema version 1 report")
    kind = bundle.get("kind")
    start = date.fromisoformat(bundle["start_date_utc"])
    end = date.fromisoformat(bundle["end_date_utc_exclusive"])
    identity = kind + ":" + (start.strftime("%Y-%m") if kind == "monthly" else str(start))
    period = ReportPeriod.from_identity(identity)
    if period.start != start or period.end != end or (expected is not None and period != expected):
        raise ValueError("Cloud result period differs from the requested period")
    # The weekly distribution's display target is a range; the index identity uses Monday.
    valid_targets = {period.target}
    if kind == "weekly":
        valid_targets.add(f"{start}_{end - timedelta(days=1)}")
    if bundle.get("target_utc") not in valid_targets:
        raise ValueError("Cloud result target differs from its period")
    if not bundle.get("source_versions"):
        raise ValueError("Cloud result has no raw dataset version references")
    payload, distribution = bundle["payload"], bundle["distribution"]
    if payload.get("date_basis") != "market_end_utc":
        raise ValueError("Unsupported report date basis")
    if kind == "daily":
        if payload.get("date_utc") != str(start) or distribution.get("date_utc") != str(start):
            raise ValueError("Daily overview and distribution dates differ")
    else:
        for data in (payload["current"], distribution):
            if (data.get("start_date_utc") != str(start)
                    or data.get("end_date_utc_exclusive") != str(end)):
                raise ValueError("Overview and distribution periods differ")
        if distribution.get("kind") != kind:
            raise ValueError("Distribution report kind differs")
    return period


def _existing_artifacts(output_dir: Path, period: ReportPeriod) -> list[Path]:
    paths = list(output_dir.glob(period.stem + ".*"))
    paths += list(output_dir.glob(period.stem + "_*"))
    return sorted(set(paths))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_bundle(bundle: dict, output_dir: Path, *, source_reference: dict | None = None) -> dict:
    """Write a text-only package from one completed cloud result; never open SQLite."""
    from polymarket_social_text import render_text

    period = validate_bundle(bundle)
    output_dir = Path(output_dir).resolve()
    if _existing_artifacts(output_dir, period):
        raise ExistingArtifacts("Preserved existing local assets for " + period.identity)
    reference = source_reference or bundle.get("source_reference") or {}
    rendered = render_text(bundle, reference)
    source_name = period.stem + ".json"
    manifest_name = period.stem + "_publish.json"
    display_target = (f"{period.start}_{period.end - timedelta(days=1)}"
                      if period.kind == "weekly" else period.target)
    manifest = {
        "schema_version": 2, "format": "text", "kind": period.kind,
        "target_utc": display_target, "start_date_utc": str(period.start),
        "end_date_utc_exclusive": str(period.end), "date_basis": "market_end_utc",
        "source_path": str(output_dir / source_name), "source_reference": reference,
        "source_versions": bundle["source_versions"], "platforms": {},
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + period.stem + ".", dir=output_dir.parent) as temporary:
        stage = Path(temporary)
        (stage / source_name).write_text(
            json.dumps({**bundle, "source_reference": reference}, ensure_ascii=False,
                       indent=2, allow_nan=False) + "\n", encoding="utf-8")
        manifest["source_sha256"] = _sha256(stage / source_name)
        for platform in ("x", "linkedin"):
            content = rendered[platform]
            posts = content["posts"] if platform == "x" else [content["body"]]
            names = ([f"{period.stem}_x_{number:02}.txt" for number in range(1, len(posts) + 1)]
                     if platform == "x" else [period.stem + "_linkedin.txt"])
            for name, body in zip(names, posts):
                (stage / name).write_text(body, encoding="utf-8")
            item = {"language": "en", "title": "", "body": posts[0],
                    "text_paths": [str(output_dir / name) for name in names],
                    "text_sha256s": [_sha256(stage / name) for name in names]}
            if platform == "x":
                item["posts"] = posts
            identity = {"title": "", "posts": posts} if platform == "x" else {"title": "", "body": posts[0]}
            item["content_sha256"] = hashlib.sha256(json.dumps(
                identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            manifest["platforms"][platform] = item
        manifest_path = stage / manifest_name
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_dir.mkdir(parents=True, exist_ok=True)
        # Publish the manifest last so consumers only see complete text packages.
        for path in sorted(stage.iterdir(), key=lambda path: (path == manifest_path, path.name)):
            path.replace(output_dir / path.name)
    return {"source": output_dir / source_name,
            "x": [Path(path) for path in manifest["platforms"]["x"]["text_paths"]],
            "linkedin": Path(manifest["platforms"]["linkedin"]["text_paths"][0]),
            "manifest": output_dir / manifest_name}


def fetch_result(store, period: ReportPeriod, download_dir: Path) -> tuple[dict, dict]:
    """Pin the index and report to one processed Kaggle dataset version."""
    slug = "pm-updown-analysis-" + period.month
    version = store.version(slug)
    if version is None:
        raise ReportNotReady("Analysis dataset does not exist yet: " + slug)
    index_path = store.download_file(slug, "index.json", version, download_dir / "index.json")
    index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    entry = index.get("results", {}).get(period.identity)
    if not entry or entry.get("status") != "complete":
        raise ReportNotReady("Result not ready: " + period.identity)
    filename = entry["filename"]
    if Path(filename).is_absolute() or ".." in Path(filename).parts:
        raise ValueError("Invalid result filename in the cloud index")
    result_path = store.download_file(slug, filename, version, download_dir / "result.json")
    bundle = json.loads(Path(result_path).read_text(encoding="utf-8"))
    validate_bundle(bundle, period)
    return bundle, {"dataset": store.owner + "/" + slug, "version": version, "filename": filename}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_pending(store, state_path: Path, output_dir: Path, *, kind: str = "all",
                now: datetime | None = None, rollout_date: date | None = None) -> dict:
    """Queue due periods before network work so a later retry keeps the original target."""
    now = now or datetime.now(timezone.utc)
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else
                 {"schema_version": 1, "rollout_date": str(rollout_date or now.astimezone(TOKYO).date()),
                  "reports": {}})
        for period in due_periods(date.fromisoformat(state["rollout_date"]), now, kind):
            state["reports"].setdefault(period.identity, {"status": "pending"})
        for item in state["reports"].values():
            if item.get("status") in {"generated", "preserved"}:
                item.pop("last_error", None)
        _save_state(state_path, state)
        for identity, item in state["reports"].items():
            period = ReportPeriod.from_identity(identity)
            if item["status"] != "pending" or kind not in ("all", period.kind):
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="kaggle-report-") as temporary:
                    bundle, reference = fetch_result(store, period, Path(temporary))
                    paths = render_bundle(bundle, output_dir, source_reference=reference)
                item.update(status="generated", format="text", source=reference,
                            manifest=str(paths["manifest"]), generated_at=now.isoformat())
                item.pop("last_error", None)
                print("Generated " + identity, flush=True)
            except ExistingArtifacts as exc:
                item.update(status="preserved", reason=str(exc))
                item.pop("last_error", None)
                print(str(exc), flush=True)
            except Exception as exc:
                # A later 30-minute invocation retries only these fixed pending periods.
                item["last_error"] = str(exc)
                print(f"Pending {identity}: {exc}", file=sys.stderr, flush=True)
            _save_state(state_path, state)
        return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("all", *KINDS), default="all")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--state-file", type=Path, default=ROOT / "outputs/kaggle_reports/state.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/social")
    parser.add_argument("--rollout-date", type=date.fromisoformat,
                        help="Initial JST deployment date; existing state retains its original date")
    parser.add_argument("--result-json", type=Path, help="Render a downloaded completed result without network access")
    parser.add_argument("--source-dataset", help="Analysis dataset owner/slug for an offline result")
    parser.add_argument("--source-version", type=int, help="Fixed analysis Dataset version for an offline result")
    args = parser.parse_args(argv)
    if bool(args.source_dataset) != (args.source_version is not None):
        parser.error("--source-dataset and --source-version must be supplied together")
    if args.source_dataset and not args.result_json:
        parser.error("--source-dataset and --source-version apply only to --result-json")
    if args.source_version is not None and args.source_version < 1:
        parser.error("--source-version must be positive")
    if args.result_json:
        try:
            bundle = json.loads(args.result_json.read_text(encoding="utf-8"))
            reference = ({"dataset": args.source_dataset, "version": args.source_version,
                          "filename": args.result_json.name} if args.source_dataset else None)
            paths = render_bundle(bundle, args.output_dir, source_reference=reference)
            print("Generated " + str(paths["manifest"]))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    from polymarket_kaggle_store import KaggleStore
    store = KaggleStore(args.owner, args.state_file.parent / "downloads")
    run_pending(store, args.state_file, args.output_dir, kind=args.kind, rollout_date=args.rollout_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
