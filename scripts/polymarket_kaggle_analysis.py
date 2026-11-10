"""Kaggle 日库统计与可合并的月度特征；复用现有统计和成交重建口径。"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Iterable, Mapping

import polymarket_updown_bot_registry as registry
import polymarket_updown_daily as collector
import polymarket_updown_period_distribution as period_distribution
import polymarket_updown_poster as poster
import polymarket_updown_price_distribution as distribution
from polymarket_market_volume_query import load_market_volume
from polymarket_updown_buy_metrics import BASIS, event_summary
from polymarket_updown_rules import MAX_INTERVAL_SECONDS, MIN_SPAN_MINUTES


IncompleteDay = poster.IncompleteDay

_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date_utc TEXT PRIMARY KEY,
    features_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_days (
    date_utc TEXT NOT NULL,
    wallet TEXT NOT NULL,
    trade_count INTEGER NOT NULL,
    first_timestamp INTEGER NOT NULL,
    last_timestamp INTEGER NOT NULL,
    amount_micro_usdc TEXT NOT NULL,
    PRIMARY KEY (date_utc, wallet)
);
CREATE TABLE IF NOT EXISTS daily_hits (
    date_utc TEXT NOT NULL,
    wallet TEXT NOT NULL,
    PRIMARY KEY (date_utc, wallet)
);
CREATE INDEX IF NOT EXISTS wallet_days_by_wallet ON wallet_days(wallet, date_utc);
"""


def _read_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _paths(store_paths: Path | Iterable[Path]) -> list[Path]:
    if isinstance(store_paths, (str, Path)):
        return [Path(store_paths)]
    return [Path(path) for path in store_paths]


def _require_complete(db: sqlite3.Connection, day: date) -> tuple:
    snapshot = distribution.source._snapshot(db, day)
    distribution.source._complete_markets(snapshot, day)
    status = collector.coverage(db, str(day))
    if not status.get("is_complete"):
        raise IncompleteDay(f"{day} 的市场覆盖尚未完整。")
    return snapshot


def extract_day_features(db_path: Path, day: date) -> dict:
    """从完成日库提取钱包特征；不读取任何本地历史库或标签缓存。"""
    with closing(_read_database(db_path)) as db:
        snapshot = _require_complete(db, day)
        wallets = [dict(row) for row in db.execute("""
            SELECT t.wallet, COUNT(*) AS trade_count,
                   MIN(t.timestamp) AS first_timestamp, MAX(t.timestamp) AS last_timestamp,
                   SUM(CASE WHEN t.side='BUY' THEN t.amount_micro_usdc ELSE 0 END) AS amount_micro_usdc
            FROM markets m
            JOIN updown_target_series s ON s.series_slug=m.series_slug
            CROSS JOIN trades t ON t.market_slug=m.slug
            WHERE date(m.end_time)=? GROUP BY t.wallet ORDER BY t.wallet
        """, (str(day),))]
        hits = [row["wallet"] for row in wallets
                if row["trade_count"] > 1
                and MIN_SPAN_MINUTES * 60 <= row["last_timestamp"] - row["first_timestamp"]
                <= MAX_INTERVAL_SECONDS * (row["trade_count"] - 1)]
        return {
            "date_utc": str(day),
            "coverage": collector.coverage(db, str(day)),
            "market_volume": load_market_volume(db, day, day + timedelta(days=1)),
            "series": [dict(row) for row in db.execute(
                "SELECT series_slug,expected_markets,first_end_time FROM updown_target_series ORDER BY series_slug")],
            "wallets": wallets,
            "daily_hits": hits,
            # This is the existing report audit fingerprint, not a download/hash protocol.
            "source_fingerprint_sha256": period_distribution.snapshot_digest(snapshot),
        }


