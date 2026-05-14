#!/usr/bin/env python3
"""Generate Polymarket BTC Up/Down volume reports for Feb-Apr 2026."""

from __future__ import annotations

import csv
import gzip
import http.client
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "poly-market-analysis/0.1"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
PAGE_LIMIT = 100
REQUEST_SLEEP_SECONDS = 0.05
DATA_RANGE_LABEL = "2026-02_2026-04"

MARKET_SERIES = {
    "5min": "btc-up-or-down-5m",
    "15min": "btc-up-or-down-15m",
    "hourly": "btc-up-or-down-hourly",
    "4hour": "btc-up-or-down-4h",
    "daily": "btc-up-or-down-daily",
}

MONTH_WINDOWS = [
    (
        "2026-02",
        datetime(2026, 2, 1, tzinfo=timezone.utc),
        datetime(2026, 3, 1, tzinfo=timezone.utc),
    ),
    (
        "2026-03",
        datetime(2026, 3, 1, tzinfo=timezone.utc),
        datetime(2026, 4, 1, tzinfo=timezone.utc),
    ),
    (
        "2026-04",
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    ),
]

RAW_PATH = ROOT / "data" / "raw" / f"btc_updown_gamma_events_{DATA_RANGE_LABEL}.jsonl.gz"
DETAIL_PATH = ROOT / "outputs" / f"btc_updown_market_detail_{DATA_RANGE_LABEL}.csv"
DAILY_PATH = ROOT / "outputs" / f"btc_updown_daily_volume_{DATA_RANGE_LABEL}.csv"
MONTHLY_PATH = ROOT / "outputs" / f"btc_updown_monthly_volume_{DATA_RANGE_LABEL}.csv"
REPORT_PATH = ROOT / "reports" / f"btc_updown_volume_summary_{DATA_RANGE_LABEL}.md"

DETAIL_FIELDS = [
    "month_utc",
    "date_utc",
    "market_type",
    "series_slug",
    "event_id",
    "event_slug",
    "market_id",
    "market_slug",
    "condition_id",
    "title",
    "end_date_utc",
    "closed_time_utc",
    "volume",
    "volume_source",
    "volume_clob",
    "volume_num",
    "event_volume",
]

DAILY_FIELDS = ["date_utc", "month_utc", "market_type", "market_count", "total_volume"]
MONTHLY_FIELDS = ["month_utc", "market_type", "market_count", "total_volume"]


class FetchError(RuntimeError):
    """Raised when the Gamma API cannot be fetched safely."""


def market_type_for_series_slug(series_slug: str) -> str:
    for market_type, slug in MARKET_SERIES.items():
        if slug == series_slug:
            return market_type
    raise ValueError(f"unknown target series slug: {series_slug}")


def parse_utc_datetime(value: str) -> datetime:
    if not value:
        raise ValueError("missing datetime value")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def isoformat_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_in_half_open_window(value: str, window_start: datetime, window_end: datetime) -> bool:
    dt = parse_utc_datetime(value)
    return window_start <= dt < window_end


def event_series_slugs(event: dict) -> set[str]:
    slugs = set()
    series_slug = event.get("seriesSlug")
    if series_slug:
        slugs.add(str(series_slug))
    series_items = event.get("series") or []
    if isinstance(series_items, dict):
        series_items = [series_items]
    for item in series_items:
        if isinstance(item, dict) and item.get("slug"):
            slugs.add(str(item["slug"]))
    return slugs


def event_has_series_slug(event: dict, series_slug: str) -> bool:
    return series_slug in event_series_slugs(event)


def decimal_from_value(value: object, field_name: str) -> Decimal:
    if value is None or value == "":
        raise ValueError(f"missing decimal value for {field_name}")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value for {field_name}: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"negative decimal value for {field_name}: {value!r}")
    return parsed


def optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return decimal_from_value(value, field_name)


def format_decimal(value: Decimal) -> str:
    formatted = format(value.normalize(), "f")
    return "0" if formatted == "-0" else formatted


def choose_volume(market: dict, event: dict) -> tuple[Decimal, str]:
    for field_name, source in (
        ("volumeClob", "market.volumeClob"),
        ("volumeNum", "market.volumeNum"),
        ("volume", "market.volume"),
    ):
        value = market.get(field_name)
        if value is not None and value != "":
            return decimal_from_value(value, source), source
    event_volume = event.get("volume")
    if event_volume is not None and event_volume != "":
        return decimal_from_value(event_volume, "event.volume"), "event.volume"
    return Decimal("0"), "missing_as_zero"


