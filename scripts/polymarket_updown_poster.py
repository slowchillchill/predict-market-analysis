#!/usr/bin/env python3
"""从本地 SQLite 生成按 UTC 市场结束日统计的英文 X 海报和推文。"""

from __future__ import annotations

import argparse

from polymarket_updown_captions import write_sidecars
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import polymarket_updown_daily as daily
from polymarket_market_volume_query import load_market_volume
from polymarket_updown_rules import MAX_INTERVAL_SECONDS, MIN_SPAN_MINUTES
from polymarket_updown_buy_metrics import BASIS, event_summary, amount as event_amount, percentage
import polymarket_updown_poster_copy as approved_copy

ROOT = daily.core.ROOT
BACKGROUND = ROOT / "assets/posters/updown_background_v1.png"
FONT = ROOT / "assets/fonts/Inter.ttf"
OUTPUT_SIZE = (1600, 2000)
SCALE = OUTPUT_SIZE[0] / 1120
WHITE = "#f6f8ff"
MUTED = "#aebfdc"
GREEN = "#39eaa5"
RED = "#ff6389"
LINE = "#356994"
BASE = "#070d1b"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class IncompleteDay(ValueError):
    """沿用现有日报的完整日要求，未完成日期不生成金额海报。"""


@dataclass(frozen=True)
class PosterData:
    day: date
    current: dict
    previous: dict | None


def load_data(db: sqlite3.Connection, day: date, *, bot_first_seen: dict | None = None) -> PosterData:
    if bot_first_seen is None:
        from polymarket_updown_bot_registry import load_first_seen
        bot_first_seen = load_first_seen(db, day)
    db.create_function('updown_bot_as_of', 2,
                       lambda wallet, stamp: int(wallet in bot_first_seen and bot_first_seen[wallet] <= stamp))
    rows = {row["date_utc"]: dict(row) for row in db.execute(
        (ROOT / "sql/updown_poster.sql").read_text(encoding="utf-8"),
        {"day": day.isoformat(), "previous_day": (day - timedelta(days=1)).isoformat(),
         "max_interval_seconds": MAX_INTERVAL_SECONDS, "min_span_seconds": MIN_SPAN_MINUTES * 60},
    )}
    if day.isoformat() not in rows:
        status = daily.coverage(db, day.isoformat())
        raise IncompleteDay(
            f"{day} 尚未完整采集：完成 {status['completed_markets']}/{status['expected_markets']} "
            "个市场；未生成海报。"
        )
    for stamp, summary in rows.items():
        start = date.fromisoformat(stamp)
        volume = load_market_volume(db, start, start + timedelta(days=1))
        summary.update(volume_basis=volume["volume_basis"], volume_micro_usdc=volume["volume_micro_usdc"],
                       wallet_volume_basis='wallet_buys_only', bot_volume_basis='wallet_buys_only')
    return PosterData(day, rows[day.isoformat()], rows.get((day - timedelta(days=1)).isoformat()))


def amount_text(micro_usdc: int | None) -> str:
    if micro_usdc is None:
        return "N/A"
    amount = (Decimal(micro_usdc) / 1_000_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:,.2f}"


def percent_text(numerator: int | None, denominator: int | None, *, change: bool = False) -> str:
    if numerator is None or denominator is None or denominator == 0:
        return "N/A"
    percentage = Decimal(numerator) / Decimal(denominator) * 100
    percentage = percentage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if percentage == 0:
        return "0.00%"
    return f"{percentage:+.2f}%" if change else f"{percentage:.2f}%"