def update_first_seen(existing: Mapping[str, str], features: dict, *,
                      history_metadata: dict | None = None) -> tuple[registry.FirstSeen, dict[str, str]]:
    """取历史有效命中的 MIN(date)，同时返回新增或提前的标签。"""
    metadata = copy.deepcopy(history_metadata if history_metadata is not None
                             else getattr(existing, "metadata", {}))
    observed = set(metadata.get("observed_dates", []))
    observed.add(features["date_utc"])
    metadata.update(observed_dates=sorted(observed), observed_start_date_utc=min(observed),
                    observed_end_date_utc=max(observed))
    metadata.setdefault("history_pending", True)
    labels = registry.FirstSeen(existing, metadata=metadata)
    changed = {}
    stamp = features["date_utc"]
    for wallet in features["daily_hits"]:
        if wallet not in labels or stamp < labels[wallet]:
            labels[wallet] = stamp
            changed[wallet] = stamp
    return labels, changed


def _labels(first_seen: Mapping[str, str], history_metadata: dict | None) -> registry.FirstSeen:
    return registry.FirstSeen(first_seen, metadata=copy.deepcopy(
        history_metadata if history_metadata is not None else getattr(first_seen, "metadata", {})))


def _bundle(kind: str, start: date, end: date, payload: dict, data: dict, sources: dict) -> dict:
    target = start.strftime("%Y-%m") if kind == "monthly" else str(start)
    return {"schema_version": 1, "kind": kind, "target_utc": target,
            "start_date_utc": str(start), "end_date_utc_exclusive": str(end),
            "status": "complete", "source_versions": sources,
            "payload": payload, "distribution": data}


def export_day(db_path: Path, day: date, first_seen: Mapping[str, str], *,
               history_metadata: dict | None = None, source_reference=None,
               previous: dict | None = None) -> dict:
    """在 Kaggle 执行现有 SQL/BUY 重建，导出本地渲染需要的 JSON。"""
    labels = _labels(first_seen, history_metadata)
    with closing(_read_database(db_path)) as db:
        _require_complete(db, day)
        data = poster.load_data(db, day, bot_first_seen=labels)
        data = poster.PosterData(day, data.current, previous if previous is not None else data.previous)
        prices = distribution.load_data(db, day, bot_first_seen=labels)
    values = poster.display_values(data, prices)
    payload = {
        "date_utc": str(day), "date_basis": "market_end_utc",
        "previous_date_utc": str(day - timedelta(days=1)),
        "volume_basis": "taker_only", "bot_volume_basis": "wallet_buys_only",
        "current": data.current, "previous": data.previous,
        "bot_rule": copy.deepcopy(registry.RULE), "bot_history": labels.metadata,
        "display": values, "copy_version": values["copy_version"],
        "buy_event_summary": event_summary(prices), "bot_panel_basis": BASIS,
    }
    return _bundle("daily", day, day + timedelta(days=1), payload, prices,
                   {str(day): source_reference} if source_reference is not None else {})


def save_day(store_path: Path, features: dict, bundle: dict, *,
             first_seen: Mapping[str, str] | None = None) -> None:
    """以单个事务更新月度特征及日结果；原始交易不写进特征库。"""
    stamp = features["date_utc"]
    if bundle.get("status") != "complete" or bundle.get("kind") != "daily" or bundle.get("target_utc") != stamp:
        raise ValueError("Only matching completed daily results can enter the analysis store")
    meta = {key: value for key, value in features.items() if key not in ("wallets", "daily_hits")}
    if first_seen is not None:
        meta["classified_bot_wallets"] = sorted(row["wallet"] for row in features["wallets"]
            if row["wallet"] in first_seen and first_seen[row["wallet"]] <= stamp)
    store_path = Path(store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store_path)) as db:
        db.executescript(_STORE_SCHEMA)
        with db:
            db.execute("DELETE FROM wallet_days WHERE date_utc=?", (stamp,))
            db.execute("DELETE FROM daily_hits WHERE date_utc=?", (stamp,))
            db.executemany("INSERT INTO wallet_days VALUES (?,?,?,?,?,?)", [
                (stamp, row["wallet"], row["trade_count"], row["first_timestamp"], row["last_timestamp"],
                 str(row["amount_micro_usdc"])) for row in features["wallets"]])
            db.executemany("INSERT INTO daily_hits VALUES (?,?)", [(stamp, wallet) for wallet in features["daily_hits"]])
            db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)", (stamp, _json(meta), _json(bundle)))


