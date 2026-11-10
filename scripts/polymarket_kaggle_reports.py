#!/usr/bin/env python3
"""Read completed Kaggle statistics and render local publication assets without raw data."""

from __future__ import annotations

import argparse
import copy
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


def _existing_artifacts(output_dir: Path, period: ReportPeriod, distribution: dict) -> list[Path]:
    from polymarket_updown_price_distribution import statistics_stem
    paths = list(output_dir.glob(period.stem + ".*"))
    paths += list(output_dir.glob(period.stem + "_*"))
    paths += list(output_dir.glob(statistics_stem(distribution) + ".*"))
    return sorted(set(paths))


def _write_daily(output_dir: Path, period: ReportPeriod, payload: dict, distribution: dict) -> dict:
    import polymarket_updown_poster as daily
    from polymarket_updown_bot_price_poster import write_outputs
    from polymarket_updown_buy_metrics import BASIS, event_summary
    from polymarket_updown_captions import write_sidecars
    from polymarket_updown_distribution_assets import DAILY_CHART_LAYOUT_VERSION, validate_distribution

    validate_distribution(distribution)
    data = daily.PosterData(period.start, payload["current"], payload.get("previous"))
    values = daily.display_values(data, distribution)
    tweet = daily.render_tweet(data, distribution)
    if not tweet.isascii() or len(tweet) > 280:
        raise ValueError("Daily tweet exceeds the existing X text budget")
    image, _ = daily.render_poster(values)
    payload.update(display=values, copy_version=values["copy_version"],
                   buy_event_summary=event_summary(distribution), bot_panel_basis=BASIS)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + period.stem + ".", dir=output_dir.parent) as temporary:
        stage = Path(temporary)
        paths = write_outputs(distribution, stage)
        payload["trade_distribution"] = {
            key: distribution[key] for key in ("date_utc", "market_scope", "series_count", "count_unit",
                                               "price_basis", "notional_basis")
        }
        payload["trade_distribution"].update(
            json_file=paths["json"].name,
            json_sha256=hashlib.sha256(paths["json"].read_bytes()).hexdigest(),
            image_file=paths["png"].name,
            image_sha256=hashlib.sha256(paths["png"].read_bytes()).hexdigest(),
            layout_version=DAILY_CHART_LAYOUT_VERSION,
        )
        source = stage / (period.stem + ".json")
        source.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        image.save(stage / (period.stem + ".png"), format="PNG")
        (stage / (period.stem + "_tweet.md")).write_text(tweet, encoding="utf-8")
        manifest = write_sidecars(source)
        for key in ("source_path", "image_path", "english_path"):
            manifest[key] = str(output_dir / Path(manifest[key]).name)
        for item in manifest["platforms"].values():
            item["image_paths"] = [str(output_dir / Path(path).name) for path in item["image_paths"]]
        manifest_path = stage / (period.stem + "_publish.json")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(stage.iterdir(), key=lambda path: (path == manifest_path, path.name)):
            path.replace(output_dir / path.name)
    return {"source": output_dir / (period.stem + ".json"),
            "image": output_dir / (period.stem + ".png"),
            "tweet": output_dir / (period.stem + "_tweet.md"),
            "manifest": output_dir / (period.stem + "_publish.json")}


def render_bundle(bundle: dict, output_dir: Path, *, source_reference: dict | None = None) -> dict:
    """Render the existing assets from cloud statistics; never open any raw SQLite database."""
    period = validate_bundle(bundle)
    output_dir = Path(output_dir).resolve()
    distribution = bundle["distribution"]
    existing = _existing_artifacts(output_dir, period, distribution)
    if existing:
        raise ExistingArtifacts("Preserved existing local assets for " + period.identity)
    payload = copy.deepcopy(bundle["payload"])
    payload["kaggle_source"] = {"source_versions": bundle["source_versions"], **(source_reference or {})}
    if period.kind == "daily":
        return _write_daily(output_dir, period, payload, distribution)

    from polymarket_updown_captions import write_sidecars
    from polymarket_updown_period_package import write_package
    current, previous = payload["current"], payload.get("previous")
    if period.kind == "weekly":
        import polymarket_updown_weekly_poster as poster
        payload["display"] = poster.display_values(current, previous, distribution)
        image, _ = poster.render(current, previous, distribution)
        tweet = poster.render_tweet(current, previous, distribution)
    else:
        import polymarket_updown_monthly_poster as poster
        scope = payload.get("scope_changes", [])
        payload["display"] = poster.display_values(current, previous, distribution)
        image, _ = poster.render(current, previous, scope, distribution)
        tweet = poster.render_tweet(current, previous, scope, distribution)
    return write_package(output_dir, period.stem, payload, image, tweet, distribution,
                         sidecars_writer=write_sidecars)


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
        _save_state(state_path, state)
        for identity, item in state["reports"].items():
            period = ReportPeriod.from_identity(identity)
            if item["status"] != "pending" or kind not in ("all", period.kind):
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="kaggle-report-") as temporary:
                    bundle, reference = fetch_result(store, period, Path(temporary))
                    paths = render_bundle(bundle, output_dir, source_reference=reference)
                item.update(status="generated", source=reference,
                            manifest=str(paths["manifest"]), generated_at=now.isoformat())
                item.pop("last_error", None)
                print("Generated " + identity, flush=True)
            except ExistingArtifacts as exc:
                item.update(status="preserved", reason=str(exc))
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
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/posters")
    parser.add_argument("--rollout-date", type=date.fromisoformat,
                        help="Initial JST deployment date; existing state retains its original date")
    parser.add_argument("--result-json", type=Path, help="Render a downloaded completed result without network access")
    args = parser.parse_args(argv)
    if args.result_json:
        try:
            bundle = json.loads(args.result_json.read_text(encoding="utf-8"))
            paths = render_bundle(bundle, args.output_dir)
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
