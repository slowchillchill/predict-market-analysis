#!/usr/bin/env python3
"""User-approved daily BUY distribution charts, rendered from existing statistics.

Amounts retain the Up-equivalent event basis. The two groups share one
trade-percentage axis; both notional percentages use all BUY notional.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from PIL import Image, ImageDraw

import polymarket_updown_poster as style
from polymarket_updown_buy_metrics import amount, event_summary, share_percent
from polymarket_updown_distribution_assets import DAILY_CHART_LAYOUT_VERSION

LAYOUT_VERSION = DAILY_CHART_LAYOUT_VERSION
SIZE = (1600, 2000)
BASE = '#070d1b'
WHITE = '#f6f8ff'
MUTED = '#aebfdc'
QUIET = '#8da3c1'
ALL = '#56b9e9'
BOT = '#39eaa5'
GRID = '#25344a'
CARD = '#0e192b'


def _percentage(value: str | None) -> str:
    return 'N/A' if value is None else value+'%'


def _validate_rows(data: dict, summary: dict) -> None:
    """Check the chart's exact bindings without changing source statistics."""
    all_notional = summary['all_wallets']['notional_usdc_exact']
    with localcontext() as context:
        context.prec = 100
        for name in ('all_wallets', 'suspected_bots'):
            population = data['populations'][name]
            bins = population['bins']
            if len(bins) != 10:
                raise ValueError('Daily distribution chart requires all ten price bins')
            for index, row in enumerate(bins):
                if (row['lower_cents'] != index*10 or row['upper_cents'] != (index+1)*10
                        or row['lower_inclusive'] is not True
                        or row['upper_inclusive'] is not (index == 9)):
                    raise ValueError('Daily distribution price-bin boundaries mismatch')
                if type(row['trade_count']) is not int or row['trade_count'] < 0:
                    raise ValueError('Invalid chart trade count')
                for field in ('shares_exact', 'notional_usdc_exact'):
                    value = Decimal(row[field])
                    if not value.is_finite() or value < 0:
                        raise ValueError('Invalid chart '+field)
                if share_percent(row['trade_count'], population['trade_count']) != row['trade_share_percent']:
                    raise ValueError('Source trade-share denominator mismatch')
                if share_percent(row['notional_usdc_exact'], all_notional) != row['notional_share_of_market_percent']:
                    raise ValueError('Source notional-share denominator mismatch')
            if sum(row['trade_count'] for row in bins) != population['trade_count']:
                raise ValueError('Daily chart trade totals do not match the bins')
            for field in ('shares_exact', 'notional_usdc_exact'):
                if sum(Decimal(row[field]) for row in bins) != Decimal(population[field]):
                    raise ValueError('Daily chart '+field+' totals do not match the bins')


def _axis(data: dict) -> tuple[Decimal, tuple[int, ...]]:
    """Use one zero-based scale for both groups, including concentrated days."""
    largest = Decimal(0)
    with localcontext() as context:
        context.prec = 100
        for population in data['populations'].values():
            if population['trade_count']:
                largest = max(largest, *(Decimal(row['trade_count'])*100/population['trade_count']
                                         for row in population['bins']))
    for upper, ticks in ((15, (0, 5, 10, 15)), (25, (0, 5, 10, 15, 20, 25)),
                         (50, (0, 10, 20, 30, 40, 50)), (75, (0, 25, 50, 75)),
                         (100, (0, 25, 50, 75, 100))):
        if largest <= upper:
            return Decimal(upper), ticks
    raise ValueError('Daily trade share exceeds 100 percent')


class Canvas:
    def __init__(self, background: Path | None = None):
        with Image.open(background or style.BACKGROUND) as source:
            art = source.convert('RGB').resize(SIZE, Image.Resampling.LANCZOS)
        self.image = Image.blend(Image.new('RGB', SIZE, BASE), art, 0.12)
        self.draw = ImageDraw.Draw(self.image)
        self.boxes = []
        self.bars = []
        # Keep the quantitative area free from the artwork at the image edges.
        self.draw.rounded_rectangle((76, 322, 1524, 1730), radius=24, fill=BASE)

    def text(self, content, x, y, size, *, weight=500, color=WHITE,
             align='left', max_width=None, role='label'):
        face = style.font(size, weight)
        b = self.draw.textbbox((0, 0), content, font=face, anchor='lt')
        width = b[2] - b[0]
        if max_width is not None and width > max_width:
            raise ValueError(f'Text does not fit without shrinking: {content}')
        if align == 'right':
            x -= width
        elif align == 'center':
            x -= width / 2
        self.draw.text((round(x), round(y)), content, font=face, fill=color, anchor='lt')
        bounds = self.draw.textbbox((round(x), round(y)), content, font=face, anchor='lt')
        if min(bounds[:2]) < 0 or bounds[2] > SIZE[0] or bounds[3] > SIZE[1]:
            raise ValueError(f'Text outside the image: {content}')
        self.boxes.append({'text': content, 'bounds': list(bounds), 'size_px': size, 'role': role})

    def line(self, xy, fill=GRID, width=1):
        self.draw.line(xy, fill=fill, width=width)


