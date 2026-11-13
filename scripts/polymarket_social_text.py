"""Render X threads and LinkedIn posts from completed cloud report JSON only."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from urllib.parse import quote

from polymarket_updown_buy_metrics import amount, event_summary, percentage, share_percent


TEXT_VERSION = "social_text_v1"
X_LIMIT = 280
LINKEDIN_LIMIT = 3000
_URL = re.compile(r"https?://[^\s]+")


def x_weighted_length(text: str) -> int:
    """Count generated ASCII posts, treating each URL as 23 X characters.

    The renderer deliberately uses ASCII outside URLs. Reject other characters
    rather than claim this small helper implements the full twitter-text parser.
    """
    plain = _URL.sub("x" * 23, text)
    if not plain.isascii():
        raise ValueError("X text must use ASCII outside URLs")
    return len(plain)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _number(value, *, compact: bool = False, money: bool = False) -> str:
    if value is None:
        return "N/A"
    number = Decimal(str(value))
    if not compact:
        return amount(number) if money else f"{int(number):,}"
    for unit, scale in (("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if number >= scale:
            with localcontext() as context:
                context.prec = max(100, len(number.as_tuple().digits) + 10)
                scaled = (number / scale).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            return f"{scaled:.2f}{unit}"
    return amount(number) if money else f"{int(number):,}"


def _volume(current: dict) -> Decimal | None:
    raw = current.get("volume_micro_usdc")
    return None if raw is None else Decimal(str(raw)) / 1_000_000


def _source(bundle: dict, reference: dict | None) -> tuple[str, str]:
    reference = reference or bundle.get("source_reference")
    if reference and reference.get("dataset") and type(reference.get("version")) is int:
        if reference["version"] <= 0:
            raise ValueError("Dataset source version must be positive; supply --source-dataset and --source-version")
        dataset = reference["dataset"]
        return (f"https://www.kaggle.com/datasets/{quote(dataset, safe='/')}/versions/{reference['version']}",
                f"Analysis: {dataset} v{reference['version']}.")
    if bundle["kind"] != "daily":
        raise ValueError("Weekly/monthly publication requires an analysis reference; supply --source-dataset and --source-version")
    stamp = bundle["target_utc"]
    raw = bundle.get("source_versions", {}).get(stamp, {})
    if raw.get("dataset") and type(raw.get("version")) is int and raw["version"] > 0:
        dataset = raw["dataset"]
        return (f"https://www.kaggle.com/datasets/{quote(dataset, safe='/')}/versions/{raw['version']}",
                f"Raw source: {stamp}, {dataset} v{raw['version']}.")
    raise ValueError("A fixed dataset version is required for publication text")


def _period_label(bundle: dict) -> str:
    start = date.fromisoformat(bundle["start_date_utc"])
    end = date.fromisoformat(bundle["end_date_utc_exclusive"])
    if bundle["kind"] == "daily":
        return str(start)
    return f"{bundle['kind'].title()} | {start} to {end - timedelta(days=1)}"


def _history(payload: dict, distribution: dict) -> dict:
    return distribution.get("bot_history") or payload.get("bot_history") or {}


def _thread(sections: list[str]) -> list[str]:
    """Keep each logical section together unless a whole section cannot fit."""
    parts = []
    # Reserve a numbering prefix even when unusually large data creates more posts.
    content_limit = X_LIMIT - len("999/999 ")
    for section in sections:
        if x_weighted_length(section) <= content_limit:
            parts.append(section)
            continue
        lines = []
        for line in section.splitlines():
            if x_weighted_length(line) > content_limit:
                raise ValueError("An X report field exceeds the post limit")
            candidate = "\n".join([*lines, line])
            if lines and x_weighted_length(candidate) > content_limit:
                parts.append("\n".join(lines))
                lines = []
            lines.append(line)
        if lines:
            parts.append("\n".join(lines))
    posts = [f"{index}/{len(parts)} {text}" for index, text in enumerate(parts, 1)]
    if any(x_weighted_length(text) > X_LIMIT for text in posts):
        raise ValueError("X report exceeds the post limit")
    return posts


def _x_sections(bundle: dict, summary: dict, url: str, *, compact: bool) -> list[str]:
    payload, distribution = bundle["payload"], bundle["distribution"]
    current = payload["current"]
    all_buy, bots = summary["all_wallets"], summary["suspected_bots"]
    number = lambda value, money=False: _number(value, compact=compact, money=money)
    series = number(distribution["series_count"])
    first = (f"Polymarket crypto Up/Down | {_period_label(bundle)}\n"
             f"UTC market end | {series} series\n"
             "Metric | Value\n"
             f"Markets | {number(current['total_markets'])}\n"
             f"Wallet addresses | {number(current['unique_wallets'])}\n"
             f"Market volume USDC | {number(_volume(current), True)}\n"
             f"Includes earlier trades in these markets.\n{url}")
    second = ("Reconstructed BUY events\n"
              "Metric | All BUY | Flagged-buyer subset\n"
              f"Events | {number(all_buy['trade_count'])} | {number(bots['trade_count'])}\n"
              f"Shares | {number(all_buy['shares_exact'], True)} | {number(bots['shares_exact'], True)}\n"
              f"Up-equiv. USDC | {number(all_buy['notional_usdc_exact'], True)} | {number(bots['notional_usdc_exact'], True)}\n"
              "Subset: events with a suspected-bot buyer; not wallet spending.")
    third = ["Flagged subset / all BUY",
             "Events | " + percentage(summary["bot_trade_coverage_percent"]),
             "Shares | " + percentage(summary["bot_shares_coverage_percent"]),
             "Up-equiv. notional | " + percentage(summary["bot_up_notional_coverage_percent"]),
             "Active flagged addresses | " + number(current["suspected_bot_wallets"])]
    if bundle["kind"] == "daily" and current.get("wallets_meeting_daily_frequency_rule") is not None:
        third.append("Daily screen matches | " + number(current["wallets_meeting_daily_frequency_rule"]))
    rule = payload.get("bot_rule") or distribution["bot_rule"]
    third.extend([f"Screen: mean interval <={rule['max_mean_interval_seconds']}s; span >={rule['min_span_minutes']}min.",
                  "Up-equiv. price = Up price or 1 - Down price."])
    pending = _history(payload, distribution).get("history_pending")
    if pending is True:
        third.append("Suspected bots; history incomplete.")
    elif pending is False:
        third.append("Suspected bots; labels use observed history.")
    else:
        third.append("Suspected bots; history unverified.")
    return [first, second, "\n".join(third)]


def _linkedin(bundle: dict, summary: dict, url: str, source_label: str) -> str:
    payload, distribution = bundle["payload"], bundle["distribution"]
    current = payload["current"]
    all_buy, bots = summary["all_wallets"], summary["suspected_bots"]
    lines = ["Polymarket Crypto Up/Down", _period_label(bundle).replace(" | ", ": "),
             "UTC market-end cohort; includes earlier trades in these markets.", "",
             "MARKET OVERVIEW", "",
             f"Official series: {distribution['series_count']:,}",
             f"Completed markets: {current['total_markets']:,}",
             f"Unique wallet addresses: {current['unique_wallets']:,}",
             f"Market volume, counted once (USDC): {_number(_volume(current), money=True)}", "",
             "ALL RECONSTRUCTED BUY EVENTS", "",
             f"Events: {all_buy['trade_count']:,}",
             f"Shares: {amount(all_buy['shares_exact'])}",
             f"Up-equivalent notional (USDC): {amount(all_buy['notional_usdc_exact'])}", "",
             "SUSPECTED-BOT BUYER PARTICIPATION", "",
             f"Events: {bots['trade_count']:,} ({percentage(summary['bot_trade_coverage_percent'])} of all BUY events)",
             f"Shares: {amount(bots['shares_exact'])} ({percentage(summary['bot_shares_coverage_percent'])} of all BUY shares)",
             f"Up-equivalent notional (USDC): {amount(bots['notional_usdc_exact'])} ({percentage(summary['bot_up_notional_coverage_percent'])} of all BUY notional)",
             "The subset contains events with at least one flagged buyer. It measures participation, not those wallets' own spending.", "",
             "UP-EQUIVALENT PRICE DISTRIBUTION", "",
             "Each row: all BUY / flagged-buyer subset, as percentages of total all-BUY Up-equivalent notional."]
    denominator = all_buy["notional_usdc_exact"]
    all_bins = distribution["populations"]["all_wallets"]["bins"]
    bot_bins = distribution["populations"]["suspected_bots"]["bins"]
    if len(all_bins) != 10 or len(bot_bins) != 10:
        raise ValueError("Publication price distribution requires ten bins per population")
    for all_row, bot_row in zip(all_bins, bot_bins):
        if any(all_row[key] != bot_row[key] for key in ("lower_cents", "upper_cents", "upper_inclusive")):
            raise ValueError("Publication price bins differ between populations")
        interval = f"[{all_row['lower_cents']},{all_row['upper_cents']}{']' if all_row['upper_inclusive'] else ')'}"
        all_percent = percentage(share_percent(all_row["notional_usdc_exact"], denominator))
        bot_percent = percentage(share_percent(bot_row["notional_usdc_exact"], denominator))
        lines.append(f"{interval} cents: {all_percent} / {bot_percent}")
    lines.extend([f"Total: {percentage(share_percent(all_buy['notional_usdc_exact'], denominator))} / {percentage(summary['bot_up_notional_coverage_percent'])}",
                  "Totals use unrounded values. Intervals include the lower bound; only the final interval includes its upper bound.", "",
                  "ADDRESS SCREEN", "",
                  f"Active historically flagged addresses: {current['suspected_bot_wallets']:,}"])
    if bundle["kind"] == "daily" and current.get("wallets_meeting_daily_frequency_rule") is not None:
        lines.append(f"Meeting the screen on this date: {current['wallets_meeting_daily_frequency_rule']:,}")
    rule = payload.get("bot_rule") or distribution["bot_rule"]
    lines.extend([f"Screen: mean trade interval <={rule['max_mean_interval_seconds']} seconds; activity span >={rule['min_span_minutes']} minutes.",
                  "Labels apply from the first observed qualifying date onward; they do not confirm automated control."])
    pending = _history(payload, distribution).get("history_pending")
    if pending is True:
        lines.append("Historical coverage is incomplete.")
    elif pending is not False:
        lines.append("Historical coverage is not specified.")
    lines.extend(["", "Up-equivalent price = original Up price or 1 minus original Down price. BUY notional differs from market volume.",
                  "Wallet addresses are not people; reconstructed events are not unique orders.", "",
                  source_label, url])
    body = "\n".join(lines)
    if utf16_length(body) > LINKEDIN_LIMIT:
        raise ValueError("LinkedIn report exceeds 3,000 UTF-16 characters")
    return body


def render_text(bundle: dict, source_reference: dict | None = None) -> dict:
    """Return independent platform copy without rendering images or opening a database."""
    if bundle.get("schema_version") != 1 or bundle.get("status") != "complete":
        raise ValueError("Publication text requires a completed schema version 1 report")
    if bundle.get("kind") not in ("daily", "weekly", "monthly"):
        raise ValueError("Unsupported publication period")
    if bundle["payload"].get("date_basis") != "market_end_utc":
        raise ValueError("Unsupported report date basis")
    summary = event_summary(bundle["distribution"])
    url, source_label = _source(bundle, source_reference)
    sections = _x_sections(bundle, summary, url, compact=False)
    if any(x_weighted_length(section) > X_LIMIT - len("999/999 ") for section in sections):
        sections = _x_sections(bundle, summary, url, compact=True)
    return {"x": {"posts": _thread(sections)},
            "linkedin": {"body": _linkedin(bundle, summary, url, source_label)}}
