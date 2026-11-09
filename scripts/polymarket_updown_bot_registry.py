"""从现库观察历史规则命中，按首次命中日持续保留疑似机器人标签。

只读源数据库。独立缓存保存每日命中名单和市场/采集进度指纹，不把
未来命中用于更早结束日，也不把缺失历史当作已经验证的非机器人。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping

import polymarket_updown_bot_price as source
from polymarket_updown_rules import MAX_INTERVAL_SECONDS, MIN_SPAN_MINUTES

ROOT = Path(__file__).resolve().parents[1]
CACHE_VERSION = 1
RULE = {
    "classification": "first_hit_day_onward",
    "classification_scope": "all_crypto_updown_series",
    "first_seen_basis": "market_end_utc",
    "future_labels_applied": False,
    "max_mean_interval_seconds": MAX_INTERVAL_SECONDS,
    "min_span_minutes": MIN_SPAN_MINUTES,
    "min_markets": None,
}


class FirstSeen(dict[str, str]):
    """wallet -> 首次观察到规则命中的 UTC 市场结束日，附源覆盖审计。"""

    def __init__(self, values=(), *, metadata: dict | None = None):
        super().__init__(values)
        self.metadata = metadata or {}


def filter_active(first_seen: Mapping[str, str], day: date,
                  active_wallets: Iterable[str]) -> set[str]:
    """筛选目标日已命中过、并在本期实际出现的钱包；不使用未来标签。"""
    stamp = day.isoformat()
    return {wallet for wallet in active_wallets
            if wallet in first_seen and date.fromisoformat(first_seen[wallet]).isoformat() <= stamp}


def _fingerprint(snapshot: tuple) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _database_identity(db: sqlite3.Connection) -> dict:
    filename = source._rows(db, "SELECT file FROM pragma_database_list WHERE name='main'")[0][0]
    if not filename:
        # 内存连接只可在同一连接内复用显式缓存，不与另一测试/数据库混用。
        return {"file": None, "connection_id": id(db)}
    path = Path(filename).resolve()
    info = path.stat()
    return {"file": str(path), "device": info.st_dev, "inode": info.st_ino}


def _read_cache(path: Path | None, identity: dict) -> dict:
    empty = {"schema_version": CACHE_VERSION, "rule": RULE,
             "database_identity": identity, "days": {}}
    if path is None:
        return empty
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return empty
    if (not isinstance(cached, dict) or cached.get("schema_version") != CACHE_VERSION
            or cached.get("rule") != RULE or cached.get("database_identity") != identity
            or not isinstance(cached.get("days"), dict)):
        return empty
    return cached


def _save_cache(path: Path | None, cache: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(cache, output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _daily_flags(db: sqlite3.Connection, snapshot: tuple) -> list[str]:
    features: dict[str, list[int]] = {}
    for market in snapshot[1]:
        for wallet, count, first, last in source._rows(db, """
            SELECT wallet, COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM trades WHERE market_slug=? GROUP BY wallet
        """, (market[0],)):
            previous = features.get(wallet)
            if previous is None:
                features[wallet] = [count, first, last]
            else:
                previous[0] += count
                previous[1] = min(previous[1], first)
                previous[2] = max(previous[2], last)
    return sorted(wallet for wallet, (count, first, last) in features.items()
                  if count > 1 and MIN_SPAN_MINUTES * 60 <= last-first
                  <= MAX_INTERVAL_SECONDS * (count-1))


def _usable_entry(entry, fingerprint: str) -> bool:
    return (isinstance(entry, dict) and entry.get("source_sha256") == fingerprint
            and isinstance(entry.get("flagged_wallets"), list)
            and all(isinstance(wallet, str) for wallet in entry["flagged_wallets"])
            and type(entry.get("coverage_complete")) is bool
            and isinstance(entry.get("coverage_problem"), (str, type(None)))
            and type(entry.get("market_count")) is int)


def load_first_seen(db: sqlite3.Connection, through_day: date, *,
                    cache_path: Path | None = None,
                    progress: Callable[[str], None] | None = None) -> FirstSeen:
    """从最早相关结束日逐日识别到 through_day，返回持续标签的首次命中表。

    对文件数据库，cache_path=None 使用 outputs/statistics 下按源路径区分的
    派生缓存；内存库默认不写文件。缓存使用前检查逐日源指纹和规则版本。
    历史不完整日仍只识别现库中已观察到的命中，并在 metadata 显式列出；
    返回日期是首次观察命中，不声称是钱包真实的首次机器人操作日。
    """
    if db.in_transaction:
        raise ValueError("请先结束外层数据库事务，再读取历史机器人标签。")
    identity = _database_identity(db)
    if cache_path is None and identity["file"]:
        key = hashlib.sha256(identity["file"].encode("utf-8")).hexdigest()[:16]
        cache_path = ROOT / "outputs/statistics" / f"updown_bot_first_seen_{key}_v1.json"
    elif cache_path is not None:
        cache_path = Path(cache_path)
        if identity["file"] and cache_path.resolve() == Path(identity["file"]):
            raise ValueError("机器人标签缓存不能覆盖源数据库。")
    cache = _read_cache(cache_path, identity)
    first = source._rows(db, """
        SELECT MIN(date(m.end_time)) FROM markets m
        JOIN updown_target_series s ON s.series_slug=m.series_slug
        WHERE date(m.end_time)<=?
    """, (through_day.isoformat(),))[0][0]
    metadata = {
        **RULE, "history_start_utc": first, "through_day_utc": through_day.isoformat(),
        "first_seen_semantics": "first_observed_daily_rule_hit",
        "incomplete_history_days": [], "missing_market_days": [],
        "daily_source_fingerprints": {}, "observed_day_count": 0,
        "cache_path": str(cache_path) if cache_path is not None else None,
    }
    result = FirstSeen(metadata=metadata)
    if first is None:
        return result
    start = date.fromisoformat(first)
    days = [start + timedelta(days=i) for i in range((through_day-start).days+1)]
    for index, day in enumerate(days, 1):
        stamp = day.isoformat()
        initial = source._snapshot(db, day)
        if not initial[0]:
            raise ValueError("历史机器人标签缺少加密 Up/Down 系列目录。")
        fingerprint = _fingerprint(initial)
        entry = cache["days"].get(stamp)
        reused = _usable_entry(entry, fingerprint)
        if not reused:
            try:
                source._complete_markets(initial, day)
                complete, problem = True, None
            except source.IncompleteDay as exc:
                complete, problem = False, str(exc)
            entry = {"source_sha256": fingerprint, "flagged_wallets": _daily_flags(db, initial),
                     "coverage_complete": complete, "coverage_problem": problem,
                     "market_count": len(initial[1])}
            if source._snapshot(db, day) != initial:
                raise source.DataChangedError(f"{stamp} 的源状态在历史机器人识别期间发生变化；请重试。")
            cache["days"][stamp] = entry
            _save_cache(cache_path, cache)
        metadata["daily_source_fingerprints"][stamp] = fingerprint
        if entry["market_count"]:
            metadata["observed_day_count"] += 1
        else:
            metadata["missing_market_days"].append(stamp)
        if not entry["coverage_complete"]:
            metadata["incomplete_history_days"].append(stamp)
        for wallet in entry["flagged_wallets"]:
            result.setdefault(wallet, stamp)
        if progress is not None:
            progress(f"机器人历史标签 {index}/{len(days)} {stamp}: "
                     f"{'缓存' if reused else '计算'}，当日命中 {len(entry['flagged_wallets']):,}，累计 {len(result):,}")
    # 日期可能很早便读完；整段结束再核对，避免中途重采造成混合状态。
    for day in days:
        if _fingerprint(source._snapshot(db, day)) != metadata["daily_source_fingerprints"][day.isoformat()]:
            raise source.DataChangedError(f"{day} 的源状态在历史标签汇总期间发生变化；请重试。")
    return result