def parse_outcomes(value: object) -> list[str]:
    if isinstance(value, list):
        outcomes = value
    elif isinstance(value, str):
        outcomes = json.loads(value)
    else:
        raise ValueError(f"invalid outcomes value: {value!r}")
    if outcomes != ["Up", "Down"]:
        raise ValueError(f"unexpected outcomes: {outcomes!r}")
    return outcomes


def normalize_event(
    event: dict,
    market_type: str,
    series_slug: str,
    month_utc: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[dict, dict]:
    if not event_has_series_slug(event, series_slug):
        raise ValueError(f"event {event.get('slug')} missing target series {series_slug}")

    markets = event.get("markets") or []
    if not markets:
        raise ValueError(f"event {event.get('slug')} has no markets")
    market = markets[0]

    end_date_value = event.get("endDate") or market.get("endDate")
    if not is_in_half_open_window(end_date_value, window_start, window_end):
        raise ValueError(f"event {event.get('slug')} outside month window")
    end_dt = parse_utc_datetime(end_date_value)

    title = str(event.get("title") or market.get("question") or "")
    if "Bitcoin Up or Down" not in title:
        raise ValueError(f"unexpected title for event {event.get('slug')}: {title!r}")

    parse_outcomes(market.get("outcomes"))
    condition_id = str(market.get("conditionId") or "")
    if not condition_id:
        raise ValueError(f"event {event.get('slug')} missing conditionId")

    volume, volume_source = choose_volume(market, event)
    volume_clob = optional_decimal(market.get("volumeClob"), "market.volumeClob")
    volume_num = optional_decimal(market.get("volumeNum"), "market.volumeNum")
    event_volume = optional_decimal(event.get("volume"), "event.volume")

    closed_time_value = market.get("closedTime") or event.get("closedTime") or ""
    closed_time_utc = ""
    if closed_time_value:
        closed_time_utc = isoformat_z(parse_utc_datetime(str(closed_time_value)))

    row = {
        "month_utc": month_utc,
        "date_utc": end_dt.date().isoformat(),
        "market_type": market_type,
        "series_slug": series_slug,
        "event_id": str(event.get("id") or ""),
        "event_slug": str(event.get("slug") or ""),
        "market_id": str(market.get("id") or ""),
        "market_slug": str(market.get("slug") or ""),
        "condition_id": condition_id,
        "title": title,
        "end_date_utc": isoformat_z(end_dt),
        "closed_time_utc": closed_time_utc,
        "volume": format_decimal(volume),
        "volume_source": volume_source,
        "volume_clob": "" if volume_clob is None else format_decimal(volume_clob),
        "volume_num": "" if volume_num is None else format_decimal(volume_num),
        "event_volume": "" if event_volume is None else format_decimal(event_volume),
    }
    raw_record = {
        "month_utc": month_utc,
        "market_type": market_type,
        "series_slug": series_slug,
        "event": event,
    }
    return row, raw_record


def validate_unique_condition_ids(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        condition_id = row["condition_id"]
        if condition_id in seen:
            raise ValueError(f"duplicate condition_id: {condition_id}")
        seen.add(condition_id)


def market_type_sort_key(market_type: str) -> int:
    return list(MARKET_SERIES).index(market_type)


def aggregate_daily(rows: list[dict]) -> list[dict]:
    validate_unique_condition_ids(rows)
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        key = (row["date_utc"], row["month_utc"], row["market_type"])
        totals[key] += decimal_from_value(row["volume"], "detail.volume")
        counts[key] += 1

    daily_rows = []
    for key in sorted(totals, key=lambda item: (item[1], item[0], market_type_sort_key(item[2]))):
        date_utc, month_utc, market_type = key
        daily_rows.append(
            {
                "date_utc": date_utc,
                "month_utc": month_utc,
                "market_type": market_type,
                "market_count": str(counts[key]),
                "total_volume": format_decimal(totals[key]),
            }
        )
    return daily_rows


def aggregate_monthly(daily_rows: list[dict]) -> list[dict]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in daily_rows:
        key = (row["month_utc"], row["market_type"])
        totals[key] += decimal_from_value(row["total_volume"], "daily.total_volume")
        counts[key] += int(row["market_count"])

    monthly_rows = []
    for key in sorted(totals, key=lambda item: (item[0], market_type_sort_key(item[1]))):
        month_utc, market_type = key
        monthly_rows.append(
            {
                "month_utc": month_utc,
                "market_type": market_type,
                "market_count": str(counts[key]),
                "total_volume": format_decimal(totals[key]),
            }
        )
    return monthly_rows


def get_json_list(base_url: str, path: str, params: dict[str, object]) -> list[dict]:
    query = urllib.parse.urlencode(params)
    url = f"{base_url}{path}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read()
            data = json.loads(payload)
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500 and exc.code != 429:
                raise FetchError(f"API HTTP {exc.code} for {url}: {body[:500]}") from exc
            if attempt == REQUEST_RETRIES:
                raise FetchError(f"API HTTP {exc.code} for {url}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
            if attempt == REQUEST_RETRIES:
                raise FetchError(f"API request failed for {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            if attempt == REQUEST_RETRIES:
                raise FetchError(f"API returned invalid JSON for {url}") from exc
        time.sleep(0.5 * attempt)
    if not isinstance(data, list):
        raise FetchError(f"API returned non-list payload for {url}")
    return data


def gamma_get_events(params: dict[str, object]) -> list[dict]:
    return get_json_list(GAMMA_API_BASE, "/events", params)


def fetch_paginated_events(
    market_type: str,
    series_slug: str,
    month_utc: str,
    window_start: datetime,
    window_end: datetime,
    use_fallback: bool = False,
) -> list[dict]:
    events = []
    seen_pages = set()
    seen_event_slugs = set()
    offset = 0
    while True:
        params = {
            "closed": "true",
            "end_date_min": isoformat_z(window_start),
            "end_date_max": isoformat_z(window_end),
            "order": "end_date",
            "ascending": "true",
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        if use_fallback:
            params["tag_slug"] = "bitcoin"
        else:
            params["series_slug"] = series_slug

        page = gamma_get_events(params)
        signature = tuple(str(event.get("id") or event.get("slug") or "") for event in page)
        if signature in seen_pages:
            mode = "fallback" if use_fallback else "primary"
            raise FetchError(f"repeated {mode} page for {market_type} {month_utc} offset={offset}")
        seen_pages.add(signature)

        for event in page:
            if not event_has_series_slug(event, series_slug):
                continue
            end_date_value = event.get("endDate")
            if not end_date_value or not is_in_half_open_window(end_date_value, window_start, window_end):
                continue
            event_slug = str(event.get("slug") or event.get("id") or "")
            if event_slug in seen_event_slugs:
                raise ValueError(f"duplicate event slug in {market_type} {month_utc}: {event_slug}")
            seen_event_slugs.add(event_slug)
            events.append(event)

        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(REQUEST_SLEEP_SECONDS)

    return events


def fetch_events_for_window(
    market_type: str,
    series_slug: str,
    month_utc: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict], str]:
    try:
        events = fetch_paginated_events(market_type, series_slug, month_utc, window_start, window_end)
    except FetchError:
        events = []
    if events:
        return events, "primary"

    fallback_events = fetch_paginated_events(
        market_type,
        series_slug,
        month_utc,
        window_start,
        window_end,
        use_fallback=True,
    )
    if fallback_events:
        return fallback_events, "fallback"
    return events, "primary_empty"


def write_jsonl_gzip(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as gzip_output:
            with io.TextIOWrapper(gzip_output, encoding="utf-8") as output:
                for record in records:
                    json.dump(record, output, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    output.write("\n")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def monthly_lookup(monthly_rows: list[dict], value_field: str) -> dict[tuple[str, str], str]:
    return {(row["month_utc"], row["market_type"]): row[value_field] for row in monthly_rows}


def build_markdown_report(
    detail_rows: list[dict],
    daily_rows: list[dict],
    monthly_rows: list[dict],
    anomalies: list[str],
) -> str:
    generated_at = isoformat_z(datetime.now(timezone.utc))
    totals = monthly_lookup(monthly_rows, "total_volume")
    counts = monthly_lookup(monthly_rows, "market_count")
    month_labels = [month_utc for month_utc, _, _ in MONTH_WINDOWS]
    market_types = list(MARKET_SERIES)

    lines = [
        "# BTC Up/Down Volume Summary",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Date range: `{MONTH_WINDOWS[0][0]}` through `{MONTH_WINDOWS[-1][0]}` using UTC half-open month windows.",
        "- Volume basis: Gamma API market cumulative volume with `volumeClob`, `volumeNum`, `volume`, then event `volume` priority.",
        "- Missing Gamma volume fields are counted as `0` and surfaced in anomalies.",
        f"- Detail rows: `{len(detail_rows)}`",
        f"- Daily rows: `{len(daily_rows)}`",
        f"- Monthly rows: `{len(monthly_rows)}`",
        f"- Anomaly count: `{len(anomalies)}`",
        "",
        "## Monthly Volume",
        "",
        "| month_utc | " + " | ".join(market_types) + " |",
        "| --- | " + " | ".join("---" for _ in market_types) + " |",
    ]
    for month_utc in month_labels:
        values = [totals.get((month_utc, market_type), "0") for market_type in market_types]
        lines.append("| " + month_utc + " | " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Market Counts",
            "",
            "| month_utc | " + " | ".join(market_types) + " |",
            "| --- | " + " | ".join("---" for _ in market_types) + " |",
        ]
    )
    for month_utc in month_labels:
        values = [counts.get((month_utc, market_type), "0") for market_type in market_types]
        lines.append("| " + month_utc + " | " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            f"- Compressed raw API snapshot: `{RAW_PATH.relative_to(ROOT)}`",
            f"- Market detail CSV: `{DETAIL_PATH.relative_to(ROOT)}`",
            f"- Daily volume CSV: `{DAILY_PATH.relative_to(ROOT)}`",
            f"- Monthly volume CSV: `{MONTHLY_PATH.relative_to(ROOT)}`",
            f"- Markdown summary: `{REPORT_PATH.relative_to(ROOT)}`",
        ]
    )
    if anomalies:
        lines.extend(["", "## Anomalies", ""])
        lines.extend(f"- {anomaly}" for anomaly in anomalies)
    return "\n".join(lines) + "\n"


def generate_report() -> tuple[list[dict], list[dict], list[dict], list[str]]:
    detail_rows = []
    raw_records = []
    anomalies = []

    for month_utc, window_start, window_end in MONTH_WINDOWS:
        for market_type, series_slug in MARKET_SERIES.items():
            events, fetch_mode = fetch_events_for_window(
                market_type, series_slug, month_utc, window_start, window_end
            )
            if fetch_mode == "fallback":
                anomalies.append(f"used fallback tag query for {month_utc} {market_type}")
            elif fetch_mode == "primary_empty":
                anomalies.append(f"no events returned for {month_utc} {market_type}")
            print(f"{month_utc} {market_type}: {len(events)} events ({fetch_mode})", flush=True)

            for event in events:
                row, raw_record = normalize_event(
                    event,
                    market_type=market_type,
                    series_slug=series_slug,
                    month_utc=month_utc,
                    window_start=window_start,
                    window_end=window_end,
                )
                detail_rows.append(row)
                raw_records.append(raw_record)

    detail_rows.sort(
        key=lambda row: (
            row["month_utc"],
            market_type_sort_key(row["market_type"]),
            row["end_date_utc"],
            row["event_slug"],
        )
    )
    validate_unique_condition_ids(detail_rows)
    missing_volume_count = sum(1 for row in detail_rows if row["volume_source"] == "missing_as_zero")
    if missing_volume_count:
        anomalies.append(f"{missing_volume_count} markets had no Gamma volume field and were counted as 0")
    daily_rows = aggregate_daily(detail_rows)
    monthly_rows = aggregate_monthly(daily_rows)

    write_jsonl_gzip(RAW_PATH, raw_records)
    write_csv(DETAIL_PATH, DETAIL_FIELDS, detail_rows)
    write_csv(DAILY_PATH, DAILY_FIELDS, daily_rows)
    write_csv(MONTHLY_PATH, MONTHLY_FIELDS, monthly_rows)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        build_markdown_report(detail_rows, daily_rows, monthly_rows, anomalies),
        encoding="utf-8",
    )

    return detail_rows, daily_rows, monthly_rows, anomalies


def main() -> int:
    detail_rows, daily_rows, monthly_rows, anomalies = generate_report()
    print("generated outputs:")
    for path in (RAW_PATH, DETAIL_PATH, DAILY_PATH, MONTHLY_PATH, REPORT_PATH):
        print(f"- {path.relative_to(ROOT)}")
    print(f"detail rows: {len(detail_rows)}")
    print(f"daily rows: {len(daily_rows)}")
    print(f"monthly rows: {len(monthly_rows)}")
    print(f"anomalies: {len(anomalies)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
