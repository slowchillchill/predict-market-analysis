"""Convert published cloud day copies without collecting Polymarket records."""

from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import sqlite3

import polymarket_kaggle_analysis as analysis
import polymarket_trade_groups as groups
import polymarket_updown_daily as collector
from polymarket_kaggle_store import write_json


def raw_summary(path: Path) -> dict:
    """Read only aggregate evidence; retain neither hashes nor wallet rows."""
    amounts = {}
    counts = {}
    total_rows = total_groups = 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        column = groups.group_column(db)
        for (slug,) in db.execute("SELECT slug FROM markets ORDER BY slug"):
            seen = set()
            amount = rows = 0
            for key, value in db.execute(
                    f"SELECT {column},amount_micro_usdc FROM trades WHERE market_slug=? "
                    "ORDER BY page_number,row_number", (slug,)):
                rows += 1
                if key not in seen:
                    seen.add(key)
                    amount += value
            amounts[slug] = amount
            counts[slug] = (rows, len(seen))
            total_rows += rows
            total_groups += len(seen)
        api_amounts = None
        if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='market_volume'").fetchone():
            api_amounts = {slug: amount for slug, amount in db.execute(
                "SELECT market_slug,amount_micro_usdc FROM market_volume WHERE completed_at IS NOT NULL")}
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
    return {"rows": total_rows, "groups": total_groups, "market_counts": counts,
            "amounts": amounts, "api_amounts": api_amounts, "tables": tables}


def business_values(features: dict, bundle: dict) -> dict:
    """Compare business values, allowing source-format metadata to change."""
    volume = features["market_volume"]
    return {
        "wallets": features["wallets"], "daily_hits": features["daily_hits"],
        "series": features["series"],
        "volume": {key: volume.get(key) for key in
                   ("volume_micro_usdc", "daily_volume_micro_usdc", "asset_volume_micro_usdc")},
        "current": bundle["payload"]["current"], "previous": bundle["payload"]["previous"],
        "display": bundle["payload"]["display"],
        "buy_event_summary": bundle["payload"]["buy_event_summary"],
        "distribution": {key: bundle["distribution"][key] for key in
                         ("populations", "all_wallet_count", "suspected_bot_wallets", "total_markets", "validation")},
    }