def display_values(data: PosterData, distribution_data: dict | None = None) -> dict[str, str]:
    current, previous = data.current, data.previous

    def change(field: str) -> tuple[str, str]:
        if previous is None:
            return "N/A", "prior day unavailable"
        if current[field] is None or previous[field] is None:
            return "N/A", "market volume unavailable"
        if previous[field] == 0:
            return "N/A", "prior day = 0"
        return percent_text(current[field] - previous[field], previous[field], change=True), "vs. previous day"

    volume_change, volume_caption = change("volume_micro_usdc")
    wallet_change, wallet_caption = change("unique_wallets")
    values = {
        "date": f"{MONTHS[data.day.month - 1]} {data.day.day}, {data.day.year} · UTC",
        "total_markets": f"{current['total_markets']:,}",
        "volume": amount_text(current["volume_micro_usdc"]),
        "volume_change": volume_change,
        "volume_change_caption": volume_caption,
        "unique_wallets": f"{current['unique_wallets']:,}",
        "wallet_change": wallet_change,
        "wallet_change_caption": wallet_caption,
        "bot_wallets": f"{current['suspected_bot_wallets']:,}",
        "bot_volume": amount_text(current["suspected_bot_volume_micro_usdc"]),
        "bot_volume_share": percent_text(current["suspected_bot_volume_micro_usdc"], current["wallet_volume_micro_usdc"]),
        "bot_panel_basis": "wallet_buys_only",
        "bot_volume_label": "Wallet BUY Amount",
        "bot_share_label": "Share of Wallet BUY Amount",
        "bot_denominator": amount_text(current['wallet_volume_micro_usdc']),
    }
    if distribution_data is not None:
        summary = event_summary(distribution_data)
        all_buy, bot_buy = summary['all_wallets'], summary['suspected_bots']
        shares_share = summary.get('bot_shares_coverage_percent')
        if shares_share is None and Decimal(all_buy['shares_exact']) != 0:
            shares_share = percent_text(Decimal(bot_buy['shares_exact']), Decimal(all_buy['shares_exact'])).rstrip('%')
        daily_rule_wallets = current.get('wallets_meeting_daily_frequency_rule')
        values.update(bot_panel_basis=BASIS,
                      bot_volume=event_amount(summary['suspected_bots']['notional_usdc_exact']),
                      bot_volume_share=percentage(summary['bot_up_notional_coverage_percent']),
                      bot_volume_label='BUY Event Up-equivalent Notional',
                      bot_share_label='Up-weighted BUY Event Coverage',
                      bot_denominator=event_amount(summary['all_wallets']['notional_usdc_exact']),
                      copy_version=approved_copy.COPY_VERSION,
                      market_end_date=f"{MONTHS[data.day.month - 1]} {data.day.day}, {data.day.year} · UTC Market End Date",
                      scope_label=f"{distribution_data.get('series_count', 'N/A')} Series · {current['total_markets']:,} Markets",
                      daily_rule_wallets='N/A' if daily_rule_wallets is None else f'{daily_rule_wallets:,}',
                      all_buy_trades=f"{all_buy['trade_count']:,}",
                      bot_buy_trades=f"{bot_buy['trade_count']:,}",
                      all_buy_trade_share='100.00%' if all_buy['trade_count'] else 'N/A',
                      bot_buy_trade_share=percentage(summary['bot_trade_coverage_percent']),
                      all_buy_shares=event_amount(all_buy['shares_exact']),
                      bot_buy_shares=event_amount(bot_buy['shares_exact']),
                      all_buy_shares_share='100.00%' if Decimal(all_buy['shares_exact']) else 'N/A',
                      bot_buy_shares_share=percentage(shares_share),
                      all_buy_notional=event_amount(all_buy['notional_usdc_exact']),
                      bot_buy_notional=event_amount(bot_buy['notional_usdc_exact']),
                      all_buy_notional_share='100.00%' if Decimal(all_buy['notional_usdc_exact']) else 'N/A',
                      bot_buy_notional_share=percentage(summary['bot_up_notional_coverage_percent']))
    return values


