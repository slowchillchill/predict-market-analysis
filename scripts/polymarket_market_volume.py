"""独立采集单边金额，或从已完成钱包明细的交易首行推导金额。"""

from __future__ import annotations

import sqlite3
from typing import Callable

import polymarket_btc5m_daily as core


def derive_market_volume(db: sqlite3.Connection, slug: str) -> int:
    """按现有页/行顺序取每个交易哈希的首行；只替换金额，不改钱包明细。"""
    progress = db.execute("SELECT completed_at FROM collection_progress WHERE market_slug=?", (slug,)).fetchone()
    if progress is None or progress[0] is None:
        raise ValueError(f"{slug} 钱包成交尚未完成，不能推导单边金额")
    seen = set()
    amount = 0
    for transaction_hash, row_amount in db.execute(
            "SELECT transaction_hash,amount_micro_usdc FROM trades WHERE market_slug=? "
            "ORDER BY page_number,row_number", (slug,)):
        if transaction_hash not in seen:
            seen.add(transaction_hash)
            amount += row_amount
    # 来源由发布元数据说明；复用原表，不把推导量伪装成已请求的 API 页数。
    with db:
        db.execute(
            "INSERT INTO market_volume(market_slug,amount_micro_usdc,committed_page,next_cursor,completed_at,last_error) "
            "VALUES (?,?,0,NULL,?,NULL) ON CONFLICT(market_slug) DO UPDATE SET "
            "amount_micro_usdc=excluded.amount_micro_usdc,committed_page=0,next_cursor=NULL,"
            "completed_at=excluded.completed_at,last_error=NULL", (slug, amount, core.utc_now()))
    return amount


def commit_page(db: sqlite3.Connection, slug: str, page: int, payload: dict) -> None:
    next_cursor = payload["pagination"]["next_cursor"]
    amount = sum(core.micro_usdc(str(row["price"]), str(row["size"]))
                 for row in payload["data"])
    # 金额与游标一起提交；中断后从下一页继续，不按交易哈希合并多笔成交。
    with db:
        db.execute(
            "UPDATE market_volume SET amount_micro_usdc=amount_micro_usdc+?, "
            "committed_page=?, next_cursor=?, completed_at=?, last_error=NULL "
            "WHERE market_slug=?",
            (amount, page, next_cursor, core.utc_now() if next_cursor is None else None, slug),
        )


def collect_market(db: sqlite3.Connection, client: core.Client, slug: str, *,
                   should_yield: Callable[[], bool] | None = None) -> None:
    with db:
        db.execute("INSERT OR IGNORE INTO market_volume(market_slug) VALUES (?)", (slug,))
    condition = db.execute("SELECT condition_id FROM markets WHERE slug=?", (slug,)).fetchone()[0]
    restarted_cursor = False
    while True:
        progress = db.execute("SELECT * FROM market_volume WHERE market_slug=?", (slug,)).fetchone()
        if progress["completed_at"] is not None:
            return
        core.check_pause(should_yield)
        params = {**core.trade_params(condition), "taker_only": "true"}
        if progress["next_cursor"] is not None:
            params["cursor"] = progress["next_cursor"]
        try:
            payload = client.get(core.TRADES_URL, params)
        except core.FetchError as exc:
            if exc.invalid_cursor and "cursor" in params and not restarted_cursor:
                # 过期游标只影响当前市场的单边汇总，不改钱包明细。
                with db:
                    db.execute(
                        "UPDATE market_volume SET amount_micro_usdc=0, committed_page=0, "
                        "next_cursor=NULL, last_error=NULL WHERE market_slug=?", (slug,),
                    )
                restarted_cursor = True
                continue
            raise
        commit_page(db, slug, progress["committed_page"] + 1, payload)
        core.check_pause(should_yield)