def migrate_day(pipeline, stamp: str) -> dict:
    """Publish a v2 day then its analysis; safely resume a publication gap."""
    from polymarket_kaggle_pipeline import DESCRIPTION, RAW_SCHEMA_VERSION, VOLUME_METHOD, month_of

    day = date.fromisoformat(stamp)
    reference = pipeline.state["raw_days"][stamp]
    slug = reference["slug"]
    folder = pipeline.work_dir / "migration" / stamp
    if folder.exists():
        shutil.rmtree(folder)
    version = pipeline.store.restore(slug, folder)
    if version is None or version < reference["version"]:
        raise ValueError(f"Cannot migrate {stamp}: missing or older raw snapshot")
    status_path = folder / "collection_status.json"
    status = json.loads(status_path.read_text())
    if status.get("status") != "complete":
        raise ValueError(f"Cannot migrate {stamp}: latest raw snapshot is incomplete")
    path = folder / "data.sqlite3"
    metadata = pipeline.history_metadata()
    previous = pipeline._previous_day(day)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        already_v2 = groups.group_column(db) == "group_id"
        original_bounds = collector.series_first_end(db)
    if not already_v2:
        before_bytes = path.stat().st_size
        before_raw = raw_summary(path)
        before_features = analysis.extract_day_features(path, day)
        before_bundle = analysis.export_day(path, day, pipeline.state["first_seen"],
            history_metadata=metadata, previous=previous)
        # A format migration preserves this snapshot's catalog. Later global
        # discovery must not silently change its metadata during conversion.
        with closing(collector.connect_database(path, series_first_end=original_bounds)) as db:
            coverage = collector.coverage(db, stamp)
            groups.finalize_database(db)
        after_raw = raw_summary(path)
        features = analysis.extract_day_features(path, day)
        bundle = analysis.export_day(path, day, pipeline.state["first_seen"],
            history_metadata=metadata, previous=previous)
        for key in ("rows", "groups", "market_counts", "amounts"):
            if before_raw[key] != after_raw[key]:
                raise ValueError(f"Migration {stamp} changed raw {key}; nothing published")
        if business_values(before_features, before_bundle) != business_values(features, bundle):
            raise ValueError(f"Migration {stamp} changed report business values; nothing published")
        api = before_raw["api_amounts"]
        differences = sum(api.get(market) != amount for market, amount in after_raw["amounts"].items()) if api is not None else None
        if differences:
            raise ValueError(f"Migration {stamp}: {differences} markets differ from old API amounts")
        summary = {
            "date_utc": stamp, "status": "passed", "source_raw_version": version,
            "raw_schema_version": RAW_SCHEMA_VERSION,
            "wallet_rows": after_raw["rows"], "transaction_groups": after_raw["groups"],
            "markets": len(after_raw["amounts"]), "api_amount_mismatched_markets": differences,
            "amount_micro_usdc": sum(after_raw["amounts"].values()),
            "before_bytes": before_bytes, "after_bytes": path.stat().st_size,
            "saved_bytes": before_bytes - path.stat().st_size,
            "tables": after_raw["tables"], "business_values_unchanged": True,
        }
        write_json(folder / "migration_summary.json", summary)
        write_json(status_path, {
            "schema_version": RAW_SCHEMA_VERSION, "date_utc": stamp, "status": "complete",
            "coverage": coverage, "error": None, "date_basis": "market_end_utc",
            "group_key": ["market_slug", "group_id"], "volume_method": VOLUME_METHOD,
        })
        (folder / "README.md").write_text(DESCRIPTION)
        version = pipeline.store.publish(slug, folder, title=f"Polymarket Updown Raw {stamp}",
            description=DESCRIPTION, message=f"{stamp}: compact group IDs; unchanged statistics")
        print(f"Migrated {stamp}: {summary['before_bytes']} -> {summary['after_bytes']} bytes; "
              f"{summary['wallet_rows']} wallet rows, {summary['transaction_groups']} groups", flush=True)
    else:
        # A preceding run can publish raw v2 and end before the monthly update.
        # Reuse that ready raw version, rather than writing the same raw day again.
        coverage = status["coverage"]
        features = analysis.extract_day_features(path, day)
        bundle = analysis.export_day(path, day, pipeline.state["first_seen"],
            history_metadata=metadata, previous=previous)
        summary_path = folder / "migration_summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {
            "date_utc": stamp, "status": "already_v2", "raw_schema_version": RAW_SCHEMA_VERSION}
    bundle["source_versions"] = {stamp: {"dataset": pipeline.store.ref(slug), "version": version}}
    bundle["raw_schema_version"] = RAW_SCHEMA_VERSION
    bundle["volume_method"] = VOLUME_METHOD
    analysis.save_day(pipeline.month(month_of(day)) / "features.sqlite3", features, bundle,
                      first_seen=pipeline.state["first_seen"])
    pipeline.put_bundle(bundle)
    pipeline.state["raw_days"][stamp] = {
        "slug": slug, "version": version, "status": "complete", "coverage": coverage,
        "raw_schema_version": RAW_SCHEMA_VERSION, "volume_method": VOLUME_METHOD,
    }
    pipeline.refresh_daily_comparison(day + timedelta(days=1))
    pipeline.refresh_periods(day)
    pipeline.save()
    shutil.rmtree(folder)
    return summary


def migrate_existing(pipeline) -> list[dict]:
    """Upgrade completed cloud days once; unfinished days upgrade on resume."""
    results = []
    for stamp, reference in sorted(pipeline.state["raw_days"].items()):
        if reference.get("status") == "complete" and reference.get("raw_schema_version", 1) < 2:
            results.append(migrate_day(pipeline, stamp))
    return results
