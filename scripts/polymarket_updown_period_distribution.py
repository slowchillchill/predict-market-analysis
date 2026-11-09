"""以同一逐日成交重建内核汇总完整 UTC 周/月的买入价位分布。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Callable

import polymarket_updown_price_distribution as daily


def period_dates(start: date, end_exclusive: date, kind: str) -> tuple[list[date], str]:
    """返回完整周或自然月的日期及显示目标，不接受部分周期。"""
    length = (end_exclusive - start).days
    if kind == "weekly":
        if start.weekday() != 0 or length != 7:
            raise ValueError("交易分布周范围必须是完整 UTC 周。")
        target = f"{start}_{end_exclusive - timedelta(days=1)}"
    elif kind == "monthly":
        if start.day != 1 or end_exclusive.day != 1 or not 28 <= length <= 31:
            raise ValueError("交易分布月范围必须是完整 UTC 自然月。")
        target = f"{start:%Y-%m}"
    else:
        raise ValueError("交易分布仅支持 weekly 或 monthly。")
    return [start + timedelta(days=i) for i in range(length)], target


def snapshot_digest(snapshot: tuple) -> str:
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def load_period_data(db: sqlite3.Connection, start: date, end_exclusive: date, *, kind: str,
                     bot_first_seen: dict[str, str] | None = None,
                     progress: Callable[[str], None] | None = None) -> dict:
    """逐日短读取、精确求和；机器人标签从首次命中日延续，避免未来回溯。"""
    if db.in_transaction:
        raise ValueError("请先结束外层数据库事务，再读取周期成交分布，以免阻塞采集。")
    days, target = period_dates(start, end_exclusive, kind)
    initial = {day: daily.source._snapshot(db, day) for day in days}
    for day, snapshot in initial.items():
        daily.source._complete_markets(snapshot, day)
    if bot_first_seen is None:
        from polymarket_updown_bot_registry import load_first_seen
        bot_first_seen = load_first_seen(db, days[-1], progress=progress)

    populations = ("all_wallets", "suspected_bots")
    counts = {name: [0] * 10 for name in populations}
    shares = {name: [Decimal(0)] * 10 for name in populations}
    notionals = {name: [Decimal(0)] * 10 for name in populations}
    wallets, bots, series = set(), set(), set()
    validation = None
    daily_audits = []
    market_count = 0
    sum_keys = ("transaction_groups", "validated_groups", "reconstructed_trades",
                "buy_participating_trades", "excluded_sell_only_trades", "unresolved_groups",
                "exact_value_groups", "tolerated_value_groups", "price_rounding_groups", "source_row_count")
    with localcontext() as context:
        context.prec = 100
        lowest_exponent, highest_adjusted = 0, 0
        for index, day in enumerate(days, 1):
            if progress:
                progress(f"[{kind} {index}/{len(days)}] 重建 {day}")
            data, day_wallets, day_bots = daily._load_day(db, day, bot_first_seen=bot_first_seen)
            wallets.update(day_wallets)
            bots.update(day_bots)
            series.update(data["included_series_slugs"])
            market_count += data["total_markets"]
            for name in populations:
                for band, row in enumerate(data["populations"][name]["bins"]):
                    quantity = Decimal(row["shares_exact"])
                    notional = Decimal(row["notional_usdc_exact"])
                    for value in (quantity, notional):
                        lowest_exponent = min(lowest_exponent, value.as_tuple().exponent)
                        highest_adjusted = max(highest_adjusted, value.adjusted())
                    context.prec = max(context.prec, highest_adjusted - lowest_exponent + len(str(len(days))) + 20)
                    counts[name][band] += row["trade_count"]
                    shares[name][band] += quantity
                    notionals[name][band] += notional
            day_validation = data["validation"]
            if validation is None:
                validation = {key: day_validation[key] for key in
                              ("value_tolerance_usdc", "roundtrip_min_decimal_places")}
                validation.update({key: 0 for key in sum_keys})
                validation["max_absolute_value_difference_usdc"] = "0"
            for key in sum_keys:
                validation[key] += day_validation[key]
            validation["max_absolute_value_difference_usdc"] = daily._decimal_text(max(
                Decimal(validation["max_absolute_value_difference_usdc"]),
                Decimal(day_validation["max_absolute_value_difference_usdc"])))
            daily_audits.append({"date_utc": str(day), "total_markets": data["total_markets"],
                                "all_wallet_count": len(day_wallets), "suspected_bot_wallets": len(day_bots),
                                "source_fingerprint_sha256": snapshot_digest(initial[day]),
                                "validation": day_validation})
            if progress:
                progress(f"[{kind} {index}/{len(days)}] {day} 完成：{day_validation['buy_participating_trades']:,} BUY trades")

        for day, snapshot in initial.items():
            if daily.source._snapshot(db, day) != snapshot:
                raise daily.source.DataChangedError(f"{day} 的市场或采集进度在周期读取期间发生变化；未导出周期分布。")
        market_notional = sum(notionals["all_wallets"], Decimal(0))
        market_shares = sum(shares["all_wallets"], Decimal(0))
        results = {name: daily._finish_population(counts[name], shares[name], notionals[name], market_notional, market_shares)
                   for name in populations}

    return {
        "schema_version": 3, "kind": kind, "target_utc": target,
        "start_date_utc": str(start), "end_date_utc_exclusive": str(end_exclusive),
        "covered_dates": [str(day) for day in days], "date_basis": "market_end_utc",
        "series_slug": None, "market_scope": "all_crypto_updown_series",
        "included_series_slugs": sorted(series), "series_count": len(series),
        "scope": "buy_participation", "bot_scope": "buy_participation",
        "price_basis": "up_equivalent", "notional_basis": "up_equivalent_price_times_shares",
        "count_unit": "reconstructed_trades",
        "reconstruction_method": "existing_db_first_row_anchor_plus_counterparty_rows_v1",
        "trade_share_denominator": "population_trade_count",
        "shares_share_denominator": "all_wallets_buy_event_shares",
        "notional_share_denominator": "all_wallets_buy_notional",
        "bot_rule": data["bot_rule"], "total_markets": market_count,
        "all_wallet_count": len(wallets), "suspected_bot_wallets": len(bots),
        "bot_history": dict(getattr(bot_first_seen, "metadata", {})),
        "validation": validation, "daily_audits": daily_audits, "populations": results,
    }
