#!/usr/bin/env python3
"""从本地完整数据生成上一个 UTC 自然月的海报、月环比和英文推文。"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import polymarket_updown_poster as daily
import polymarket_updown_period as period

ROOT = daily.ROOT
BACKGROUND = ROOT / "assets/posters/updown_monthly_background_v1.png"
PAPER, INK, GREEN, MUTED = "#f4f2e9", "#202923", "#1f5d42", "#5d6b60"
LINE, NEGATIVE = "#c7d2c7", "#a6463d"
COLORS = ("#1f5d42", "#568166", "#87a68b", "#c4d2bd")


def month_start(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("月份格式为 YYYY-MM") from exc


def next_month(start: date) -> date:
    return (start.replace(day=28)+timedelta(days=4)).replace(day=1)


def previous_month_start(today: date) -> date:
    return (today.replace(day=1)-timedelta(days=1)).replace(day=1)


def days_in_period(data: dict) -> int:
    return (date.fromisoformat(data["end_date_utc_exclusive"])-date.fromisoformat(data["start_date_utc"])).days


def average_change(current: dict, previous: dict | None) -> tuple[str, str]:
    if previous is None or previous["volume_micro_usdc"] == 0:
        return period.changes(current,previous,"volume_micro_usdc","month")
    before = previous["volume_micro_usdc"]*days_in_period(current)
    now = current["volume_micro_usdc"]*days_in_period(previous)
    return daily.percent_text(now-before,before,change=True), "vs prior month"


def display_values(current: dict, previous: dict | None) -> dict:
    return {
        "volume_change":period.changes(current,previous,"volume_micro_usdc","month"),
        "wallet_change":period.changes(current,previous,"unique_wallets","month"),
        "average_daily_volume":daily.amount_text(Decimal(current["volume_micro_usdc"])/days_in_period(current)),
        "average_daily_volume_change":average_change(current,previous),
        "bot_share":daily.percent_text(current["suspected_bot_volume_micro_usdc"],current["volume_micro_usdc"]),
        "bot_share_change":period.bot_share_change(current,previous,"month"),
    }


def scope_changes(db: sqlite3.Connection, start: date, end: date) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT series_slug,first_end_time FROM updown_target_series "
        "WHERE date(first_end_time)>=? AND date(first_end_time)<? ORDER BY first_end_time,series_slug",
        (str(start),str(end)))]


def scope_note(changes: list[dict]) -> str:
    groups = sorted({(row["series_slug"].split("-",1)[0].upper(),date.fromisoformat(row["first_end_time"][:10]).strftime("%b %Y"))
                     for row in changes})
    return "Scope: " + "; ".join(f"{coin} added during {month}" for coin,month in groups) + "." if groups else ""


def render_tweet(current: dict, previous: dict | None, scope: list[dict]) -> str:
    start = date.fromisoformat(current["start_date_utc"])
    prior = previous_month_start(start)
    amount = current["volume_micro_usdc"]
    volume = period.millions(amount)+"M" if amount >= 10**12 else daily.amount_text(amount)
    values = display_values(current,previous)

    def comparison(name):
        value = values[name][0]
        if value != "N/A":
            return value+" MoM"
        return "N/A: no prior month" if previous is None else "N/A: zero base"

    share = values["bot_share"] if amount else "N/A (zero volume)"
    lines = [
        "#Polymarket Crypto Up/Down | Monthly",
        f"{start:%b %Y} vs {prior:%b %Y} UTC",
        f"{volume} USDC wallet volume ({comparison('volume_change')})",
        f"{current['unique_wallets']:,} unique wallets ({comparison('wallet_change')})",
        f"Suspected bots: {share} of volume",
        "Market end dates; buys+sells. Bot rules in chart.",
    ]
    note = scope_note(scope)
    if note:
        lines.append(note)
    if len("\n".join(lines)+"\n") > 280:
        lines[0] = "#Polymarket Crypto Up/Down"
        lines[5] = "Details in chart."
    return "\n".join(lines)+"\n"


class Canvas(daily.PosterCanvas):
    def text(self, text, x, y, size, *, weight=500, color=MUTED, width=None):
        return super().text(text,x,y,size,weight=weight,color=color,width=width)

    def line(self, xy, color=LINE, width=1):
        self.draw.line(tuple(round(v*daily.SCALE) for v in xy),fill=color,width=width)

    def rect(self, xy, color):
        self.draw.rectangle(tuple(round(v*daily.SCALE) for v in xy),fill=color)

    def change(self, value, caption, x, y, *, width, size):
        color = GREEN if value.startswith("+") else NEGATIVE if value.startswith("-") else MUTED
        box = self.text(value,x,y,size,weight=700,color=color,width=width)
        self.text(caption,box[2]+12,y+size-19,18,width=1030-box[2]-12)


def render(current: dict, previous: dict | None, scope: list[dict]):
    c = Canvas(BACKGROUND)
    start = date.fromisoformat(current["start_date_utc"])
    last = date.fromisoformat(current["end_date_utc_exclusive"])-timedelta(days=1)
    prior = previous_month_start(start)
    values = display_values(current,previous)
    c.text("POLYMARKET / CRYPTO UP/DOWN",86,47,20,weight=650,color=GREEN)
    c.text("MONTHLY REPORT",81,84,65,weight=800,color=INK,width=955)
    c.text(f"{start:%B %Y}".upper(),86,163,27,weight=650,color=GREEN)
    c.text(f"{start:%b %d} – {last:%b %d} UTC  /  VS {prior:%b %Y}".upper(),490,170,20,width=543)
    c.line((84,211,1034,211),GREEN,2)

    c.text("WALLET TRADING VOLUME",87,234,23,weight=650,color=INK)
    box = c.text(daily.amount_text(current["volume_micro_usdc"]),81,274,77,weight=780,color=GREEN,width=800)
    c.text("USDC",box[2]+14,309,28,width=1034-box[2]-14)
    c.change(*values["volume_change"],87,365,width=305,size=34)
    c.line((84,415,1034,415))

    c.text("MONTHLY UNIQUE WALLETS",87,434,22,weight=650,color=INK,width=457)
    c.text("AVERAGE DAILY VOLUME",607,434,22,weight=650,color=INK,width=427)
    c.text(f"{current['unique_wallets']:,}",82,476,55,weight=780,color=INK,width=438)
    average = Decimal(current["volume_micro_usdc"])/days_in_period(current)
    avg = period.millions(average)+"M USDC" if average >= 10**12 else daily.amount_text(average)+" USDC"
    c.text(avg,601,480,43,weight=750,color=INK,width=433)
    # 两栏变化说明分别占一行，避免大数值挤入相邻栏目。
    for key,x in (("wallet_change",87),("average_daily_volume_change",607)):
        change,caption = values[key]
        color = GREEN if change.startswith("+") else NEGATIVE if change.startswith("-") else MUTED
        box = c.text(change,x,544,25,weight=700,color=color,width=170)
        c.text(caption,box[2]+10,549,17,width=x+420-box[2]-10)
    c.text(f"{current['total_markets']:,} MARKETS  /  {days_in_period(current)} CALENDAR DAYS",87,587,19,weight=600,width=940)
    c.line((84,626,1034,626))

    c.text("DAILY WALLET VOLUME",87,648,22,weight=650,color=INK)
    c.text("M USDC",938,650,19,width=96)
    amounts = list(current["daily_volume_micro_usdc"].values())
    maximum = max(amounts)
    ceiling = max(5,math.ceil(maximum/10**12/5)*5)
    left,right,bottom,height = 122,1032,869,169
    for i in range(5):
        value = ceiling*i/4
        y = bottom-height*i/4
        c.line((left,y,right,y))
        c.text(f"{value:g}",80,y-8,16,width=35)
    step = (right-left)/len(amounts)
    for i,amount in enumerate(amounts):
        x = left+(i+.17)*step
        top = bottom-height*(amount/10**12)/ceiling
        if amount:
            c.rect((x,min(top,bottom-1),x+.66*step,bottom-1),GREEN if amount==maximum else COLORS[2])
        if i==0 or (i+1)%5==0 or i==len(amounts)-1:
            c.text(f"{i+1:02}",left+i*step+1,bottom+14,16,width=step)
    peak = amounts.index(maximum)+1
    if maximum:
        c.text(f"PEAK {peak:02} {start:%b}: {period.millions(maximum)}M".upper(),590,674,17,weight=650,color=GREEN,width=442)
    c.line((84,916,1034,916))

    c.text("VOLUME BY ASSET",87,937,22,weight=650,color=INK)
    ranked = list(current["asset_volume_micro_usdc"].items())
    groups = ranked[:3]+([("OTHER",sum(v for _,v in ranked[3:]))] if len(ranked)>3 else [])
    total,consumed = current["volume_micro_usdc"],0
    for i,(coin,amount) in enumerate(groups):
        x = 87+947*consumed/total if total else 87
        consumed += amount
        right_edge = 87+947*consumed/total if total else 87
        if total and right_edge>x:
            c.rect((x,976,right_edge,1001),COLORS[i])
        x = 87+i*239
        c.rect((x,1024,x+10,1034),COLORS[i])
        c.text(f"{coin} {daily.percent_text(amount,total)}",x+19,1018,20,weight=650,color=INK,width=213)
    c.line((84,1065,1034,1065))

    c.text("SUSPECTED BOT ACTIVITY",87,1086,22,weight=650,color=INK)
    box = c.text(values["bot_share"],82,1127,59,weight=780,color=GREEN,width=335)
    c.text("of monthly volume",box[2]+22,1132,21,width=560)
    c.change(*values["bot_share_change"],box[2]+22,1167,width=226,size=25)
    c.text(f"{current['suspected_bot_wallets']:,} wallets",87,1220,27,weight=700,color=INK,width=413)
    c.text(period.millions(current["suspected_bot_volume_micro_usdc"])+"M USDC",607,1220,27,
           weight=700,color=INK,width=427)
    c.line((84,1267,1034,1267))
    footnotes = [
        "UTC market end dates; all trades in selected markets. Volume = buys + sells.",
        "Wallets deduplicated across the month; daily average uses calendar days.",
        "Bots flagged daily: mean gap <=60s, span >=90min; monthly wallet union.",
        "Bot volume sums flagged wallet-days; behavioral estimate, not identity.",
    ]
    if scope:
        footnotes.append(scope_note(scope))
    for i,line in enumerate(footnotes):
        c.text(line,87,1281+i*22,16,weight=500 if i<4 else 650,color=MUTED if i<4 else GREEN,width=947)
    return c.image,c.text_boxes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month",type=month_start,help="UTC 自然月 YYYY-MM；默认上一个完整月")
    parser.add_argument("--db",type=Path,default=daily.daily.core.DEFAULT_DB)
    parser.add_argument("--output-dir",type=Path,default=ROOT/"outputs/posters")
    args = parser.parse_args(argv)
    start = args.month or previous_month_start(datetime.now(timezone.utc).date())
    end,prior = next_month(start),previous_month_start(start)
    print(f"目标统计月：{start:%Y-%m}（UTC）；比较月份：{prior:%Y-%m}。",flush=True)
    db = sqlite3.connect(args.db.resolve().as_uri()+"?mode=ro",uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        current = period.load_period(db,start,end)
        print("目标月汇总完成。",flush=True)
        previous = period.load_period(db,prior,start,required=False)
        print("前月汇总完成。",flush=True)
        scope = scope_changes(db,prior,end)
    except daily.IncompleteDay as exc:
        print(exc,file=sys.stderr)
        return 2
    finally:
        db.close()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    stem = f"updown_monthly_{start:%Y-%m}"
    image,_ = render(current,previous,scope)
    image.save(args.output_dir/f"{stem}.png")
    payload = {
        "date_basis":"market_end_utc", "current":current, "previous":previous,
        "scope_changes":scope,
        "bot_rule":{"max_mean_interval_seconds":daily.MAX_INTERVAL_SECONDS,
                    "min_span_minutes":daily.MIN_SPAN_MINUTES,"classification":"daily",
                    "monthly_wallets":"union_of_daily_flagged_wallets",
                    "monthly_volume":"sum_of_daily_flagged_wallet_volume"},
        "display":display_values(current,previous),
    }
    (args.output_dir/f"{stem}.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (args.output_dir/f"{stem}_tweet.md").write_text(render_tweet(current,previous,scope),encoding="utf-8")
    print(f"海报：{args.output_dir/f'{stem}.png'}\n数据：{args.output_dir/f'{stem}.json'}\n推文：{args.output_dir/f'{stem}_tweet.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
