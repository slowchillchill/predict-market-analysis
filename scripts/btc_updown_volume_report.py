#!/usr/bin/env python3
"""Generate cross-platform BTC Up/Down volume reports for Feb-Apr 2026."""

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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "poly-market-analysis/0.1"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
PAGE_LIMIT = 100
KALSHI_PAGE_LIMIT = 1000
REQUEST_SLEEP_SECONDS = 0.05
DATA_RANGE_LABEL = "2026-02_2026-04"

MARKET_TYPES = ["5min", "15min", "hourly", "4hour", "daily"]

POLYMARKET_SERIES = {
    "5min": "btc-up-or-down-5m",
    "15min": "btc-up-or-down-15m",
    "hourly": "btc-up-or-down-hourly",
    "4hour": "btc-up-or-down-4h",
    "daily": "btc-up-or-down-daily",
}

KALSHI_EXACT_DIRECTION_SERIES = {"KXBTC15M": "15min"}
KALSHI_EXCLUDED_SERIES = {
    "KXBTCD": "absolute above/below multi-strike market",
    "BTCD": "daily absolute above/below market",
    "BTCD-B": "daily absolute above/below market",
    "KXBTC": "range bucket market",
}
KALSHI_COMPLETED_STATUSES = {"settled", "finalized", "closed"}

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

GLOBAL_START = MONTH_WINDOWS[0][1]
GLOBAL_END = MONTH_WINDOWS[-1][2]

RAW_PATH = ROOT / "data" / "raw" / f"btc_updown_platform_events_{DATA_RANGE_LABEL}.jsonl.gz"
LEGACY_POLYMARKET_DETAIL_PATH = ROOT / "outputs" / f"btc_updown_market_detail_{DATA_RANGE_LABEL}.csv"
DETAIL_PATH = ROOT / "outputs" / f"btc_updown_market_detail_by_platform_{DATA_RANGE_LABEL}.csv"
PLATFORM_DAILY_PATH = ROOT / "outputs" / f"btc_updown_daily_volume_by_platform_{DATA_RANGE_LABEL}.csv"
PLATFORM_MONTHLY_PATH = ROOT / "outputs" / f"btc_updown_monthly_volume_by_platform_{DATA_RANGE_LABEL}.csv"
COMBINED_DAILY_PATH = ROOT / "outputs" / f"btc_updown_daily_volume_combined_{DATA_RANGE_LABEL}.csv"
COMBINED_MONTHLY_PATH = ROOT / "outputs" / f"btc_updown_monthly_volume_combined_{DATA_RANGE_LABEL}.csv"
REPORT_PATH = ROOT / "reports" / f"btc_updown_cross_platform_volume_summary_{DATA_RANGE_LABEL}.md"
DISCOVERY_REPORT_PATH = ROOT / "reports" / f"kalshi_btc_series_discovery_{DATA_RANGE_LABEL}.md"

DETAIL_FIELDS = [
    "platform",
    "month_utc",
    "date_utc",
    "market_type",
    "comparable",
    "platform_series",
    "platform_event_id",
    "platform_market_id",
    "title",
    "subtitle",
    "end_time_utc",
    "status",
    "result",
    "volume",
    "volume_source",
    "raw_volume",
]

PLATFORM_DAILY_FIELDS = ["platform", "date_utc", "month_utc", "market_type", "market_count", "total_volume"]
PLATFORM_MONTHLY_FIELDS = ["platform", "month_utc", "market_type", "market_count", "total_volume"]
COMBINED_DAILY_FIELDS = ["date_utc", "month_utc", "market_type", "platform_count", "market_count", "total_volume"]
COMBINED_MONTHLY_FIELDS = ["month_utc", "market_type", "platform_count", "market_count", "total_volume"]


class FetchError(RuntimeError):
    """Raised when an API cannot be fetched safely."""


def market_type_for_series_slug(series_slug: str) -> str:
    for market_type, slug in POLYMARKET_SERIES.items():
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


