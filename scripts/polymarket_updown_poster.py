#!/usr/bin/env python3
"""从本地 SQLite 生成按 UTC 市场结束日统计的英文 X 海报和推文。"""

from __future__ import annotations

import argparse

from polymarket_updown_captions import write_sidecars
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import polymarket_updown_daily as daily
from polymarket_market_volume_query import load_market_volume

ROOT = daily.core.ROOT
BACKGROUND = ROOT / "assets/posters/updown_background_v1.png"
FONT = ROOT / "assets/fonts/Inter.ttf"
OUTPUT_SIZE = (1600, 2000)
SCALE = OUTPUT_SIZE[0] / 1120
MAX_INTERVAL_SECONDS = 60
MIN_SPAN_MINUTES = 90
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


def load_data(db: sqlite3.Connection, day: date) -> PosterData:
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
        summary.update(volume_basis=volume["volume_basis"], volume_micro_usdc=volume["volume_micro_usdc"])
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


def display_values(data: PosterData) -> dict[str, str]:
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
    return {
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
    }


def render_tweet(data: PosterData) -> str:
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
        f"{values['bot_wallets']} suspected bot wallets: {values['bot_volume_share']} of wallet volume",
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
        self.text_boxes.append({"text": text, "bounds": bounds, "baseline": baseline})
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


def render_poster(values: dict[str, str], background: Path = BACKGROUND) -> tuple[Image.Image, list[dict]]:
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
    canvas.text("Suspected Bot Activity", 104, 838, 40, weight=750, color=WHITE)
    canvas.text("Wallet Trading Volume", 105, 899, 29)
    canvas.money(values["bot_volume"], 102, 942, 82)
    canvas.rule((560, 1040, 560, 1149))
    canvas.text("Wallets", 105, 1040, 27, width=425)
    canvas.text("Share of Wallet Volume", 610, 1040, 27, width=425)
    canvas.text(values["bot_wallets"], 103, 1080, 74, weight=750, color=WHITE, width=425)
    canvas.text(values["bot_volume_share"], 607, 1080, 74, weight=750, color=WHITE, width=428)

    # 页脚也保持干净底色，避免背景中的边缘线路穿过小字。
    canvas.draw.rectangle(tuple(round(v * SCALE) for v in (98, 1181, 760, 1267)), fill=BASE)
    canvas.text("Markets ending on the UTC date shown.", 106, 1189, 20, weight=400)
    canvas.text("Market volume counts the taker side once.", 106, 1215, 20, weight=400)
    canvas.text(f"Suspected bots: avg. interval ≤{MAX_INTERVAL_SECONDS}s; span ≥{MIN_SPAN_MINUTES}m.",
                106, 1241, 20, weight=400)
    return canvas.image, canvas.text_boxes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True, help="市场结束日，UTC YYYY-MM-DD")
    parser.add_argument("--db", type=Path, default=daily.core.DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/posters")
    args = parser.parse_args(argv)
    db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        # 只读快照让当前日与前日取自同一数据库状态，不调用采集器或写入视图。
        db.execute("BEGIN")
        data = load_data(db, args.date)
    except IncompleteDay as exc:
        print(exc, file=sys.stderr)
        return 2
    finally:
        db.close()

    values = display_values(data)
    poster, _ = render_poster(values)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"updown_{args.date.isoformat()}"
    image_path = args.output_dir / f"{stem}.png"
    data_path = args.output_dir / f"{stem}.json"
    tweet_path = args.output_dir / f"{stem}_tweet.md"
    poster.save(image_path, format="PNG")
    payload = {
        "date_utc": args.date.isoformat(), "date_basis": "market_end_utc",
        "volume_basis": "taker_only", "bot_volume_basis": "wallet_buys_plus_sells",
        "previous_date_utc": (args.date - timedelta(days=1)).isoformat(),
        "current": data.current, "previous": data.previous,
        "bot_rule": {"max_mean_interval_seconds": MAX_INTERVAL_SECONDS,
                     "min_span_minutes": MIN_SPAN_MINUTES, "min_markets": None},
        "display": values,
    }
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tweet_path.write_text(render_tweet(data), encoding="utf-8")
    write_sidecars(args.output_dir / f"{stem}.json")
    print(f"海报：{image_path}\n数据：{data_path}\n推文：{tweet_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
