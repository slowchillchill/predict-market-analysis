"""只读市场金额：新库按成交组首行计算，旧哈希库保留已存 API 摘要。"""

from collections import defaultdict
from datetime import date, timedelta
import sqlite3

import polymarket_updown_bot_price as source
from polymarket_trade_groups import group_column


def _asset(series: str) -> str:
    coin = series.split("-", 1)[0]
    return {"solana": "sol", "dogecoin": "doge"}.get(coin, coin).upper()


def _legacy_api_amounts(db: sqlite3.Connection, start: date, end: date, volumes: dict) -> dict:
    """旧版本的已采 API 结果仍可用于对照，未完成值不补零。"""
    assets = defaultdict(int)
    incomplete = set()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='updown_coverage'").fetchone():
        for stamp, complete in db.execute(
                "SELECT date_utc,is_complete FROM updown_coverage WHERE date_utc>=? AND date_utc<?",
                (str(start), str(end))):
            if complete:
                volumes[stamp] = 0
    for day, series, complete, amount in db.execute("""
        SELECT date(m.end_time),m.series_slug,COUNT(*)=COUNT(v.completed_at),
               COALESCE(SUM(v.amount_micro_usdc),0)
        FROM markets m
        JOIN updown_target_series s ON s.series_slug=m.series_slug
        LEFT JOIN market_volume v ON v.market_slug=m.slug
        WHERE date(m.end_time)>=? AND date(m.end_time)<?
        GROUP BY date(m.end_time),m.series_slug
    """, (str(start), str(end))):
        if not complete:
            incomplete.add(day)
            volumes[day] = None
        elif day not in incomplete:
            volumes[day] = (volumes[day] or 0) + amount
        assets[_asset(series)] += amount
    return assets


def _derived_amounts(db: sqlite3.Connection, start: date, end: date, volumes: dict) -> dict:
    """同一市场内分组；完整空市场为零，未完成日期仍是缺失值。"""
    group = group_column(db)
    assets = defaultdict(int)
    day = start
    while day < end:
        try:
            markets = source._complete_markets(source._snapshot(db, day), day)
        except source.IncompleteDay:
            day += timedelta(days=1)
            continue
        total = 0
        for market in markets:
            seen = set()
            amount = 0
            cursor = db.execute(
                f"SELECT {group},amount_micro_usdc FROM trades WHERE market_slug=? ORDER BY page_number,row_number",
                (market[0],))
            try:
                for identity, row_amount in cursor:
                    if identity not in seen:
                        seen.add(identity)
                        amount += row_amount
            finally:
                cursor.close()
            total += amount
            assets[_asset(market[1])] += amount
        volumes[str(day)] = total
        day += timedelta(days=1)
    return assets


def load_market_volume(db: sqlite3.Connection, start: date, end: date) -> dict:
    """保持报告金额接口；不写原始库，也不需要独立金额表或第二套进度。"""
    days = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days)]
    volumes = dict.fromkeys(days, None)
    columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
    has_api_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone() is not None
    if "group_id" not in columns and has_api_table:
        assets = _legacy_api_amounts(db, start, end, volumes)
    elif ("group_id" in columns or "transaction_hash" in columns) and {
            "market_slug", "page_number", "row_number", "amount_micro_usdc"} <= columns:
        assets = _derived_amounts(db, start, end, volumes)
    else:
        # 极旧的只读审计库没有分组或行顺序，仍可展示钱包指标，金额为 N/A。
        assets = {}
    complete = all(value is not None for value in volumes.values())
    return {
        "volume_basis": "taker_only",
        "volume_micro_usdc": sum(volumes.values()) if complete else None,
        "daily_volume_micro_usdc": volumes,
        "asset_volume_micro_usdc": dict(sorted(assets.items(), key=lambda item: (-item[1], item[0]))) if complete else None,
    }
