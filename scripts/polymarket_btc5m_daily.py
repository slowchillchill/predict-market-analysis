#!/usr/bin/env python3
"""采集 BTC 5 分钟逐笔成交，以 SQLite 明细生成离线日统计。"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERIES = "btc-up-or-down-5m"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
TRADES_URL = "https://data-api.polymarket.com/v2/trades"
DEFAULT_DB = ROOT / "data/raw/btc5m.sqlite3"
# 沿用旧入口的 30 秒超时、最多 3 次请求和 0.5 * attempt 秒退避。
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
DISCOVERY_BATCH_SIZE = 48


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FetchError(Exception):
    def __init__(self, message: str, *, invalid_cursor: bool = False):
        super().__init__(message)
        self.invalid_cursor = invalid_cursor


class Client:
    def __init__(self, proxy: str):
        # 离线 report 不需要加载 requests，也不创建网络会话。
        import requests

        self.requests = requests
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": proxy, "https": proxy}
        self.session.headers["User-Agent"] = "poly-market-analysis/btc5m"

    def close(self) -> None:
        self.session.close()

    def get(self, url: str, params: dict) -> object:
        for attempt in range(1, REQUEST_RETRIES + 1):
            delay = 0.5 * attempt
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
                if response.status_code == 200:
                    # 保留接口十进制字面值，不经过二进制浮点转换。
                    return json.loads(response.text, parse_float=str)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                message = body.get("error", "") if isinstance(body, dict) else ""
                invalid_cursor = (
                    response.status_code == 400
                    and isinstance(body, dict)
                    and body.get("code") == "invalid_request"
                    and (body.get("parameter") == "cursor" or "cursor" in message.lower())
                )
                error = FetchError(
                    f"HTTP {response.status_code}: {message or url}",
                    invalid_cursor=invalid_cursor,
                )
                if response.status_code != 429 and response.status_code < 500:
                    raise error
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (self.requests.RequestException, json.JSONDecodeError) as exc:
                # 不将可能带有本机代理凭据的异常文本写入数据库或报告。
                error = FetchError(f"{url}: {type(exc).__name__}")
            if attempt == REQUEST_RETRIES:
                raise error
            time.sleep(delay)
        raise AssertionError("不可达分支")


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript((ROOT / "sql/btc5m_schema.sql").read_text())
    db.executescript((ROOT / "sql/btc5m_daily.sql").read_text())
    return db


def slots(start: date, end: date):
    current = datetime.combine(start, datetime.min.time(), timezone.utc)
    stop = datetime.combine(end, datetime.min.time(), timezone.utc)
    while current < stop:
        yield f"btc-updown-5m-{int(current.timestamp())}", current
        current += timedelta(minutes=5)


def seed_markets(db: sqlite3.Connection, start: date, end: date) -> None:
    with db:
        for slug, dt in slots(start, end):
            db.execute("INSERT OR IGNORE INTO markets(slug, date_utc) VALUES (?, ?)",
                       (slug, dt.date().isoformat()))
            db.execute("INSERT OR IGNORE INTO collection_progress(market_slug) VALUES (?)", (slug,))


def trade_params(condition: str) -> dict:
    return {"condition": condition, "limit": 1000, "taker_only": "false",
            "filter_type": "TOKENS", "filter_amount": "1e-18"}


def discover(db: sqlite3.Connection, client: Client, start: date, end: date) -> None:
    pending = db.execute(
        "SELECT slug FROM markets WHERE date_utc >= ? AND date_utc < ? "
        "AND slug GLOB 'btc-updown-5m-*' AND condition_id IS NULL ORDER BY slug", (start.isoformat(), end.isoformat())
    ).fetchall()
    for offset in range(0, len(pending), DISCOVERY_BATCH_SIZE):
        slugs = [row["slug"] for row in pending[offset:offset + DISCOVERY_BATCH_SIZE]]
        try:
            events = client.get(GAMMA_EVENTS, {"slug": slugs, "limit": len(slugs)})
            by_slug = {event["slug"]: event for event in events}
        except (FetchError, KeyError, TypeError, ValueError) as exc:
            with db:
                db.executemany("UPDATE collection_progress SET last_error = ? WHERE market_slug = ?",
                               [(f"市场发现失败：{exc}", slug) for slug in slugs])
            continue
        for slug in slugs:
            try:
                event = by_slug.get(slug)
                if event is None:
                    raise ValueError("Gamma 未返回该标准时段的市场")
                series = {s["slug"] for s in event.get("series", [])}
                series.add(event.get("seriesSlug"))
                if SERIES not in series:
                    raise ValueError("所属系列与 BTC 5 分钟系列不符")
                market = next(m for m in event["markets"] if m["slug"] == slug)
                dt = datetime.fromisoformat(market["eventStartTime"].replace("Z", "+00:00"))
                expected = datetime.fromtimestamp(int(slug.rsplit("-", 1)[1]), timezone.utc)
                if dt != expected:
                    raise ValueError("eventStartTime 与标准开始时刻不符")
                with db:
                    db.execute(
                        "UPDATE markets SET market_id=?, condition_id=?, event_id=?, title=?, "
                        "series_slug=?, event_start_time=?, end_time=? WHERE slug=?",
                        (str(market["id"]), market["conditionId"], str(event["id"]),
                         market["question"], SERIES, dt.isoformat(), market["endDate"], slug),
                    )
                    db.execute(
                        "UPDATE collection_progress SET query_params=?, last_error=NULL WHERE market_slug=?",
                        (json.dumps(trade_params(market["conditionId"]), sort_keys=True), slug),
                    )
            except (KeyError, TypeError, ValueError, StopIteration) as exc:
                with db:
                    db.execute("UPDATE collection_progress SET last_error=? WHERE market_slug=?",
                               (f"市场发现失败：{exc}", slug))


def micro_usdc(price: str, size: str) -> int:
    p, s = Decimal(price), Decimal(size)
    with localcontext() as ctx:
        ctx.prec = max(28, len(p.as_tuple().digits) + len(s.as_tuple().digits) + 8,
                       p.adjusted() + s.adjusted() + 10)
        return int((p * s * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def commit_page(db: sqlite3.Connection, slug: str, page: int, payload: dict) -> None:
    next_cursor = payload["pagination"]["next_cursor"]
    with db:
        for row_number, trade in enumerate(payload["data"]):
            price, size = str(trade["price"]), str(trade["size"])
            db.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, page, row_number, trade["proxy_wallet"].lower(), trade["transaction_hash"],
                 trade["timestamp"], trade["side"], trade["outcome"], trade["outcome_index"],
                 trade["token_id"], price, size, micro_usdc(price, size)),
            )
        db.execute(
            "UPDATE collection_progress SET committed_page=?, next_cursor=?, completed_at=?, "
            "last_error=NULL WHERE market_slug=?",
            (page, next_cursor, utc_now() if next_cursor is None else None, slug),
        )


def collect_market(db: sqlite3.Connection, client: Client, slug: str) -> None:
    while True:
        progress = db.execute("SELECT * FROM collection_progress WHERE market_slug=?", (slug,)).fetchone()
        if progress["completed_at"] is not None:
            return
        params = json.loads(progress["query_params"])
        if progress["next_cursor"] is not None:
            params["cursor"] = progress["next_cursor"]
        try:
            payload = client.get(TRADES_URL, params)
        except FetchError as exc:
            if exc.invalid_cursor and "cursor" in params:
                # 只重建当前未完成市场；旧页与旧游标同时移除。
                with db:
                    db.execute("DELETE FROM trades WHERE market_slug=?", (slug,))
                    db.execute(
                        "UPDATE collection_progress SET committed_page=0, next_cursor=NULL, "
                        "last_error=NULL WHERE market_slug=?", (slug,),
                    )
                continue
            raise
        commit_page(db, slug, progress["committed_page"] + 1, payload)


def coverage(db: sqlite3.Connection, day: str) -> dict:
    row = db.execute("SELECT * FROM btc5m_coverage WHERE date_utc=?", (day,)).fetchone()
    return dict(row) if row else dict(date_utc=day, expected_markets=288, discovered_markets=0,
                                     completed_markets=0, committed_pages=0, is_complete=0)


def collect(db: sqlite3.Connection, client: Client, start: date, end: date) -> None:
    seed_markets(db, start, end)
    discover(db, client, start, end)
    pending = db.execute(
        "SELECT m.slug FROM markets m JOIN collection_progress p ON p.market_slug=m.slug "
        "WHERE m.date_utc >= ? AND m.date_utc < ? AND m.condition_id IS NOT NULL "
        "AND m.slug GLOB 'btc-updown-5m-*' "
        "AND p.completed_at IS NULL ORDER BY m.slug", (start.isoformat(), end.isoformat()),
    ).fetchall()
    for index, row in enumerate(pending, 1):
        slug = row["slug"]
        try:
            collect_market(db, client, slug)
            print(f"[{index}/{len(pending)}] {slug} 完成", flush=True)
        except (FetchError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            with db:
                db.execute("UPDATE collection_progress SET last_error=? WHERE market_slug=?", (str(exc), slug))
            print(f"[{index}/{len(pending)}] {slug} 未完成：{exc}", file=sys.stderr, flush=True)


def amount_text(amount: int) -> str:
    return f"{Decimal(amount) / 1_000_000:.6f}"


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def report_day(db: sqlite3.Connection, day: str, output: Path) -> bool:
    output.mkdir(parents=True, exist_ok=True)
    status = coverage(db, day)
    summary_path = output / f"btc5m_daily_{day}.csv"
    top_path = output / f"btc5m_top10_{day}.csv"
    lines = [f"# Polymarket BTC 5 分钟日统计：{day}（UTC）", "",
             "日期按市场 eventStartTime 归属；提前或跨日成交仍随市场开始日统计。",
             "钱包成交总额为所有参与钱包的买入支出加卖出收入；每笔价格 × 份数，",
             "按 ROUND_HALF_UP 舍入至 0.000001 USDC 后以整数微 USDC 相加。", "",
             f"采集覆盖：发现 {status['discovered_markets']}/288 个市场，"
             f"完成 {status['completed_markets']}/288 个市场，已提交 {status['committed_pages']} 页。",
             "查询：/v2/trades，taker_only=false，filter_type=TOKENS，filter_amount=1e-18，limit=1000。",
             "完整仅指这些条件下官方接口的全部分页；市场查询受接口三年窗口限制，未作全量链上对账。", ""]
    if not status["is_complete"]:
        # 覆盖不完整时移除同一日期的旧结果，避免被误读为本次完整统计。
        summary_path.unlink(missing_ok=True)
        top_path.unlink(missing_ok=True)
        lines += ["采集未完成，仅报告进度和缺口，暂不生成金额统计。", ""]
        found = {row["slug"]: row for row in db.execute(
            "SELECT m.slug, m.condition_id, p.completed_at, p.last_error FROM markets m "
            "LEFT JOIN collection_progress p ON p.market_slug=m.slug WHERE m.date_utc=?", (day,))}
        d = date.fromisoformat(day)
        for slug, _ in slots(d, d + timedelta(days=1)):
            row = found.get(slug)
            if row is None or row["completed_at"] is None:
                reason = row["last_error"] if row is not None else "尚未发现市场"
                lines.append(f"- {slug}：{reason or '成交分页尚未完成'}")
    else:
        summary = dict(db.execute("SELECT * FROM btc5m_daily_summary WHERE date_utc=?", (day,)).fetchone())
        top = [dict(row) for row in db.execute(
            "SELECT * FROM btc5m_wallet_ranked WHERE date_utc=? AND wallet_rank<=10 ORDER BY wallet_rank", (day,))]
        summary["wallet_total_usdc"] = amount_text(summary["wallet_total_micro_usdc"])
        summary["top10_usdc"] = amount_text(summary["top10_micro_usdc"])
        summary.update(expected_markets=288, completed_markets=status["completed_markets"],
                       committed_pages=status["committed_pages"])
        for row in top:
            row["amount_usdc"] = amount_text(row["amount_micro_usdc"])
        write_csv(summary_path, list(summary), [summary])
        write_csv(top_path, ["date_utc", "wallet", "amount_micro_usdc", "wallet_rank", "amount_usdc"], top)
        share = "空（钱包成交总额为零）" if summary["top10_share"] is None else f"{summary['top10_share']:.6%}"
        lines += [f"- 去重钱包数：{summary['unique_wallets']}",
                  f"- 钱包成交总额：{summary['wallet_total_usdc']} USDC",
                  f"- Top10 合计：{summary['top10_usdc']} USDC；占比：{share}", "",
                  f"日汇总：{summary_path.name}；Top10 明细：{top_path.name}。"]
    content = "\n".join(lines) + "\n"
    (output / f"btc5m_daily_{day}.md").write_text(content, encoding="utf-8")
    print(content)
    return bool(status["is_complete"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="经香港代理采集逐笔成交")
    collect_parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    collect_parser.add_argument("--end-date", type=date.fromisoformat, required=True, help="不包含结束日期")
    collect_parser.add_argument("--proxy", required=True, help="本机香港代理 URL，如 socks5h://127.0.0.1:端口")
    report_parser = commands.add_parser("report", help="完全离线生成 CSV 和中文报告")
    report_parser.add_argument("--date", type=date.fromisoformat, required=True)
    report_parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/btc5m")
    for sub in (collect_parser, report_parser):
        sub.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    if args.command == "report":
        # 只读连接保证报告命令不创建数据库或修改采集状态。
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
    client = Client(args.proxy)
    try:
        collect(db, client, args.start_date, args.end_date)
        all_complete = True
        day = args.start_date
        while day < args.end_date:
            state = coverage(db, day.isoformat())
            count = db.execute("SELECT COUNT(*) FROM trades t JOIN markets m ON m.slug=t.market_slug "
                               "WHERE m.date_utc=? AND m.slug GLOB 'btc-updown-5m-*'", (day.isoformat(),)).fetchone()[0]
            print(f"{day}: 完成 {state['completed_markets']}/288 市场，"
                  f"{state['committed_pages']} 页，{count} 条，缺口 {288-state['completed_markets']}")
            all_complete = all_complete and bool(state["is_complete"])
            day += timedelta(days=1)
        print(f"数据库 {args.db.stat().st_size} 字节；本次耗时 {time.monotonic()-started:.2f} 秒")
        return 0 if all_complete else 2
    except KeyboardInterrupt:
        print("采集已中断；已提交页和游标保留，可用同一命令续传。", file=sys.stderr)
        return 130
    finally:
        client.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
