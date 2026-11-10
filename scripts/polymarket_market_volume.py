"""旧入口的只读兼容辅助；正常采集不调用此模块，不写金额实体表。"""

from __future__ import annotations

import sqlite3
from polymarket_trade_groups import group_column


def derive_market_volume(db: sqlite3.Connection, slug: str) -> int:
    """只读计算完成市场的首行金额；不创建或更新 market_volume。"""
    progress = db.execute("SELECT completed_at FROM collection_progress WHERE market_slug=?", (slug,)).fetchone()
    if progress is None or progress[0] is None:
        raise ValueError(f"{slug} 钱包成交尚未完成，不能推导单边金额")
    seen, amount = set(), 0
    for group, row_amount in db.execute(
            f"SELECT {group_column(db)},amount_micro_usdc FROM trades WHERE market_slug=? "
            "ORDER BY page_number,row_number", (slug,)):
        if group not in seen:
            seen.add(group)
            amount += row_amount
    return amount


def legacy_market_volume(db: sqlite3.Connection, slug: str) -> int | None:
    """只读旧库中已经完成的 API 金额，供迁移前参考。"""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_volume'").fetchone() is None:
        return None
    row = db.execute("SELECT amount_micro_usdc FROM market_volume WHERE market_slug=? AND completed_at IS NOT NULL",
                     (slug,)).fetchone()
    return row[0] if row is not None else None
