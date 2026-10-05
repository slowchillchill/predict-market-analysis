"""只读汇总单边市场成交额；缺少完整单边分页的日期返回 None。"""

from collections import defaultdict
from datetime import date, timedelta
import sqlite3


def load_market_volume(db: sqlite3.Connection, start: date, end: date) -> dict:
    days = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days)]
    volumes = dict.fromkeys(days, None)
    assets = defaultdict(int)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone():
        volumes = dict.fromkeys(days, 0)
        for row in db.execute("""
            SELECT date(m.end_time) AS day, m.series_slug,
                   COUNT(*)=COUNT(v.completed_at) AS is_complete,
                   COALESCE(SUM(v.amount_micro_usdc),0) AS amount
            FROM markets m
            JOIN updown_target_series s ON s.series_slug=m.series_slug
            LEFT JOIN market_volume v ON v.market_slug=m.slug
            WHERE date(m.end_time)>=? AND date(m.end_time)<?
            GROUP BY date(m.end_time), m.series_slug
        """, (str(start), str(end))):
            day, series, complete, amount = row
            if not complete:
                volumes[day] = None
            elif volumes[day] is not None:
                volumes[day] += amount
            coin = series.split("-", 1)[0]
            coin = {"solana": "sol", "dogecoin": "doge"}.get(coin, coin).upper()
            assets[coin] += amount
    complete = all(value is not None for value in volumes.values())
    return {
        "volume_basis": "taker_only",
        "volume_micro_usdc": sum(volumes.values()) if complete else None,
        "daily_volume_micro_usdc": volumes,
        "asset_volume_micro_usdc": dict(sorted(assets.items(), key=lambda item: (-item[1], item[0]))) if complete else None,
    }
