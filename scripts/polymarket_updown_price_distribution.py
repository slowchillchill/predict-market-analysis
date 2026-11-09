"""从现有钱包记录重建买入参与的成交，并统计全部钱包和疑似机器人的价位分布。

每个市场、交易哈希组的首行作为汇总方，其余行作为对手明细。只有份数、
经济方向、时间与费前价值均通过校验的组才用于统计；不补采或修改数据库。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

import polymarket_updown_bot_price as source


class InvalidReconstruction(ValueError):
    """源记录无法按已声明的汇总方／明细方规则重建。"""


VALUE_TOLERANCE = Decimal("0.000001")
ROUNDTRIP_MIN_DECIMAL_PLACES = 10


def statistics_stem(data: dict) -> str:
    """统一日、周、月的统计文件名；结束日显示为包含该日。"""
    series = data.get("series_slug")
    scope = series.replace("-up-or-down-", "_") if series else "all_series"
    if "date_utc" in data:
        target = date.fromisoformat(data["date_utc"]).isoformat()
    elif data.get("kind") == "weekly":
        start = date.fromisoformat(data["start_date_utc"])
        end = date.fromisoformat(data["end_date_utc_exclusive"])
        if start.weekday() != 0 or (end - start).days != 7:
            raise ValueError("交易分布周范围必须是完整 UTC 周。")
        target = f"weekly_{start}_{end - timedelta(days=1)}"
    elif data.get("kind") == "monthly":
        start = date.fromisoformat(data["start_date_utc"])
        end = date.fromisoformat(data["end_date_utc_exclusive"])
        if start.day != 1 or end.day != 1 or not 28 <= (end - start).days <= 31:
            raise ValueError("交易分布月范围必须是完整 UTC 自然月。")
        target = f"monthly_{start:%Y-%m}"
    else:
        raise ValueError("交易分布缺少有效日期或周期。")
    return f"updown_buy_trade_distribution_{scope}_{target}"


def save_statistics(data: dict, output_dir: Path) -> Path:
    """原子导出精确统计 JSON；供日、周、月统计及海报入口共用。"""
    stem = statistics_stem(data)
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{stem}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_dir,
                                         prefix=f".{stem}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


@dataclass(frozen=True)
class _Row:
    wallet: str
    transaction_hash: str
    timestamp: int
    side: str
    outcome: str
    price: Decimal
    size: Decimal
    up_price: Decimal
    direction: int


def _number(raw: str, label: str, market: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidReconstruction(f"{market} 的{label}无效：{raw!r}") from exc
    if not value.is_finite():
        raise InvalidReconstruction(f"{market} 的{label}必须是有限十进制数。")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _amount_percent(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = max(context.prec, 100,
                           len(numerator.as_tuple().digits) + len(denominator.as_tuple().digits) + 10)
        percentage = (numerator * 100 / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{percentage:.2f}"


def _finish_population(counts: list[int], shares: list[Decimal], notionals: list[Decimal],
                       market_notional: Decimal, market_shares: Decimal) -> dict:
    total_count = sum(counts)
    total_shares = sum(shares, Decimal(0))
    total_notional = sum(notionals, Decimal(0))
    return {
        "trade_count": total_count,
        "shares_exact": _decimal_text(total_shares),
        "share_of_all_buy_shares_percent": _amount_percent(total_shares, market_shares),
        "notional_usdc_exact": _decimal_text(total_notional),
        "notional_share_of_market_percent": _amount_percent(total_notional, market_notional),
        "bins": [
            {
                "lower_cents": index * 10,
                "upper_cents": (index + 1) * 10,
                "lower_inclusive": True,
                "upper_inclusive": index == 9,
                "interval_cents": f"[{index * 10}, {(index + 1) * 10}{']' if index == 9 else ')'}",
                "trade_count": count,
                "trade_share_percent": source._share_percent(count, total_count),
                "shares_exact": _decimal_text(shares[index]),
                "share_of_all_buy_shares_percent": _amount_percent(shares[index], market_shares),
                "notional_usdc_exact": _decimal_text(notionals[index]),
                "notional_share_of_market_percent": _amount_percent(notionals[index], market_notional),
            }
            for index, count in enumerate(counts)
        ],
    }


def load_data(db: sqlite3.Connection, day: date, *, series_slug: str | None = None,
              bot_first_seen: dict[str, str] | None = None) -> dict:
    """只读完整目标日；首次命中当日起保留标签，每笔重建成交各组最多一次。"""
    result, _, _ = _load_day(db, day, series_slug=series_slug, bot_first_seen=bot_first_seen)
    return result


def _load_day(db: sqlite3.Connection, day: date, *, series_slug: str | None = None,
              bot_first_seen: dict[str, str] | None = None) -> tuple[dict, set[str], set[str]]:
    """内部返回当日活跃钱包集合，供周期做并集；集合不写入公开统计 JSON。"""
    if db.in_transaction:
        raise ValueError("请先结束外层数据库事务，再读取成交分布，以免阻塞采集。")
    initial = source._snapshot(db, day, series_slug)
    markets = source._complete_markets(initial, day)
    if bot_first_seen is None:
        from polymarket_updown_bot_registry import load_first_seen
        bot_first_seen = load_first_seen(db, day)

    features: dict[str, list[int]] = {}
    for market in markets:
        for wallet, count, first, last in source._rows(db, """
            SELECT wallet, COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM trades WHERE market_slug=? GROUP BY wallet
        """, (market[0],)):
            if wallet not in features:
                features[wallet] = [count, first, last]
            else:
                feature = features[wallet]
                feature[0] += count
                feature[1] = min(feature[1], first)
                feature[2] = max(feature[2], last)
    bots = set()
    for wallet in features:
        first_day = bot_first_seen.get(wallet)
        if first_day is not None and date.fromisoformat(first_day) <= day:
            bots.add(wallet)

    counts = {population: [0] * 10 for population in ("all_wallets", "suspected_bots")}
    shares = {population: [Decimal(0)] * 10 for population in counts}
    notionals = {population: [Decimal(0)] * 10 for population in counts}
    validation = {
        "transaction_groups": 0,
        "validated_groups": 0,
        "reconstructed_trades": 0,
        "buy_participating_trades": 0,
        "excluded_sell_only_trades": 0,
        "unresolved_groups": 0,
        "exact_value_groups": 0,
        "tolerated_value_groups": 0,
        "price_rounding_groups": 0,
        "value_tolerance_usdc": _decimal_text(VALUE_TOLERANCE),
        "roundtrip_min_decimal_places": ROUNDTRIP_MIN_DECIMAL_PLACES,
        "max_absolute_value_difference_usdc": "0",
        "source_row_count": 0,
    }
    with localcontext() as context:
        context.prec = 100
        lowest_exponent = 0
        highest_adjusted = 0
        max_value_difference = Decimal(0)
        for market in markets:
            slug = market[0]
            # 完整消费并关闭每市场游标，避免跨市场持有读事务。
            raw_rows = source._rows(db, """
                SELECT page_number, row_number, wallet, transaction_hash, timestamp,
                       side, outcome, raw_price, raw_size
                FROM trades WHERE market_slug=? ORDER BY page_number, row_number
            """, (slug,))
            groups: dict[str, list[_Row]] = defaultdict(list)
            for _, _, wallet, tx, timestamp, side, outcome, raw_price, raw_size in raw_rows:
                validation["source_row_count"] += 1
                if side not in ("BUY", "SELL") or outcome not in ("Up", "Down"):
                    raise InvalidReconstruction(f"{slug} / {tx} 的买卖方向或结果方向无法识别。")
                price = _number(raw_price, "价格", slug)
                size = _number(raw_size, "份数", slug)
                if not 0 <= price <= 1 or size <= 0:
                    raise InvalidReconstruction(f"{slug} / {tx} 要求价格位于 [0,1] 且份数大于零。")
                # 提高精度以精确保留 1-p、价格×份数和跨市场累计的小数位。
                for value in (price, size):
                    lowest_exponent = min(lowest_exponent, value.as_tuple().exponent)
                    highest_adjusted = max(highest_adjusted, value.adjusted())
                context.prec = max(context.prec,
                                   2 * (highest_adjusted - lowest_exponent + 1)
                                   + len(str(validation["source_row_count"])) + 20)
                up_price = price if outcome == "Up" else 1 - price
                direction = 1 if (side == "BUY" and outcome == "Up") or (side == "SELL" and outcome == "Down") else -1
                groups[tx].append(_Row(wallet, tx, timestamp, side, outcome, price, size,
                                      up_price, direction))

            for tx, rows in groups.items():
                validation["transaction_groups"] += 1
                anchor, details = rows[0], rows[1:]
                if not details or len({row.timestamp for row in rows}) != 1:
                    raise InvalidReconstruction(f"{slug} / {tx} 缺少对手明细或组内时间不一致。")
                if anchor.size != sum((row.size for row in details), Decimal(0)):
                    raise InvalidReconstruction(f"{slug} / {tx} 的汇总份数与明细份数不平衡。")
                if any(row.direction != -anchor.direction for row in details):
                    raise InvalidReconstruction(f"{slug} / {tx} 的汇总方和明细方经济方向不相反。")
                detail_value = sum((row.up_price * row.size for row in details), Decimal(0))
                difference = abs(anchor.up_price * anchor.size - detail_value)
                if difference == 0:
                    value_group = "exact_value_groups"
                elif difference <= VALUE_TOLERANCE:
                    value_group = "tolerated_value_groups"
                else:
                    # 大额汇总均价的末位舍入会放大为超过一微 USDC 的价值差。
                    # 只接受从明细反算均价后，按原精度舍入可严格返回原价的组。
                    # 短价至少检查十位，不能用 .5 的一位精度宽松接纳不平衡组。
                    places = max(ROUNDTRIP_MIN_DECIMAL_PLACES, -anchor.price.as_tuple().exponent)
                    quantum = Decimal(1).scaleb(-places)
                    rebuilt_up_price = detail_value / anchor.size
                    rebuilt_price = rebuilt_up_price if anchor.outcome == "Up" else 1 - rebuilt_up_price
                    if rebuilt_price.quantize(quantum, rounding=ROUND_HALF_UP) != anchor.price:
                        raise InvalidReconstruction(f"{slug} / {tx} 的 Up 等价价值不平衡，差额 {difference} USDC。")
                    value_group = "price_rounding_groups"
                validation["validated_groups"] += 1
                validation[value_group] += 1
                max_value_difference = max(max_value_difference, difference)
                validation["reconstructed_trades"] += len(details)
                anchor_bot_buy = anchor.wallet in bots and anchor.side == "BUY"
                for row in details:
                    if anchor.side != "BUY" and row.side != "BUY":
                        validation["excluded_sell_only_trades"] += 1
                        continue
                    validation["buy_participating_trades"] += 1
                    index = source._price_bin(str(row.up_price), slug)
                    notional = row.up_price * row.size
                    counts["all_wallets"][index] += 1
                    shares["all_wallets"][index] += row.size
                    notionals["all_wallets"][index] += notional
                    if anchor_bot_buy or (row.wallet in bots and row.side == "BUY"):
                        counts["suspected_bots"][index] += 1
                        shares["suspected_bots"][index] += row.size
                        notionals["suspected_bots"][index] += notional

        if source._snapshot(db, day, series_slug) != initial:
            raise source.DataChangedError(f"{day} 的市场或采集进度在读取期间发生变化；请重试。")
        market_notional = sum(notionals["all_wallets"], Decimal(0))
        market_shares = sum(shares["all_wallets"], Decimal(0))
        validation["max_absolute_value_difference_usdc"] = _decimal_text(max_value_difference)
        populations = {
            name: _finish_population(counts[name], shares[name], notionals[name], market_notional, market_shares)
            for name in counts
        }
        assert sum(counts["all_wallets"]) == validation["buy_participating_trades"]
        assert validation["buy_participating_trades"] + validation["excluded_sell_only_trades"] == validation["reconstructed_trades"]
        assert validation["exact_value_groups"] + validation["tolerated_value_groups"] + validation["price_rounding_groups"] == validation["validated_groups"]

    result = {
        "schema_version": 2,
        "date_utc": day.isoformat(),
        "date_basis": "market_end_utc",
        "series_slug": series_slug,
        "market_scope": "all_crypto_updown_series" if series_slug is None else "selected_series",
        "included_series_slugs": [row[0] for row in initial[0]],
        "series_count": len(initial[0]),
        "scope": "buy_participation",
        "bot_scope": "buy_participation",
        "price_basis": "up_equivalent",
        "notional_basis": "up_equivalent_price_times_shares",
        "count_unit": "reconstructed_trades",
        "reconstruction_method": "existing_db_first_row_anchor_plus_counterparty_rows_v1",
        "trade_share_denominator": "population_trade_count",
        "shares_share_denominator": "all_wallets_buy_event_shares",
        "notional_share_denominator": "all_wallets_buy_notional",
        "bot_rule": {
            "max_mean_interval_seconds": source.MAX_INTERVAL_SECONDS,
            "min_span_minutes": source.MIN_SPAN_MINUTES,
            "min_markets": None,
            "classification": "first_hit_day_onward",
            "classification_scope": "all_crypto_updown_series",
            "first_seen_basis": "market_end_utc",
            "future_labels_applied": False,
        },
        "total_markets": len(markets),
        "all_wallet_count": len(features),
        "suspected_bot_wallets": len(bots),
        "bot_history": dict(getattr(bot_first_seen, "metadata", {})),
        "validation": validation,
        "populations": populations,
    }
    return result, set(features), bots