def _tweet_decimal(value, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid tweet {name}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid tweet {name}: expected a finite nonnegative number")
    return number


def _tweet_compact(number: Decimal) -> str:
    # Fixed precision keeps small amounts readable and prevents double rounding.
    # Values outside this range need editorial review rather than an enormous tweet.
    if number >= Decimal("1e15"):
        raise ValueError("tweet metric exceeds supported compact range (<1 quadrillion)")
    units = ((Decimal(1), ""), (Decimal(1000), "K"), (Decimal(1000000), "M"),
             (Decimal(1000000000), "B"), (Decimal(1000000000000), "T"))
    index = max(i for i, (divisor, _) in enumerate(units) if number >= divisor) if number >= 1 else 0
    rounded = (number / units[index][0]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded >= 1000 and index < len(units) - 1:
        index += 1
        rounded = (number / units[index][0]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:.2f}{units[index][1]}"


def _distribution_tweet(data: PosterData, distribution_data: dict) -> str:
    if distribution_data.get("date_utc", data.day.isoformat()) != data.day.isoformat():
        raise ValueError("tweet overview and BUY distribution dates differ")
    for field, expected in (("date_basis", "market_end_utc"),
                            ("market_scope", "all_crypto_updown_series"),
                            ("scope", "buy_participation"), ("bot_scope", "buy_participation"),
                            ("price_basis", "up_equivalent"),
                            ("notional_basis", "up_equivalent_price_times_shares"),
                            ("count_unit", "reconstructed_trades"),
                            ("notional_share_denominator", "all_wallets_buy_notional")):
        if field in distribution_data and distribution_data[field] != expected:
            raise ValueError(f"unsupported tweet distribution {field}")
    populations = distribution_data["populations"]
    groups = []
    for key in ("all_wallets", "suspected_bots"):
        group = populations[key]
        count = _tweet_decimal(group["trade_count"], f"{key} trades")
        if count != count.to_integral_value():
            raise ValueError(f"invalid tweet {key} trades: expected a whole number")
        groups.append((count, _tweet_decimal(group["shares_exact"], f"{key} shares"),
                       _tweet_decimal(group["notional_usdc_exact"], f"{key} notional")))
    all_buy, bot_buy = groups
    if any(bot > total for total, bot in zip(all_buy, bot_buy)):
        raise ValueError("suspected bot BUY metrics exceed all BUY metrics")
    bot_share = "N/A" if all_buy[2] == 0 else (
        f"{(bot_buy[2] / all_buy[2] * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}%")
    wallets = _tweet_decimal(data.current["unique_wallets"], "wallets")
    if wallets != wallets.to_integral_value():
        raise ValueError("invalid tweet wallets: expected a whole number")
    raw_volume = data.current["volume_micro_usdc"]
    if raw_volume is None:
        volume = "N/A"
    else:
        micro_volume = _tweet_decimal(raw_volume, "market volume")
        if micro_volume >= Decimal("1e21"):
            raise ValueError("tweet market volume exceeds supported compact range")
        volume = _tweet_compact(micro_volume / 1000000)

    def count_text(value: Decimal, *, compact: bool) -> str:
        if compact and value >= 1000:
            return _tweet_compact(value)
        # Keep exact counts when the complete, fully labelled tweet fits.
        if value >= Decimal("1e15"):
            raise ValueError("tweet metric exceeds supported compact range (<1 quadrillion)")
        return f"{int(value):,}"

    # First retain exact counts, then compact trades, then compact wallet count.
    # Every version retains all units, the price basis, and the full denominator.
    variants = ((False, False, False, False), (True, False, False, False),
                (True, True, False, False), (True, False, True, False),
                (True, True, True, False), (True, True, True, True))
    for compact_trades, compact_wallets, short_header, compact_formula in variants:
        lines = [
            (f"Polymarket | {data.day.isoformat()} UTC market end" if short_header else
             f"Polymarket Crypto Up/Down | {data.day.isoformat()} UTC market end"),
            f"Market vol {volume} USDC | {count_text(wallets, compact=compact_wallets)} wallets",
        ]
        for label, group in zip(("All BUY", "Bot-buyer subset"), groups):
            count, shares, notional = group
            lines.append(f"{label}: {count_text(count, compact=compact_trades)} trades | "
                         f"{_tweet_compact(shares)} shares | {_tweet_compact(notional)} USDC")
        formula = ('Notional=Up price*shares' if compact_formula
                   else 'Notional=Up-equivalent price*shares')
        lines.append(formula+'; '
                     f'bot-buyer participation={bot_share} of all BUY notional.')
        text = "\n".join(lines) + "\n"
        if len(text) <= 280:
            return text
    raise ValueError("BUY distribution tweet exceeds the 280-character X limit")


def render_tweet(data: PosterData, distribution_data: dict | None = None) -> str:
    if distribution_data is not None:
        return _distribution_tweet(data, distribution_data)
    values = display_values(data)
    volume = values["volume"]
    if data.current["volume_micro_usdc"] is not None and data.current["volume_micro_usdc"] >= 1_000_000_000_000:
        millions = (Decimal(data.current["volume_micro_usdc"]) / 1_000_000_000_000).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        volume = f"{millions:,.2f}M"

    def change_text(metric: str) -> str:
        value = values[f"{metric}_change"]
        if value == "N/A":
            return f"N/A; {values[f'{metric}_change_caption']}"
        return value

    lines = [
        f"Polymarket Crypto Up/Down | {MONTHS[data.day.month - 1]} {data.day.day}, {data.day.year} UTC",
        "",
        f"{volume} USDC market volume ({change_text('volume')})",
        f"{values['unique_wallets']} trading wallets ({change_text('wallet')})",
        f"{values['bot_wallets']} suspected bot wallets: {values['bot_volume_share']} of wallet BUY amount",
        "",
    ]
    # 只描述显示值明确的涨跌，避免把缺失环比或舍入到零的变化写成趋势。
    wallets = {"+": "More", "-": "Fewer"}.get(values["wallet_change"][:1])
    volume_direction = {"+": "higher", "-": "lower"}.get(values["volume_change"][:1])
    if wallets and volume_direction:
        lines.append(f"{wallets} wallets, {volume_direction} volume.")
    lines.extend([
        "Markets ending that day; taker side once. Bot criteria in chart.",
        "#Polymarket",
    ])
    return "\n".join(lines) + "\n"


@lru_cache(maxsize=256)
def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(FONT), size)
    # 随项目附带的 Inter 字体轴依次为光学尺寸、字重，不依赖本机字体目录。
    face.set_variation_by_axes([14, weight])
    return face


def fitted_font(text: str, size: float, weight: int, width: float) -> ImageFont.FreeTypeFont:
    pixels = round(size * SCALE)
    while True:
        face = font(pixels, weight)
        left, _, right, _ = face.getbbox(text, anchor="lt")
        if right - left <= width * SCALE or pixels == 1:
            return face
        pixels -= 1


class PosterCanvas:
    def __init__(self, background: Path):
        with Image.open(background) as source:
            artwork = source.convert("RGBA").resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)
        # 生成背景带有 alpha；铺上固定底色，使导出在 X 的浅色、深色界面上外观一致。
        base = Image.new("RGBA", OUTPUT_SIZE, BASE)
        self.image = Image.alpha_composite(base, artwork).convert("RGB")
        self.draw = ImageDraw.Draw(self.image)
        self.text_boxes: list[dict] = []

    def text(self, text: str, x: float, y: float, size: float, *, weight: int = 500,
             color: str = MUTED, width: float | None = None) -> tuple[float, float, float, float]:
        face = fitted_font(text, size, weight, width if width is not None else 1035 - x)
        xy = (round(x * SCALE), round(y * SCALE))
        self.draw.text(xy, text, font=face, fill=color, anchor="lt")
        bounds = self.draw.textbbox(xy, text, font=face, anchor="lt")
        baseline = xy[1] - face.getbbox(text, anchor="ls")[1]
        self.text_boxes.append({"text": text, "bounds": bounds, "baseline": baseline,
                                "font_size_px": face.size})
        return tuple(value / SCALE for value in bounds)

    def rule(self, xy: tuple[float, float, float, float]) -> None:
        self.draw.line(tuple(round(value * SCALE) for value in xy), fill=LINE, width=2)

    def money(self, amount: str, x: float, y: float, size: float) -> None:
        unit = font(round(32 * SCALE), 500)
        unit_bounds = unit.getbbox("USDC", anchor="lt")
        unit_width = (unit_bounds[2] - unit_bounds[0]) / SCALE
        bounds = self.text(amount, x, y, size, weight=750, color=WHITE,
                           width=1035 - x - unit_width - 20)
        baseline = self.text_boxes[-1]["baseline"]
        self.text("USDC", bounds[2] + 20, (baseline + unit.getbbox("USDC", anchor="ls")[1]) / SCALE,
                  32, color=MUTED, width=unit_width + 1)

    def change(self, value: str, caption: str, x: float, y: float, *, width: float, size: float) -> None:
        color = GREEN if value.startswith("+") else RED if value.startswith("-") else MUTED
        caption_size = 22 if x > 500 else 28
        caption_face = font(round(caption_size * SCALE), 500)
        b = caption_face.getbbox(caption, anchor="lt")
        caption_width = (b[2] - b[0]) / SCALE
        bounds = self.text(value, x, y, size, weight=700, color=color, width=width - caption_width - 14)
        baseline = self.text_boxes[-1]["baseline"]
        self.text(caption, bounds[2] + 14, (baseline + caption_face.getbbox(caption, anchor="ls")[1]) / SCALE,
                  caption_size, width=caption_width + 1)


