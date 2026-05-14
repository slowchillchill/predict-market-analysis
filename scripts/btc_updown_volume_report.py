#!/usr/bin/env python3
"""Generate cross-platform crypto Up/Down volume reports for Feb-Apr 2026."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import http.client
import io
import json
import re
import sqlite3
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
POLYMARKET_DATA_API_BASE = "https://data-api.polymarket.com"
KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "poly-market-analysis/0.1"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
PAGE_LIMIT = 100
POLYMARKET_TRADE_LIMIT = 1000
POLYMARKET_TRADE_BATCH_SIZE = 5
POLYMARKET_TRADE_WORKERS = 12
POLYMARKET_MAX_TRADE_OFFSET = 3000
POLYMARKET_POSITIONS_LIMIT = 500
POLYMARKET_POSITIONS_WORKERS = 24
POLYMARKET_MAX_POSITIONS_OFFSET = 10000
KALSHI_PAGE_LIMIT = 1000
REQUEST_SLEEP_SECONDS = 0.05
DATA_RANGE_LABEL = "2026-02_2026-04"
POLYMARKET_WALLET_STATUS_UNAVAILABLE_REASON = "polymarket_trade_history_generation_not_run"

MARKET_TYPES = ["5min", "15min", "hourly", "4hour", "daily"]

ASSET_CONFIGS = {
    "btc": {
        "display": "BTC",
        "full_name": "Bitcoin",
        "title_phrase": "Bitcoin Up or Down",
        "fallback_tag_slug": "bitcoin",
        "title_terms": ("btc", "bitcoin"),
        "polymarket_series": {
            "5min": "btc-up-or-down-5m",
            "15min": "btc-up-or-down-15m",
            "hourly": "btc-up-or-down-hourly",
            "4hour": "btc-up-or-down-4h",
            "daily": "btc-up-or-down-daily",
        },
        "kalshi_exact_direction_series": {"KXBTC15M": "15min"},
        "kalshi_excluded_series": {
            "KXBTCD": "absolute above/below multi-strike market",
            "BTCD": "daily absolute above/below market",
            "BTCD-B": "daily absolute above/below market",
            "KXBTC": "range bucket market",
        },
    },
    "eth": {
        "display": "ETH",
        "full_name": "Ethereum",
        "title_phrase": "Ethereum Up or Down",
        "fallback_tag_slug": "ethereum",
        "title_terms": ("eth", "ethereum"),
        "polymarket_series": {
            "5min": "eth-up-or-down-5m",
            "15min": "eth-up-or-down-15m",
            "hourly": "eth-up-or-down-hourly",
            "4hour": "eth-up-or-down-4h",
            "daily": "eth-up-or-down-daily",
        },
        "kalshi_exact_direction_series": {"KXETH15M": "15min"},
        "kalshi_excluded_series": {
            "ETH": "daily Ethereum range market",
            "KXETH": "range bucket market",
            "ETHD": "daily absolute above/below market",
            "KXETHD": "absolute above/below multi-strike market",
            "ETHATH": "all-time-high market",
            "KXETHATH": "all-time-high market",
            "ETHETF": "ETF listed market",
            "KXETHETF": "ETF listed market",
            "ETHMAXY": "annual high threshold market",
            "KXETHMAXY": "annual high threshold market",
            "ETHMINY": "annual low threshold market",
            "KXETHMINY": "annual low threshold market",
            "KXETHMAXMON": "monthly one-touch market",
            "KXETHMINMON": "monthly one-touch market",
        },
    },
}

ASSET_KEY = ""
ASSET_DISPLAY = ""
ASSET_FULL_NAME = ""
ASSET_TITLE_PHRASE = ""
POLYMARKET_FALLBACK_TAG_SLUG = ""
KALSHI_TITLE_TERMS: tuple[str, ...] = ()
POLYMARKET_SERIES: dict[str, str] = {}
KALSHI_EXACT_DIRECTION_SERIES: dict[str, str] = {}
KALSHI_EXCLUDED_SERIES: dict[str, str] = {}
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

RAW_PATH: Path
LEGACY_POLYMARKET_DETAIL_PATH: Path
DETAIL_PATH: Path
PLATFORM_DAILY_PATH: Path
PLATFORM_MONTHLY_PATH: Path
COMBINED_DAILY_PATH: Path
COMBINED_MONTHLY_PATH: Path
REPORT_PATH: Path
DISCOVERY_REPORT_PATH: Path
POLYMARKET_TRADES_RAW_PATH: Path
WALLET_POSITIONS_STATE_PATH: Path
WALLET_MARKET_PATH: Path
WALLET_DAILY_PATH: Path
WALLET_MONTHLY_PATH: Path

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
WALLET_MARKET_FIELDS = [
    "asset",
    "platform",
    "month_utc",
    "date_utc",
    "market_type",
    "platform_series",
    "platform_market_id",
    "title",
    "active_wallet_count",
    "participant_trade_count",
    "taker_trade_count",
    "trade_history_volume",
    "top10_wallet_volume",
    "top10_wallet_volume_share",
    "platform_reported_volume",
    "volume_gap",
    "volume_gap_pct",
    "data_status",
]
WALLET_DAILY_FIELDS = [
    "asset",
    "platform",
    "date_utc",
    "month_utc",
    "market_type",
    "active_wallet_count",
    "participant_trade_count",
    "taker_trade_count",
    "trade_history_volume",
    "top10_wallet_volume",
    "top10_wallet_volume_share",
    "platform_reported_volume",
    "volume_gap",
    "volume_gap_pct",
    "data_status",
    "unavailable_reason",
]
WALLET_MONTHLY_FIELDS = [
    "asset",
    "platform",
    "month_utc",
    "market_type",
    "active_wallet_count",
    "participant_trade_count",
    "taker_trade_count",
    "trade_history_volume",
    "top10_wallet_volume",
    "top10_wallet_volume_share",
    "platform_reported_volume",
    "volume_gap",
    "volume_gap_pct",
    "data_status",
    "unavailable_reason",
]


def configure_asset(asset_key: str) -> None:
    config = ASSET_CONFIGS.get(asset_key)
    if config is None:
        raise ValueError(f"unknown asset: {asset_key}")

    global ASSET_KEY, ASSET_DISPLAY, ASSET_FULL_NAME, ASSET_TITLE_PHRASE
    global POLYMARKET_FALLBACK_TAG_SLUG, KALSHI_TITLE_TERMS
    global POLYMARKET_SERIES, KALSHI_EXACT_DIRECTION_SERIES, KALSHI_EXCLUDED_SERIES
    global RAW_PATH, LEGACY_POLYMARKET_DETAIL_PATH, DETAIL_PATH
    global PLATFORM_DAILY_PATH, PLATFORM_MONTHLY_PATH, COMBINED_DAILY_PATH, COMBINED_MONTHLY_PATH
    global REPORT_PATH, DISCOVERY_REPORT_PATH
    global POLYMARKET_TRADES_RAW_PATH, WALLET_POSITIONS_STATE_PATH, WALLET_MARKET_PATH, WALLET_DAILY_PATH, WALLET_MONTHLY_PATH

    ASSET_KEY = asset_key
    ASSET_DISPLAY = str(config["display"])
    ASSET_FULL_NAME = str(config["full_name"])
    ASSET_TITLE_PHRASE = str(config["title_phrase"])
    POLYMARKET_FALLBACK_TAG_SLUG = str(config["fallback_tag_slug"])
    KALSHI_TITLE_TERMS = tuple(config["title_terms"])
    POLYMARKET_SERIES = dict(config["polymarket_series"])
    KALSHI_EXACT_DIRECTION_SERIES = dict(config["kalshi_exact_direction_series"])
    KALSHI_EXCLUDED_SERIES = dict(config["kalshi_excluded_series"])

    output_prefix = f"{asset_key}_updown"
    RAW_PATH = ROOT / "data" / "raw" / f"{output_prefix}_platform_events_{DATA_RANGE_LABEL}.jsonl.gz"
    LEGACY_POLYMARKET_DETAIL_PATH = ROOT / "outputs" / f"{output_prefix}_market_detail_{DATA_RANGE_LABEL}.csv"
    DETAIL_PATH = ROOT / "outputs" / f"{output_prefix}_market_detail_by_platform_{DATA_RANGE_LABEL}.csv"
    PLATFORM_DAILY_PATH = ROOT / "outputs" / f"{output_prefix}_daily_volume_by_platform_{DATA_RANGE_LABEL}.csv"
    PLATFORM_MONTHLY_PATH = ROOT / "outputs" / f"{output_prefix}_monthly_volume_by_platform_{DATA_RANGE_LABEL}.csv"
    COMBINED_DAILY_PATH = ROOT / "outputs" / f"{output_prefix}_daily_volume_combined_{DATA_RANGE_LABEL}.csv"
    COMBINED_MONTHLY_PATH = ROOT / "outputs" / f"{output_prefix}_monthly_volume_combined_{DATA_RANGE_LABEL}.csv"
    REPORT_PATH = ROOT / "reports" / f"{output_prefix}_cross_platform_volume_summary_{DATA_RANGE_LABEL}.md"
    DISCOVERY_REPORT_PATH = ROOT / "reports" / f"kalshi_{asset_key}_series_discovery_{DATA_RANGE_LABEL}.md"
    POLYMARKET_TRADES_RAW_PATH = ROOT / "data" / "raw" / f"{output_prefix}_polymarket_trades_{DATA_RANGE_LABEL}.jsonl.gz"
    WALLET_POSITIONS_STATE_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_positions_state_{DATA_RANGE_LABEL}.sqlite3"
    WALLET_MARKET_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_activity_by_market_{DATA_RANGE_LABEL}.csv"
    WALLET_DAILY_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_activity_daily_by_platform_{DATA_RANGE_LABEL}.csv"
    WALLET_MONTHLY_PATH = ROOT / "outputs" / f"{output_prefix}_wallet_activity_monthly_by_platform_{DATA_RANGE_LABEL}.csv"


configure_asset("btc")


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


def normalize_polymarket_trade(trade: dict, expected_condition_ids: str | set[str]) -> dict:
    wallet = str(trade.get("proxyWallet") or "").lower()
    condition_id = str(trade.get("conditionId") or "").lower()
    if isinstance(expected_condition_ids, str):
        expected = {expected_condition_ids.lower()}
    else:
        expected = {condition_id.lower() for condition_id in expected_condition_ids}
    if not wallet:
        raise ValueError("Polymarket trade missing proxyWallet")
    if condition_id not in expected:
        raise ValueError(f"Polymarket trade conditionId mismatch: {condition_id} not in {sorted(expected)}")
    size = decimal_from_value(trade.get("size"), "trade.size")
    timestamp = int(trade.get("timestamp"))
    return {
        "wallet": wallet,
        "condition_id": condition_id,
        "size": size,
        "price": str(trade.get("price") or ""),
        "timestamp": timestamp,
        "side": str(trade.get("side") or ""),
        "outcome": str(trade.get("outcome") or ""),
        "transaction_hash": str(trade.get("transactionHash") or ""),
        "raw": trade,
    }


def normalize_polymarket_position(position: dict, expected_condition_id: str) -> dict:
    wallet = str(position.get("proxyWallet") or "").lower()
    condition_id = str(position.get("conditionId") or "").lower()
    expected = expected_condition_id.lower()
    if not wallet:
        raise ValueError("Polymarket position missing proxyWallet")
    if condition_id != expected:
        raise ValueError(f"Polymarket position conditionId mismatch: {condition_id} != {expected}")
    return {
        "wallet": wallet,
        "condition_id": condition_id,
        "asset": str(position.get("asset") or ""),
        "outcome": str(position.get("outcome") or ""),
        "outcome_index": str(position.get("outcomeIndex") if position.get("outcomeIndex") is not None else ""),
        "total_bought": decimal_from_value(position.get("totalBought") or 0, "position.totalBought"),
        "raw": position,
    }


def polymarket_trade_key(trade: dict) -> tuple[str, str, str, str, int, str, str, str]:
    return (
        trade["transaction_hash"],
        trade["wallet"],
        trade["condition_id"],
        trade["outcome"],
        trade["timestamp"],
        trade["side"],
        format_decimal(trade["size"]),
        trade["price"],
    )


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
    if ASSET_TITLE_PHRASE not in title:
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


def is_kalshi_asset_candidate(ticker: str, title: str) -> bool:
    normalized_ticker = ticker.upper()
    if ASSET_KEY == "eth":
        if normalized_ticker.startswith(("ETH", "KXETH")):
            return True
        if re.search(r"(^|[^A-Za-z0-9])ETH([^A-Za-z0-9]|$)", title) or re.search(
            r"(^|[^A-Za-z0-9])Ethereum([^A-Za-z0-9]|$)",
            title,
            re.IGNORECASE,
        ):
            return True
        explicit_segments = ("BTCETH", "VSETH", "SOLETH", "FLIPETH", "REVETH")
        if any(segment in normalized_ticker for segment in explicit_segments):
            return True
        return False

    normalized_title = title.lower()
    return any(term in normalized_ticker.lower() or term in normalized_title for term in KALSHI_TITLE_TERMS)


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
                "reason": f"exact {ASSET_DISPLAY} directional series",
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

    if is_kalshi_asset_candidate(ticker, title):
        return {
            "ticker": ticker,
            "title": title,
            "frequency": frequency,
            "classification": f"unmapped_{ASSET_KEY}_candidate",
            "market_type": "",
            "comparable": "false",
            "reason": f"not approved as {ASSET_DISPLAY} Up/Down equivalent",
            "volume_fp": str(series.get("volume_fp") or ""),
        }

    return {
        "ticker": ticker,
        "title": title,
        "frequency": frequency,
        "classification": "irrelevant",
        "market_type": "",
        "comparable": "false",
        "reason": f"not {ASSET_DISPLAY} related",
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
    if not is_kalshi_asset_candidate(series_ticker, str(market.get("title") or "")):
        return False
    if "price up" in title or "up down" in title or "up in next" in title:
        return True
    return ("15 min" in title or "15m" in title) and strike_type == "greater_or_equal"


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
        raise ValueError(f"Kalshi market {market.get('ticker')} is not directional {ASSET_DISPLAY} Up/Down")

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


def decimal_ratio(numerator: Decimal, denominator: Decimal) -> str:
    if denominator == 0:
        return ""
    return format_decimal((numerator / denominator).quantize(Decimal("0.0001")))


def volume_gap_pct(gap: Decimal, denominator: Decimal) -> str:
    if denominator == 0:
        return ""
    return format_decimal((gap / denominator).quantize(Decimal("0.0001")))


def aggregate_wallet_activity(detail_rows: list[dict], trades_by_market: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict]]:
    market_rows = []
    daily_active_wallets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    daily_taker_wallet_sizes: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily_participant_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    daily_taker_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    daily_trade_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    daily_platform_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    daily_statuses: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    monthly_active_wallets: dict[tuple[str, str], set[str]] = defaultdict(set)
    monthly_taker_wallet_sizes: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    monthly_participant_counts: dict[tuple[str, str], int] = defaultdict(int)
    monthly_taker_counts: dict[tuple[str, str], int] = defaultdict(int)
    monthly_trade_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    monthly_platform_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    monthly_statuses: dict[tuple[str, str], set[str]] = defaultdict(set)

    for detail in detail_rows:
        if detail["platform"] != "polymarket":
            continue
        condition_id = detail["platform_market_id"]
        trade_group = trades_by_market.get(
            condition_id,
            {
                "participant_trades": [],
                "participant_status": "complete",
                "taker_trades": [],
                "taker_status": "complete",
            },
        )
        participant_trades = trade_group["participant_trades"]
        taker_trades = trade_group["taker_trades"]
        participant_wallets = {trade["wallet"] for trade in participant_trades}
        taker_wallet_sizes: dict[str, Decimal] = defaultdict(Decimal)
        for trade in taker_trades:
            taker_wallet_sizes[trade["wallet"]] += trade["size"]
        trade_total = sum(taker_wallet_sizes.values(), Decimal("0"))
        top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
        platform_total = decimal_from_value(detail["volume"], "detail.volume")
        gap = platform_total - trade_total
        daily_key = (detail["date_utc"], detail["month_utc"], detail["market_type"])
        monthly_key = (detail["month_utc"], detail["market_type"])
        for wallet, size in taker_wallet_sizes.items():
            daily_taker_wallet_sizes[daily_key][wallet] += size
            monthly_taker_wallet_sizes[monthly_key][wallet] += size
        daily_active_wallets[daily_key].update(participant_wallets)
        monthly_active_wallets[monthly_key].update(participant_wallets)
        daily_participant_counts[daily_key] += len(participant_trades)
        daily_taker_counts[daily_key] += len(taker_trades)
        daily_trade_totals[daily_key] += trade_total
        daily_platform_totals[daily_key] += platform_total
        daily_statuses[daily_key].update({trade_group["participant_status"], trade_group["taker_status"]})
        monthly_participant_counts[monthly_key] += len(participant_trades)
        monthly_taker_counts[monthly_key] += len(taker_trades)
        monthly_trade_totals[monthly_key] += trade_total
        monthly_platform_totals[monthly_key] += platform_total
        monthly_statuses[monthly_key].update({trade_group["participant_status"], trade_group["taker_status"]})
        market_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "month_utc": detail["month_utc"],
                "date_utc": detail["date_utc"],
                "market_type": detail["market_type"],
                "platform_series": detail["platform_series"],
                "platform_market_id": condition_id,
                "title": detail["title"],
                "active_wallet_count": str(len(participant_wallets)),
                "participant_trade_count": str(len(participant_trades)),
                "taker_trade_count": str(len(taker_trades)),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": (
                    "truncated"
                    if "truncated" in {trade_group["participant_status"], trade_group["taker_status"]}
                    else "complete"
                ),
            }
        )

    daily_rows = []
    for key in sorted(daily_trade_totals, key=lambda item: (item[1], item[0], market_type_sort_key(item[2]))):
        date_utc, month_utc, market_type = key
        active_wallets = daily_active_wallets[key]
        taker_wallet_sizes = daily_taker_wallet_sizes[key]
        trade_total = daily_trade_totals[key]
        top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
        platform_total = daily_platform_totals[key]
        gap = platform_total - trade_total
        daily_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "date_utc": date_utc,
                "month_utc": month_utc,
                "market_type": market_type,
                "active_wallet_count": str(len(active_wallets)),
                "participant_trade_count": str(daily_participant_counts[key]),
                "taker_trade_count": str(daily_taker_counts[key]),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": "truncated" if "truncated" in daily_statuses[key] else "complete",
                "unavailable_reason": "",
            }
        )

    monthly_rows = []
    for key in sorted(monthly_trade_totals, key=lambda item: (item[0], market_type_sort_key(item[1]))):
        month_utc, market_type = key
        active_wallets = monthly_active_wallets[key]
        taker_wallet_sizes = monthly_taker_wallet_sizes[key]
        trade_total = monthly_trade_totals[key]
        top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
        platform_total = monthly_platform_totals[key]
        gap = platform_total - trade_total
        monthly_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "month_utc": month_utc,
                "market_type": market_type,
                "active_wallet_count": str(len(active_wallets)),
                "participant_trade_count": str(monthly_participant_counts[key]),
                "taker_trade_count": str(monthly_taker_counts[key]),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": "truncated" if "truncated" in monthly_statuses[key] else "complete",
                "unavailable_reason": "",
            }
        )
    return market_rows, daily_rows, monthly_rows


def build_kalshi_wallet_unavailable_rows(platform_rows: list[dict]) -> list[dict]:
    rows = []
    for row in platform_rows:
        if row["platform"] != "kalshi":
            continue
        unavailable_row = {
            "asset": ASSET_KEY,
            "platform": "kalshi",
            "month_utc": row["month_utc"],
            "market_type": row["market_type"],
            "active_wallet_count": "",
            "participant_trade_count": "",
            "taker_trade_count": "",
            "trade_history_volume": "",
            "top10_wallet_volume": "",
            "top10_wallet_volume_share": "",
            "platform_reported_volume": row["total_volume"],
            "volume_gap": "",
            "volume_gap_pct": "",
            "data_status": "not_available",
            "unavailable_reason": "kalshi_public_trades_have_no_wallet_identifier",
        }
        if "date_utc" in row:
            unavailable_row["date_utc"] = row["date_utc"]
        rows.append(unavailable_row)
    return rows


def build_polymarket_wallet_not_generated_rows(platform_rows: list[dict]) -> list[dict]:
    rows = []
    for row in platform_rows:
        if row["platform"] != "polymarket":
            continue
        not_generated_row = {
            "asset": ASSET_KEY,
            "platform": "polymarket",
            "month_utc": row["month_utc"],
            "market_type": row["market_type"],
            "active_wallet_count": "",
            "participant_trade_count": "",
            "taker_trade_count": "",
            "trade_history_volume": "",
            "top10_wallet_volume": "",
            "top10_wallet_volume_share": "",
            "platform_reported_volume": row["total_volume"],
            "volume_gap": "",
            "volume_gap_pct": "",
            "data_status": "not_generated",
            "unavailable_reason": POLYMARKET_WALLET_STATUS_UNAVAILABLE_REASON,
        }
        if "date_utc" in row:
            not_generated_row["date_utc"] = row["date_utc"]
        rows.append(not_generated_row)
    return rows


def build_polymarket_wallet_market_not_generated_rows(detail_rows: list[dict]) -> list[dict]:
    rows = []
    for row in detail_rows:
        if row["platform"] != "polymarket":
            continue
        rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "month_utc": row["month_utc"],
                "date_utc": row["date_utc"],
                "market_type": row["market_type"],
                "platform_series": row["platform_series"],
                "platform_market_id": row["platform_market_id"],
                "title": row["title"],
                "active_wallet_count": "",
                "participant_trade_count": "",
                "taker_trade_count": "",
                "trade_history_volume": "",
                "top10_wallet_volume": "",
                "top10_wallet_volume_share": "",
                "platform_reported_volume": format_decimal(decimal_from_value(row["volume"], "detail.volume")),
                "volume_gap": "",
                "volume_gap_pct": "",
                "data_status": "not_generated",
            }
        )
    return rows


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


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_polymarket_trades_for_markets(condition_ids: list[str], taker_only: bool) -> dict[str, dict]:
    lower_to_original = {condition_id.lower(): condition_id for condition_id in condition_ids}
    grouped = {condition_id: {"trades": [], "status": "complete"} for condition_id in condition_ids}
    if not condition_ids:
        return grouped

    offset = 0
    seen_keys: dict[str, set[tuple[str, str, str, str, int, str, str, str]]] = defaultdict(set)
    expected = set(lower_to_original)
    while True:
        params = {
            "market": ",".join(condition_ids),
            "limit": POLYMARKET_TRADE_LIMIT,
            "offset": offset,
            "takerOnly": "true" if taker_only else "false",
        }
        page = get_json_list(POLYMARKET_DATA_API_BASE, "/trades", params)
        for raw_trade in page:
            normalized = normalize_polymarket_trade(raw_trade, expected)
            original_condition_id = lower_to_original[normalized["condition_id"]]
            key = polymarket_trade_key(normalized)
            if key in seen_keys[original_condition_id]:
                continue
            seen_keys[original_condition_id].add(key)
            grouped[original_condition_id]["trades"].append(normalized)

        if len(page) < POLYMARKET_TRADE_LIMIT:
            break
        if offset >= POLYMARKET_MAX_TRADE_OFFSET:
            for group in grouped.values():
                group["status"] = "truncated"
            break
        offset += POLYMARKET_TRADE_LIMIT
        time.sleep(REQUEST_SLEEP_SECONDS)
    if len(condition_ids) > 1 and any(group["status"] == "truncated" for group in grouped.values()):
        split_at = len(condition_ids) // 2
        left = fetch_polymarket_trades_for_markets(condition_ids[:split_at], taker_only)
        right = fetch_polymarket_trades_for_markets(condition_ids[split_at:], taker_only)
        return {**left, **right}
    return grouped


def fetch_polymarket_trades_for_market(condition_id: str, taker_only: bool) -> tuple[list[dict], str]:
    group = fetch_polymarket_trades_for_markets([condition_id], taker_only)[condition_id]
    return group["trades"], group["status"]


def fetch_polymarket_trade_batch(condition_ids: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    participant_by_market = fetch_polymarket_trades_for_markets(condition_ids, taker_only=False)
    taker_by_market = fetch_polymarket_trades_for_markets(condition_ids, taker_only=True)
    return participant_by_market, taker_by_market


def fetch_polymarket_positions_for_market(condition_id: str) -> dict:
    wallet_sizes: dict[str, Decimal] = defaultdict(Decimal)
    seen_keys: set[tuple[str, str, str]] = set()
    position_count = 0
    offset = 0
    status = "positions_complete"
    while True:
        params = {
            "market": condition_id,
            "status": "ALL",
            "limit": POLYMARKET_POSITIONS_LIMIT,
            "offset": offset,
            "sortBy": "TOKENS",
            "sortDirection": "DESC",
        }
        page = get_json_list(POLYMARKET_DATA_API_BASE, "/v1/market-positions", params)
        max_group_count = 0
        for token_group in page:
            group_positions = token_group.get("positions") or []
            max_group_count = max(max_group_count, len(group_positions))
            for raw_position in group_positions:
                position = normalize_polymarket_position(raw_position, condition_id)
                key = (position["wallet"], position["asset"], position["outcome_index"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                position_count += 1
                wallet_sizes[position["wallet"]] += position["total_bought"]

        if max_group_count < POLYMARKET_POSITIONS_LIMIT:
            break
        if offset >= POLYMARKET_MAX_POSITIONS_OFFSET:
            status = "positions_truncated"
            break
        offset += POLYMARKET_POSITIONS_LIMIT
        time.sleep(REQUEST_SLEEP_SECONDS)
    return {"wallet_sizes": dict(wallet_sizes), "position_count": position_count, "status": status}


def initialize_wallet_positions_state(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS completed_markets (
            platform_market_id TEXT PRIMARY KEY,
            date_utc TEXT NOT NULL,
            month_utc TEXT NOT NULL,
            market_type TEXT NOT NULL,
            active_wallet_count INTEGER NOT NULL,
            position_count INTEGER NOT NULL,
            position_total REAL NOT NULL,
            top10_wallet_volume REAL NOT NULL,
            platform_reported_volume REAL NOT NULL,
            data_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_wallets (
            date_utc TEXT NOT NULL,
            month_utc TEXT NOT NULL,
            market_type TEXT NOT NULL,
            wallet TEXT NOT NULL,
            total_bought REAL NOT NULL,
            PRIMARY KEY (date_utc, month_utc, market_type, wallet)
        );
        CREATE TABLE IF NOT EXISTS monthly_wallets (
            month_utc TEXT NOT NULL,
            market_type TEXT NOT NULL,
            wallet TEXT NOT NULL,
            total_bought REAL NOT NULL,
            PRIMARY KEY (month_utc, market_type, wallet)
        );
        """
    )


