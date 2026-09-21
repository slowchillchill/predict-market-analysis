#!/usr/bin/env python3
"""从本地完整日数据生成周海报和英文推文；日期、金额与每日机器人规则沿用日海报。"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import polymarket_updown_poster as daily

ROOT = daily.ROOT
BACKGROUND = ROOT / "assets/posters/updown_weekly_background_v1.png"
WHITE, MUTED, GOLD = "#fff4df", "#dfc9b6", "#e4bd76"
COLORS = ("#e9c36d", "#c98d69", "#e5b39e", "#947861")


def load_week(db: sqlite3.Connection, start: date, *, required: bool = True) -> dict | None:
    end = start + timedelta(days=7)
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    coverage = {r["date_utc"]: dict(r) for r in db.execute(
        "SELECT * FROM updown_coverage WHERE date_utc>=? AND date_utc<?", (str(start), str(end)))}
    missing = [d for d in days if not coverage.get(d, {}).get("is_complete")]
    # 延续日海报的完整日要求和前期缺失语义。
    if missing:
        if required:
            raise daily.IncompleteDay("以下日期尚未完整采集，未生成周海报：" + ", ".join(missing))
        return None
    wallets = set()
    wallet_days = {}
    assets = defaultdict(int)
    volumes = dict.fromkeys(days, 0)
    query = (ROOT / "sql/updown_weekly_poster.sql").read_text(encoding="utf-8")
    for r in db.execute(query, {"start": str(start), "end": str(end)}):
        d, wallet, amount = r["date_utc"], r["wallet"], r["amount_micro_usdc"]
        wallets.add(wallet)
        volumes[d] += amount
        coin = r["series_slug"].split("-", 1)[0]
        coin = {"solana": "sol", "dogecoin": "doge"}.get(coin, coin).upper()
        assets[coin] += amount
        key = (d, wallet)
        if key not in wallet_days:
            wallet_days[key] = [0, r["first_timestamp"], r["last_timestamp"], 0]
        f = wallet_days[key]
        f[0] += r["trade_count"]
        f[1] = min(f[1], r["first_timestamp"])
        f[2] = max(f[2], r["last_timestamp"])
        f[3] += amount
    bot_wallets = set()
    bot_volume = 0
    for (_, wallet), (count, first, last, amount) in wallet_days.items():
        span = last - first
        if (count > 1 and span <= daily.MAX_INTERVAL_SECONDS * (count - 1)
                and span >= daily.MIN_SPAN_MINUTES * 60):
            bot_wallets.add(wallet)
            bot_volume += amount
    return {
        "start_date_utc": str(start), "end_date_utc_exclusive": str(end),
        "coverage": [coverage[d] for d in days],
        "total_markets": sum(coverage[d]["completed_markets"] for d in days),
        "volume_micro_usdc": sum(volumes.values()), "unique_wallets": len(wallets),
        "suspected_bot_wallets": len(bot_wallets), "suspected_bot_volume_micro_usdc": bot_volume,
        "daily_volume_micro_usdc": volumes,
        "asset_volume_micro_usdc": dict(sorted(assets.items(), key=lambda pair: (-pair[1], pair[0]))),
    }


def changes(current: dict, previous: dict | None, field: str) -> tuple[str, str]:
    if previous is None:
        return "N/A", "prior week unavailable"
    if previous[field] == 0:
        return "N/A", "prior week = 0"
    return daily.percent_text(current[field] - previous[field], previous[field], change=True), "vs prior week"


def bot_share_change(current: dict, previous: dict | None) -> tuple[str, str]:
    if previous is None:
        return "N/A", "prior week unavailable"
    if not current["volume_micro_usdc"] or not previous["volume_micro_usdc"]:
        return "N/A", "zero volume denominator"
    delta = ((Decimal(current["suspected_bot_volume_micro_usdc"]) / current["volume_micro_usdc"]
              - Decimal(previous["suspected_bot_volume_micro_usdc"]) / previous["volume_micro_usdc"]) * 100)
    delta = delta.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (f"{delta:+.2f} pp" if delta else "0.00 pp"), "vs prior week"


def millions(value: int) -> str:
    return str((Decimal(value) / 10**12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def render_tweet(current: dict, previous: dict | None) -> str:
    start = date.fromisoformat(current["start_date_utc"])
    last = date.fromisoformat(current["end_date_utc_exclusive"]) - timedelta(days=1)
    amount = current["volume_micro_usdc"]
    volume = millions(amount) + "M" if amount >= 10**12 else daily.amount_text(amount)

    def comparison(field: str) -> str:
        value, _ = changes(current, previous, field)
        if value != "N/A":
            return f"{value} WoW"
        return "N/A: no prior week" if previous is None else "N/A: zero base"

    volume_change = comparison("volume_micro_usdc")
    wallet_change = comparison("unique_wallets")
    bot_share = daily.percent_text(current["suspected_bot_volume_micro_usdc"], amount)
    if amount == 0:
        bot_share = "N/A (zero volume)"
    lines = [
        "#Polymarket Crypto Up/Down | Weekly",
        f"{start.isoformat()}/{last.isoformat()} UTC",
        f"{volume} USDC wallet volume ({volume_change})",
        f"{current['unique_wallets']:,} unique wallets ({wallet_change})",
        f"{current['suspected_bot_wallets']:,} suspected bot wallets: {bot_share} of volume",
        "Market end dates; buys+sells. Bot rules in chart.",
    ]
    # 本模板只有 ASCII 数字与文字，无链接；X 加权长度等于字符数。
    # 长数字占用更多空间时，将机器人钱包数与详细口径留在配图，保留完整核心指标。
    if len("\n".join(lines) + "\n") > 280:
        lines[4] = f"Suspected bots: {bot_share} of volume"
        lines[5] = "Details in chart."
    return "\n".join(lines) + "\n"


class Canvas(daily.PosterCanvas):
    def text(self, text, x, y, size, *, weight=500, color=MUTED, width=None):
        return super().text(text, x, y, size, weight=weight, color=color, width=width)

    def line(self, xy, color=GOLD, width=1):
        self.draw.line(tuple(round(v * daily.SCALE) for v in xy), fill=color, width=width)

    def rect(self, xy, color):
        self.draw.rectangle(tuple(round(v * daily.SCALE) for v in xy), fill=color)

    def change(self, value, caption, x, y, *, width, size):
        color = daily.GREEN if value.startswith("+") else "#ffabb2" if value.startswith("-") else MUTED
        bounds = self.text(value, x, y, size, color=color, weight=700, width=width)
        self.text(caption, bounds[2]+12, y+size-23, 22, width=1032-bounds[2]-12)


def render(current: dict, previous: dict | None):
    c = Canvas(BACKGROUND)
    start = date.fromisoformat(current["start_date_utc"])
    last = start + timedelta(days=6)
    period = f"{start:%b %d} – {last:%b %d, %Y} · UTC".upper()
    c.text("POLYMARKET / CRYPTO UP/DOWN", 101, 53, 22, weight=650, color=GOLD)
    c.text("WEEKLY REPORT", 98, 84, 60, weight=800, color=WHITE)
    c.text(period, 101, 151, 26, color=WHITE)
    c.line((96, 193, 1026, 193))
    c.text("WALLET TRADING VOLUME", 101, 212, 23, weight=650, color=WHITE)
    box = c.text(daily.amount_text(current["volume_micro_usdc"]), 96, 252, 82,
                 weight=780, color=WHITE, width=784)
    c.text("USDC", box[2]+15, 289, 30, color=MUTED, width=137)
    c.change(*changes(current, previous, "volume_micro_usdc"), 101, 336, width=300, size=37)
    c.line((96, 388, 1026, 388))
    c.text("WEEKLY UNIQUE WALLETS", 101, 407, 23, weight=650, color=WHITE)
    box = c.text(f"{current['unique_wallets']:,}", 96, 447, 73, weight=780, color=WHITE, width=337)
    c.change(*changes(current, previous, "unique_wallets"), box[2]+28, 473, width=240, size=35)
    c.line((96, 529, 1026, 529))

    c.text("7-DAY VOLUME", 101, 549, 23, weight=650, color=WHITE)
    c.text("M USDC", 943, 551, 20, width=89)
    amounts = list(current["daily_volume_micro_usdc"].values())
    maximum = max(amounts)
    ceiling = max(5, math.ceil(maximum / 10**12 / 5) * 5)
    bottom, height = 808, 201
    for i in range(6):
        value = ceiling * i / 5
        y = bottom - height * i / 5
        c.line((113, y, 1030, y), color="#63402a")
        c.text(f"{value:g}", 66, y-8, 17, width=39)
    c.line((113, bottom-height, 113, bottom), color=MUTED)
    c.line((113, bottom, 1030, bottom), color=MUTED)
    for i, amount in enumerate(amounts):
        x = 137 + i*133
        y = bottom - height * (amount / 10**12) / ceiling
        if amount:
            c.rect((x, y, x+76, bottom-1), GOLD if amount == maximum else COLORS[1])
        label = millions(amount)
        face = daily.font(round(22*daily.SCALE), 650)
        label_width = face.getlength(label)/daily.SCALE
        c.text(label, x+38-label_width/2, y-29, 22, weight=650, color=WHITE, width=120)
        c.text((start+timedelta(days=i)).strftime("%a").upper(), x+10, bottom+15, 18, width=76)
        if amount == maximum and maximum:
            c.text("PEAK", x+10, y-52, 16, weight=700, color=GOLD, width=76)
    c.line((96, 860, 1026, 860))

    c.text("VOLUME BY ASSET", 101, 880, 23, weight=650, color=WHITE)
    ranked = list(current["asset_volume_micro_usdc"].items())
    groups = ranked[:3] + ([("OTHER", sum(v for _, v in ranked[3:]))] if len(ranked) > 3 else [])
    total, consumed = current["volume_micro_usdc"], 0
    for i, (coin, amount) in enumerate(groups):
        left = 102 + 922 * consumed / total if total else 102
        consumed += amount
        right = 102 + 922 * consumed / total if total else 102
        if total and right > left:
            c.rect((left, 916, right, 949), COLORS[i])
        x = 102 + i*235
        c.rect((x, 969, x+11, 980), COLORS[i])
        c.text(f"{coin} {daily.percent_text(amount,total)}", x+20, 963, 21,
               weight=650, color=WHITE, width=210)
    c.line((96, 1010, 1026, 1010))

    c.text("SUSPECTED BOT ACTIVITY", 101, 1030, 23, weight=650, color=WHITE)
    share = daily.percent_text(current["suspected_bot_volume_micro_usdc"], total)
    box = c.text(share, 96, 1069, 74, weight=780, color=WHITE, width=366)
    c.text("of weekly volume", box[2]+18, 1080, 22, width=350)
    c.change(*bot_share_change(current, previous), box[2]+18, 1111, width=250, size=28)
    box = c.text(f"{current['suspected_bot_wallets']:,}", 98, 1160, 51, weight=750, color=WHITE, width=231)
    c.text("wallets", box[2]+12, 1181, 23, width=118)
    c.line((451, 1158, 451, 1213), color=MUTED)
    bot_amount = millions(current["suspected_bot_volume_micro_usdc"]) + "M"
    box = c.text(bot_amount, 480, 1160, 51, weight=750, color=WHITE, width=386)
    c.text("USDC", box[2]+15, 1181, 23, width=119)
    c.line((96, 1240, 1026, 1240))
    # 延续日报的小字衬底处理，避免背景金色光弧穿过口径说明。
    c.rect((96, 1250, 1030, 1354), "#270b12")
    footnotes = [
        "UTC market end dates; includes all trades in those markets.",
        "Volume = buys + sells. Wallets deduplicated across the week.",
        "Bots flagged daily: mean gap ≤60s, span ≥90min; weekly wallet union.",
        "Bot volume sums flagged wallet-days. Behavioral estimate, not identity.",
    ]
    for i, line in enumerate(footnotes):
        c.text(line, 102, 1258+i*24, 17, weight=450, color=MUTED, width=922)
    return c.image, c.text_boxes


def previous_week_start(today: date) -> date:
    return today - timedelta(days=today.weekday()+7)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--week-start", type=date.fromisoformat, help="周一起始日，UTC；默认上一个完整周")
    p.add_argument("--db", type=Path, default=daily.daily.core.DEFAULT_DB)
    p.add_argument("--output-dir", type=Path, default=ROOT / "outputs/posters")
    args = p.parse_args(argv)
    if args.week_start is None:
        args.week_start = previous_week_start(datetime.now(timezone.utc).date())
    print(f"目标统计周：{args.week_start} 至 {args.week_start+timedelta(days=6)}（UTC）。", flush=True)
    db = sqlite3.connect(args.db.resolve().as_uri()+"?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        current = load_week(db, args.week_start)
        print("本周汇总完成。", flush=True)
        previous = load_week(db, args.week_start-timedelta(days=7), required=False)
        print("前周汇总完成。", flush=True)
    except daily.IncompleteDay as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        db.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"updown_weekly_{args.week_start}_{args.week_start+timedelta(days=6)}"
    image, _ = render(current, previous)
    image.save(args.output_dir / f"{stem}.png")
    payload = {
        "date_basis": "market_end_utc", "source_database": str(args.db),
        "current": current, "previous": previous,
        "bot_rule": {"max_mean_interval_seconds": daily.MAX_INTERVAL_SECONDS,
                     "min_span_minutes": daily.MIN_SPAN_MINUTES,
                     "classification": "daily", "weekly_wallets": "union_of_daily_flagged_wallets",
                     "weekly_volume": "sum_of_daily_flagged_wallet_volume"},
        "display": {"volume_change": changes(current, previous, "volume_micro_usdc"),
                    "wallet_change": changes(current, previous, "unique_wallets"),
                    "bot_share_change": bot_share_change(current, previous)},
    }
    (args.output_dir / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n")
    tweet_path = args.output_dir / f"{stem}_tweet.md"
    tweet_path.write_text(render_tweet(current, previous), encoding="utf-8")
    print(f"海报：{args.output_dir / f'{stem}.png'}\n数据：{args.output_dir / f'{stem}.json'}\n推文：{tweet_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