def unix_seconds(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def is_in_half_open_window(value: str, window_start: datetime, window_end: datetime) -> bool:
    dt = parse_utc_datetime(value)
    return window_start <= dt < window_end


def month_for_datetime(dt: datetime) -> tuple[str, datetime, datetime] | None:
    utc_dt = dt.astimezone(timezone.utc)
    for month_utc, window_start, window_end in MONTH_WINDOWS:
        if window_start <= utc_dt < window_end:
            return month_utc, window_start, window_end
    return None


def iter_day_windows(window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
    windows = []
    day_start = window_start
    while day_start < window_end:
        day_end = min(day_start + timedelta(days=1), window_end)
        windows.append((day_start, day_end))
        day_start = day_end
    return windows


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


def format_decimal(value: Decimal) -> str:
    formatted = format(value.normalize(), "f")
    return "0" if formatted == "-0" else formatted


def choose_polymarket_volume(market: dict, event: dict) -> tuple[Decimal, str, str]:
    for field_name, source in (
        ("volumeClob", "market.volumeClob"),
        ("volumeNum", "market.volumeNum"),
        ("volume", "market.volume"),
    ):
        value = market.get(field_name)
        if value is not None and value != "":
            return decimal_from_value(value, source), source, str(value)
    event_volume = event.get("volume")
    if event_volume is not None and event_volume != "":
        return decimal_from_value(event_volume, "event.volume"), "event.volume", str(event_volume)
    return Decimal("0"), "missing_as_zero", ""


def choose_volume(market: dict, event: dict) -> tuple[Decimal, str]:
    volume, source, _ = choose_polymarket_volume(market, event)
    return volume, source


def choose_kalshi_volume(market: dict) -> tuple[Decimal, str, str]:
    value = market.get("volume_fp")
    if value is None or value == "":
        return Decimal("0"), "missing_as_zero", ""
    return decimal_from_value(value, "market.volume_fp"), "market.volume_fp", str(value)


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


def normalize_polymarket_event(
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
    market_id = str(market.get("id") or condition_id)
    if not condition_id and not market_id:
        raise ValueError(f"event {event.get('slug')} missing market id")

    volume, volume_source, raw_volume = choose_polymarket_volume(market, event)
    closed_time_value = market.get("closedTime") or event.get("closedTime") or ""
    closed_time_utc = ""
    if closed_time_value:
        closed_time_utc = isoformat_z(parse_utc_datetime(str(closed_time_value)))

    row = {
        "platform": "polymarket",
        "month_utc": month_utc,
        "date_utc": end_dt.date().isoformat(),
        "market_type": market_type,
        "comparable": "true",
        "platform_series": series_slug,
        "platform_event_id": str(event.get("id") or event.get("slug") or ""),
        "platform_market_id": condition_id or market_id,
        "title": title,
        "subtitle": str(market.get("question") or ""),
        "end_time_utc": isoformat_z(end_dt),
        "status": "closed",
        "result": "",
        "volume": format_decimal(volume),
        "volume_source": volume_source,
        "raw_volume": raw_volume,
    }
    raw_record = {
        "platform": "polymarket",
        "month_utc": month_utc,
        "market_type": market_type,
        "platform_series": series_slug,
        "event": event,
    }
    return row, raw_record


def kalshi_series_ticker(series: dict) -> str:
    return str(series.get("ticker") or series.get("series_ticker") or "")


def classify_kalshi_series(series: dict) -> dict:
    ticker = kalshi_series_ticker(series)
    title = str(series.get("title") or "")
    frequency = str(series.get("frequency") or "")
    normalized_title = title.lower()

    if ticker in KALSHI_EXACT_DIRECTION_SERIES:
        if "up down" in normalized_title or "up in next" in normalized_title:
            return {
                "ticker": ticker,
                "title": title,
                "frequency": frequency,
                "classification": "exact_direction",
                "market_type": KALSHI_EXACT_DIRECTION_SERIES[ticker],
                "comparable": "true",
                "reason": "exact BTC directional series",
                "volume_fp": str(series.get("volume_fp") or ""),
            }
        return {
            "ticker": ticker,
            "title": title,
            "frequency": frequency,
            "classification": "rejected",
            "market_type": "",
            "comparable": "false",
            "reason": "known ticker no longer has directional title",
            "volume_fp": str(series.get("volume_fp") or ""),
        }

    if ticker in KALSHI_EXCLUDED_SERIES:
        return {
            "ticker": ticker,
            "title": title,
            "frequency": frequency,
            "classification": "excluded_related_product",
            "market_type": "",
            "comparable": "false",
            "reason": KALSHI_EXCLUDED_SERIES[ticker],
            "volume_fp": str(series.get("volume_fp") or ""),
        }

    if "btc" in ticker.lower() or "bitcoin" in normalized_title:
        return {
            "ticker": ticker,
            "title": title,
            "frequency": frequency,
            "classification": "unmapped_btc_candidate",
            "market_type": "",
            "comparable": "false",
            "reason": "not approved as BTC Up/Down equivalent",
            "volume_fp": str(series.get("volume_fp") or ""),
        }

    return {
        "ticker": ticker,
        "title": title,
        "frequency": frequency,
        "classification": "irrelevant",
        "market_type": "",
        "comparable": "false",
        "reason": "not BTC related",
        "volume_fp": str(series.get("volume_fp") or ""),
    }


def is_kalshi_completed_market(market: dict) -> bool:
    return str(market.get("status") or "").lower() in KALSHI_COMPLETED_STATUSES


def is_kalshi_directional_market(market: dict, series_ticker: str) -> bool:
    if series_ticker not in KALSHI_EXACT_DIRECTION_SERIES:
        return False
    title = str(market.get("title") or "").lower()
    market_type = str(market.get("market_type") or "").lower()
    strike_type = str(market.get("strike_type") or "").lower()
    if market_type != "binary":
        return False
    if "btc price up" in title:
        return True
    return "btc" in title and "15 min" in title and strike_type == "greater_or_equal"


def normalize_kalshi_market(
    market: dict,
    market_type: str,
    series_ticker: str,
) -> tuple[dict, dict] | None:
    close_time_value = str(market.get("close_time") or "")
    if not close_time_value:
        raise ValueError(f"Kalshi market {market.get('ticker')} missing close_time")
    close_dt = parse_utc_datetime(close_time_value)
    month_window = month_for_datetime(close_dt)
    if month_window is None:
        return None
    month_utc, _, _ = month_window

    if not is_kalshi_completed_market(market):
        return None
    if not is_kalshi_directional_market(market, series_ticker):
        raise ValueError(f"Kalshi market {market.get('ticker')} is not directional BTC Up/Down")

    volume, volume_source, raw_volume = choose_kalshi_volume(market)
    row = {
        "platform": "kalshi",
        "month_utc": month_utc,
        "date_utc": close_dt.date().isoformat(),
        "market_type": market_type,
        "comparable": "true",
        "platform_series": series_ticker,
        "platform_event_id": str(market.get("event_ticker") or ""),
        "platform_market_id": str(market.get("ticker") or ""),
        "title": str(market.get("title") or ""),
        "subtitle": str(market.get("yes_sub_title") or market.get("no_sub_title") or ""),
        "end_time_utc": isoformat_z(close_dt),
        "status": str(market.get("status") or ""),
        "result": str(market.get("result") or ""),
        "volume": format_decimal(volume),
        "volume_source": volume_source,
        "raw_volume": raw_volume,
    }
    raw_record = {
        "platform": "kalshi",
        "month_utc": month_utc,
        "market_type": market_type,
        "platform_series": series_ticker,
        "market": market,
    }
    return row, raw_record


def validate_unique_platform_market_ids(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        key = (row["platform"], row["platform_market_id"])
        if key in seen:
            raise ValueError(f"duplicate platform market id: {key}")
        seen.add(key)


def market_type_sort_key(market_type: str) -> int:
    return MARKET_TYPES.index(market_type)


def platform_sort_key(platform: str) -> int:
    return {"polymarket": 0, "kalshi": 1}.get(platform, 99)


def comparable_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["comparable"] == "true"]


def aggregate_platform_daily(rows: list[dict]) -> list[dict]:
    totals: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for row in rows:
        key = (row["platform"], row["date_utc"], row["month_utc"], row["market_type"])
        totals[key] += decimal_from_value(row["volume"], "detail.volume")
        counts[key] += 1

    daily_rows = []
    for key in sorted(totals, key=lambda item: (platform_sort_key(item[0]), item[2], item[1], market_type_sort_key(item[3]))):
        platform, date_utc, month_utc, market_type = key
        daily_rows.append(
            {
                "platform": platform,
                "date_utc": date_utc,
                "month_utc": month_utc,
                "market_type": market_type,
                "market_count": str(counts[key]),
                "total_volume": format_decimal(totals[key]),
            }
        )
    return daily_rows


def aggregate_platform_monthly(rows: list[dict]) -> list[dict]:
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        key = (row["platform"], row["month_utc"], row["market_type"])
        totals[key] += decimal_from_value(row["volume"], "detail.volume")
        counts[key] += 1

    monthly_rows = []
    for key in sorted(totals, key=lambda item: (platform_sort_key(item[0]), item[1], market_type_sort_key(item[2]))):
        platform, month_utc, market_type = key
        monthly_rows.append(
            {
                "platform": platform,
                "month_utc": month_utc,
                "market_type": market_type,
                "market_count": str(counts[key]),
                "total_volume": format_decimal(totals[key]),
            }
        )
    return monthly_rows


def aggregate_combined_daily(rows: list[dict]) -> list[dict]:
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    platforms: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in comparable_rows(rows):
        key = (row["date_utc"], row["month_utc"], row["market_type"])
        totals[key] += decimal_from_value(row["volume"], "detail.volume")
        counts[key] += 1
        platforms[key].add(row["platform"])

    daily_rows = []
    for key in sorted(totals, key=lambda item: (item[1], item[0], market_type_sort_key(item[2]))):
        date_utc, month_utc, market_type = key
        daily_rows.append(
            {
                "date_utc": date_utc,
                "month_utc": month_utc,
                "market_type": market_type,
                "platform_count": str(len(platforms[key])),
                "market_count": str(counts[key]),
                "total_volume": format_decimal(totals[key]),
            }
        )
    return daily_rows


def aggregate_combined_monthly(rows: list[dict]) -> list[dict]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    platforms: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in comparable_rows(rows):
        key = (row["month_utc"], row["market_type"])
        totals[key] += decimal_from_value(row["volume"], "detail.volume")
        counts[key] += 1
        platforms[key].add(row["platform"])

    monthly_rows = []
    for key in sorted(totals, key=lambda item: (item[0], market_type_sort_key(item[1]))):
        month_utc, market_type = key
        monthly_rows.append(
            {
                "month_utc": month_utc,
                "market_type": market_type,
                "platform_count": str(len(platforms[key])),
                "market_count": str(counts[key]),
                "total_volume": format_decimal(totals[key]),
            }
        )
    return monthly_rows


def get_json_payload(base_url: str, path: str, params: dict[str, object] | None = None) -> object:
    query = urllib.parse.urlencode(params or {})
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read()
            return json.loads(payload)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500 and exc.code != 429:
                raise FetchError(f"API HTTP {exc.code} for {url}: {body[:500]}") from exc
            if attempt == REQUEST_RETRIES:
                raise FetchError(f"API HTTP {exc.code} for {url}: {body[:500]}") from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            ConnectionResetError,
        ) as exc:
            if attempt == REQUEST_RETRIES:
                raise FetchError(f"API request failed for {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            if attempt == REQUEST_RETRIES:
                raise FetchError(f"API returned invalid JSON for {url}") from exc
        time.sleep(0.5 * attempt)
    raise FetchError(f"API request failed for {url}")


def get_json_list(base_url: str, path: str, params: dict[str, object]) -> list[dict]:
    data = get_json_payload(base_url, path, params)
    if not isinstance(data, list):
        raise FetchError(f"API returned non-list payload for {path}")
    return data


def gamma_get_events(params: dict[str, object]) -> list[dict]:
    return get_json_list(GAMMA_API_BASE, "/events", params)


def kalshi_get(path: str, params: dict[str, object] | None = None) -> dict:
    data = get_json_payload(KALSHI_API_BASE, path, params)
    if not isinstance(data, dict):
        raise FetchError(f"Kalshi returned non-object payload for {path}")
    return data


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


def fetch_kalshi_cutoff() -> datetime:
    payload = kalshi_get("/historical/cutoff")
    cutoff_value = str(payload.get("market_settled_ts") or "")
    if not cutoff_value:
        raise FetchError("Kalshi cutoff payload missing market_settled_ts")
    return parse_utc_datetime(cutoff_value)


def kalshi_window_routes(window_start: datetime, window_end: datetime, cutoff: datetime) -> list[tuple[str, datetime, datetime]]:
    routes = []
    if window_start < cutoff:
        routes.append(("historical", window_start, min(window_end, cutoff)))
    if window_end > cutoff:
        routes.append(("live", max(window_start, cutoff), window_end))
    return routes


def fetch_kalshi_series_by_ticker(ticker: str) -> dict | None:
    try:
        payload = kalshi_get(f"/series/{ticker}")
    except FetchError:
        return None
    series = payload.get("series", payload)
    return series if isinstance(series, dict) else None


def fetch_kalshi_series_discovery() -> list[dict]:
    series_by_ticker: dict[str, dict] = {}
    payload = kalshi_get("/series", {"include_volume": "true"})
    series_list = payload.get("series") or []
    if not isinstance(series_list, list):
        raise FetchError("Kalshi series payload missing series list")
    for series in series_list:
        if isinstance(series, dict):
            ticker = kalshi_series_ticker(series)
            if ticker:
                series_by_ticker[ticker] = series

    for ticker in sorted(set(KALSHI_EXACT_DIRECTION_SERIES) | set(KALSHI_EXCLUDED_SERIES)):
        if ticker not in series_by_ticker:
            series = fetch_kalshi_series_by_ticker(ticker)
            if series:
                series_by_ticker[ticker] = series

    classified = [classify_kalshi_series(series) for series in series_by_ticker.values()]
    btc_rows = [row for row in classified if row["classification"] != "irrelevant"]
    return sorted(btc_rows, key=lambda row: (row["classification"] != "exact_direction", row["ticker"]))


def fetch_kalshi_historical_markets(series_ticker: str, window_start: datetime, window_end: datetime) -> list[dict]:
    markets = []
    cursor = ""
    seen_cursors = set()
    while True:
        params = {"series_ticker": series_ticker, "limit": KALSHI_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        payload = kalshi_get("/historical/markets", params)
        page = payload.get("markets") or []
        if not isinstance(page, list):
            raise FetchError("Kalshi historical markets payload missing markets list")

        close_times = []
        for market in page:
            if not isinstance(market, dict):
                continue
            close_time_value = str(market.get("close_time") or "")
            if not close_time_value:
                continue
            close_dt = parse_utc_datetime(close_time_value)
            close_times.append(close_dt)
            if window_start <= close_dt < window_end:
                markets.append(market)

        if close_times and min(close_times) < window_start:
            break

        next_cursor = str(payload.get("cursor") or "")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise FetchError(f"repeated Kalshi historical cursor for {series_ticker}: {next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_SLEEP_SECONDS)

    return markets


def fetch_kalshi_live_markets(series_ticker: str, window_start: datetime, window_end: datetime) -> list[dict]:
    markets = []
    cursor = ""
    seen_cursors = set()
    while True:
        params = {
            "series_ticker": series_ticker,
            "min_close_ts": unix_seconds(window_start),
            "max_close_ts": unix_seconds(window_end),
            "limit": KALSHI_PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        payload = kalshi_get("/markets", params)
        page = payload.get("markets") or []
        if not isinstance(page, list):
            raise FetchError("Kalshi markets payload missing markets list")

        for market in page:
            if not isinstance(market, dict):
                continue
            close_time_value = str(market.get("close_time") or "")
            if not close_time_value:
                continue
            close_dt = parse_utc_datetime(close_time_value)
            if window_start <= close_dt < window_end:
                markets.append(market)

        next_cursor = str(payload.get("cursor") or "")
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise FetchError(f"repeated Kalshi live cursor for {series_ticker}: {next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_SLEEP_SECONDS)

    return markets


def fetch_kalshi_markets_for_range(series_ticker: str, cutoff: datetime) -> list[dict]:
    markets_by_ticker: dict[str, dict] = {}
    for route_name, route_start, route_end in kalshi_window_routes(GLOBAL_START, GLOBAL_END, cutoff):
        if route_start >= route_end:
            continue
        if route_name == "historical":
            markets = fetch_kalshi_historical_markets(series_ticker, route_start, route_end)
        else:
            markets = fetch_kalshi_live_markets(series_ticker, route_start, route_end)
        for market in markets:
            ticker = str(market.get("ticker") or "")
            if ticker:
                markets_by_ticker[ticker] = market
    return list(markets_by_ticker.values())


def collect_polymarket_rows() -> tuple[list[dict], list[dict], list[str]]:
    cached = load_cached_polymarket_rows()
    if cached is not None:
        return cached

    detail_rows = []
    raw_records = []
    anomalies = []

    for month_utc, window_start, window_end in MONTH_WINDOWS:
        for market_type, series_slug in POLYMARKET_SERIES.items():
            events = []
            for day_start, day_end in iter_day_windows(window_start, window_end):
                try:
                    day_events = fetch_paginated_events(market_type, series_slug, month_utc, day_start, day_end)
                    fetch_mode = "primary" if day_events else "primary_empty"
                except FetchError:
                    day_events = fetch_paginated_events(
                        market_type,
                        series_slug,
                        month_utc,
                        day_start,
                        day_end,
                        use_fallback=True,
                    )
                    fetch_mode = "fallback" if day_events else "fallback_empty"
                if fetch_mode == "fallback":
                    anomalies.append(
                        f"Polymarket used fallback tag query for {month_utc} {market_type} {day_start.date()}"
                    )
                elif fetch_mode in {"primary_empty", "fallback_empty"}:
                    anomalies.append(f"Polymarket returned no events for {month_utc} {market_type} {day_start.date()}")
                print(
                    f"polymarket {month_utc} {market_type} {day_start.date()}: {len(day_events)} events ({fetch_mode})",
                    flush=True,
                )
                events.extend(day_events)
            print(f"polymarket {month_utc} {market_type}: {len(events)} events total", flush=True)

            for event in events:
                row, raw_record = normalize_polymarket_event(
                    event,
                    market_type=market_type,
                    series_slug=series_slug,
                    month_utc=month_utc,
                    window_start=window_start,
                    window_end=window_end,
                )
                detail_rows.append(row)
                raw_records.append(raw_record)

    return detail_rows, raw_records, anomalies


def raw_volume_from_cached_polymarket_row(row: dict) -> str:
    source = row.get("volume_source", "")
    if source == "market.volumeClob":
        return row.get("volume_clob", "")
    if source == "market.volumeNum":
        return row.get("volume_num", "")
    if source == "event.volume":
        return row.get("event_volume", "")
    if source == "market.volume":
        return row.get("volume", "")
    return ""


def load_cached_polymarket_rows() -> tuple[list[dict], list[dict], list[str]] | None:
    if not LEGACY_POLYMARKET_DETAIL_PATH.exists():
        return None

    detail_rows = []
    raw_records = []
    with LEGACY_POLYMARKET_DETAIL_PATH.open(encoding="utf-8", newline="") as input_file:
        for cached_row in csv.DictReader(input_file):
            row = {
                "platform": "polymarket",
                "month_utc": cached_row["month_utc"],
                "date_utc": cached_row["date_utc"],
                "market_type": cached_row["market_type"],
                "comparable": "true",
                "platform_series": cached_row["series_slug"],
                "platform_event_id": cached_row.get("event_id") or cached_row.get("event_slug") or "",
                "platform_market_id": cached_row.get("condition_id") or cached_row.get("market_id") or "",
                "title": cached_row["title"],
                "subtitle": cached_row.get("market_slug", ""),
                "end_time_utc": cached_row["end_date_utc"],
                "status": "closed",
                "result": "",
                "volume": cached_row["volume"],
                "volume_source": cached_row["volume_source"],
                "raw_volume": raw_volume_from_cached_polymarket_row(cached_row),
            }
            detail_rows.append(row)
            raw_records.append(
                {
                    "platform": "polymarket",
                    "cache_source": str(LEGACY_POLYMARKET_DETAIL_PATH.relative_to(ROOT)),
                    "row": cached_row,
                }
            )

    print(
        f"polymarket loaded {len(detail_rows)} detail rows from {LEGACY_POLYMARKET_DETAIL_PATH.relative_to(ROOT)}",
        flush=True,
    )
    return detail_rows, raw_records, [
        f"Polymarket loaded from local cache `{LEGACY_POLYMARKET_DETAIL_PATH.relative_to(ROOT)}`"
    ]


def collect_kalshi_rows(discovery_rows: list[dict]) -> tuple[list[dict], list[dict], list[str], datetime]:
    detail_rows = []
    raw_records = []
    anomalies = []
    cutoff = fetch_kalshi_cutoff()

    exact_rows = [row for row in discovery_rows if row["classification"] == "exact_direction" and row["comparable"] == "true"]
    for discovery_row in exact_rows:
        series_ticker = discovery_row["ticker"]
        market_type = discovery_row["market_type"]
        markets = fetch_kalshi_markets_for_range(series_ticker, cutoff)
        print(f"kalshi {series_ticker} {market_type}: {len(markets)} fetched markets", flush=True)
        skipped_status = 0
        for market in markets:
            if not is_kalshi_completed_market(market):
                skipped_status += 1
                continue
            normalized = normalize_kalshi_market(market, market_type, series_ticker)
            if normalized is None:
                continue
            row, raw_record = normalized
            detail_rows.append(row)
            raw_records.append(raw_record)
        if skipped_status:
            anomalies.append(f"Kalshi skipped {skipped_status} incomplete {series_ticker} markets")

    for market_type in MARKET_TYPES:
        if market_type not in {row["market_type"] for row in exact_rows}:
            anomalies.append(f"Kalshi has no approved comparable {market_type} BTC Up/Down series")

    return detail_rows, raw_records, anomalies, cutoff


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


def lookup(rows: list[dict], key_fields: tuple[str, ...], value_field: str) -> dict[tuple[str, ...], str]:
    return {tuple(row[field] for field in key_fields): row[value_field] for row in rows}


def build_discovery_report(discovery_rows: list[dict]) -> str:
    lines = [
        "# Kalshi BTC Series Discovery",
        "",
        f"- Generated at: `{isoformat_z(datetime.now(timezone.utc))}`",
        "- Exact directional rows may be included in combined BTC Up/Down totals.",
        "- Excluded rows are diagnostics only and are not included in platform or combined volume outputs.",
        "",
        "| ticker | title | frequency | classification | market_type | comparable | reason | volume_fp |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in discovery_rows:
        lines.append(
            "| "
            + " | ".join(
                str(row[field]).replace("|", "\\|")
                for field in (
                    "ticker",
                    "title",
                    "frequency",
                    "classification",
                    "market_type",
                    "comparable",
                    "reason",
                    "volume_fp",
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def build_markdown_report(
    detail_rows: list[dict],
    platform_daily_rows: list[dict],
    platform_monthly_rows: list[dict],
    combined_daily_rows: list[dict],
    combined_monthly_rows: list[dict],
    anomalies: list[str],
    kalshi_cutoff: datetime,
) -> str:
    generated_at = isoformat_z(datetime.now(timezone.utc))
    platform_totals = lookup(platform_monthly_rows, ("platform", "month_utc", "market_type"), "total_volume")
    platform_counts = lookup(platform_monthly_rows, ("platform", "month_utc", "market_type"), "market_count")
    combined_totals = lookup(combined_monthly_rows, ("month_utc", "market_type"), "total_volume")
    month_labels = [month_utc for month_utc, _, _ in MONTH_WINDOWS]

    lines = [
        "# BTC Up/Down Cross-Platform Volume Summary",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Date range: `{MONTH_WINDOWS[0][0]}` through `{MONTH_WINDOWS[-1][0]}` using UTC half-open month windows.",
        "- Platforms: `polymarket`, `kalshi`.",
        "- Combined totals include only semantically comparable `comparable=true` BTC direction rows.",
        "- Volume basis: platform-reported cumulative contract/share volume, not USD notional.",
        "- Polymarket volume priority: `market.volumeClob`, `market.volumeNum`, `market.volume`, then `event.volume`.",
        "- Kalshi volume source: `market.volume_fp`.",
        f"- Kalshi historical cutoff used at runtime: `{isoformat_z(kalshi_cutoff)}`",
        f"- Detail rows: `{len(detail_rows)}`",
        f"- Platform daily rows: `{len(platform_daily_rows)}`",
        f"- Platform monthly rows: `{len(platform_monthly_rows)}`",
        f"- Combined daily rows: `{len(combined_daily_rows)}`",
        f"- Combined monthly rows: `{len(combined_monthly_rows)}`",
        f"- Anomaly count: `{len(anomalies)}`",
        "",
        "## Platform Monthly Volume",
        "",
        "| platform | month_utc | " + " | ".join(MARKET_TYPES) + " |",
        "| --- | --- | " + " | ".join("---" for _ in MARKET_TYPES) + " |",
    ]
    for platform in ("polymarket", "kalshi"):
        for month_utc in month_labels:
            values = [platform_totals.get((platform, month_utc, market_type), "0") for market_type in MARKET_TYPES]
            lines.append("| " + platform + " | " + month_utc + " | " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Combined Monthly Volume",
            "",
            "| month_utc | " + " | ".join(MARKET_TYPES) + " |",
            "| --- | " + " | ".join("---" for _ in MARKET_TYPES) + " |",
        ]
    )
    for month_utc in month_labels:
        values = [combined_totals.get((month_utc, market_type), "0") for market_type in MARKET_TYPES]
        lines.append("| " + month_utc + " | " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Platform Market Counts",
            "",
            "| platform | month_utc | " + " | ".join(MARKET_TYPES) + " |",
            "| --- | --- | " + " | ".join("---" for _ in MARKET_TYPES) + " |",
        ]
    )
    for platform in ("polymarket", "kalshi"):
        for month_utc in month_labels:
            values = [platform_counts.get((platform, month_utc, market_type), "0") for market_type in MARKET_TYPES]
            lines.append("| " + platform + " | " + month_utc + " | " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Kalshi Coverage Notes",
            "",
            "- `KXBTC15M` is the only approved comparable Kalshi BTC Up/Down series discovered for this run.",
            "- No approved comparable Kalshi `5min`, `hourly`, `4hour`, or `daily` series is included.",
            "- `KXBTCD`, `BTCD`, `BTCD-B`, and `KXBTC` remain excluded because they are above/below or range products.",
            "",
            "## Output Files",
            "",
            f"- Compressed raw platform snapshot: `{RAW_PATH.relative_to(ROOT)}`",
            f"- Market detail CSV: `{DETAIL_PATH.relative_to(ROOT)}`",
            f"- Platform daily volume CSV: `{PLATFORM_DAILY_PATH.relative_to(ROOT)}`",
            f"- Platform monthly volume CSV: `{PLATFORM_MONTHLY_PATH.relative_to(ROOT)}`",
            f"- Combined daily volume CSV: `{COMBINED_DAILY_PATH.relative_to(ROOT)}`",
            f"- Combined monthly volume CSV: `{COMBINED_MONTHLY_PATH.relative_to(ROOT)}`",
            f"- Markdown summary: `{REPORT_PATH.relative_to(ROOT)}`",
            f"- Kalshi discovery diagnostics: `{DISCOVERY_REPORT_PATH.relative_to(ROOT)}`",
        ]
    )
    if anomalies:
        lines.extend(["", "## Anomalies", ""])
        lines.extend(f"- {anomaly}" for anomaly in anomalies)
    return "\n".join(lines) + "\n"


def generate_report() -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[str]]:
    discovery_rows = fetch_kalshi_series_discovery()
    DISCOVERY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_REPORT_PATH.write_text(build_discovery_report(discovery_rows), encoding="utf-8")

    polymarket_rows, polymarket_raw, polymarket_anomalies = collect_polymarket_rows()
    kalshi_rows, kalshi_raw, kalshi_anomalies, kalshi_cutoff = collect_kalshi_rows(discovery_rows)
    detail_rows = polymarket_rows + kalshi_rows
    raw_records = polymarket_raw + kalshi_raw
    anomalies = polymarket_anomalies + kalshi_anomalies

    detail_rows.sort(
        key=lambda row: (
            platform_sort_key(row["platform"]),
            row["month_utc"],
            market_type_sort_key(row["market_type"]),
            row["end_time_utc"],
            row["platform_market_id"],
        )
    )
    validate_unique_platform_market_ids(detail_rows)

    missing_volume_count = sum(1 for row in detail_rows if row["volume_source"] == "missing_as_zero")
    if missing_volume_count:
        anomalies.append(f"{missing_volume_count} markets had no platform volume field and were counted as 0")

    platform_daily_rows = aggregate_platform_daily(detail_rows)
    platform_monthly_rows = aggregate_platform_monthly(detail_rows)
    combined_daily_rows = aggregate_combined_daily(detail_rows)
    combined_monthly_rows = aggregate_combined_monthly(detail_rows)

    write_jsonl_gzip(RAW_PATH, raw_records)
    write_csv(DETAIL_PATH, DETAIL_FIELDS, detail_rows)
    write_csv(PLATFORM_DAILY_PATH, PLATFORM_DAILY_FIELDS, platform_daily_rows)
    write_csv(PLATFORM_MONTHLY_PATH, PLATFORM_MONTHLY_FIELDS, platform_monthly_rows)
    write_csv(COMBINED_DAILY_PATH, COMBINED_DAILY_FIELDS, combined_daily_rows)
    write_csv(COMBINED_MONTHLY_PATH, COMBINED_MONTHLY_FIELDS, combined_monthly_rows)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        build_markdown_report(
            detail_rows,
            platform_daily_rows,
            platform_monthly_rows,
            combined_daily_rows,
            combined_monthly_rows,
            anomalies,
            kalshi_cutoff,
        ),
        encoding="utf-8",
    )

    return detail_rows, platform_daily_rows, platform_monthly_rows, combined_daily_rows, combined_monthly_rows, anomalies


def main() -> int:
    detail_rows, platform_daily_rows, platform_monthly_rows, combined_daily_rows, combined_monthly_rows, anomalies = (
        generate_report()
    )
    print("generated outputs:")
    for path in (
        RAW_PATH,
        DETAIL_PATH,
        PLATFORM_DAILY_PATH,
        PLATFORM_MONTHLY_PATH,
        COMBINED_DAILY_PATH,
        COMBINED_MONTHLY_PATH,
        REPORT_PATH,
        DISCOVERY_REPORT_PATH,
    ):
        print(f"- {path.relative_to(ROOT)}")
    print(f"detail rows: {len(detail_rows)}")
    print(f"platform daily rows: {len(platform_daily_rows)}")
    print(f"platform monthly rows: {len(platform_monthly_rows)}")
    print(f"combined daily rows: {len(combined_daily_rows)}")
    print(f"combined monthly rows: {len(combined_monthly_rows)}")
    print(f"anomalies: {len(anomalies)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
