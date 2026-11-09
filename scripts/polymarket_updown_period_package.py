"""周/月共用的分布推文、只读加载和完整图文包提交。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from polymarket_updown_buy_metrics import BASIS, coverage_note, event_summary


def _number(value, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"Invalid distribution tweet {label}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"Invalid distribution tweet {label}")
    return number


def _compact(number: Decimal) -> str:
    if number >= Decimal('1e15'):
        raise ValueError('Distribution tweet metric exceeds supported compact range')
    units = ((Decimal(1), ''), (Decimal(1000), 'K'), (Decimal(1000000), 'M'),
             (Decimal(1000000000), 'B'), (Decimal(1000000000000), 'T'))
    index = max((i for i, (scale, _) in enumerate(units) if number >= scale), default=0)
    with localcontext() as context:
        context.prec = 100
        value = (number / units[index][0]).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if value >= 1000 and index < len(units)-1:
            index += 1
            value = (number / units[index][0]).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return f'{value:.2f}{units[index][1]}'


def render_distribution_tweet(current: dict, distribution_data: dict, kind: str, *, scope_note: str = '') -> str:
    """保持单位、Up 名义金额定义和全 BUY 分母，按 280 ASCII 字符预算排版。"""
    from polymarket_updown_distribution_assets import validate_distribution
    validate_distribution(distribution_data)
    start = date.fromisoformat(current['start_date_utc'])
    end = date.fromisoformat(current['end_date_utc_exclusive'])
    if (distribution_data.get('kind') != kind
            or distribution_data['start_date_utc'] != str(start)
            or distribution_data['end_date_utc_exclusive'] != str(end)):
        raise ValueError('Overview and distribution tweet periods differ')
    wallets = _number(current['unique_wallets'], 'wallets')
    if wallets != wallets.to_integral_value():
        raise ValueError('Distribution tweet wallet count must be an integer')
    volume = ('N/A' if current['volume_micro_usdc'] is None
              else _compact(_number(current['volume_micro_usdc'], 'market volume') / 1000000))
    populations = distribution_data['populations']
    with localcontext() as context:
        context.prec = 100
        total = _number(populations['all_wallets']['notional_usdc_exact'], 'all BUY notional')
        bot = _number(populations['suspected_bots']['notional_usdc_exact'], 'bot BUY notional')
        share = 'N/A' if total == 0 else f'{(bot*100/total).quantize(Decimal(".01"), rounding=ROUND_HALF_UP):.2f}%'
    period = (f'{start}/{end-timedelta(days=1)}' if kind == 'weekly' else f'{start:%Y-%m}')
    for short_header, compact_counts, compact_wallets, dense, ultra in ((False, False, False, False, False), (False, True, False, False, False),
                                                               (True, True, False, False, False), (True, True, True, False, False),
                                                               (True, True, True, True, False), (True, True, True, True, True)):
        header = ([f'Polymarket {period} UTC'] if ultra else [f'Polymarket {kind.title()} {period} UTC ends'] if dense else
                  [f'Polymarket {kind.title()} | {period} UTC market end'] if short_header
                  else [f'Polymarket Crypto Up/Down | {kind.title()}', f'{period} UTC market end'])
        wallet_text = _compact(wallets) if compact_wallets and wallets >= 1000 else f'{int(wallets):,}'
        separator = ';' if dense else ' | '
        lines = header + [f'{"Mkt vol" if ultra else "Market vol"} {volume} USDC{separator}{wallet_text} wallets']
        for key, label in [('all_wallets', 'All BUY'), ('suspected_bots', 'Suspected bot BUY')]:
            row = populations[key]
            count = _number(row['trade_count'], key+' trades')
            if count != count.to_integral_value():
                raise ValueError('Distribution tweet trade count must be an integer')
            count_text = _compact(count) if compact_counts and count >= 1000 else f'{int(count):,}'
            lines.append(f'{label}{" " if dense else ": "}{count_text} trades{separator}{_compact(_number(row["shares_exact"], key+" shares"))} shares{separator}'
                         f'{_compact(_number(row["notional_usdc_exact"], key+" notional"))} USDC')
        lines.append(coverage_note(share))
        if scope_note:
            lines.append(scope_note)
        text = '\n'.join(lines)+'\n'
        if text.isascii() and len(text) <= 280:
            return text
    raise ValueError('Distribution tweet exceeds the 280-character ASCII budget; no truncation')


def load_period_distribution(db_path: Path, start: date, end: date, kind: str, first_seen: dict) -> dict:
    from polymarket_updown_period_distribution import load_period_data
    db = sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.25)
    try:
        db.execute('PRAGMA query_only=ON')
        return load_period_data(db, start, end, kind=kind, bot_first_seen=first_seen,
                                progress=lambda message: print(message, flush=True))
    finally:
        db.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_package(output_dir: Path, stem: str, payload: dict, image, tweet: str,
                  distribution_data: dict | None, *, sidecars_writer) -> dict[str, Path]:
    """先在独立目录生成并校验整套文件；所有生成完成后提交，发布清单最后写入。"""
    output_dir = Path(output_dir).resolve()
    data = copy.deepcopy(payload)
    if distribution_data is not None:
        from polymarket_updown_distribution_assets import validate_distribution
        validate_distribution(distribution_data)
        if not tweet.isascii() or len(tweet) > 280:
            raise ValueError('Distribution tweet exceeds the 280-character ASCII budget')
        for overview_key, distribution_key in [('total_markets', 'total_markets'),
                                                ('unique_wallets', 'all_wallet_count'),
                                                ('suspected_bot_wallets', 'suspected_bot_wallets')]:
            if data['current'][overview_key] != distribution_data[distribution_key]:
                raise ValueError('Overview and distribution population mismatch: '+overview_key)
        data['buy_event_summary'] = event_summary(distribution_data)
        data['bot_panel_basis'] = BASIS
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{stem}.', dir=output_dir.parent) as temp:
        stage = Path(temp)
        if distribution_data is not None:
            from polymarket_updown_bot_price_poster import write_outputs
            paths = write_outputs(distribution_data, stage)
            reference = {key: distribution_data[key] for key in ('kind', 'target_utc', 'start_date_utc',
                'end_date_utc_exclusive', 'market_scope', 'series_count', 'count_unit', 'price_basis', 'notional_basis')}
            reference.update(json_file=paths['json'].name, json_sha256=_sha256(paths['json']),
                             image_file=paths['png'].name, image_sha256=_sha256(paths['png']))
            data['trade_distribution'] = reference
        source = stage/(stem+'.json')
        image.save(stage/(stem+'.png'), format='PNG')
        source.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        (stage/(stem+'_tweet.md')).write_text(tweet, encoding='utf-8')
        manifest = sidecars_writer(source)
        for key in ('source_path', 'image_path', 'english_path'):
            manifest[key] = str(output_dir/Path(manifest[key]).name)
        for item in manifest['platforms'].values():
            if 'image_paths' in item:
                item['image_paths'] = [str(output_dir/Path(path).name) for path in item['image_paths']]
        manifest_path = stage/(stem+'_publish.json')
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        output_dir.mkdir(parents=True, exist_ok=True)
        files = list(stage.iterdir())
        for path in sorted(files, key=lambda path: (path == manifest_path, path.name)):
            path.replace(output_dir/path.name)
    return {'source': output_dir/(stem+'.json'), 'image': output_dir/(stem+'.png'),
            'tweet': output_dir/(stem+'_tweet.md'), 'manifest': output_dir/(stem+'_publish.json')}