def render(data: dict, background: Path | None = None) -> tuple[Canvas, dict]:
    """Return an inspectable canvas and the shared, exact BUY summary."""
    summary = event_summary(data)
    _validate_rows(data, summary)
    c = Canvas(background)
    day = date.fromisoformat(data['date_utc'])
    date_text = f'{day.day:02d} {style.MONTHS[day.month-1].upper()} {day.year} · UTC MARKET END DATE'
    c.text('Polymarket · Crypto Up/Down', 100, 64, 25, weight=650, color=MUTED)
    c.text(date_text, 1500, 68, 21, color=MUTED, align='right')
    c.text('BUY Price Distribution', 96, 124, 74, weight=780, max_width=1404, role='title')
    c.text('Up-equivalent price · 10¢ bins', 102, 220, 29, color=MUTED)
    c.text(f'ALL CRYPTO UP/DOWN  ·  {data["series_count"]} SERIES  ·  {data["total_markets"]:,} MARKETS',
           102, 272, 21, weight=600, color=QUIET)
    c.line((100, 315, 1500, 315), fill='#2a405d')

    c.text('SUSPECTED-BOT BUYER PARTICIPATION', 100, 344, 22, weight=650, color=MUTED)
    c.text('Share of all BUY', 1500, 344, 22, color=MUTED, align='right')
    metrics = [
        ('TRADES', 'trade_count', 'bot_trade_coverage_percent', 'of all BUY trades'),
        ('MATCHED SHARES', 'shares_exact', 'bot_shares_coverage_percent', 'of all BUY shares'),
        ('UP NOTIONAL · USDC', 'notional_usdc_exact', 'bot_up_notional_coverage_percent', 'of all BUY notional'),
    ]
    card_gap = 22
    card_width = (1400-card_gap*2)/3
    for i, (title, field, share_field, caption) in enumerate(metrics):
        left = 100+i*(card_width+card_gap)
        right = left+card_width
        c.draw.rounded_rectangle((round(left), 386, round(right), 635), radius=18, fill=CARD)
        c.text(title, left+24, 410, 23, weight=650, color=WHITE, max_width=card_width-48)
        c.text(_percentage(summary[share_field]), left+22, 449, 52, weight=750, color=BOT, role='coverage')
        c.text(caption, left+24, 509, 20, color=MUTED)
        for group, label, y, color in [('all_wallets', 'All BUY', 550, ALL),
                                       ('suspected_bots', 'Bot subset', 587, BOT)]:
            value = summary[group][field]
            value = f'{value:,}' if field == 'trade_count' else amount(value)
            c.text(label, left+24, y+3, 20, color=MUTED, max_width=105)
            c.text(value, right-24, y, 25, weight=650, color=color,
                   align='right', max_width=card_width-160, role='exact_total')

    axis_max, ticks = _axis(data)
    c.axis_max_percent = axis_max
    plot_left, plot_right = 285, 795
    plot_width = plot_right-plot_left
    for name, title, color, top in [
        ('all_wallets', 'All BUY Trades', ALL, 686),
        ('suspected_bots', 'Trades With Suspected Bot Buyers', BOT, 1204),
    ]:
        c.draw.rounded_rectangle((100, top+1, 106, top+34), radius=3, fill=color)
        c.text(title, 124, top, 34, weight=720, max_width=1376, role='panel_title')
        c.text('Up price · ¢', 100, top+65, 21, color=MUTED)
        c.text('Trade mix · %', plot_left, top+65, 21, color=MUTED)
        c.text('Trades · Group %', 1135, top+65, 21, color=MUTED, align='right')
        c.text('Notional USDC · All BUY %', 1500, top+65, 21, color=MUTED, align='right', max_width=340)
        row_start = top+114
        grid_top, grid_bottom = row_start-8, row_start+9*35+29
        population = data['populations'][name]
        if population['trade_count']:
            for tick in ticks:
                x = plot_left + float(Decimal(tick)/axis_max)*plot_width
                c.line((round(x), grid_top, round(x), grid_bottom))
                c.text(f'{tick}%', x, grid_bottom+15, 19, color=QUIET, align='center')
        else:
            c.text('No BUY trades in this group', (plot_left+plot_right)/2,
                   row_start+158, 22, color=QUIET, align='center', max_width=plot_width,
                   role='empty_group')
        for i, row in enumerate(population['bins']):
            y = row_start+i*35
            band = f'{row["lower_cents"]}–{row["upper_cents"]}' if row['upper_inclusive'] else f'{row["lower_cents"]}–<{row["upper_cents"]}'
            c.text(band, 100, y, 25, color=MUTED, max_width=180, role='bin')
            with localcontext() as context:
                context.prec = 100
                share = (Decimal(row['trade_count'])*100/Decimal(population['trade_count'])
                         if population['trade_count'] else None)
            if share is not None and share > axis_max:
                raise ValueError('Trade-share axis does not contain a price bin')
            if share_percent(row['trade_count'], population['trade_count']) != row['trade_share_percent']:
                raise ValueError('Source trade-share denominator mismatch')
            right = plot_left+float(share/axis_max)*plot_width if share is not None else plot_left
            drawn = round(right) > plot_left
            if drawn:
                c.draw.rounded_rectangle((plot_left, y+4, round(right), y+22), radius=6, fill=color)
            c.text(f'{row["trade_count"]:,}  ·  {_percentage(row["trade_share_percent"])}', 1135, y,
                   25, weight=550, color=WHITE, align='right', max_width=300, role='bin_metric')
            notional_percent = share_percent(row['notional_usdc_exact'], summary['all_wallets']['notional_usdc_exact'])
            if notional_percent != row['notional_share_of_market_percent']:
                raise ValueError('Source notional-share denominator mismatch')
            c.text(f'{amount(row["notional_usdc_exact"])}  ·  {_percentage(notional_percent)}', 1500, y,
                   24, weight=550, color=WHITE, align='right', max_width=340, role='bin_notional')
            c.bars.append({'group': name, 'interval_cents': row['interval_cents'],
                           'trade_count': row['trade_count'], 'trade_share_percent': row['trade_share_percent'],
                           'denominator': population['trade_count'],
                           'unrounded_share_percent': str(share) if share is not None else None,
                           'drawn': drawn,
                           'notional_usdc_exact': row['notional_usdc_exact'],
                           'notional_usdc_display': amount(row['notional_usdc_exact']),
                           'notional_share_of_all_buy_percent': notional_percent,
                           'notional_denominator_usdc_exact': summary['all_wallets']['notional_usdc_exact'],
                           'bounds': [plot_left, y+4, round(right), y+22]})
    c.line((100, 1749, 1500, 1749), fill='#2a405d')
    notes = [
        'Group %: each group’s trades. USDC %: all BUY Up-equivalent notional. Cards: % of all BUY.',
        'Bot subset: at least one labelled buyer. Each match is counted once; Trades are not unique orders.',
        'Up price: Up = p; Down = 1 − p. Up notional = Up price × matched shares.',
        'Shares and notional describe participation, not holdings or wallet spending.',
        'Bins: [lower, upper), with 100¢ included in the final bin. Percentages may differ due to rounding.',
        'Full per-bin matched shares remain in the accompanying data JSON.',
    ]
    for i, note in enumerate(notes):
        c.text(note, 100, 1780+i*32, 21, color=MUTED, max_width=1400, role='method_note')

    # Catch collisions at the actual exported type sizes; never fix them by shrinking text.
    overlaps = []
    for i, a in enumerate(c.boxes):
        for b in c.boxes[i+1:]:
            aa, bb = a['bounds'], b['bounds']
            if min(aa[2],bb[2])-max(aa[0],bb[0])>0 and min(aa[3],bb[3])-max(aa[1],bb[1])>0:
                overlaps.append([a['text'],b['text']])
    if overlaps:
        raise ValueError(f'Text collisions in exported chart: {overlaps}')
    return c, summary


def render_poster(data: dict, background: Path | None = None) -> tuple[Image.Image, list[dict]]:
    """Keep the image/text-box contract used by the existing poster pipeline."""
    canvas, _ = render(data, background)
    return canvas.image, canvas.boxes