def _paragraph(canvas: PosterCanvas, text: str, x: float, y: float, size: float, *,
               width: float, line_height: float, color: str = MUTED,
               weight: int = 500) -> float:
    """Wrap approved text at word boundaries without changing or truncating its words."""
    face = font(round(size * SCALE), weight)
    line = ''
    lines = []
    for word in text.split():
        candidate = f'{line} {word}' if line else word
        if line and face.getlength(candidate) > (width-4) * SCALE:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    for index, line in enumerate(lines):
        canvas.text(line, x, y + index*line_height, size, weight=weight, color=color, width=width)
        canvas.text_boxes[-1].update(source_text=text, line_index=index)
    return y + len(lines)*line_height


def _right_text(canvas: PosterCanvas, text: str, right: float, y: float, size: float, *,
                width: float, weight: int = 600, color: str = WHITE) -> None:
    face = fitted_font(text, size, weight, width)
    left, _, end, _ = face.getbbox(text, anchor='lt')
    start = right - (end-left)/SCALE
    canvas.text(text, start, y, size, weight=weight, color=color, width=width)


def _render_approved_buy_overview(values: dict[str, str], background: Path) -> tuple[Image.Image, list[dict]]:
    canvas = PosterCanvas(background)
    canvas.text(approved_copy.BRAND, 102, 60, 30, weight=700, color=WHITE)
    canvas.text(approved_copy.OVERVIEW_TITLE, 100, 110, 60, weight=800, color=WHITE)
    canvas.text(values['market_end_date'], 104, 192, 26)
    canvas.text(values['scope_label'], 105, 235, 20)
    canvas.rule((102, 278, 1037, 278))

    canvas.text(approved_copy.MARKET_CONTEXT_TITLE, 104, 300, 28, weight=750, color=WHITE)
    context_values = (values['volume']+' USDC', values['volume_change'], values['unique_wallets'],
                      values['bot_wallets'], values['daily_rule_wallets'])
    for index, (label, value) in enumerate(zip(approved_copy.CONTEXT_LABELS, context_values)):
        y = 345 + index*44
        canvas.text(label, 105, y, 21, width=650)
        color = (GREEN if value.startswith('+') else RED if value.startswith('-') else WHITE)
        _right_text(canvas, value, 1035, y-3, 28, width=315, weight=650, color=color)

    canvas.rule((102, 583, 1037, 583))
    canvas.text(approved_copy.SUMMARY_TITLE, 104, 605, 28, weight=750, color=WHITE)
    canvas.text(approved_copy.SUMMARY_COLUMNS[0], 105, 656, 18, weight=650)
    _paragraph(canvas, approved_copy.GROUP_ALL, 575, 648, 20, width=215, line_height=25,
               weight=700, color='#56b9e9')
    _paragraph(canvas, approved_copy.GROUP_BOTS, 810, 635, 20, width=225, line_height=25,
               weight=700, color=GREEN)
    canvas.rule((102, 690, 1037, 690))

    fields = (('all_buy_trades', 'bot_buy_trades'),
              ('all_buy_trade_share', 'bot_buy_trade_share'),
              ('all_buy_shares', 'bot_buy_shares'),
              ('all_buy_shares_share', 'bot_buy_shares_share'),
              ('all_buy_notional', 'bot_buy_notional'),
              ('all_buy_notional_share', 'bot_buy_notional_share'))
    for index, (label, (all_key, bot_key)) in enumerate(zip(approved_copy.SUMMARY_METRICS, fields)):
        y = 711 + index*64
        _paragraph(canvas, label, 105, y, 21, width=445, line_height=25, color=WHITE)
        _right_text(canvas, values[all_key], 790, y+7, 27, width=220, weight=700, color='#56b9e9')
        _right_text(canvas, values[bot_key], 1035, y+7, 27, width=220, weight=700, color=GREEN)
        canvas.rule((102, y+53, 1037, y+53))

    canvas.draw.rectangle(tuple(round(v*SCALE) for v in (98, 1111, 1040, 1374)), fill=BASE)
    y = 1126
    for note in approved_copy.OVERVIEW_NOTES:
        y = _paragraph(canvas, note, 105, y, 15, width=930, line_height=21, weight=400) + 11
    if y > 1375:
        raise ValueError('Approved overview copy does not fit; do not truncate content')
    return canvas.image, canvas.text_boxes


