#!/usr/bin/env python3
"""从现库重建买入参与成交，生成全部钱包与疑似机器人两组价位统计。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

from PIL import Image, ImageDraw

import polymarket_updown_price_distribution as distribution
import polymarket_updown_poster as style
import polymarket_updown_buy_metrics as buy_metrics
import polymarket_updown_poster_copy as copy

ALL_COLOR = "#56b9e9"
BOT_COLOR = style.GREEN
COPY_VERSION = copy.COPY_VERSION
ROW_HEIGHT = 27
PRICE_LEFT = 104
TRADE_RIGHT = 350
TRADE_PERCENT_RIGHT = 446
SHARES_RIGHT = 641
SHARES_PERCENT_RIGHT = 735
NOTIONAL_RIGHT = 929
NOTIONAL_PERCENT_RIGHT = 1035
COLUMNS = ((104, 108), (222, 128), (360, 86), (464, 177),
           (655, 80), (749, 180), (943, 92))


@dataclass(frozen=True)
class Theme:
    background: Path
    ink: str
    muted: str
    all_color: str
    bot_color: str
    line: str
    base: str


THEMES = {
    "daily": Theme(style.BACKGROUND, style.WHITE, style.MUTED, ALL_COLOR, BOT_COLOR,
                   style.LINE, style.BASE),
    "weekly": Theme(style.ROOT / "assets/posters/updown_weekly_background_v1.png",
                    "#fff4df", "#dfc9b6", "#e4bd76", "#e5b39e", "#785745", "#28121c"),
    "monthly": Theme(style.ROOT / "assets/posters/updown_monthly_background_v1.png",
                     "#202923", "#5d6b60", "#1f5d42", "#496d52", "#c7d2c7", "#f4f2e9"),
}


def period_label(data: dict) -> tuple[str, str]:
    """Return the validated cadence and human-readable UTC market-end period."""
    kind = data.get("kind", "daily")
    if kind == "daily":
        day = date.fromisoformat(data["date_utc"])
        return kind, f"{style.MONTHS[day.month - 1]} {day.day}, {day.year} · UTC"
    if kind not in ("weekly", "monthly"):
        raise ValueError("Unsupported distribution period kind")
    start = date.fromisoformat(data["start_date_utc"])
    end = date.fromisoformat(data["end_date_utc_exclusive"])
    last = end - timedelta(days=1)
    if kind == "weekly":
        if start.weekday() != 0 or end - start != timedelta(days=7):
            raise ValueError("Weekly distribution requires a complete UTC Monday–Sunday week")
        if start.year != last.year:
            label = f"{style.MONTHS[start.month-1]} {start.day}, {start.year} – {style.MONTHS[last.month-1]} {last.day}, {last.year}"
        elif start.month != last.month:
            label = f"{style.MONTHS[start.month-1]} {start.day} – {style.MONTHS[last.month-1]} {last.day}, {last.year}"
        else:
            label = f"{style.MONTHS[start.month-1]} {start.day}–{last.day}, {last.year}"
    else:
        expected_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        if start.day != 1 or end != expected_end:
            raise ValueError("Monthly distribution requires a complete UTC calendar month")
        label = f"{style.MONTHS[start.month-1]} {start.year}"
    return kind, label + " · UTC"


def _canvas(background: Path, theme: Theme) -> style.PosterCanvas:
    canvas = style.PosterCanvas(background)
    if theme.base != style.BASE:
        # Flatten transparent margins against this period's own paper/base color.
        with Image.open(background) as source:
            artwork = source.convert("RGBA").resize(style.OUTPUT_SIZE, Image.Resampling.LANCZOS)
        base = Image.new("RGBA", style.OUTPUT_SIZE, theme.base)
        canvas.image = Image.alpha_composite(base, artwork).convert("RGB")
        canvas.draw = ImageDraw.Draw(canvas.image)
    return canvas


def _rule(canvas: style.PosterCanvas, xy: tuple, theme: Theme) -> None:
    canvas.draw.line(tuple(round(value * style.SCALE) for value in xy), fill=theme.line, width=2)


def series_label(series_slug: str) -> str:
    return series_slug.replace("-up-or-down-", " ").upper()


def number_text(value: str | Decimal) -> str:
    value = Decimal(value)
    with localcontext() as context:
        context.prec = max(100, len(value.as_tuple().digits) + abs(value.adjusted()) + 4)
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def percentage_text(value: str | None) -> str:
    return "N/A" if value is None else f"{value}%"


def _aligned_text(canvas: style.PosterCanvas, text: str, x: float, y: float,
                  size: float, *, align: str = "right", width: float = 150,
                  weight: int = 500, color: str = style.MUTED) -> None:
    face = style.fitted_font(text, size, weight, width)
    left, _, right, _ = face.getbbox(text, anchor="lt")
    length = (right - left) / style.SCALE
    start = x - length if align == "right" else x - length / 2
    canvas.text(text, start, y, size, width=width, weight=weight, color=color)


def _wrapped_lines(text: str, size: float, weight: int, width: float) -> list[str]:
    """Wrap approved copy without changing or silently deleting any words."""
    face = style.font(round(size * style.SCALE), weight)
    lines, line = [], ''
    for word in text.split():
        candidate = line + (' ' if line else '') + word
        if line and face.getlength(candidate) > width * style.SCALE:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if any(face.getlength(line) > width * style.SCALE for line in lines):
        raise ValueError('Approved distribution copy does not fit at its declared font size')
    return lines


def _wrapped_text(canvas: style.PosterCanvas, text: str, x: float, y: float,
                  size: float, *, width: float, line_height: float,
                  weight: int = 500, color: str, right: bool = False) -> float:
    lines = _wrapped_lines(text, size, weight, width)
    for index, line in enumerate(lines):
        if right:
            _aligned_text(canvas, line, x+width, y+index*line_height, size,
                          width=width, weight=weight, color=color)
        else:
            canvas.text(line, x, y+index*line_height, size,
                        width=width, weight=weight, color=color)
    return y+len(lines)*line_height


def _metrics(canvas: style.PosterCanvas, row: dict, y: float, *, color: str = style.WHITE,
             total_row: bool = False) -> None:
    weight = 700 if total_row else 550
    size = 20
    _aligned_text(canvas, f"{row['trade_count']:,}", TRADE_RIGHT, y, size,
                  width=128, weight=weight, color=color)
    _aligned_text(canvas, percentage_text(row.get("trade_share_percent")),
                  TRADE_PERCENT_RIGHT, y, size, width=86, weight=weight, color=color)
    _aligned_text(canvas, number_text(row["shares_exact"]), SHARES_RIGHT, y, size,
                  width=177, weight=weight, color=color)
    _aligned_text(canvas, percentage_text(row["share_of_all_buy_shares_percent"]),
                  SHARES_PERCENT_RIGHT, y, size, width=80, weight=weight, color=color)
    _aligned_text(canvas, number_text(row["notional_usdc_exact"]), NOTIONAL_RIGHT, y, size,
                  width=180, weight=weight, color=color)
    _aligned_text(canvas, percentage_text(row["notional_share_of_market_percent"]),
                  NOTIONAL_PERCENT_RIGHT, y, size, width=92, weight=weight, color=color)


def _panel(canvas: style.PosterCanvas, data: dict, population_key: str, top: float,
           theme: Theme) -> None:
    population = data["populations"][population_key]
    is_bot = population_key == "suspected_bots"
    color = theme.bot_color if is_bot else theme.all_color
    title = copy.GROUP_BOTS if is_bot else copy.GROUP_ALL
    canvas.text(title, 103, top, 28, weight=750, color=theme.ink)
    _aligned_text(canvas, f"{population['trade_count']:,} trades", 1035, top + 2, 24,
                  width=360, weight=700, color=color)

    header = top + 51
    for index, (heading, (left, width)) in enumerate(zip(copy.DISTRIBUTION_COLUMNS, COLUMNS)):
        bottom = _wrapped_text(canvas, heading, left, header, 14, width=width,
                               line_height=17, weight=650, color=theme.muted, right=index>0)
        if bottom > top+115:
            raise ValueError('Approved distribution column header exceeds its allotted height')

    row_top = top + 126
    all_shares = data['populations']['all_wallets']['shares_exact']
    for i, row in enumerate(population["bins"]):
        y = row_top + i * ROW_HEIGHT
        interval = f"[{row['lower_cents']},{row['upper_cents']}{']' if row['upper_inclusive'] else ')'}"
        canvas.text(interval, PRICE_LEFT, y + 1, 20, color=theme.ink, width=108)
        values = {**row, 'share_of_all_buy_shares_percent': buy_metrics.share_percent(row['shares_exact'], all_shares)}
        _metrics(canvas, values, y + 1, color=theme.ink)

    total_y = row_top + 10 * ROW_HEIGHT + 8
    _rule(canvas, (102, total_y - 12, 1037, total_y - 12), theme)
    canvas.text("Total", 104, total_y, 17, weight=750, color=color, width=108)
    totals = dict(population)
    totals["trade_share_percent"] = "100.00" if population["trade_count"] else None
    totals['share_of_all_buy_shares_percent'] = buy_metrics.share_percent(population['shares_exact'], all_shares)
    _metrics(canvas, totals, total_y, color=color, total_row=True)


def render_poster(data: dict, background: Path | None = None) -> tuple[Image.Image, list[dict]]:
    if (data.get('kind', 'daily') == 'daily'
            and data.get('market_scope') == 'all_crypto_updown_series'
            and data.get('series_slug') is None):
        from polymarket_updown_distribution_chart import render_poster as render_daily_chart
        return render_daily_chart(data, background)
    kind, label = period_label(data)
    theme = THEMES[kind]
    canvas = _canvas(background or theme.background, theme)
    canvas.text(copy.BRAND, 103, 52, 28, weight=700, color=theme.ink)
    if kind != 'daily':
        _aligned_text(canvas, kind.upper(), 1035, 57, 17, width=180, weight=650, color=theme.muted)
    _wrapped_text(canvas, copy.DISTRIBUTION_TITLE, 100, 104, 49, width=935,
                  line_height=55, weight=800, color=theme.ink)
    scope_label = series_label(data["series_slug"]) if data.get("series_slug") else "All Crypto Up/Down"
    label += ' Market End Date'
    canvas.text(label, 103, 231, 24, color=theme.muted)
    scope_caption = f"{data['series_count']} Series · {data['total_markets']:,} Markets"
    if data.get('series_slug'):
        scope_caption = scope_label+' · '+scope_caption
    canvas.text(scope_caption, 105, 271, 18, color=theme.muted)
    _rule(canvas, (82, 305, 1037, 305), theme)

    _panel(canvas, data, "all_wallets", 326, theme)
    _rule(canvas, (82, 769, 1037, 769), theme)
    _panel(canvas, data, "suspected_bots", 788, theme)

    _rule(canvas, (82, 1269, 1037, 1269), theme)
    canvas.draw.rectangle(tuple(round(v * style.SCALE) for v in (98, 1283, 1040, 1386)), fill=theme.base)
    y = 1284
    for note in copy.DISTRIBUTION_NOTES:
        y = _wrapped_text(canvas, note, 105, y, 13, width=930, line_height=18,
                          weight=400, color=theme.muted)+3
    if y > 1390:
        raise ValueError('Approved distribution footer exceeds the image height')
    return canvas.image, canvas.text_boxes


def write_outputs(data: dict, output_dir: Path) -> dict[str, Path]:
    """使用同一份已校验统计生成 JSON 和海报，供独立入口与每日入口复用。"""
    image, _ = render_poster(data)
    data_path = distribution.save_statistics(data, output_dir)
    image_path = data_path.with_suffix(".png")
    temporary = image_path.with_suffix(".png.tmp")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(image_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"json": data_path, "png": image_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument("--date", type=date.fromisoformat, help="市场结束日，UTC YYYY-MM-DD")
    period.add_argument("--week-start", type=date.fromisoformat, help="完整 UTC 周的周一，YYYY-MM-DD")
    period.add_argument("--month", type=_month_start, help="完整 UTC 自然月，YYYY-MM")
    parser.add_argument("--db", type=Path, default=style.daily.core.DEFAULT_DB)
    parser.add_argument("--series", help="单日可选市场系列；周/月固定为全部加密系列")
    parser.add_argument("--output-dir", type=Path, help="默认写入 outputs/previews 的对应周期目录")
    args = parser.parse_args(argv)
    if args.week_start is not None and args.week_start.weekday() != 0:
        parser.error("--week-start 必须是 UTC 周一")
    if args.date is None and args.series is not None:
        parser.error("周/月分布固定覆盖全部加密市场，不能同时指定 --series")
    db = None
    try:
        db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        db.execute("PRAGMA query_only=ON")
        if args.date is not None:
            data = distribution.load_data(db, args.date, series_slug=args.series)
        else:
            from polymarket_updown_period_distribution import load_period_data
            kind = "weekly" if args.week_start is not None else "monthly"
            start = args.week_start or args.month
            end = (start + timedelta(days=7) if kind == "weekly"
                   else (start.replace(day=28) + timedelta(days=4)).replace(day=1))
            data = load_period_data(db, start, end, kind=kind,
                                    progress=lambda message: print(message, file=sys.stderr, flush=True))
    except (ValueError, sqlite3.Error) as exc:
        print(f"未生成买入成交分布海报：{exc}", file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()

    scope = args.series.replace("-up-or-down-", "_") if args.series else "all_series"
    if args.date is not None:
        folder = f"buy_trade_distribution_{scope}_{args.date}_v3"
    else:
        folder = distribution.statistics_stem(data) + "_v1"
    output_dir = args.output_dir or style.ROOT / "outputs/previews" / folder
    paths = write_outputs(data, output_dir)
    print(f"海报：{paths['png']}\n数据：{paths['json']}")
    return 0


def _month_start(value: str) -> date:
    try:
        if len(value) != 7 or value[4] != "-":
            raise ValueError(value)
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("月份格式必须是 YYYY-MM") from exc


if __name__ == "__main__":
    raise SystemExit(main())