def list_days(store_paths: Path | Iterable[Path]) -> list[str]:
    stamps = set()
    for path in _paths(store_paths):
        if path.exists():
            with closing(_read_database(path)) as db:
                stamps.update(row[0] for row in db.execute("SELECT date_utc FROM days"))
    return sorted(stamps)


def load_day(store_paths: Path | Iterable[Path], day: date) -> dict | None:
    for path in _paths(store_paths):
        if path.exists():
            with closing(_read_database(path)) as db:
                row = db.execute("SELECT result_json FROM days WHERE date_utc=?", (str(day),)).fetchone()
                if row is not None:
                    return json.loads(row[0])
    return None


def update_day_previous(store_path: Path, day: date, previous: dict | None) -> dict | None:
    """仅更新日结果的前日比较；不重读原始交易。无变更时返回 None。"""
    path = Path(store_path)
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT result_json FROM days WHERE date_utc=?", (str(day),)).fetchone()
        if row is None:
            return None
        bundle = json.loads(row[0])
        payload = bundle["payload"]
        if payload.get("previous") == previous:
            return None
        payload["previous"] = previous
        payload["display"] = poster.display_values(
            poster.PosterData(day, payload["current"], previous), bundle["distribution"])
        with db:
            db.execute("UPDATE days SET result_json=? WHERE date_utc=?", (_json(bundle), str(day)))
    return bundle


def affected_dates(store_paths: Path | Iterable[Path], changed_wallet_dates: Mapping[str, str]) -> list[str]:
    """仅返回更早命中会改变当日分类的活跃日期，避免重读无关原始日库。"""
    affected = set()
    if not changed_wallet_dates:
        return []
    for path in _paths(store_paths):
        if not path.exists():
            continue
        with closing(_read_database(path)) as db:
            classifications = {}
            for wallet, first in changed_wallet_dates.items():
                for row in db.execute("""
                    SELECT w.date_utc,d.features_json FROM wallet_days w
                    JOIN days d ON d.date_utc=w.date_utc
                    WHERE w.wallet=? AND w.date_utc>=?
                """, (wallet, first)):
                    stamp = row["date_utc"]
                    if stamp not in classifications:
                        recorded = json.loads(row["features_json"]).get("classified_bot_wallets")
                        classifications[stamp] = set(recorded) if recorded is not None else set()
                    if wallet not in classifications[stamp]:
                        affected.add(stamp)
    return sorted(affected)


def _read_days(store_paths: Path | Iterable[Path], start: date, end: date) -> dict:
    result = {}
    for path in _paths(store_paths):
        if not path.exists():
            continue
        with closing(_read_database(path)) as db:
            for row in db.execute("SELECT * FROM days WHERE date_utc>=? AND date_utc<?", (str(start), str(end))):
                stamp = row["date_utc"]
                result[stamp] = (json.loads(row["features_json"]), json.loads(row["result_json"]), [dict(wallet)
                    for wallet in db.execute("SELECT * FROM wallet_days WHERE date_utc=?", (stamp,))])
    return result


