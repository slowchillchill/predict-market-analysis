#!/usr/bin/env python3
"""Generate localized publishing sidecars from existing report data; never collect or render images."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
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


def render_zh(payload: dict) -> tuple[str, str]:
    kind, target, start, end = period_info(payload)
    label, previous_label = {'daily': ('日报', '前日'), 'weekly': ('周报', '前周'), 'monthly': ('月报', '前月')}[kind]
    current, previous = payload['current'], payload.get('previous')
    title_date = target if kind != 'weekly' else start[5:]+'至'+end[5:]
    title = f'{title_date}涨跌市场{label}'
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
    volume_label = '市场成交额' if market_volume else '钱包交易量'
    wallet_volume = current['wallet_volume_micro_usdc'] if market_volume else current['volume_micro_usdc']
    share = percent(current['suspected_bot_volume_micro_usdc'], wallet_volume)
    share = '不适用（钱包成交额为0）' if share == 'N/A' else share
    lines = [f'Polymarket 加密资产涨跌市场{label}', f'统计区间：{start}'+(f' 至 {end}' if end != start else '')+'（UTC）', '',
             f'{volume_label}：{amount} USDC（{comparison("volume_micro_usdc")}）',
             f'去重交易钱包：{current["unique_wallets"]:,} 个（{comparison("unique_wallets")}）',
             f'疑似机器人钱包：{current["suspected_bot_wallets"]:,} 个，占钱包成交额 {share}', '',
             ('按市场结束日期统计；市场成交额仅计吃单方一次。钱包成交额为所有钱包买入＋卖出。' if market_volume
              else '按市场结束日期统计；钱包交易量为买入＋卖出。')]
    rule = payload['bot_rule']
    lines.append(f'疑似机器人判定：平均交易间隔≤{rule["max_mean_interval_seconds"]}秒，交易跨度≥{rule["min_span_minutes"]}分钟。')
    if kind != 'daily':
        lines.append('疑似机器人按日判定；周期钱包数取每日名单并集，机器人成交额汇总每日被标记钱包的成交额。')
    groups = sorted({(r['series_slug'].split('-', 1)[0].upper(), r['first_end_time'][:7]) for r in payload.get('scope_changes', []) if r.get('first_end_time')})
    for asset, month in groups:
        lines.append(f'统计范围变化：{asset} 于 {month} 加入。')
    adjustment = payload.get('coverage_adjustment')
    if adjustment:
        slots = adjustment['excluded_unavailable_slots']
        count = len(slots) if isinstance(slots, list) else int(slots)
        lines.append(f'比较期口径：{adjustment["comparison_month"]} 排除 {count} 个不可用市场时段；该调整仅适用于本次报告。')
    lines.extend(['', '#Polymarket #数据分析'])
    return title, '\n'.join(lines)+'\n'


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le'))//2


def build_manifest(source: Path) -> dict:
    source = source.resolve()
    payload = json.loads(source.read_text(encoding='utf-8'))
    if payload.get('date_basis') != 'market_end_utc':
        raise ValueError('Unexpected date basis')
    kind, target, start, end = period_info(payload)
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
    title, chinese = render_zh(payload)
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
    return {'schema_version': 1, 'kind': kind, 'target_utc': target, 'start_date_utc': start, 'end_date_utc': end,
            'date_basis': 'market_end_utc', 'image_path': str(image), 'image_sha256': sha256(image),
            'source_path': str(source), 'source_sha256': sha256(source), 'english_path': str(tweet), 'english_sha256': sha256(tweet),
            'music_config': str(ROOT/'config/polymarket_publish.json'), 'platforms': platforms}


def write_sidecars(source: Path) -> dict:
    manifest = build_manifest(source)
    for path, text in [(source.with_name(source.stem+'_caption_zh.md'), manifest['platforms']['douyin']['body']),
                       (source.with_name(source.stem+'_publish.json'), json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')]:
        temporary = path.with_suffix(path.suffix+'.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Existing poster JSON; source PNG/JSON/tweet stay unchanged')
    args = parser.parse_args()
    result = write_sidecars(args.source)
    print(f'{result["kind"]} {result["target_utc"]}: Chinese caption and publish manifest ready')