def store_wallet_position_aggregate(connection: sqlite3.Connection, row: dict, group: dict) -> None:
    wallet_sizes = group["wallet_sizes"]
    position_total = sum(wallet_sizes.values(), Decimal("0"))
    top10_total = sum(sorted(wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
    platform_total = decimal_from_value(row["volume"], "detail.volume")
    with connection:
        connection.execute(
            """
            INSERT INTO completed_markets (
                platform_market_id, date_utc, month_utc, market_type, active_wallet_count, position_count,
                position_total, top10_wallet_volume, platform_reported_volume, data_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["platform_market_id"],
                row["date_utc"],
                row["month_utc"],
                row["market_type"],
                len(wallet_sizes),
                group["position_count"],
                float(position_total),
                float(top10_total),
                float(platform_total),
                group["status"],
            ),
        )
        for wallet, total_bought in wallet_sizes.items():
            connection.execute(
                """
                INSERT INTO daily_wallets (date_utc, month_utc, market_type, wallet, total_bought)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date_utc, month_utc, market_type, wallet)
                DO UPDATE SET total_bought = total_bought + excluded.total_bought
                """,
                (row["date_utc"], row["month_utc"], row["market_type"], wallet, float(total_bought)),
            )
            connection.execute(
                """
                INSERT INTO monthly_wallets (month_utc, market_type, wallet, total_bought)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(month_utc, market_type, wallet)
                DO UPDATE SET total_bought = total_bought + excluded.total_bought
                """,
                (row["month_utc"], row["market_type"], wallet, float(total_bought)),
            )


def decimal_from_sqlite(value: object) -> Decimal:
    return decimal_from_value(str(value or 0), "sqlite.total_bought")


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
            params["tag_slug"] = POLYMARKET_FALLBACK_TAG_SLUG
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
    asset_rows = [row for row in classified if row["classification"] != "irrelevant"]
    return sorted(asset_rows, key=lambda row: (row["classification"] != "exact_direction", row["ticker"]))


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


def load_cached_detail_rows() -> tuple[list[dict], list[dict], list[str]] | None:
    if not DETAIL_PATH.exists():
        return None

    detail_rows = []
    raw_records = []
    try:
        cache_source = str(DETAIL_PATH.relative_to(ROOT))
    except ValueError:
        cache_source = str(DETAIL_PATH)
    with DETAIL_PATH.open(encoding="utf-8", newline="") as input_file:
        for cached_row in csv.DictReader(input_file):
            row = {field: cached_row.get(field, "") for field in DETAIL_FIELDS}
            detail_rows.append(row)
            raw_records.append(
                {
                    "platform": row["platform"],
                    "cache_source": cache_source,
                    "row": cached_row,
                }
            )

    print(f"loaded {len(detail_rows)} detail rows from {cache_source}", flush=True)
    return detail_rows, raw_records, [f"Loaded platform detail from local cache `{cache_source}`"]


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
            anomalies.append(f"Kalshi has no approved comparable {market_type} {ASSET_DISPLAY} Up/Down series")

    return detail_rows, raw_records, anomalies, cutoff


def collect_polymarket_wallet_activity(detail_rows: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    market_rows = []
    anomalies = []
    daily_active_wallets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    daily_taker_wallet_sizes: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily_participant_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    daily_taker_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    daily_trade_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    daily_platform_totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    daily_statuses: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    monthly_active_wallets: dict[tuple[str, str], set[str]] = defaultdict(set)
    monthly_taker_wallet_sizes: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    monthly_participant_counts: dict[tuple[str, str], int] = defaultdict(int)
    monthly_taker_counts: dict[tuple[str, str], int] = defaultdict(int)
    monthly_trade_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    monthly_platform_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    monthly_statuses: dict[tuple[str, str], set[str]] = defaultdict(set)
    polymarket_rows = [row for row in detail_rows if row["platform"] == "polymarket"]
    batch_rows_by_conditions = [
        ([row["platform_market_id"] for row in batch_rows], batch_rows)
        for batch_rows in chunked(polymarket_rows, POLYMARKET_TRADE_BATCH_SIZE)
    ]

    POLYMARKET_TRADES_RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with POLYMARKET_TRADES_RAW_PATH.open("wb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as gzip_output:
            with io.TextIOWrapper(gzip_output, encoding="utf-8") as output:
                with concurrent.futures.ThreadPoolExecutor(max_workers=POLYMARKET_TRADE_WORKERS) as executor:
                    pending = {}
                    batch_iter = iter(batch_rows_by_conditions)

                    def submit_next_batch() -> None:
                        try:
                            condition_ids, batch_rows = next(batch_iter)
                        except StopIteration:
                            return
                        pending[executor.submit(fetch_polymarket_trade_batch, condition_ids)] = batch_rows

                    for _ in range(POLYMARKET_TRADE_WORKERS * 2):
                        submit_next_batch()

                    while pending:
                        done, _ = concurrent.futures.wait(
                            pending, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        future = done.pop()
                        batch_rows = pending.pop(future)
                        submit_next_batch()
                        participant_by_market, taker_by_market = future.result()
                        print(
                            f"polymarket trade batch: {len(batch_rows)} markets, "
                            f"{sum(len(group['trades']) for group in participant_by_market.values())} participant rows, "
                            f"{sum(len(group['trades']) for group in taker_by_market.values())} taker rows",
                            flush=True,
                        )
                        for row in batch_rows:
                            condition_id = row["platform_market_id"]
                            participant_group = participant_by_market[condition_id]
                            taker_group = taker_by_market[condition_id]
                            participant_trades = participant_group["trades"]
                            taker_trades = taker_group["trades"]
                            participant_status = participant_group["status"]
                            taker_status = taker_group["status"]
                            participant_wallets = {trade["wallet"] for trade in participant_trades}
                            taker_wallet_sizes: dict[str, Decimal] = defaultdict(Decimal)
                            for trade in taker_trades:
                                taker_wallet_sizes[trade["wallet"]] += trade["size"]
                            trade_total = sum(taker_wallet_sizes.values(), Decimal("0"))
                            top10_total = sum(sorted(taker_wallet_sizes.values(), reverse=True)[:10], Decimal("0"))
                            platform_total = decimal_from_value(row["volume"], "detail.volume")
                            gap = platform_total - trade_total
                            daily_key = (row["date_utc"], row["month_utc"], row["market_type"])
                            monthly_key = (row["month_utc"], row["market_type"])
                            for wallet, size in taker_wallet_sizes.items():
                                daily_taker_wallet_sizes[daily_key][wallet] += size
                                monthly_taker_wallet_sizes[monthly_key][wallet] += size
                            daily_active_wallets[daily_key].update(participant_wallets)
                            monthly_active_wallets[monthly_key].update(participant_wallets)
                            daily_participant_counts[daily_key] += len(participant_trades)
                            daily_taker_counts[daily_key] += len(taker_trades)
                            daily_trade_totals[daily_key] += trade_total
                            daily_platform_totals[daily_key] += platform_total
                            daily_statuses[daily_key].update({participant_status, taker_status})
                            monthly_participant_counts[monthly_key] += len(participant_trades)
                            monthly_taker_counts[monthly_key] += len(taker_trades)
                            monthly_trade_totals[monthly_key] += trade_total
                            monthly_platform_totals[monthly_key] += platform_total
                            monthly_statuses[monthly_key].update({participant_status, taker_status})
                            market_rows.append(
                                {
                                    "asset": ASSET_KEY,
                                    "platform": "polymarket",
                                    "month_utc": row["month_utc"],
                                    "date_utc": row["date_utc"],
                                    "market_type": row["market_type"],
                                    "platform_series": row["platform_series"],
                                    "platform_market_id": condition_id,
                                    "title": row["title"],
                                    "active_wallet_count": str(len(participant_wallets)),
                                    "participant_trade_count": str(len(participant_trades)),
                                    "taker_trade_count": str(len(taker_trades)),
                                    "trade_history_volume": format_decimal(trade_total),
                                    "top10_wallet_volume": format_decimal(top10_total),
                                    "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                                    "platform_reported_volume": format_decimal(platform_total),
                                    "volume_gap": format_decimal(gap),
                                    "volume_gap_pct": volume_gap_pct(gap, platform_total),
                                    "data_status": (
                                        "truncated"
                                        if "truncated" in {participant_status, taker_status}
                                        else "complete"
                                    ),
                                }
                            )
                            for taker_only, trades in ((False, participant_trades), (True, taker_trades)):
                                for trade in trades:
                                    json.dump(
                                        {
                                            "platform": "polymarket",
                                            "asset": ASSET_KEY,
                                            "month_utc": row["month_utc"],
                                            "market_type": row["market_type"],
                                            "platform_market_id": condition_id,
                                            "taker_only": taker_only,
                                            "trade": trade["raw"],
                                        },
                                        output,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    )
                                    output.write("\n")
                            if participant_status == "truncated" or taker_status == "truncated":
                                anomalies.append(f"Polymarket trades truncated for {condition_id}")

    market_rows.sort(
        key=lambda row: (
            row["month_utc"],
            row["date_utc"],
            market_type_sort_key(row["market_type"]),
            row["platform_market_id"],
        )
    )
    daily_rows = []
    for key in sorted(daily_trade_totals, key=lambda item: (item[1], item[0], market_type_sort_key(item[2]))):
        date_utc, month_utc, market_type = key
        trade_total = daily_trade_totals[key]
        top10_total = sum(sorted(daily_taker_wallet_sizes[key].values(), reverse=True)[:10], Decimal("0"))
        platform_total = daily_platform_totals[key]
        gap = platform_total - trade_total
        daily_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "date_utc": date_utc,
                "month_utc": month_utc,
                "market_type": market_type,
                "active_wallet_count": str(len(daily_active_wallets[key])),
                "participant_trade_count": str(daily_participant_counts[key]),
                "taker_trade_count": str(daily_taker_counts[key]),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": "truncated" if "truncated" in daily_statuses[key] else "complete",
                "unavailable_reason": "",
            }
        )

    monthly_rows = []
    for key in sorted(monthly_trade_totals, key=lambda item: (item[0], market_type_sort_key(item[1]))):
        month_utc, market_type = key
        trade_total = monthly_trade_totals[key]
        top10_total = sum(sorted(monthly_taker_wallet_sizes[key].values(), reverse=True)[:10], Decimal("0"))
        platform_total = monthly_platform_totals[key]
        gap = platform_total - trade_total
        monthly_rows.append(
            {
                "asset": ASSET_KEY,
                "platform": "polymarket",
                "month_utc": month_utc,
                "market_type": market_type,
                "active_wallet_count": str(len(monthly_active_wallets[key])),
                "participant_trade_count": str(monthly_participant_counts[key]),
                "taker_trade_count": str(monthly_taker_counts[key]),
                "trade_history_volume": format_decimal(trade_total),
                "top10_wallet_volume": format_decimal(top10_total),
                "top10_wallet_volume_share": decimal_ratio(top10_total, trade_total),
                "platform_reported_volume": format_decimal(platform_total),
                "volume_gap": format_decimal(gap),
                "volume_gap_pct": volume_gap_pct(gap, platform_total),
                "data_status": "truncated" if "truncated" in monthly_statuses[key] else "complete",
                "unavailable_reason": "",
            }
        )
    return market_rows, daily_rows, monthly_rows, anomalies


def collect_polymarket_wallet_positions(detail_rows: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    anomalies = []
    polymarket_rows = [row for row in detail_rows if row["platform"] == "polymarket"]
    detail_by_market = {row["platform_market_id"]: row for row in polymarket_rows}

    if POLYMARKET_TRADES_RAW_PATH.exists():
        POLYMARKET_TRADES_RAW_PATH.unlink()

    WALLET_POSITIONS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(WALLET_POSITIONS_STATE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        initialize_wallet_positions_state(connection)
        completed_markets = {
            row[0]
            for row in connection.execute("SELECT platform_market_id FROM completed_markets")
            if row[0] in detail_by_market
        }
        pending_rows = [row for row in polymarket_rows if row["platform_market_id"] not in completed_markets]
        completed = len(completed_markets)
        total = len(polymarket_rows)
        if completed:
            print(
                f"polymarket positions resume: {completed}/{total} markets already completed "
                f"from {WALLET_POSITIONS_STATE_PATH.relative_to(ROOT)}",
                flush=True,
            )

        pending_iter = iter(pending_rows)

        def submit_next(executor, pending: dict) -> bool:
            try:
                row = next(pending_iter)
            except StopIteration:
                return False
            pending[executor.submit(fetch_polymarket_positions_for_market, row["platform_market_id"])] = row
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=POLYMARKET_POSITIONS_WORKERS) as executor:
            pending = {}
            for _ in range(POLYMARKET_POSITIONS_WORKERS):
                if not submit_next(executor, pending):
                    break
            while pending:
                done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    row = pending.pop(future)
                    condition_id = row["platform_market_id"]
                    group = future.result()
                    store_wallet_position_aggregate(connection, row, group)
                    completed += 1
                    if completed == 1 or completed % 250 == 0 or group["status"] == "positions_truncated":
                        print(
                            f"polymarket positions: {completed}/{total} markets, "
                            f"{group['position_count']} position rows for {condition_id}",
                            flush=True,
                        )
                    submit_next(executor, pending)

        completed_by_market = {
            row["platform_market_id"]: row
            for row in connection.execute(
                """
                SELECT platform_market_id, active_wallet_count, position_count, position_total, top10_wallet_volume,
                       platform_reported_volume, data_status
                FROM completed_markets
                """
            )
            if row["platform_market_id"] in detail_by_market
        }

        market_rows = []
        for condition_id, metrics in completed_by_market.items():
            row = detail_by_market[condition_id]
            position_total = decimal_from_sqlite(metrics["position_total"])
            top10_total = decimal_from_sqlite(metrics["top10_wallet_volume"])
            platform_total = decimal_from_sqlite(metrics["platform_reported_volume"])
            gap = platform_total - position_total
            market_rows.append(
                {
                    "asset": ASSET_KEY,
                    "platform": "polymarket",
                    "month_utc": row["month_utc"],
                    "date_utc": row["date_utc"],
                    "market_type": row["market_type"],
                    "platform_series": row["platform_series"],
                    "platform_market_id": condition_id,
                    "title": row["title"],
                    "active_wallet_count": str(metrics["active_wallet_count"]),
                    "participant_trade_count": str(metrics["position_count"]),
                    "taker_trade_count": "",
                    "trade_history_volume": format_decimal(position_total),
                    "top10_wallet_volume": format_decimal(top10_total),
                    "top10_wallet_volume_share": decimal_ratio(top10_total, position_total),
                    "platform_reported_volume": format_decimal(platform_total),
                    "volume_gap": format_decimal(gap),
                    "volume_gap_pct": volume_gap_pct(gap, platform_total),
                    "data_status": metrics["data_status"],
                }
            )
            if metrics["data_status"] == "positions_truncated":
                anomalies.append(f"Polymarket positions truncated for {condition_id}")

        market_rows.sort(
            key=lambda row: (
                row["month_utc"],
                row["date_utc"],
                market_type_sort_key(row["market_type"]),
                row["platform_market_id"],
            )
        )

        daily_rows = []
        for stats in connection.execute(
            """
            SELECT date_utc, month_utc, market_type, SUM(position_count), SUM(position_total),
                   SUM(platform_reported_volume),
                   MAX(CASE WHEN data_status = 'positions_truncated' THEN 1 ELSE 0 END)
            FROM completed_markets
            GROUP BY date_utc, month_utc, market_type
            ORDER BY month_utc, date_utc, market_type
            """
        ):
            date_utc, month_utc, market_type = stats[0], stats[1], stats[2]
            active_wallet_count = connection.execute(
                "SELECT COUNT(*) FROM daily_wallets WHERE date_utc = ? AND month_utc = ? AND market_type = ?",
                (date_utc, month_utc, market_type),
            ).fetchone()[0]
            top10_total = sum(
                decimal_from_sqlite(row[0])
                for row in connection.execute(
                    """
                    SELECT total_bought FROM daily_wallets
                    WHERE date_utc = ? AND month_utc = ? AND market_type = ?
                    ORDER BY CAST(total_bought AS REAL) DESC
                    LIMIT 10
                    """,
                    (date_utc, month_utc, market_type),
                )
            )
            position_total = decimal_from_sqlite(stats[4])
            platform_total = decimal_from_sqlite(stats[5])
            gap = platform_total - position_total
            daily_rows.append(
                {
                    "asset": ASSET_KEY,
                    "platform": "polymarket",
                    "date_utc": date_utc,
                    "month_utc": month_utc,
                    "market_type": market_type,
                    "active_wallet_count": str(active_wallet_count),
                    "participant_trade_count": str(stats[3]),
                    "taker_trade_count": "",
                    "trade_history_volume": format_decimal(position_total),
                    "top10_wallet_volume": format_decimal(top10_total),
                    "top10_wallet_volume_share": decimal_ratio(top10_total, position_total),
                    "platform_reported_volume": format_decimal(platform_total),
                    "volume_gap": format_decimal(gap),
                    "volume_gap_pct": volume_gap_pct(gap, platform_total),
                    "data_status": "positions_truncated" if stats[6] else "positions_complete",
                    "unavailable_reason": "",
                }
            )
        daily_rows.sort(key=lambda row: (row["month_utc"], row["date_utc"], market_type_sort_key(row["market_type"])))

        monthly_rows = []
        for stats in connection.execute(
            """
            SELECT month_utc, market_type, SUM(position_count), SUM(position_total),
                   SUM(platform_reported_volume),
                   MAX(CASE WHEN data_status = 'positions_truncated' THEN 1 ELSE 0 END)
            FROM completed_markets
            GROUP BY month_utc, market_type
            ORDER BY month_utc, market_type
            """
        ):
            month_utc, market_type = stats[0], stats[1]
            active_wallet_count = connection.execute(
                "SELECT COUNT(*) FROM monthly_wallets WHERE month_utc = ? AND market_type = ?",
                (month_utc, market_type),
            ).fetchone()[0]
            top10_total = sum(
                decimal_from_sqlite(row[0])
                for row in connection.execute(
                    """
                    SELECT total_bought FROM monthly_wallets
                    WHERE month_utc = ? AND market_type = ?
                    ORDER BY CAST(total_bought AS REAL) DESC
                    LIMIT 10
                    """,
                    (month_utc, market_type),
                )
            )
            position_total = decimal_from_sqlite(stats[3])
            platform_total = decimal_from_sqlite(stats[4])
            gap = platform_total - position_total
            monthly_rows.append(
                {
                    "asset": ASSET_KEY,
                    "platform": "polymarket",
                    "month_utc": month_utc,
                    "market_type": market_type,
                    "active_wallet_count": str(active_wallet_count),
                    "participant_trade_count": str(stats[2]),
                    "taker_trade_count": "",
                    "trade_history_volume": format_decimal(position_total),
                    "top10_wallet_volume": format_decimal(top10_total),
                    "top10_wallet_volume_share": decimal_ratio(top10_total, position_total),
                    "platform_reported_volume": format_decimal(platform_total),
                    "volume_gap": format_decimal(gap),
                    "volume_gap_pct": volume_gap_pct(gap, platform_total),
                    "data_status": "positions_truncated" if stats[5] else "positions_complete",
                    "unavailable_reason": "",
                }
            )
        monthly_rows.sort(key=lambda row: (row["month_utc"], market_type_sort_key(row["market_type"])))
    finally:
        connection.close()
    return market_rows, daily_rows, monthly_rows, anomalies


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
        f"# Kalshi {ASSET_DISPLAY} Series Discovery",
        "",
        f"- Generated at: `{isoformat_z(datetime.now(timezone.utc))}`",
        f"- Exact directional rows may be included in combined {ASSET_DISPLAY} Up/Down totals.",
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
    wallet_daily_rows: list[dict],
    wallet_monthly_rows: list[dict],
    anomalies: list[str],
    kalshi_cutoff: datetime | None,
) -> str:
    generated_at = isoformat_z(datetime.now(timezone.utc))
    platform_totals = lookup(platform_monthly_rows, ("platform", "month_utc", "market_type"), "total_volume")
    platform_counts = lookup(platform_monthly_rows, ("platform", "month_utc", "market_type"), "market_count")
    combined_totals = lookup(combined_monthly_rows, ("month_utc", "market_type"), "total_volume")
    month_labels = [month_utc for month_utc, _, _ in MONTH_WINDOWS]
    approved_kalshi = ", ".join(f"`{ticker}`" for ticker in sorted(KALSHI_EXACT_DIRECTION_SERIES)) or "`none`"
    missing_kalshi_types = [
        market_type
        for market_type in MARKET_TYPES
        if market_type not in set(KALSHI_EXACT_DIRECTION_SERIES.values())
    ]
    excluded_kalshi = ", ".join(f"`{ticker}`" for ticker in sorted(KALSHI_EXCLUDED_SERIES)) or "`none`"

    lines = [
        f"# {ASSET_DISPLAY} Up/Down Cross-Platform Volume Summary",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Date range: `{MONTH_WINDOWS[0][0]}` through `{MONTH_WINDOWS[-1][0]}` using UTC half-open month windows.",
        "- Platforms: `polymarket`, `kalshi`.",
        f"- Combined totals include only semantically comparable `comparable=true` {ASSET_DISPLAY} direction rows.",
        "- Volume basis: platform-reported cumulative contract/share volume, not USD notional.",
        "- Polymarket volume priority: `market.volumeClob`, `market.volumeNum`, `market.volume`, then `event.volume`.",
        "- Kalshi volume source: `market.volume_fp`.",
        "- Kalshi historical cutoff used at runtime: "
        + (f"`{isoformat_z(kalshi_cutoff)}`" if kalshi_cutoff is not None else "`not fetched; detail cache reused`"),
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
            "Wallet metric notes:",
            "- `active_wallet_count` uses Polymarket `takerOnly=false` participant rows and counts unique wallets.",
            "- `top10_wallet_volume_share` uses Polymarket `takerOnly=true` rows so the denominator stays market-volume aligned.",
            "- Rows marked `positions_complete` or `positions_truncated` use Polymarket `/v1/market-positions` "
            "`status=ALL`; wallet volume is based on `totalBought`, not the `/trades` taker stream.",
            "- Kalshi wallet metrics are `not_available` because public Kalshi trades do not expose wallet identifiers.",
            "- Rows marked `not_generated` intentionally leave wallet counts blank; see unavailable reasons below.",
            "",
            "## Wallet Activity Daily",
            "",
            "| platform | date_utc | market_type | active_wallet_count | top10_wallet_volume_share | data_status |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in wallet_daily_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["platform"],
                    row["date_utc"],
                    row["market_type"],
                    row["active_wallet_count"],
                    row["top10_wallet_volume_share"],
                    row["data_status"],
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Wallet Activity Monthly",
            "",
            "| platform | month_utc | market_type | active_wallet_count | top10_wallet_volume_share | data_status |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in wallet_monthly_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["platform"],
                    row["month_utc"],
                    row["market_type"],
                    row["active_wallet_count"],
                    row["top10_wallet_volume_share"],
                    row["data_status"],
                ]
            )
            + " |"
        )

    unavailable_reasons = sorted(
        {row["unavailable_reason"] for row in wallet_daily_rows + wallet_monthly_rows if row["unavailable_reason"]}
    )
    if unavailable_reasons:
        lines.extend(["", "Wallet metric unavailable reasons:"])
        lines.extend(f"- `{reason}`" for reason in unavailable_reasons)

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
            f"- Approved comparable Kalshi {ASSET_DISPLAY} Up/Down series: {approved_kalshi}.",
            "- No approved comparable Kalshi "
            + ", ".join(f"`{market_type}`" for market_type in missing_kalshi_types)
            + " series is included.",
            f"- Excluded Kalshi {ASSET_DISPLAY} diagnostics include: {excluded_kalshi}.",
            "",
            "## Output Files",
            "",
            f"- Compressed raw platform snapshot: `{RAW_PATH.relative_to(ROOT)}`",
            f"- Market detail CSV: `{DETAIL_PATH.relative_to(ROOT)}`",
            f"- Platform daily volume CSV: `{PLATFORM_DAILY_PATH.relative_to(ROOT)}`",
            f"- Platform monthly volume CSV: `{PLATFORM_MONTHLY_PATH.relative_to(ROOT)}`",
            f"- Combined daily volume CSV: `{COMBINED_DAILY_PATH.relative_to(ROOT)}`",
            f"- Combined monthly volume CSV: `{COMBINED_MONTHLY_PATH.relative_to(ROOT)}`",
            f"- Wallet market activity CSV: `{WALLET_MARKET_PATH.relative_to(ROOT)}`",
            f"- Wallet daily activity CSV: `{WALLET_DAILY_PATH.relative_to(ROOT)}`",
            f"- Wallet monthly activity CSV: `{WALLET_MONTHLY_PATH.relative_to(ROOT)}`",
            f"- Markdown summary: `{REPORT_PATH.relative_to(ROOT)}`",
            f"- Kalshi discovery diagnostics: `{DISCOVERY_REPORT_PATH.relative_to(ROOT)}`",
        ]
    )
    if POLYMARKET_TRADES_RAW_PATH.exists():
        insert_at = lines.index("## Output Files") + 9
        lines.insert(insert_at, f"- Polymarket raw trades snapshot: `{POLYMARKET_TRADES_RAW_PATH.relative_to(ROOT)}`")
    if anomalies:
        lines.extend(["", "## Anomalies", ""])
        lines.extend(f"- {anomaly}" for anomaly in anomalies)
    return "\n".join(lines) + "\n"


def generate_report(
    wallet_mode: str = "full",
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[str]]:
    cached_detail = load_cached_detail_rows()
    if cached_detail is None:
        discovery_rows = fetch_kalshi_series_discovery()
        DISCOVERY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DISCOVERY_REPORT_PATH.write_text(build_discovery_report(discovery_rows), encoding="utf-8")

        polymarket_rows, polymarket_raw, polymarket_anomalies = collect_polymarket_rows()
        kalshi_rows, kalshi_raw, kalshi_anomalies, kalshi_cutoff = collect_kalshi_rows(discovery_rows)
        detail_rows = polymarket_rows + kalshi_rows
        raw_records = polymarket_raw + kalshi_raw
        anomalies = polymarket_anomalies + kalshi_anomalies
    else:
        detail_rows, raw_records, anomalies = cached_detail
        kalshi_cutoff = None
        DISCOVERY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DISCOVERY_REPORT_PATH.write_text(
            "\n".join(
                [
                    f"# Kalshi {ASSET_DISPLAY} Series Discovery",
                    "",
                    f"- Skipped at: `{isoformat_z(datetime.now(timezone.utc))}`",
                    f"- Reason: reused `{DETAIL_PATH.relative_to(ROOT)}` instead of refetching platform markets.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

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
    if wallet_mode == "full":
        (
            wallet_market_rows,
            wallet_daily_rows,
            wallet_monthly_rows,
            wallet_anomalies,
        ) = collect_polymarket_wallet_activity(detail_rows)
    elif wallet_mode == "positions":
        (
            wallet_market_rows,
            wallet_daily_rows,
            wallet_monthly_rows,
            wallet_anomalies,
        ) = collect_polymarket_wallet_positions(detail_rows)
    elif wallet_mode == "status-only":
        wallet_market_rows = build_polymarket_wallet_market_not_generated_rows(detail_rows)
        wallet_daily_rows = build_polymarket_wallet_not_generated_rows(platform_daily_rows)
        wallet_monthly_rows = build_polymarket_wallet_not_generated_rows(platform_monthly_rows)
        wallet_anomalies = [
            "Polymarket wallet trade history generation was skipped; wallet metrics are marked not_generated"
        ]
        write_jsonl_gzip(POLYMARKET_TRADES_RAW_PATH, [])
    else:
        raise ValueError(f"unsupported wallet_mode: {wallet_mode}")
    wallet_daily_rows.extend(build_kalshi_wallet_unavailable_rows(platform_daily_rows))
    wallet_monthly_rows.extend(build_kalshi_wallet_unavailable_rows(platform_monthly_rows))
    anomalies.extend(wallet_anomalies)

    write_jsonl_gzip(RAW_PATH, raw_records)
    write_csv(DETAIL_PATH, DETAIL_FIELDS, detail_rows)
    write_csv(PLATFORM_DAILY_PATH, PLATFORM_DAILY_FIELDS, platform_daily_rows)
    write_csv(PLATFORM_MONTHLY_PATH, PLATFORM_MONTHLY_FIELDS, platform_monthly_rows)
    write_csv(COMBINED_DAILY_PATH, COMBINED_DAILY_FIELDS, combined_daily_rows)
    write_csv(COMBINED_MONTHLY_PATH, COMBINED_MONTHLY_FIELDS, combined_monthly_rows)
    write_csv(WALLET_MARKET_PATH, WALLET_MARKET_FIELDS, wallet_market_rows)
    write_csv(WALLET_DAILY_PATH, WALLET_DAILY_FIELDS, wallet_daily_rows)
    write_csv(WALLET_MONTHLY_PATH, WALLET_MONTHLY_FIELDS, wallet_monthly_rows)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        build_markdown_report(
            detail_rows,
            platform_daily_rows,
            platform_monthly_rows,
            combined_daily_rows,
            combined_monthly_rows,
            wallet_daily_rows,
            wallet_monthly_rows,
            anomalies,
            kalshi_cutoff,
        ),
        encoding="utf-8",
    )

    return (
        detail_rows,
        platform_daily_rows,
        platform_monthly_rows,
        combined_daily_rows,
        combined_monthly_rows,
        wallet_market_rows,
        wallet_daily_rows,
        wallet_monthly_rows,
        anomalies,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset",
        choices=sorted(ASSET_CONFIGS),
        default="btc",
        help="asset family to report; defaults to btc for backward compatibility",
    )
    parser.add_argument(
        "--wallet-mode",
        choices=("full", "positions", "status-only"),
        default="full",
        help=(
            "full fetches Polymarket wallet trades; positions uses market-position wallet data; "
            "status-only writes report rows without wallet metrics"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_asset(args.asset)
    (
        detail_rows,
        platform_daily_rows,
        platform_monthly_rows,
        combined_daily_rows,
        combined_monthly_rows,
        wallet_market_rows,
        wallet_daily_rows,
        wallet_monthly_rows,
        anomalies,
    ) = generate_report(wallet_mode=args.wallet_mode)
    print("generated outputs:")
    output_paths = [
        RAW_PATH,
        DETAIL_PATH,
        PLATFORM_DAILY_PATH,
        PLATFORM_MONTHLY_PATH,
        COMBINED_DAILY_PATH,
        COMBINED_MONTHLY_PATH,
        WALLET_MARKET_PATH,
        WALLET_DAILY_PATH,
        WALLET_MONTHLY_PATH,
        REPORT_PATH,
        DISCOVERY_REPORT_PATH,
    ]
    if POLYMARKET_TRADES_RAW_PATH.exists():
        output_paths.insert(6, POLYMARKET_TRADES_RAW_PATH)
    for path in output_paths:
        print(f"- {path.relative_to(ROOT)}")
    print(f"detail rows: {len(detail_rows)}")
    print(f"platform daily rows: {len(platform_daily_rows)}")
    print(f"platform monthly rows: {len(platform_monthly_rows)}")
    print(f"combined daily rows: {len(combined_daily_rows)}")
    print(f"combined monthly rows: {len(combined_monthly_rows)}")
    print(f"wallet market rows: {len(wallet_market_rows)}")
    print(f"wallet daily rows: {len(wallet_daily_rows)}")
    print(f"wallet monthly rows: {len(wallet_monthly_rows)}")
    print(f"anomalies: {len(anomalies)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