def _period_prices(rows: list, start: date, end: date, kind: str,
                   labels: registry.FirstSeen, wallets: set[str], bots: set[str]) -> dict:
    populations = ("all_wallets", "suspected_bots")
    counts = {key: [0] * 10 for key in populations}
    shares = {key: [Decimal(0)] * 10 for key in populations}
    notionals = {key: [Decimal(0)] * 10 for key in populations}
    sum_keys = ("transaction_groups", "validated_groups", "reconstructed_trades", "buy_participating_trades",
                "excluded_sell_only_trades", "unresolved_groups", "exact_value_groups", "tolerated_value_groups",
                "price_rounding_groups", "source_row_count")
    validation = {key: 0 for key in sum_keys}
    first = rows[0][1]["distribution"]
    validation.update({key: first["validation"][key] for key in
                       ("value_tolerance_usdc", "roundtrip_min_decimal_places")})
    validation["max_absolute_value_difference_usdc"] = "0"
    series, audits = set(), []
    decimal_values = [Decimal(band[key]) for _, bundle, _ in rows
        for population in bundle["distribution"]["populations"].values() for band in population["bins"]
        for key in ("shares_exact", "notional_usdc_exact")]
    with localcontext() as context:
        context.prec = max(100, max((v.adjusted() for v in decimal_values), default=0)
                           - min((v.as_tuple().exponent for v in decimal_values), default=0)
                           + len(str(len(rows))) + 30)
        for features, bundle, _ in rows:
            day = bundle["distribution"]
            series.update(day["included_series_slugs"])
            for name in populations:
                for index, band in enumerate(day["populations"][name]["bins"]):
                    counts[name][index] += band["trade_count"]
                    shares[name][index] += Decimal(band["shares_exact"])
                    notionals[name][index] += Decimal(band["notional_usdc_exact"])
            audit = day["validation"]
            for key in sum_keys:
                validation[key] += audit[key]
            validation["max_absolute_value_difference_usdc"] = distribution._decimal_text(max(
                Decimal(validation["max_absolute_value_difference_usdc"]),
                Decimal(audit["max_absolute_value_difference_usdc"])))
            audits.append({"date_utc": features["date_utc"], "total_markets": day["total_markets"],
                           "all_wallet_count": day["all_wallet_count"],
                           "suspected_bot_wallets": day["suspected_bot_wallets"],
                           "source_fingerprint_sha256": features["source_fingerprint_sha256"],
                           "validation": audit})
        total_notional = sum(notionals["all_wallets"], Decimal(0))
        total_shares = sum(shares["all_wallets"], Decimal(0))
        finished = {name: distribution._finish_population(counts[name], shares[name], notionals[name],
                    total_notional, total_shares) for name in populations}
    _, target = period_distribution.period_dates(start, end, kind)
    return {"schema_version": 3, "kind": kind, "target_utc": target,
        "start_date_utc": str(start), "end_date_utc_exclusive": str(end),
        "covered_dates": [row[0]["date_utc"] for row in rows], "date_basis": "market_end_utc",
        "series_slug": None, "market_scope": "all_crypto_updown_series",
        "included_series_slugs": sorted(series), "series_count": len(series),
        "scope": "buy_participation", "bot_scope": "buy_participation", "price_basis": "up_equivalent",
        "notional_basis": "up_equivalent_price_times_shares", "count_unit": "reconstructed_trades",
        "reconstruction_method": first["reconstruction_method"],
        "trade_share_denominator": "population_trade_count", "shares_share_denominator": "all_wallets_buy_event_shares",
        "notional_share_denominator": "all_wallets_buy_notional", "bot_rule": first["bot_rule"],
        "total_markets": sum(row[1]["distribution"]["total_markets"] for row in rows),
        "all_wallet_count": len(wallets), "suspected_bot_wallets": len(bots), "bot_history": labels.metadata,
        "validation": validation, "daily_audits": audits, "populations": finished}


