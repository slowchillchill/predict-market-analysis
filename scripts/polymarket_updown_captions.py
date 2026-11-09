#!/usr/bin/env python3
"""Generate publishing sidecars and Douyin cards from existing report data; never collect."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percent(numerator: int, denominator: int, *, signed: bool = False) -> str:
    if not denominator:
        return 'N/A'
    value = (Decimal(numerator) * 100 / denominator).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if value == 0:
        return '0.00%'
    return f'{value:+.2f}%' if signed else f'{value:.2f}%'


def period_info(payload: dict) -> tuple[str, str, str, str]:
    if 'date_utc' in payload:
        day = date.fromisoformat(payload['date_utc']).isoformat()
        return 'daily', day, day, day
    current = payload['current']
    start = date.fromisoformat(current['start_date_utc'])
    end = date.fromisoformat(current['end_date_utc_exclusive'])
    if start.day == 1 and end.day == 1 and 28 <= (end-start).days <= 31:
        return 'monthly', start.strftime('%Y-%m'), str(start), str(end-timedelta(days=1))
    if start.weekday() != 0 or (end-start).days != 7:
        raise ValueError('Not a complete UTC calendar week/month')
    last = str(end-timedelta(days=1))
    return 'weekly', f'{start}_{last}', str(start), last


def wallet_buy_only(payload: dict) -> bool:
    current = payload['current']
    return (current.get('wallet_volume_basis') == 'wallet_buys_only'
            or current.get('bot_volume_basis') == 'wallet_buys_only'
            or payload.get('bot_volume_basis') == 'wallet_buys_only')


def render_zh(payload: dict, *, distribution_data: dict | None = None) -> tuple[str, str]:
    if distribution_data is not None:
        return render_distribution_zh(payload, distribution_data)
    kind, target, start, end = period_info(payload)
    label, previous_label = {'daily': ('日报', '前日'), 'weekly': ('周报', '前周'), 'monthly': ('月报', '前月')}[kind]
    current, previous = payload['current'], payload.get('previous')
    persistent = payload['bot_rule'].get('classification') == 'first_hit_day_onward'
    title_date = target if kind != 'weekly' else start[5:]+'至'+end[5:]
    title = f'{title_date}地址观察{label}'
    # Keep the displayed USDC amount exact to cents; no float or 万/亿 conversion.
    amount = 'N/A' if current['volume_micro_usdc'] is None else f"{(Decimal(current['volume_micro_usdc'])/1_000_000).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"

    def comparison(field: str) -> str:
        if previous is None:
            return f'暂无可比{previous_label}数据'
        if current[field] is None or previous[field] is None:
            return '单边市场成交额数据不完整，环比不适用'
        if previous[field] == 0:
            return f'{previous_label}基数为0，环比不适用'
        return f'较{previous_label} {percent(current[field]-previous[field], previous[field], signed=True)}'

    market_volume = current.get('volume_basis') == 'taker_only'
    buy_only = wallet_buy_only(payload)
    wallet_label = '钱包侧 BUY 原价金额' if buy_only else '地址侧买入＋卖出金额'
    volume_label = '市场成交额' if market_volume else ('钱包侧 BUY 原价金额' if buy_only else '地址侧买入与卖出金额合计')
    wallet_volume = current['wallet_volume_micro_usdc'] if market_volume else current['volume_micro_usdc']
    share = percent(current['suspected_bot_volume_micro_usdc'], wallet_volume)
    share = ('不适用（钱包侧 BUY 原价金额为0）' if buy_only else '不适用（地址侧买入与卖出金额合计为0）') if share == 'N/A' else share
    from polymarket_updown_douyin import observation, series, extrema_note
    headline, _ = observation(payload)
    lines = [headline+'。',
             f'统计区间：{start}'+(f' 至 {end}' if end != start else '')+'（UTC）', '',
             f'去重交易地址：{current["unique_wallets"]:,} 个（{comparison("unique_wallets")}）。',
             '地址数不等于参与人数，同一人可有多个地址。', '',
             (f'历史命中后持续归类、本期活跃：{current["suspected_bot_wallets"]:,} 个疑似机器人地址。' if persistent
              else f'符合高频筛选条件：{current["suspected_bot_wallets"]:,} 个地址。'),
             (f'其钱包侧 BUY 原价金额占比：{share}（分母为全部钱包 BUY 原价金额）。' if buy_only else
              f'其地址侧买入＋卖出金额占比：{share}（分母为全部地址侧金额）。')]
    rule = payload['bot_rule']
    lines.append(f'筛选条件：平均交易间隔≤{rule["max_mean_interval_seconds"]}秒，交易跨度≥{rule["min_span_minutes"]}分钟。')
    if kind == 'daily' and current.get('wallets_meeting_daily_frequency_rule') is not None:
        lines.append(f'当天符合上述规则：{current["wallets_meeting_daily_frequency_rule"]:,}个地址。')
    lines.append('规则筛选，不代表已确认由自动程序控制。')
    if persistent:
        lines.append('首次命中当日及以后持续归为疑似机器人，不使用未来标签回算；周期地址数取实际活跃地址并集。')
    elif kind != 'daily':
        lines.append('按日筛选；同一地址跨日出现只计一次，金额仅计入被标记当日。')
    lines.extend(['', f'{volume_label}：{amount} USDC（{comparison("volume_micro_usdc")}）。',
                  (('每笔成交仅统计吃单方金额一次；钱包 BUY 原价金额按各买方记账，两者口径不同。' if buy_only else
                    '每笔成交仅统计吃单方金额一次；地址侧为买入＋卖出，两者口径不同。') if market_volume
                   else f'金额按{wallet_label}统计，不等于仅计吃单方一次的市场成交额。'), '',
                  '数据来源：Polymarket 公开数据；范围为本期 UTC 结束的 Up/Down 类市场。'])
    rows = series(payload, start, end)
    if kind != 'daily' and rows:
        note = extrema_note(rows)
        lines.extend(['', ('单日成交额'+note if note.startswith('最高') else note)])
    groups = sorted({(r['series_slug'].split('-', 1)[0].upper(), r['first_end_time'][:7]) for r in payload.get('scope_changes', []) if r.get('first_end_time')})
    for asset, month in groups:
        lines.append(f'统计范围变化：{asset} 于 {month} 加入。')
    adjustment = payload.get('coverage_adjustment')
    if adjustment:
        slots = adjustment['excluded_unavailable_slots']
        count = len(slots) if isinstance(slots, list) else int(slots)
        lines.append(f'比较期口径：{adjustment["comparison_month"]} 排除 {count} 个不可用市场时段；该调整仅适用于本次报告。')
    lines.extend(['', '仅作历史统计与方法说明，不提供参与指引或投资建议。', '#数据分析 #统计方法'])
    return title, '\n'.join(lines)+'\n'


def render_distribution_zh(payload: dict, distribution: dict) -> tuple[str, str]:
    from polymarket_updown_distribution_assets import validate_distribution
    from polymarket_updown_buy_metrics import share_percent
    kind, target, start, end = period_info(payload)
    from polymarket_updown_douyin import daily_distribution_chart
    chart_layout = daily_distribution_chart(payload, kind)
    validate_distribution(distribution, target)
    label, previous_label = {'daily': ('日报', '前日'), 'weekly': ('周报', '前周'), 'monthly': ('月报', '前月')}[kind]
    current = payload['current']
    previous = payload.get('previous')
    def comparison(field):
        if previous is None:
            return f'暂无可比{previous_label}数据'
        if current[field] is None or previous[field] is None:
            return '数据不完整，环比不适用'
        if previous[field] == 0:
            return f'{previous_label}基数为0，环比不适用'
        return f'较{previous_label} {percent(current[field]-previous[field], previous[field], signed=True)}'
    def amount(value):
        with localcontext() as context:
            context.prec = 100
            return f"{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    market_amount = ('N/A' if current['volume_micro_usdc'] is None
                     else amount(Decimal(current['volume_micro_usdc']) / 1_000_000))
    interval = start if start == end else f'{start} 至 {end}'
    lines = [f'{interval} 买入成交分布（UTC 市场结束日）。',
             f'范围：数据库全部加密 Up/Down 市场，{distribution["series_count"]:,} 个系列、{distribution["total_markets"]:,} 个市场。',
             f'去重交易地址：{current["unique_wallets"]:,} 个（{comparison("unique_wallets")}）；地址数不等于人数。',
             f'市场成交额：{market_amount} USDC（{comparison("volume_micro_usdc")}），吃单方每笔只计一次。', '']
    for key, population_label in [('all_wallets', '全部钱包买入参与成交'), ('suspected_bots', '疑似机器人买方参与成交')]:
        row = distribution['populations'][key]
        all_buy = distribution['populations']['all_wallets']
        trade_share = share_percent(row['trade_count'], all_buy['trade_count'])
        shares_share = share_percent(row['shares_exact'], all_buy['shares_exact'])
        lines.extend([f'{population_label}：', f'成交笔数（Trades）：{row["trade_count"]:,}。',
                      f'占全部 BUY 成交笔数：{trade_share+"%" if trade_share is not None else "N/A"}。',
                      f'成交份数（Shares）：{amount(row["shares_exact"])}。',
                      f'占全部 BUY 成交份数：{shares_share+"%" if shares_share is not None else "N/A"}。',
                      f'Up 等价名义金额（事件权重）：{amount(row["notional_usdc_exact"])} USDC。',
                      f'占全部 BUY Up 等价金额：{row["notional_share_of_market_percent"]+"%" if row["notional_share_of_market_percent"] is not None else "N/A"}。'])
    rule = distribution['bot_rule']
    persistent = rule.get('classification') == 'first_hit_day_onward'
    population_count = (f'历史首次命中后持续归类，本期活跃疑似机器人地址：{distribution["suspected_bot_wallets"]:,}个。'
                        if persistent else f'本期符合高频筛选条件：{distribution["suspected_bot_wallets"]:,}个地址。')
    retention = ('首次命中当日及以后持续归类，不使用未来标签回算。' if persistent else '')
    denominator_note = ('笔数%以本组总笔数为分母；金额%以全部 BUY 的 Up 等价名义金额为分母。'
                        '汇总份数占比以全部 BUY 份数为分母；每档份数见配套 JSON。' if chart_layout else
                        'Trade % 分母为本图总成交笔数；Shares % 分母为全部 BUY 成交份数；Notional % 分母为全部 BUY 事件 Up 等价金额。')
    lines.extend(['',
                  '两张图均只统计 BUY 参与；本地汇总记录与对手明细重建，每笔只计一次。机器人组纳入任一买方为已标记地址的事件。Trades 不是唯一订单数。',
                  'Up 用原价，Down 用 1−原价；Up 等价名义金额＝Up 等价价×Shares。这是事件参与权重，不代表机器人钱包自身支出，也不等于吃单方市场成交额。',
                  denominator_note,
                  '每10美分一档，左闭右开，最后一档含100美分。份数、金额及占比保留两位小数，总计取未舍入值。',
                  f'高频筛选阈值：平均交易间隔≤{rule["max_mean_interval_seconds"]}秒、跨度≥{rule["min_span_minutes"]}分钟；不代表已确认由自动程序控制。',
                  population_count, retention])
    if kind == 'daily' and current.get('wallets_meeting_daily_frequency_rule') is not None:
        lines.append(f'当天符合上述规则：{current["wallets_meeting_daily_frequency_rule"]:,}个地址。')
    groups = sorted({(r['series_slug'].split('-', 1)[0].upper(), r['first_end_time'][:7])
                     for r in payload.get('scope_changes', []) if r.get('first_end_time')})
    for asset, month in groups:
        lines.append(f'统计范围变化：{asset} 于 {month} 加入。')
    adjustment = payload.get('coverage_adjustment')
    if adjustment:
        slots = adjustment['excluded_unavailable_slots']
        count = len(slots) if isinstance(slots, list) else int(slots)
        lines.append(f'比较期口径：{adjustment["comparison_month"]} 排除 {count} 个不可用市场时段；该调整仅适用于本次报告。')
    lines.extend(['数据来源：现有数据库中的 Polymarket 公开数据。仅作历史统计与方法说明。', '#数据分析 #统计方法'])
    title_date = target if kind != 'weekly' else start[5:]+'至'+end[5:]
    return f'{title_date}买入分布{label}', '\n'.join(lines)+'\n'


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le'))//2


def build_manifest(source: Path, *, output_dir: Path | None = None) -> dict:
    source = source.resolve()
    payload = json.loads(source.read_text(encoding='utf-8'))
    if payload.get('date_basis') != 'market_end_utc':
        raise ValueError('Unexpected date basis')
    kind, target, start, end = period_info(payload)
    from polymarket_updown_distribution_assets import distribution_asset_versions
    english_version, douyin_version = distribution_asset_versions(payload, kind)
    expected = 'updown_'+(target if kind == 'daily' else kind+'_'+target)
    if source.stem != expected:
        raise ValueError('Source filename/period mismatch')
    image = source.with_suffix('.png')
    tweet = source.with_name(source.stem+'_tweet.md')
    from PIL import Image
    with Image.open(image) as im:
        im.verify()
    english = tweet.read_text(encoding='utf-8')
    if not english.strip():
        raise ValueError('Empty English caption')
    from polymarket_updown_distribution_assets import load_distribution
    distribution = load_distribution(source, payload)
    distribution_image = None
    if distribution is not None:
        distribution_image = source.parent / payload['trade_distribution']['image_file']
        with Image.open(distribution_image) as im:
            im.verify()
        if len(english) > 280:
            raise ValueError('Distribution English caption exceeds 280 characters')
    title, chinese = render_zh(payload, distribution_data=distribution)
    tt_title = f'Polymarket Crypto Up/Down | {kind.title()} | {target}'
    platforms = {
        'x': {'language': 'en', 'title': '', 'body': english},
        'linkedin': {'language': 'en', 'title': '', 'body': english},
        'douyin': {'language': 'zh-CN', 'title': title, 'body': chinese, 'music_profile': 'douyin'},
        'tiktok': {'language': 'en', 'title': tt_title, 'body': english, 'music_profile': 'tiktok'},
    }
    for name, title_limit, body_limit in [('douyin',20,1000), ('tiktok',90,4000)]:
        item = platforms[name]
        if utf16_length(item['title']) > title_limit or utf16_length(item['body']) > body_limit:
            raise ValueError(f'{name} caption exceeds platform limits; no truncation')
    for item in platforms.values():
        item['content_sha256'] = hashlib.sha256(json.dumps({'title': item['title'], 'body': item['body'], 'image_sha256': sha256(image)},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if distribution is not None:
        for name in ('x', 'linkedin', 'tiktok'):
            item = platforms[name]
            item['image_paths'] = [str(image), str(distribution_image)]
            item['image_sha256s'] = [sha256(image), sha256(distribution_image)]
            item['image_purposes'] = [kind.title()+' overview', 'All-wallet and suspected-bot BUY distribution']
            item['asset_version'] = english_version
            item['content_sha256'] = hashlib.sha256(json.dumps(
                {'title': item['title'], 'body': item['body'], 'image_sha256s': item['image_sha256s']},
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    from polymarket_updown_douyin import write_cards
    paths = write_cards(payload, kind, start, end, output_dir or source.parent, source.stem,
                        distribution_data=distribution)
    douyin = platforms['douyin']
    douyin['image_paths'] = [str(path) for path in paths]
    douyin['image_sha256s'] = [sha256(path) for path in paths]
    if distribution is not None:
        douyin['image_purposes'] = (['本期总览', '全部钱包买入分布', '疑似机器人买入分布', '数据与方法']
                                    if kind == 'daily' else
                                    ['本期总览', '每日市场成交额趋势', '高频地址解释', '全部钱包买入分布', '疑似机器人买入分布', '数据与方法'])
        douyin['asset_version'] = douyin_version
    else:
        douyin['image_purposes'] = ['本期发现', '成交额分布', '高频地址解释', '数据与方法']
        douyin['asset_version'] = 'research_cards_v1'
    douyin['content_sha256'] = hashlib.sha256(json.dumps(
        {'title': title, 'body': chinese, 'image_sha256s': douyin['image_sha256s']},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'schema_version': 1, 'kind': kind, 'target_utc': target, 'start_date_utc': start, 'end_date_utc': end,
            'date_basis': 'market_end_utc', 'image_path': str(image), 'image_sha256': sha256(image),
            'source_path': str(source), 'source_sha256': sha256(source), 'english_path': str(tweet), 'english_sha256': sha256(tweet),
            'music_config': str(ROOT/'config/polymarket_publish.json'), 'platforms': platforms}


def write_sidecars(source: Path, *, output_dir: Path | None = None) -> dict:
    destination = output_dir or source.parent
    destination.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(source, output_dir=destination)
    for path, text in [(destination/(source.stem+'_caption_zh.md'), manifest['platforms']['douyin']['body']),
                       (destination/(source.stem+'_publish.json'), json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')]:
        temporary = path.with_suffix(path.suffix+'.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Existing poster JSON; source PNG/JSON/tweet stay unchanged')
    parser.add_argument('--output-dir', type=Path, help='Save preview sidecars/cards separately; leave existing sidecars unchanged')
    args = parser.parse_args()
    result = write_sidecars(args.source, output_dir=args.output_dir)
    print(f'{result["kind"]} {result["target_utc"]}: Chinese caption, {len(result["platforms"]["douyin"]["image_paths"])} Douyin cards and manifest ready')
