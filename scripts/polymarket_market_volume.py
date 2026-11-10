"""累计 taker_only=true 的成交金额，独立保留可续传分页进度。"""

from __future__ import annotations

import sqlite3
from typing import Callable

import polymarket_btc5m_daily as core


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