def export_period(store_paths: Path | Iterable[Path], start: date, end: date, kind: str,
                  first_seen: Mapping[str, str], *, history_metadata: dict | None = None,
                  previous: dict | None = None) -> dict:
    """由日特征做钱包并集，并从精确的每日分布累计完整 UTC 周/月。"""
    days, _ = period_distribution.period_dates(start, end, kind)
    stored = _read_days(store_paths, start, end)
    missing = [str(day) for day in days if str(day) not in stored]
    if missing:
        raise IncompleteDay("以下日期尚未完整采集，未生成周期结果：" + ", ".join(missing))
    rows = [stored[str(day)] for day in days]
    labels = _labels(first_seen, history_metadata)
    wallets, bots = set(), set()
    wallet_amount = bot_amount = 0
    volume = 0
    daily_volumes, assets, sources, series_catalog = {}, {}, {}, {}
    for features, bundle, day_wallets in rows:
        stamp = features["date_utc"]
        if bundle.get("status") != "complete":
            raise IncompleteDay(f"{stamp} 的日结果尚未完成。")
        active_bots = {row["wallet"] for row in day_wallets
                       if row["wallet"] in labels and labels[row["wallet"]] <= stamp}
        recorded = features.get("classified_bot_wallets")
        if recorded is None or active_bots != set(recorded):
            raise IncompleteDay(f"{stamp} 的机器人标签已变化，需要先重算该日分布。")
        wallets.update(row["wallet"] for row in day_wallets)
        bots.update(active_bots)
        for row in day_wallets:
            amount = int(row["amount_micro_usdc"])
            wallet_amount += amount
            if row["wallet"] in active_bots:
                bot_amount += amount
        market = features["market_volume"]
        if volume is not None:
            volume = (volume + market["volume_micro_usdc"]
                      if market["volume_micro_usdc"] is not None else None)
        daily_volumes.update(market["daily_volume_micro_usdc"])
        for asset, amount in (market["asset_volume_micro_usdc"] or {}).items():
            assets[asset] = assets.get(asset, 0) + amount
        sources.update(bundle["source_versions"])
        series_catalog.update({row["series_slug"]: row for row in features["series"]})
    current = {"start_date_utc": str(start), "end_date_utc_exclusive": str(end),
        "coverage": [row[0]["coverage"] for row in rows],
        "total_markets": sum(row[1]["payload"]["current"]["total_markets"] for row in rows),
        "wallet_volume_micro_usdc": wallet_amount, "unique_wallets": len(wallets),
        "suspected_bot_wallets": len(bots), "suspected_bot_volume_micro_usdc": bot_amount,
        "wallet_volume_basis": "wallet_buys_only", "bot_volume_basis": "wallet_buys_only",
        "volume_basis": "taker_only", "volume_micro_usdc": volume,
        "daily_volume_micro_usdc": daily_volumes,
        "asset_volume_micro_usdc": (dict(sorted(assets.items(), key=lambda item: (-item[1], item[0])))
                                    if volume is not None else None)}
    prices = _period_prices(rows, start, end, kind, labels, wallets, bots)
    rule = copy.deepcopy(registry.RULE)
    rule.update({kind + "_wallets": "union_of_active_retained_wallets",
                 kind + "_volume": "wallet_volume_from_first_hit_onward"})
    payload = {"date_basis": "market_end_utc", "volume_basis": "taker_only",
        "bot_volume_basis": "wallet_buys_only", "current": current, "previous": previous,
        "bot_rule": rule, "bot_history": labels.metadata,
        "buy_event_summary": event_summary(prices), "bot_panel_basis": BASIS}
    if kind == "monthly":
        import polymarket_updown_monthly_poster as renderer
        prior = (start - timedelta(days=1)).replace(day=1)
        payload["scope_changes"] = sorted([{"series_slug": row["series_slug"], "first_end_time": row["first_end_time"]}
            for row in series_catalog.values() if row["first_end_time"] is not None
            and str(prior) <= row["first_end_time"][:10] < str(end)], key=lambda row: (row["first_end_time"], row["series_slug"]))
    else:
        import polymarket_updown_weekly_poster as renderer
    payload["display"] = renderer.display_values(current, previous, prices)
    bundle = _bundle(kind, start, end, payload, prices, sources)
    bundle["raw_schema_versions"] = {row[0]["date_utc"]: row[1].get("raw_schema_version", 1) for row in rows}
    bundle["volume_methods"] = {row[0]["date_utc"]: row[1].get("volume_method", "legacy_taker_api") for row in rows}
    return bundle
