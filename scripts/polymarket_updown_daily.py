#!/usr/bin/env python3
"""采集 UTC 指定日期结束的 38 个加密货币 Up/Down 系列市场的全部成交。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polymarket_btc5m_daily as core

# 范围来自 reports/crypto_updown_market_types_2026-09-17.md。
# 小时线和日线使用实际系列标识，不能全部由币种缩写拼接。
TARGET_SERIES = {
    **{f"{coin}-up-or-down-{period}": count
       for coin in ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec")
       for period, count in (("5m", 288), ("15m", 96), ("4h", 6))},
    **{f"{coin}-up-or-down-hourly": 24
       for coin in ("btc", "eth", "solana", "xrp", "doge", "bnb", "hype")},
    **{f"{coin}-up-or-down-daily": 1
       for coin in ("btc", "eth", "solana", "xrp", "dogecoin", "bnb", "hype")},
}
# 官方系列创建信息及首期市场详情见 docs/updown_daily.md 的历史范围说明。
# 边界是第一期市场结束时间，避免把上线首日的未开设时段算作漏采。
SERIES_FIRST_END_UTC = {
    "zec-up-or-down-5m": "2026-08-04T21:40:00+00:00",
    "zec-up-or-down-15m": "2026-08-04T21:45:00+00:00",
    "zec-up-or-down-4h": "2026-08-05T00:00:00+00:00",
}
GAMMA_SERIES = "https://gamma-api.polymarket.com/series"
GAMMA_EVENTS_KEYSET = "https://gamma-api.polymarket.com/events/keyset"
GAMMA_EVENT_SLUG = "https://gamma-api.polymarket.com/events/slug/"
EVENT_PAGE_SIZE = 100
ENGLISH_MONTHS = ("january", "february", "march", "april", "may", "june",
                  "july", "august", "september", "october", "november", "december")


def connect_database(path: Path) -> sqlite3.Connection:
    db = core.connect_database(path)
    # 目录只在此定义；持久化视图让 sqlite3 与离线报告使用同一目录。
    values = ", ".join(
        f"('{slug}', {count}, " + (f"'{SERIES_FIRST_END_UTC[slug]}'" if slug in SERIES_FIRST_END_UTC else "NULL") + ")"
        for slug, count in TARGET_SERIES.items())
    db.executescript(
        "DROP VIEW IF EXISTS updown_target_series; "
        "CREATE VIEW updown_target_series(series_slug, expected_markets, first_end_time) AS VALUES " + values + ";"
    )
    db.executescript((core.ROOT / "sql/updown_daily.sql").read_text())
    return db


def utc_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), timezone.utc)


def expected_slots(series: str, day: date):
    """给出已知系列计划中的结束时点；详情返回的市场元数据决定最终归属。"""
    count = TARGET_SERIES[series]
    step = timedelta(seconds=86400 // count)
    ended = midnight(day)
    if count == 1:
        ended = datetime.combine(day, datetime.min.time(), ZoneInfo("America/New_York")).replace(
            hour=12).astimezone(timezone.utc)
    first = SERIES_FIRST_END_UTC.get(series)
    while ended < midnight(day + timedelta(days=1)):
        if first is None or ended >= utc_time(first):
            yield ended
        ended += step


def market_slug(series: str, ended: datetime) -> str:
    coin, period = series.split("-", 1)[0], series.rsplit("-", 1)[1]
    if period in ("5m", "15m", "4h"):
        began = ended - timedelta(seconds=86400 // TARGET_SERIES[series])
        return f"{coin}-updown-{period}-{int(began.timestamp())}"
    coin = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "doge": "dogecoin"}.get(coin, coin)
    stamp = ended if period == "daily" else ended - timedelta(hours=1)
    local = stamp.astimezone(ZoneInfo("America/New_York"))
    stamp_text = f"{ENGLISH_MONTHS[local.month-1]}-{local.day}-{local.year}"
    if period == "daily":
        return f"{coin}-up-or-down-on-{stamp_text}"
    hour = local.hour % 12 or 12
    return f"{coin}-up-or-down-{stamp_text}-{hour}{'am' if local.hour < 12 else 'pm'}-et"


def save_event(db: sqlite3.Connection, event: dict, series: str, start: date, end: date) -> None:
    membership = {s["slug"] for s in event.get("series", [])}
    membership.add(event.get("seriesSlug"))
    if series not in membership:
        return
    for market in event["markets"]:
        ended = utc_time(market["endDate"])
        # Gamma 时间边界可能包含上限，必须在市场层落实半开区间。
        if not midnight(start) <= ended < midnight(end):
            continue
        began = utc_time(market["eventStartTime"])
        slug = market["slug"]
        db.execute(
            "INSERT INTO markets(slug,date_utc,market_id,condition_id,event_id,title,"
            "series_slug,event_start_time,end_time) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET market_id=excluded.market_id, "
            "condition_id=excluded.condition_id,event_id=excluded.event_id, "
            "title=excluded.title,series_slug=excluded.series_slug, "
            "event_start_time=excluded.event_start_time,end_time=excluded.end_time",
            (slug, began.date().isoformat(), str(market["id"]), market["conditionId"],
             str(event["id"]), market["question"], series, began.isoformat(), ended.isoformat()),
        )
        db.execute(
            "INSERT INTO collection_progress(market_slug,query_params) VALUES (?,?) "
            "ON CONFLICT(market_slug) DO UPDATE SET query_params=excluded.query_params",
            (slug, json.dumps(core.trade_params(market["conditionId"]), sort_keys=True)),
        )


def discover_series(db: sqlite3.Connection, client: core.Client, series: str,
                    start: date, end: date) -> None:
    series_rows = client.get(GAMMA_SERIES, {"slug": series})
    series_id = next(row["id"] for row in series_rows if row["slug"] == series)
    cursor = None
    while True:
        params = {
            "series_id": series_id, "end_date_min": midnight(start).isoformat(),
            "end_date_max": midnight(end).isoformat(), "limit": EVENT_PAGE_SIZE,
            "order": "id", "ascending": "true",
        }
        if cursor:
            params["after_cursor"] = cursor
        page = client.get(GAMMA_EVENTS_KEYSET, params)
        with db:
            for event in page["events"]:
                save_event(db, event, series, start, end)
        cursor = page.get("next_cursor")
        if not cursor:
            return


def discover_missing_markets(db: sqlite3.Connection, client: core.Client, start: date, end: date) -> None:
    """补查列表接口未返回的预期时段；404 等失败保留为覆盖缺口。"""
    existing = {}
    for row in db.execute(
        "SELECT series_slug,end_time FROM markets WHERE date(end_time)>=? AND date(end_time)<? "
        "AND condition_id IS NOT NULL", (str(start), str(end))):
        ended = utc_time(row["end_time"])
        existing.setdefault((row["series_slug"], ended.date()), set()).add(ended)
    day = start
    while day < end:
        for series in TARGET_SERIES:
            for ended in expected_slots(series, day):
                if ended in existing.get((series, day), set()):
                    continue
                slug = market_slug(series, ended)
                try:
                    event = client.get(GAMMA_EVENT_SLUG + slug, {})
                    with db:
                        save_event(db, event, series, start, end)
                    print(f"详情补查 {slug} 完成", flush=True)
                except (core.FetchError, KeyError, TypeError, ValueError) as exc:
                    print(f"详情补查 {slug} 未完成：{exc}", file=sys.stderr, flush=True)
        day += timedelta(days=1)


def coverage(db: sqlite3.Connection, day: str) -> dict:
    row = db.execute("SELECT * FROM updown_coverage WHERE date_utc=?", (day,)).fetchone()
    return dict(row) if row else dict(date_utc=day, expected_markets=sum(
                                     sum(1 for _ in expected_slots(s, date.fromisoformat(day))) for s in TARGET_SERIES),
                                     discovered_markets=0, completed_markets=0,
                                     committed_pages=0, is_complete=0)


def collect(db: sqlite3.Connection, client: core.Client, start: date, end: date) -> None:
    for index, series in enumerate(TARGET_SERIES, 1):
        try:
            discover_series(db, client, series, start, end)
            print(f"发现 [{index}/{len(TARGET_SERIES)}] {series} 完成", flush=True)
        except (core.FetchError, KeyError, TypeError, ValueError, StopIteration) as exc:
            print(f"发现 {series} 未完成：{exc}", file=sys.stderr, flush=True)
    discover_missing_markets(db, client, start, end)
    pending = db.execute(
        "SELECT m.slug FROM markets m JOIN updown_target_series s ON s.series_slug=m.series_slug "
        "JOIN collection_progress p ON p.market_slug=m.slug "
        "WHERE date(m.end_time)>=? AND date(m.end_time)<? AND m.condition_id IS NOT NULL "
        "AND p.completed_at IS NULL ORDER BY m.series_slug,m.end_time,m.slug",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    for index, row in enumerate(pending, 1):
        slug = row["slug"]
        try:
            core.collect_market(db, client, slug)
            print(f"成交 [{index}/{len(pending)}] {slug} 完成", flush=True)
        except (core.FetchError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            with db:
                db.execute("UPDATE collection_progress SET last_error=? WHERE market_slug=?", (str(exc), slug))
            print(f"成交 [{index}/{len(pending)}] {slug} 未完成：{exc}", file=sys.stderr, flush=True)


def report_day(db: sqlite3.Connection, day: str, output: Path) -> bool:
    output.mkdir(parents=True, exist_ok=True)
    status = coverage(db, day)
    summary_path = output / f"updown_daily_{day}.csv"
    top_path = output / f"updown_top200_{day}.csv"
    lines = [f"# Polymarket 加密货币 Up/Down：{day}（UTC 结束日）", "",
             "范围：文档列出的 38 个系列。按市场 endDate 所属 UTC 日归属，包含这些市场的",
             "全部提前成交和跨日成交，不按成交时间截断。原 BTC 开始日统计保持独立。",
             "钱包成交额为买入支出加卖出收入；每条 price × size 以 ROUND_HALF_UP 舍入至微 USDC 后求和。",
             "查询 /v2/trades：taker_only=false，filter_type=TOKENS，filter_amount=1e-18，limit=1000。",
             "完整指采集时以上条件下官方接口三年窗口内的全部分页，未作全量链上对账。", "",
             f"采集覆盖：发现 {status['discovered_markets']}/{status['expected_markets']} 个市场；"
             f"完成 {status['completed_markets']} 个；已提交 {status['committed_pages']} 页。", ""]
    if not status["is_complete"]:
        summary_path.unlink(missing_ok=True)
        top_path.unlink(missing_ok=True)
        lines += ["采集尚有缺口，暂不生成金额统计。", ""]
        rows = db.execute("SELECT * FROM updown_series_coverage WHERE date_utc=? AND NOT is_complete "
                          "ORDER BY series_slug", (day,)).fetchall()
        if not rows:
            lines.append("尚未发现当日目标市场。")
        for row in rows:
            lines.append(f"- {row['series_slug']}：发现 {row['discovered_markets']}，"
                         f"完成 {row['completed_markets']}，预期 {row['expected_markets']}。")
        for row in db.execute(
            "SELECT m.slug,p.last_error FROM markets m "
            "JOIN updown_target_series s ON s.series_slug=m.series_slug "
            "JOIN collection_progress p ON p.market_slug=m.slug "
            "WHERE date(m.end_time)=? AND p.completed_at IS NULL ORDER BY m.slug", (day,)
        ):
            lines.append(f"- {row['slug']}：{row['last_error'] or '成交分页尚未完成'}")
    else:
        summary = dict(db.execute("SELECT * FROM updown_daily_summary WHERE date_utc=?", (day,)).fetchone())
        summary.update(status)
        summary["wallet_total_usdc"] = core.amount_text(summary["wallet_total_micro_usdc"])
        summary["top200_usdc"] = core.amount_text(summary["top200_micro_usdc"])
        top = [dict(row) for row in db.execute(
            "SELECT * FROM updown_wallet_ranked WHERE date_utc=? AND wallet_rank<=200 ORDER BY wallet_rank", (day,))]
        for row in top:
            row["amount_usdc"] = core.amount_text(row["amount_micro_usdc"])
        core.write_csv(summary_path, list(summary), [summary])
        core.write_csv(top_path, ["date_utc", "wallet", "amount_micro_usdc", "wallet_rank", "amount_usdc"], top)
        share = "空（钱包成交总额为零）" if summary["top200_share"] is None else f"{summary['top200_share']:.6%}"
        lines += [f"- 去重钱包数：{summary['unique_wallets']}",
                  f"- 钱包成交总额：{summary['wallet_total_usdc']} USDC",
                  f"- 前 200 钱包合计：{summary['top200_usdc']} USDC；占比：{share}", "",
                  f"日汇总：{summary_path.name}；前 200 钱包：{top_path.name}。"]
    content = "\n".join(lines) + "\n"
    (output / f"updown_daily_{day}.md").write_text(content, encoding="utf-8")
    print(content)
    return bool(status["is_complete"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cp = commands.add_parser("collect", help="经香港代理下载指定 UTC 日期结束的市场全部成交")
    cp.add_argument("--start-date", type=date.fromisoformat, required=True, help="包含该 UTC 结束日")
    cp.add_argument("--end-date", type=date.fromisoformat, required=True, help="不包含该 UTC 结束日")
    cp.add_argument("--proxy", required=True, help="本机香港代理 URL")
    rp = commands.add_parser("report", help="完全离线生成按市场结束日统计的报告")
    rp.add_argument("--date", type=date.fromisoformat, required=True)
    rp.add_argument("--output-dir", type=Path, default=core.ROOT / "outputs/updown")
    for sub in (cp, rp):
        sub.add_argument("--db", type=Path, default=core.DEFAULT_DB)
    args = parser.parse_args(argv)
    if args.command == "report":
        db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            return 0 if report_day(db, args.date.isoformat(), args.output_dir) else 2
        finally:
            db.close()
    if args.start_date >= args.end_date:
        parser.error("--end-date 必须晚于 --start-date")
    if not args.proxy.strip():
        parser.error("--proxy 必须指定本机香港代理 URL")
    started = time.monotonic()
    db = connect_database(args.db)
    client = core.Client(args.proxy)
    try:
        collect(db, client, args.start_date, args.end_date)
        complete = True
        day = args.start_date
        while day < args.end_date:
            status = coverage(db, day.isoformat())
            print(f"{day}（UTC 结束日）：{json.dumps(status, ensure_ascii=False)}", flush=True)
            complete = complete and bool(status["is_complete"])
            day += timedelta(days=1)
        print(f"数据库 {args.db.stat().st_size} 字节；本次耗时 {time.monotonic()-started:.2f} 秒")
        return 0 if complete else 2
    except KeyboardInterrupt:
        print("采集已中断；已提交页和游标保留，可用同一命令续传。", file=sys.stderr)
        return 130
    finally:
        client.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
