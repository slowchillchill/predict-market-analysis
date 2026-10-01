"""周、月报告共用的只读区间聚合；按日处理钱包特征，保留区间钱包并集。"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import sqlite3

import polymarket_updown_poster as daily


def changes(current: dict, previous: dict | None, field: str, period: str) -> tuple[str, str]:
    if previous is None:
        return "N/A", f"prior {period} unavailable"
    if previous[field] == 0:
        return "N/A", f"prior {period} = 0"
    return daily.percent_text(current[field]-previous[field],previous[field],change=True), f"vs prior {period}"


def bot_share_change(current: dict, previous: dict | None, period: str) -> tuple[str, str]:
    if previous is None:
        return "N/A", f"prior {period} unavailable"
    if not current["volume_micro_usdc"] or not previous["volume_micro_usdc"]:
        return "N/A", "zero volume denominator"
    delta = ((Decimal(current["suspected_bot_volume_micro_usdc"])/current["volume_micro_usdc"]
              - Decimal(previous["suspected_bot_volume_micro_usdc"])/previous["volume_micro_usdc"])*100)
    delta = delta.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    return (f"{delta:+.2f} pp" if delta else "0.00 pp"), f"vs prior {period}"


def millions(value: int) -> str:
    return str((Decimal(value)/10**12).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))


def load_period(db: sqlite3.Connection, start: date, end: date, *, required: bool = True) -> dict | None:
    days = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days)]
    coverage = {r["date_utc"]: dict(r) for r in db.execute(
        "SELECT * FROM updown_coverage WHERE date_utc>=? AND date_utc<?", (str(start),str(end)))}
    missing = [d for d in days if not coverage.get(d, {}).get("is_complete")]
    if missing:
        if required:
            raise daily.IncompleteDay("以下日期尚未完整采集，未生成海报：" + ", ".join(missing))
        return None
    wallets, bot_wallets = set(), set()
    assets = defaultdict(int)
    volumes = dict.fromkeys(days,0)
    bot_volume = 0
    query = (daily.ROOT / "sql/updown_period.sql").read_text(encoding="utf-8")
    for day in days:
        wallet_days = {}
        for r in db.execute(query,{"start": day, "end": str(date.fromisoformat(day)+timedelta(days=1))}):
            wallet, amount = r["wallet"], r["amount_micro_usdc"]
            wallets.add(wallet)
            volumes[day] += amount
            coin = r["series_slug"].split("-",1)[0]
            coin = {"solana":"sol", "dogecoin":"doge"}.get(coin,coin).upper()
            assets[coin] += amount
            if wallet not in wallet_days:
                wallet_days[wallet] = [0,r["first_timestamp"],r["last_timestamp"],0]
            f = wallet_days[wallet]
            f[0] += r["trade_count"]
            f[1] = min(f[1],r["first_timestamp"])
            f[2] = max(f[2],r["last_timestamp"])
            f[3] += amount
        for wallet,(count,first,last,amount) in wallet_days.items():
            span = last-first
            if (count>1 and span <= daily.MAX_INTERVAL_SECONDS*(count-1)
                    and span >= daily.MIN_SPAN_MINUTES*60):
                bot_wallets.add(wallet)
                bot_volume += amount
    return {
        "start_date_utc":str(start), "end_date_utc_exclusive":str(end),
        "coverage":[coverage[d] for d in days],
        "total_markets":sum(coverage[d]["completed_markets"] for d in days),
        "volume_micro_usdc":sum(volumes.values()), "unique_wallets":len(wallets),
        "suspected_bot_wallets":len(bot_wallets), "suspected_bot_volume_micro_usdc":bot_volume,
        "daily_volume_micro_usdc":volumes,
        "asset_volume_micro_usdc":dict(sorted(assets.items(),key=lambda pair:(-pair[1],pair[0]))),
    }