def render_poster(values: dict[str, str], background: Path = BACKGROUND) -> tuple[Image.Image, list[dict]]:
    if values.get('copy_version') == approved_copy.COPY_VERSION:
        return _render_approved_buy_overview(values, background)
    canvas = PosterCanvas(background)
    canvas.text("Polymarket", 102, 78, 38, weight=700, color=WHITE)
    canvas.text("Crypto Up/Down", 100, 137, 78, weight=800, color=WHITE)
    canvas.text("Market Overview", 100, 219, 78, weight=800, color=WHITE)
    canvas.text(values["date"], 103, 302, 31)

    canvas.text("Market Volume", 105, 373, 31)
    canvas.money(values["volume"], 100, 422, 104)
    canvas.change(values["volume_change"], values["volume_change_caption"], 106, 525, width=925, size=43)

    canvas.rule((82, 588, 1037, 588))
    canvas.rule((560, 616, 560, 777))
    canvas.text("Total Markets", 105, 620, 28, width=425)
    canvas.text("Unique Trading Wallets", 610, 620, 28, width=425)
    canvas.text(values["total_markets"], 100, 664, 86, weight=750, color=WHITE, width=425)
    canvas.text(values["unique_wallets"], 606, 664, 86, weight=750, color=WHITE, width=429)
    canvas.change(values["wallet_change"], values["wallet_change_caption"], 610, 750, width=425, size=33)

    canvas.rule((82, 808, 1037, 808))
    canvas.text("Suspected Bot BUY Participation", 104, 838, 38, weight=750, color=WHITE)
    canvas.text(values.get('bot_volume_label','Wallet BUY Amount'), 105, 899, 29)
    canvas.money(values["bot_volume"], 102, 942, 82)
    canvas.rule((560, 1040, 560, 1149))
    canvas.text("Wallets", 105, 1040, 27, width=425)
    canvas.text(values.get('bot_share_label','Share of Wallet BUY Amount'), 610, 1040, 23, width=425)
    canvas.text(values["bot_wallets"], 103, 1080, 74, weight=750, color=WHITE, width=425)
    canvas.text(values["bot_volume_share"], 607, 1080, 74, weight=750, color=WHITE, width=428)
    denominator_label = ('All BUY Up-equivalent notional' if values.get('bot_panel_basis') == BASIS
                         else 'All wallet BUY amount')
    canvas.text(f'{denominator_label}: {values["bot_denominator"]} USDC',106,1162,15,width=925)

    # 页脚也保持干净底色，避免背景中的边缘线路穿过小字。
    canvas.draw.rectangle(tuple(round(v * SCALE) for v in (98, 1181, 760, 1267)), fill=BASE)
    canvas.text("Markets ending on the UTC date shown.", 106, 1189, 20, weight=400)
    canvas.text("Market volume counts the taker side once.", 106, 1215, 20, weight=400)
    canvas.text("Bots: retained from first daily hit (gap ≤60s; span ≥90m).",
                106, 1241, 20, weight=400, width=900)
    return canvas.image, canvas.text_boxes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True, help="市场结束日，UTC YYYY-MM-DD")
    parser.add_argument("--db", type=Path, default=daily.core.DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/posters")
    parser.add_argument("--with-distribution", action="store_true",
                        help="同时生成全部加密系列的买入成交分布 PNG 和 JSON")
    args = parser.parse_args(argv)
    db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        from polymarket_updown_bot_registry import load_first_seen
        first_seen = load_first_seen(db, args.date)
        # 只读快照让当前日与前日取自同一数据库状态，不调用采集器或写入视图。
        db.execute("BEGIN")
        data = load_data(db, args.date, bot_first_seen=first_seen)
    except (ValueError, sqlite3.Error) as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        db.close()

    distribution_data = None
    distribution_poster = None
    if args.with_distribution:
        # 延迟导入，避免统计模块复用本文件字体与常量时形成顶层循环。
        import polymarket_updown_price_distribution as distribution
        import polymarket_updown_bot_price_poster as distribution_poster

        distribution_db = None
        try:
            # load_data 的短查询不得继承上方总览查询的全日只读事务。
            distribution_db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro",
                                             uri=True, timeout=0.25)
            distribution_db.execute("PRAGMA query_only=ON")
            distribution_data = distribution.load_data(distribution_db, args.date,
                                                       series_slug=None, bot_first_seen=first_seen)
        except (ValueError, sqlite3.Error) as exc:
            print(f"交易分布统计未完成：{exc}；本次未生成日报图文。", file=sys.stderr)
            return 2
        finally:
            if distribution_db is not None:
                distribution_db.close()

    try:
        tweet = render_tweet(data, distribution_data)
        if distribution_data is not None and len(tweet) > 280:
            raise ValueError("BUY distribution tweet exceeds the 280-character X limit")
    except (ValueError, KeyError, TypeError) as exc:
        print(f"推文生成未完成：{exc}；本次未生成日报图文。", file=sys.stderr)
        return 2

    values = display_values(data, distribution_data)
    poster, _ = render_poster(values)
    distribution_paths = None
    if distribution_data is not None:
        # 两张图片的数据与版式均验证后才开始写入；发布清单始终最后生成。
        distribution_paths = distribution_poster.write_outputs(distribution_data, args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"updown_{args.date.isoformat()}"
    image_path = args.output_dir / f"{stem}.png"
    data_path = args.output_dir / f"{stem}.json"
    tweet_path = args.output_dir / f"{stem}_tweet.md"
    poster.save(image_path, format="PNG")
    payload = {
        "date_utc": args.date.isoformat(), "date_basis": "market_end_utc",
        "volume_basis": "taker_only", "bot_volume_basis": "wallet_buys_only",
        "previous_date_utc": (args.date - timedelta(days=1)).isoformat(),
        "current": data.current, "previous": data.previous,
        "bot_rule": {"max_mean_interval_seconds": MAX_INTERVAL_SECONDS,
                     "min_span_minutes": MIN_SPAN_MINUTES, "min_markets": None,
                     "classification": "first_hit_day_onward",
                     "classification_scope": "all_crypto_updown_series",
                     "first_seen_basis": "market_end_utc", "future_labels_applied": False},
        "bot_history": dict(getattr(first_seen, 'metadata', {})),
        "display": values,
    }
    if distribution_paths is not None:
        payload['copy_version'] = values['copy_version']
        payload['buy_event_summary'] = event_summary(distribution_data)
        payload['bot_panel_basis'] = BASIS
        payload["trade_distribution"] = {
            "date_utc": distribution_data["date_utc"],
            "market_scope": distribution_data["market_scope"],
            "series_count": distribution_data["series_count"],
            "count_unit": distribution_data["count_unit"],
            "price_basis": distribution_data["price_basis"],
            "notional_basis": distribution_data["notional_basis"],
            "json_file": distribution_paths["json"].name,
            "json_sha256": hashlib.sha256(distribution_paths["json"].read_bytes()).hexdigest(),
            "image_file": distribution_paths["png"].name,
            "image_sha256": hashlib.sha256(distribution_paths["png"].read_bytes()).hexdigest(),
        }
        from polymarket_updown_distribution_assets import DAILY_CHART_LAYOUT_VERSION
        payload['trade_distribution']['layout_version'] = DAILY_CHART_LAYOUT_VERSION
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tweet_path.write_text(tweet, encoding="utf-8")
    write_sidecars(args.output_dir / f"{stem}.json")
    print(f"海报：{image_path}\n数据：{data_path}\n推文：{tweet_path}")
    if distribution_paths is not None:
        print(f"交易分布海报：{distribution_paths['png']}\n交易分布数据：{distribution_paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
