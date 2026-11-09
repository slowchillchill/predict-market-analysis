#!/usr/bin/env python3
"""Readable research cards, rendered only from an existing report JSON."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, localcontext
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont
from polymarket_updown_buy_metrics import share_percent
from polymarket_updown_distribution_assets import DAILY_CHART_LAYOUT_VERSION

W, H = 1080, 1440
INK, MUTED, BLUE, TEAL = '#162A3A', '#536778', '#235EA8', '#147568'
PAPER, LINE = '#F5F3ED', '#D7DEDf'


def daily_distribution_chart(payload, kind):
    """Use the approved chart only for daily packages declaring that layout."""
    return (kind == 'daily' and payload.get('trade_distribution', {}).get('layout_version')
            == DAILY_CHART_LAYOUT_VERSION)


def wallet_buy_only(payload, summary=None):
    current = summary is None or summary is payload.get('current')
    summary = payload['current'] if summary is None else summary
    for key in ('wallet_volume_basis', 'bot_volume_basis'):
        if key in summary:
            return summary[key] == 'wallet_buys_only'
    # 顶层声明只描述本期；不能推断比较期采用了相同金额口径。
    return current and payload.get('bot_volume_basis') == 'wallet_buys_only'


def money(value):
    if value is None:
        return 'N/A'
    return f'{(Decimal(value)/1_000_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,.2f}'


def pct(n, d, signed=False):
    if not d or n is None:
        return 'N/A'
    v = (Decimal(n)*100/Decimal(d)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if v == 0:
        return '0.00%'
    return f'{v:+.2f}%' if signed else f'{v:.2f}%'


def change(current, previous, field):
    if not previous:
        return '暂无可比数据'
    a, b = current.get(field), previous.get(field)
    if a is None or b is None:
        return '数据不完整，环比不适用'
    if b == 0:
        return '比较期基数为0，环比不适用'
    return pct(a-b, b, True)


def observation(payload):
    c, p = payload['current'], payload.get('previous')
    if not p or any(c.get(k) is None or p.get(k) in (None, 0)
                    for k in ('unique_wallets', 'volume_micro_usdc')):
        return '先看本期数据，再看统计口径', '比较期缺失、数据不完整或基数为0时，不推断变化方向。'
    def direction(field):
        # Match the displayed precision; tiny movements displayed as 0.00% are stable.
        value = change(c, p, field)
        return '持平' if value == '0.00%' else '增加' if value.startswith('+') else '下降'
    address, volume = direction('unique_wallets'), direction('volume_micro_usdc')
    adjective = '小幅' if volume != '持平' and abs(Decimal(c['volume_micro_usdc']-p['volume_micro_usdc'])/p['volume_micro_usdc']) < Decimal('.01') else ''
    heading = f'地址数{address}，成交额{adjective}{volume}'
    note = ('两个指标没有同步变化；地址数增加不等于成交额增加。' if address == '增加' and volume == '下降'
            else '这是两个历史统计指标的比较，不推断人数、价格或未来走势。')
    if c.get('volume_basis') != 'taker_only':
        basis = '钱包侧 BUY 原价金额' if wallet_buy_only(payload) else '地址侧买入＋卖出'
        return '地址活动与金额变化', f'本报告金额按{basis}统计，不等于单边市场成交额。'
    return heading, note


def series(payload, start, end):
    c = payload['current']
    if start == end:
        prev = payload.get('previous')
        rows = []
        if prev:
            rows.append((payload.get('previous_date_utc', str(date.fromisoformat(start)-timedelta(days=1))), prev.get('volume_micro_usdc')))
        return rows + [(start, c.get('volume_micro_usdc'))]
    raw = c.get('daily_volume_micro_usdc')
    if not raw:
        return []
    expected = [(date.fromisoformat(start)+timedelta(days=i)).isoformat()
                for i in range((date.fromisoformat(end)-date.fromisoformat(start)).days+1)]
    if set(raw) != set(expected):
        raise ValueError('Daily series dates do not match the report period')
    return [(d, raw[d]) for d in expected]


def extrema_note(rows):
    if not rows:
        return '源数据未提供每日序列；不补造趋势。'
    if any(v is None for _, v in rows):
        return '每日序列存在缺口；缺失标为 N/A，不以0替代。'
    high, low = max(v for _, v in rows), min(v for _, v in rows)
    if high == low:
        return '所列日期成交额相同。'
    highs, lows = [d[5:] for d, v in rows if v == high], [d[5:] for d, v in rows if v == low]
    return f'最高：{highs[0]}'+('（并列）' if len(highs)>1 else '')+f'；最低：{lows[0]}'+('（并列）' if len(lows)>1 else '')+'。'


def font(size, bold=False):
    path = Path('/usr/share/fonts/opentype/noto')/('NotoSansCJK-Bold.ttc' if bold else 'NotoSansCJK-Regular.ttc')
    if not path.is_file():
        raise FileNotFoundError('Install Noto Sans CJK fonts to render Chinese cards')
    return ImageFont.truetype(str(path), size, index=2)


class Card:
    def __init__(self, label, period, number, topic, *, total=4):
        self.image = Image.new('RGB', (W, H), PAPER)
        self.draw = ImageDraw.Draw(self.image)
        self.draw.rectangle((0, 0, W, 14), fill=BLUE)
        self.text('公开数据研究 / '+label, 64, 51, 25, color=BLUE, bold=True)
        self.text(f'{number:02d} / {total:02d}', 880, 51, 25, color=MUTED)
        self.text(period+' · UTC', 64, 107, 30, color=MUTED)
        self.text(topic, 64, 174, 53, bold=True)
        self.draw.line((64, 266, 1016, 266), fill=LINE, width=2)
        self.draw.line((64, 1300, 1016, 1300), fill=LINE, width=2)
        self.text('来源：Polymarket 公开数据 · Up/Down 类市场', 64, 1330, 25, color=MUTED)
        self.text('历史统计与方法说明 · 不提供参与指引或投资建议', 64, 1374, 23, color=MUTED)

    def text(self, text, x, y, size=32, color=INK, bold=False):
        face = font(size, bold)
        width = face.getlength(text)
        if x+width > W-56 or y+size+12 > H:
            raise ValueError(f'Text does not fit the card: {text}')
        self.draw.text((x, y), text, fill=color, font=face)

    def paragraph(self, text, y, size=31, color=INK, x=64, width=952, bold=False):
        face, lines, line = font(size, bold), [], ''
        for ch in text:
            if ch == '\n':
                lines.append(line); line = ''
            elif face.getlength(line+ch)>width:
                lines.append(line); line = ch
            else:
                line += ch
        if line:
            lines.append(line)
        for line in lines:
            if y+size+12>1290:
                raise ValueError('Card content overflow; do not shrink or truncate methods')
            self.text(line, x, y, size, color, bold)
            y += size+17
        return y

    def panel(self, top, bottom):
        self.draw.rounded_rectangle((64, top, 1016, bottom), radius=20, fill='white')


def exact_amount(value):
    """Format distribution USDC/shares directly; these values are not micro-USDC."""
    if value is None:
        return 'N/A'
    return f'{Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,.2f}'


def distribution_percent(value):
    return 'N/A' if value is None else exact_amount(value)+'%'


def _right_text(card, text, right, y, size=24, *, left=64, color=INK, bold=False):
    x = right-font(size, bold).getlength(text)
    if x < left:
        raise ValueError(f'Distribution value does not fit its column: {text}')
    card.text(text, x, y, size, color=color, bold=bold)


def _distribution_card(data, period, population, number, *, label='日报', total=4):
    is_bot = population == 'suspected_bots'
    item = data['populations'][population]
    card = Card(label, period, number, '疑似机器人买入分布' if is_bot else '全部钱包买入分布', total=total)
    card.text(f'数据库全部加密市场 · {data["series_count"]} 个系列 · {data["total_markets"]:,} 个市场',
              64, 289, 26, color=MUTED)
    if is_bot:
        retained = data.get('bot_rule', {}).get('classification') == 'first_hit_day_onward'
        text = (f'{data["suspected_bot_wallets"]:,} 个活跃地址：首次命中起持续归类' if retained
                else f'{data["suspected_bot_wallets"]:,} 个地址符合活动频率筛选条件')
        card.text(text, 64, 329, 26, color=TEAL)
    else:
        card.text('仅买入参与的重建成交；同一笔成交只计一次', 64, 329, 26, color=TEAL)

    card.text('成交笔数 / Trades', 64, 382, 24, color=MUTED)
    all_buy = data['populations']['all_wallets']
    trade_coverage = share_percent(item['trade_count'], all_buy['trade_count'])
    _right_text(card, '全BUY笔数 '+distribution_percent(trade_coverage), 520, 382,
                size=22, left=294, color=TEAL)
    card.text(f'{item["trade_count"]:,}', 64, 419, 43, bold=True)
    card.text('成交份数 / Shares', 552, 382, 24, color=MUTED)
    all_shares = all_buy['shares_exact']
    shares_coverage = share_percent(item['shares_exact'], all_shares)
    _right_text(card, '全BUY份数 '+distribution_percent(shares_coverage), 1016, 382,
                size=22, left=790, color=TEAL)
    card.text(exact_amount(item['shares_exact']), 552, 419, 39, bold=True)
    card.text('Up 等价事件金额 / USDC', 64, 484, 24, color=MUTED)
    card.text(exact_amount(item['notional_usdc_exact']), 64, 520, 43, bold=True)
    card.text('Up 等价事件参与覆盖率', 640, 484, 24, color=MUTED)
    card.text(distribution_percent(item['notional_share_of_market_percent']), 640, 520, 43,
              color=BLUE, bold=True)

    # Fixed column boundaries keep long, real-world totals and all ten bins legible.
    # A population may put 100.00% in one band. Keep >=102px for percent
    # columns (also fits that value in bold), without shrinking 24px digits.
    columns = ((283, 153), (394, 292), (583, 403), (694, 592), (895, 703), (1016, 904))
    card.draw.rounded_rectangle((64, 591, 1016, 662), radius=12, fill='white')
    card.text('Up 价 / ¢', 64, 602, 24, color=MUTED, bold=True)
    for heading, english, (right, left) in zip(
            ('笔数', '笔数%', '成交份数', '份数%', 'Up事件金额', '金额%'),
            ('Trades', 'Trade %', 'Shares', 'Shares %', 'USDC', 'Notional %'), columns):
        _right_text(card, heading, right, 596, size=20 if '%' in heading else 24,
                    left=left, color=BLUE, bold=True)
        _right_text(card, english, right, 629, size=21, left=left, color=MUTED)

    bins = item['bins']
    if len(bins) != 10:
        raise ValueError('Buy distribution cards require all ten 10-cent bins')
    for i, row in enumerate(bins):
        y = 682+i*42
        lower, upper = row['lower_cents'], row['upper_cents']
        if (lower, upper) != (i*10, (i+1)*10):
            raise ValueError('Buy distribution cards require ordered 10-cent bins')
        band = f'{lower}–{upper}' if i == 9 else f'{lower}–<{upper}'
        card.text(band, 64, y, 24)
        values = (f'{row["trade_count"]:,}', distribution_percent(row['trade_share_percent']),
                  exact_amount(row['shares_exact']),
                  distribution_percent(share_percent(row['shares_exact'], all_shares)),
                  exact_amount(row['notional_usdc_exact']),
                  distribution_percent(row['notional_share_of_market_percent']))
        for value, (right, left) in zip(values, columns):
            _right_text(card, value, right, y, left=left)
        card.draw.line((64, y+35, 1016, y+35), fill=LINE, width=1)

    card.paragraph('笔数%：本组总笔数为分母。\n份数%：全部 BUY 成交份数为分母。\n金额%：全部 BUY 的 Up 等价金额为分母。',
                   1121, 22, color=MUTED)
    card.paragraph('事件参与权重≠钱包支出或持仓；末档含100¢；总计取未舍入值。',
                   1247, 20, color=MUTED)
    return card.image


def _distribution_chart_axis(data):
    """A shared zero-based percentage axis contains both groups, including 100%."""
    with localcontext() as context:
        context.prec = 100
        maximum = max((Decimal(row['trade_count']) * 100 / Decimal(group['trade_count'])
                       for group in data['populations'].values() if group['trade_count']
                       for row in group['bins']), default=Decimal(0))
        if maximum <= 15:
            return Decimal(15), (0, 5, 10, 15)
        step = (maximum / 4 / 5).to_integral_value(rounding=ROUND_CEILING) * 5
        return step * 4, tuple(step * i for i in range(5))


def _distribution_chart_card(data, period, population, number):
    """Daily chart counterpart: per-bin trades and Up notional; shares in summary."""
    is_bot = population == 'suspected_bots'
    item = data['populations'][population]
    all_buy = data['populations']['all_wallets']
    color = TEAL if is_bot else BLUE
    card = Card('日报', period, number, '疑似机器人买入分布' if is_bot else '全部钱包买入分布')
    card.text(f'数据库全部加密市场 · {data["series_count"]} 个系列 · {data["total_markets"]:,} 个市场',
              64, 289, 26, color=MUTED)
    if is_bot:
        retained = data.get('bot_rule', {}).get('classification') == 'first_hit_day_onward'
        text = (f'{data["suspected_bot_wallets"]:,} 个活跃地址：首次命中起持续归类' if retained
                else f'{data["suspected_bot_wallets"]:,} 个地址符合活动频率筛选条件')
        card.text(text, 64, 329, 26, color=TEAL)
    else:
        card.text('仅买入参与的重建成交；同一笔成交只计一次', 64, 329, 26, color=TEAL)

    card.text('成交笔数 / Trades', 64, 382, 24, color=MUTED)
    _right_text(card, '全BUY笔数 '+distribution_percent(share_percent(
        item['trade_count'], all_buy['trade_count'])), 520, 382, size=22, left=294, color=TEAL)
    card.text(f'{item["trade_count"]:,}', 64, 419, 43, bold=True)
    card.text('成交份数 / Shares', 552, 382, 24, color=MUTED)
    _right_text(card, '全BUY份数 '+distribution_percent(share_percent(
        item['shares_exact'], all_buy['shares_exact'])), 1016, 382, size=22, left=790, color=TEAL)
    card.text(exact_amount(item['shares_exact']), 552, 419, 39, bold=True)
    card.text('Up 等价名义金额 / USDC', 64, 484, 24, color=MUTED)
    card.text(exact_amount(item['notional_usdc_exact']), 64, 520, 43, bold=True)
    card.text('占全部 BUY 名义金额', 640, 484, 24, color=MUTED)
    card.text(distribution_percent(share_percent(item['notional_usdc_exact'],
              all_buy['notional_usdc_exact'])), 640, 520, 43, color=BLUE, bold=True)

    # All headings are short, authored single lines. Values stay at 24px.
    card.draw.rounded_rectangle((64, 601, 1016, 651), radius=12, fill='white')
    card.text('Up价/¢', 64, 610, 22, color=MUTED, bold=True)
    card.text('笔数分布', 190, 610, 22, color=MUTED, bold=True)
    _right_text(card, '笔数·本组%', 690, 610, size=22, left=435, color=BLUE, bold=True)
    _right_text(card, '名义USDC·全BUY%', 1016, 610, size=22, left=712, color=BLUE, bold=True)

    ceiling, ticks = _distribution_chart_axis(data)
    plot_left, plot_right = 190, 425
    row_top, row_height = 680, 39
    plot_bottom = row_top + row_height * 9 + 37
    for tick in ticks:
        x = plot_left + float(Decimal(tick)/ceiling) * (plot_right-plot_left)
        card.draw.line((round(x), row_top, round(x), plot_bottom), fill=LINE, width=1)
        tick_text = f'{tick:g}%'
        card.text(tick_text, x-font(18).getlength(tick_text)/2, plot_bottom+7, 18, color=MUTED)
    if len(item['bins']) != 10:
        raise ValueError('Buy distribution cards require all ten 10-cent bins')
    for index, row in enumerate(item['bins']):
        lower, upper = row['lower_cents'], row['upper_cents']
        if (lower, upper) != (index*10, (index+1)*10):
            raise ValueError('Buy distribution cards require ordered 10-cent bins')
        y = row_top + index*row_height
        band = f'{lower}–{upper}' if index == 9 else f'{lower}–<{upper}'
        card.text(band, 64, y, 24)
        if item['trade_count'] and row['trade_count']:
            with localcontext() as context:
                context.prec = 100
                mix = Decimal(row['trade_count']) * 100 / Decimal(item['trade_count'])
            right = plot_left + float(mix/ceiling) * (plot_right-plot_left)
            card.draw.rounded_rectangle((plot_left, y+12, round(right), y+28), radius=4, fill=color)
        trade_mix = distribution_percent(share_percent(row['trade_count'], item['trade_count']))
        _right_text(card, f'{row["trade_count"]:,} · {trade_mix}', 690, y,
                    size=24, left=435)
        notional_mix = distribution_percent(share_percent(row['notional_usdc_exact'],
                                                         all_buy['notional_usdc_exact']))
        _right_text(card, f'{exact_amount(row["notional_usdc_exact"])} · {notional_mix}',
                    1016, y, size=24, left=712)

    card.paragraph('横条及笔数%：本组总笔数为分母。\n金额%：全部 BUY 的 Up 等价名义金额为分母。\n每档成交份数见数据 JSON；汇总份数见上方。',
                   1121, 22, color=MUTED)
    card.paragraph('事件参与权重≠钱包支出或持仓；末档含100¢；总计取未舍入值。',
                   1247, 20, color=MUTED)
    return card.image


def _distribution_methods(payload, data, period, *, label='日报', number=4, total=4):
    card = Card(label, period, number, '买入分布：数据与方法', total=total)
    rule = data.get('bot_rule', payload['bot_rule'])
    blocks = [
        ('01  全系列与日期',
         f'数据库全部加密 Up/Down 市场：{data["series_count"]} 个系列、{data["total_markets"]:,} 个市场。'
         '按 UTC 市场结束日归属，包含这些市场的提前及跨日成交。'),
        ('02  买入与成交笔数',
         '本地汇总记录与对手明细重建成交，每笔只计一次。全部图统计有买方的事件；机器人图统计任一买方为已标记地址的事件。'
         'Trades 是成交笔数，不是唯一订单数。'),
        ('03  价格、份数与金额',
         '统一到 Up 价格：Up 用原价，Down 用 1−原价。名义金额＝Up 等价价×成交份数，是事件参与权重，不代表机器人自身支出。'
         '与吃单方市场成交额不同。笔数%以各图总笔数为分母；份数%以全部 BUY 成交份数为分母；金额%以全部 BUY 事件 Up 等价金额为分母。'
         '价格左闭右开，末档含100¢。份数和金额保留2位，总计取未舍入值。'),
        ('04  活动频率筛选与来源',
         f'平均交易间隔≤{rule["max_mean_interval_seconds"]}秒，首末交易跨度≥{rule["min_span_minutes"]}分钟。'+
         ('首次命中当日及以后持续归类，不使用未来标签回算。' if rule.get('classification') == 'first_hit_day_onward' else '')+
         '筛选不能确认自动程序或操作者身份。基于现有公开数据汇总，非平台官方报告。'),
    ]
    extra = _scope_notes(payload)
    if extra:
        blocks[-1] = (blocks[-1][0], blocks[-1][1]+'\n'+'\n'.join(extra))
    y = 295
    for heading, body in blocks:
        y = card.paragraph(heading, y, 31, color=BLUE, bold=True)
        y = card.paragraph(body, y+3, 28 if total == 4 else 26, color=MUTED)+16
    return card.image


def render_cards(payload, kind, start, end, *, distribution_data=None):
    c, p = payload['current'], payload.get('previous')
    label = {'daily':'日报','weekly':'周报','monthly':'月报'}[kind]
    comparison = {'daily':'前日','weekly':'前周','monthly':'前月'}[kind]
    period = start if start == end else start+' — '+end
    headline, note = observation(payload)
    buy_only = wallet_buy_only(payload)
    volume_label = '市场成交额' if c.get('volume_basis') == 'taker_only' else ('钱包侧 BUY 原价金额' if buy_only else '地址侧买入＋卖出金额')
    if distribution_data is not None:
        if (distribution_data.get('market_scope') != 'all_crypto_updown_series'
                or distribution_data.get('scope') != 'buy_participation'
                or distribution_data.get('bot_scope') != 'buy_participation'):
            raise ValueError('Cards require all-crypto BUY-participation distribution data')
        if kind != 'daily' and (distribution_data.get('kind') != kind
                or distribution_data.get('start_date_utc') != start
                or distribution_data.get('end_date_utc_exclusive') != str(date.fromisoformat(end)+timedelta(days=1))):
            raise ValueError('Distribution card period mismatch')
    total = 6 if distribution_data is not None and kind != 'daily' else 4
    a = Card(label, period, 1, '本期发现', total=total)
    y = a.paragraph(headline.replace('，','，\n'), 315, 57, bold=True)
    a.paragraph(note, y+30, 31, color=MUTED)
    a.panel(630, 865)
    a.text('去重交易地址', 94, 657, 31, color=MUTED)
    a.text(f'{c["unique_wallets"]:,}', 94, 710, 77, bold=True)
    a.text('较'+comparison+'：'+change(c,p,'unique_wallets'), 94, 812, 30, color=TEAL)
    a.panel(895, 1138)
    a.text(volume_label+' / USDC', 94, 924, 31, color=MUTED)
    a.text(money(c.get('volume_micro_usdc')), 94, 978, 64, bold=True)
    a.text('较'+comparison+'：'+change(c,p,'volume_micro_usdc'), 94, 1085, 30, color=BLUE)
    a.paragraph('地址数≠人数：同一人可能使用多个地址。', 1190, 30, color=MUTED)

    if kind == 'daily' and distribution_data is not None:
        distribution_renderer = (_distribution_chart_card if daily_distribution_chart(payload, kind)
                                 else _distribution_card)
        return [a.image,
                distribution_renderer(distribution_data, period, 'all_wallets', 2),
                distribution_renderer(distribution_data, period, 'suspected_bots', 3),
                _distribution_methods(payload, distribution_data, period)]

    b = Card(label, period, 2, '成交额如何分布', total=total)
    rows = series(payload, start, end)
    b.paragraph(extrema_note(rows), 316, 37, bold=True)
    b.text('每日'+volume_label+' · 单位：百万 USDC', 64, 421, 29, color=MUTED)
    if rows:
        maximum = max((v for _,v in rows if v is not None), default=0)
        step = max(1, math.ceil(maximum/1_000_000_000_000/4))
        ceiling, left, right, top, bottom = step*4, 130, 992, 554, 985
        for i in range(5):
            y = bottom-(bottom-top)*i/4
            b.draw.line((left,y,right,y), fill=LINE, width=2)
            b.text(str(step*i), 66, y-21, 27, color=MUTED)
        if len(rows)<=7:
            stride = (right-left)/len(rows)
            for i,(d,v) in enumerate(rows):
                x = left+stride*(i+.5)
                y = bottom if v is None else bottom-(bottom-top)*v/1_000_000_000_000/ceiling
                if v:
                    b.draw.rounded_rectangle((x-stride*.32,y,x+stride*.32,bottom),radius=5,fill=BLUE if v != maximum else TEAL)
                value = 'N/A' if v is None else f'{Decimal(v)/1_000_000_000_000:.2f}'
                size = 33 if len(rows)==7 else 35
                b.text(value, x-font(size,True).getlength(value)/2, y-45, size, bold=True)
                b.text(d[5:], x-44, bottom+22, 30, color=MUTED)
        else:
            last = None
            for i,(d,v) in enumerate(rows):
                x = left+(right-left)*i/(len(rows)-1)
                if v is None:
                    last = None
                    b.text('N/A',x-22,bottom-35,20,color=MUTED)
                    continue
                y = bottom-(bottom-top)*v/1_000_000_000_000/ceiling
                if last:
                    b.draw.line((*last,x,y), fill=BLUE, width=5)
                b.draw.ellipse((x-5,y-5,x+5,y+5), fill=BLUE)
                last=(x,y)
                if i in (0,len(rows)-1) or i%7==0:
                    b.text(d[5:],x-35,bottom+22,24,color=MUTED)
    else:
        b.paragraph('未提供每日明细，无法绘制分布图。', 700, 36, color=MUTED)
    b.paragraph('图表按市场结束日归属。日期对应的是市场结束日，包含这些市场的提前及跨日成交。', 1120, 31, color=MUTED)

    d = c.get('wallet_volume_micro_usdc', c.get('volume_micro_usdc'))
    share = (distribution_percent(distribution_data['populations']['suspected_bots']['notional_share_of_market_percent'])
             if distribution_data is not None else pct(c['suspected_bot_volume_micro_usdc'],d))
    h = Card(label, period, 3, '高频地址的统计边界', total=total)
    h.text(('首次命中后持续归类、本期活跃' if payload['bot_rule'].get('classification') == 'first_hit_day_onward'
            else '符合活动频率筛选条件'),64,319,34,color=MUTED)
    h.text(f'{c["suspected_bot_wallets"]:,} 个地址',64,380,71,bold=True)
    h.text(('Up 等价金额参与覆盖率' if distribution_data is not None else
            '占全部钱包 BUY 原价金额' if buy_only else '占地址侧买入＋卖出金额'),64,513,34,color=MUTED)
    h.text(share,64,578,94,color=BLUE,bold=True)
    if distribution_data is None and p and p.get('suspected_bot_volume_micro_usdc') is not None and wallet_buy_only(payload,p) == buy_only:
        prev_d=p.get('wallet_volume_micro_usdc',p.get('volume_micro_usdc'))
        if d and prev_d:
            pp=(Decimal(c['suspected_bot_volume_micro_usdc'])/d-Decimal(p['suspected_bot_volume_micro_usdc'])/prev_d)*100
            pp=pp.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            h.text(f'较{comparison} {pp:+.2f} 个百分点' if pp else f'较{comparison} 0.00 个百分点',64,709,30,color=TEAL)
    h.panel(811, 1033)
    if distribution_data is not None:
        groups = distribution_data['populations']
        h.text('机器人买方参与事件金额 / USDC',94,832,25,color=MUTED)
        h.text(exact_amount(groups['suspected_bots']['notional_usdc_exact']),94,872,34,bold=True)
        h.text('分母：全部 BUY 事件 Up 等价金额 / USDC',94,940,25,color=MUTED)
        h.text(exact_amount(groups['all_wallets']['notional_usdc_exact']),94,977,34,bold=True)
        h.paragraph('任一已标记买方参与即计入整笔重建事件。\n金额是 Up 等价事件权重，不是钱包自身支出。\n行为筛选不确认自动程序或操作者身份。',1090,29,color=MUTED)
    else:
        h.paragraph('筛选只描述历史活动频率，\n不代表已确认由自动程序控制，\n不能据此确定操作者身份。',846,35,x=94,width=885,bold=True)
        body = (f'分子：被标记地址的 BUY 原价金额。\n分母：全部地址的 BUY 原价金额。\n分母金额：{money(d)} USDC。\n与单边市场成交额口径不同。' if buy_only else
                '分子：被标记地址的买入＋卖出金额。\n分母：全部地址的买入＋卖出金额。\n与单边市场成交额口径不同。')
        h.paragraph(body,1090,30,color=MUTED)

    if distribution_data is not None:
        return [a.image, b.image, h.image,
                _distribution_card(distribution_data, period, 'all_wallets', 4, label=label, total=6),
                _distribution_card(distribution_data, period, 'suspected_bots', 5, label=label, total=6),
                _distribution_methods(payload, distribution_data, period, label=label, number=6, total=6)]

    z = Card(label, period, 4, '数据与方法说明')
    blocks = [
        ('01  统计范围', '按 UTC 市场结束日归属；范围为 Polymarket 的 Up/Down 类市场，包含所选市场的全部成交。'),
        ('02  金额与地址', ('每笔成交仅统计吃单方金额一次。' if c.get('volume_basis')=='taker_only' else '金额按钱包侧 BUY 原价统计。' if buy_only else '金额按地址侧买入＋卖出统计。')+'同一地址'+('跨日出现只计一次；地址数不等于人数。' if kind!='daily' else '本期出现只计一次；地址数不等于人数。')),
        ('03  高频筛选', f'平均交易间隔≤{payload["bot_rule"]["max_mean_interval_seconds"]}秒，首末交易跨度≥{payload["bot_rule"]["min_span_minutes"]}分钟。'+
         ('首次命中当日及以后持续归类，不使用未来标签回算。周期地址数跨日去重。' if payload['bot_rule'].get('classification') == 'first_hit_day_onward'
          else '按日筛选，周期地址数跨日去重；金额仅计入被标记当日。' if kind!='daily' else '这是规则筛选，不确认自动程序或身份。')),
        ('04  来源与限制', '基于公开数据自行汇总，非平台官方报告；历史比较不推断价格或未来走势。'),
    ]
    extra = _scope_notes(payload)
    if extra:
        blocks[-1]=(blocks[-1][0],blocks[-1][1]+'\n'+'\n'.join(extra))
    y=310
    for heading,body in blocks:
        y=z.paragraph(heading,y,35,color=BLUE,bold=True)
        y=z.paragraph(body,y+7,33,color=MUTED)+28
    return [a.image,b.image,h.image,z.image]


def _scope_notes(payload):
    scope = {(r['series_slug'].split('-',1)[0].upper(),r['first_end_time'][:7])
             for r in payload.get('scope_changes',[]) if r.get('first_end_time')}
    extra = [f'范围变化：{asset} 于 {month} 加入。' for asset,month in sorted(scope)]
    adjustment=payload.get('coverage_adjustment')
    if adjustment:
        slots=adjustment['excluded_unavailable_slots']
        extra.append(f'比较期 {adjustment["comparison_month"]} 排除 {len(slots) if isinstance(slots,list) else slots} 个不可用市场时段；仅适用于本报告。')
    return extra


def write_cards(payload, kind, start, end, folder, stem, *, distribution_data=None):
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    paths=[]
    for i,im in enumerate(render_cards(payload,kind,start,end,distribution_data=distribution_data),1):
        path=folder/f'{stem}_douyin_{i:02d}.png'
        temporary=path.with_suffix('.png.tmp')
        im.save(temporary,format='PNG');temporary.replace(path);paths.append(path.resolve())
    return paths
