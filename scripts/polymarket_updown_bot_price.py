"""旧版原始 BUY 记录审计及共享只读工具。

正式日、周、月交易分布使用 polymarket_updown_price_distribution 的成交重建
和共享历史机器人标签；本模块的旧 load_data 保留原始记录审计语义。

日期、系列和机器人条件沿用日报。记录按接口返回的行计数，不按交易哈希
去重；Up、Down 的 BUY 使用各自原始成交价格，SELL 不转换为镜像 BUY。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from polymarket_updown_rules import MAX_INTERVAL_SECONDS, MIN_SPAN_MINUTES


class IncompleteDay(ValueError):
    """目标日的钱包成交尚未完整采集。"""


class InvalidTradePrice(ValueError):
    """计数范围内的 BUY 价格无效。"""


class DataChangedError(ValueError):
    """短读取期间目标日的市场或采集进度发生变化。"""


_PRICE_LIMITS = tuple(Decimal(f"0.{i}") for i in range(1, 10))


def _rows(db: sqlite3.Connection, sql: str, parameters: tuple = ()) -> list:
    # 每个查询完整消费并关闭游标，不持有跨市场读取事务。
    cursor = db.execute(sql, parameters)
    try:
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _expected_markets(day: date, count: int, first_end: str | None) -> int:
    if first_end is None:
        return count
    first = datetime.fromisoformat(first_end.replace("Z", "+00:00")).astimezone(timezone.utc)
    if first.date() < day:
        return count
    if first.date() > day:
        return 0
    stop = datetime.combine(day + timedelta(days=1), time(), timezone.utc)
    remaining = int((stop - first).total_seconds())
    step = 86400 // count
    return (remaining + step - 1) // step


def _snapshot(db: sqlite3.Connection, day: date,
              series_slug: str | None = None) -> tuple[tuple, tuple]:
    catalog = tuple(_rows(db, "SELECT series_slug, expected_markets, first_end_time "
                         "FROM updown_target_series WHERE (? IS NULL OR series_slug=?) "
                         "ORDER BY series_slug", (series_slug, series_slug)))
    if series_slug is not None and not catalog:
        raise ValueError(f"未知市场系列：{series_slug}。")
    markets = tuple(_rows(db, """
        SELECT m.slug, m.series_slug, m.condition_id, m.end_time,
               p.committed_page, p.completed_at, p.next_cursor, p.last_error, p.query_params
        FROM markets m
        JOIN updown_target_series s ON s.series_slug=m.series_slug
        LEFT JOIN collection_progress p ON p.market_slug=m.slug
        WHERE date(m.end_time)=? AND (? IS NULL OR m.series_slug=?) ORDER BY m.slug
    """, (day.isoformat(), series_slug, series_slug)))
    return catalog, markets


def _complete_markets(snapshot: tuple[tuple, tuple], day: date) -> tuple:
    catalog, markets = snapshot
    if not catalog:
        raise IncompleteDay(f"{day} 的市场系列目录为空；未生成价格分布。")
    grouped: dict[str, list] = {series: [] for series, _, _ in catalog}
    for market in markets:
        grouped[market[1]].append(market)
    problems = []
    for series, count, first in catalog:
        expected = _expected_markets(day, count, first)
        rows = grouped[series]
        discovered = sum(row[2] is not None for row in rows)
        completed = sum(row[2] is not None and row[5] is not None for row in rows)
        if len(rows) != expected or discovered != expected or completed != expected:
            problems.append(f"{series}: 已发现 {discovered}、已完成 {completed}、预期 {expected}")
    if problems:
        raise IncompleteDay(f"{day} 钱包成交尚未完整采集；" + "；".join(problems))
    return markets


def _price_bin(raw_price: str, market_slug: str) -> int:
    try:
        price = Decimal(raw_price)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidTradePrice(f"{market_slug} 的机器人 BUY 价格无效：{raw_price!r}") from exc
    if not price.is_finite() or price < 0 or price > 1:
        raise InvalidTradePrice(f"{market_slug} 的机器人 BUY 价格超出有效范围：[0, 1]。")
    # 直接比较十进制边界，不让浮点或 Decimal 上下文舍入把临界价格分错档。
    return next((index for index, upper in enumerate(_PRICE_LIMITS) if price < upper), 9)


def _share_percent(count: int, total: int) -> str | None:
    if not total:
        return None
    with localcontext() as context:
        context.prec = max(28, len(str(total)) + 6)
        result = (Decimal(count) * 100 / Decimal(total)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{result:.2f}"


def load_data(db: sqlite3.Connection, day: date, *, series_slug: str | None = None) -> dict:
    """返回一个完整市场结束日的 BUY 记录分布；只执行短 SELECT 查询。

    不接受调用方打开的事务，以免在正在采集的数据库上持有长读取锁。
    已完成历史市场不会被正常采集器重写；前后检查元数据以识别主动重采。
    指定 series_slug 时仅检查该系列完整性，机器人也在该系列市场内重新判定。
    """
    if db.in_transaction:
        raise ValueError("请先结束外层数据库事务，再读取价格分布，以免阻塞采集。")
    initial = _snapshot(db, day, series_slug)
    markets = _complete_markets(initial, day)
    features: dict[str, list[int]] = {}
    for market in markets:
        for wallet, count, first, last in _rows(db, """
            SELECT wallet, COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM trades WHERE market_slug=? GROUP BY wallet
        """, (market[0],)):
            if wallet not in features:
                features[wallet] = [count, first, last]
            else:
                summary = features[wallet]
                summary[0] += count
                summary[1] = min(summary[1], first)
                summary[2] = max(summary[2], last)
    bots = {
        wallet for wallet, (count, first, last) in features.items()
        if count > 1 and MIN_SPAN_MINUTES * 60 <= last - first
        <= MAX_INTERVAL_SECONDS * (count - 1)
    }
    counts = [0] * 10
    for market in markets:
        for wallet, price, count in _rows(db, """
            SELECT wallet, raw_price, COUNT(*) FROM trades
            WHERE market_slug=? AND side='BUY' GROUP BY wallet, raw_price
        """, (market[0],)):
            if wallet in bots:
                counts[_price_bin(price, market[0])] += count
    if _snapshot(db, day, series_slug) != initial:
        raise DataChangedError(f"{day} 的市场或采集进度在读取期间发生变化；请重试。")
    total = sum(counts)
    result = {
        "date_utc": day.isoformat(),
        "date_basis": "market_end_utc",
        "trade_basis": "suspected_bot_wallet_buy_records",
        "bot_rule": {
            "max_mean_interval_seconds": MAX_INTERVAL_SECONDS,
            "min_span_minutes": MIN_SPAN_MINUTES,
            "min_markets": None,
        },
        "total_markets": len(markets),
        "suspected_bot_wallets": len(bots),
        "buy_record_count": total,
        "bins": [
            {"lower_cents": index * 10, "upper_cents": (index + 1) * 10,
             "upper_inclusive": index == 9, "record_count": count,
             "share_percent": _share_percent(count, total)}
            for index, count in enumerate(counts)
        ],
    }
    if series_slug is not None:
        result.update(series_slug=series_slug, bot_classification_scope="selected_series")
    return result
