"""验证日、周、月图文引用的本地买入分布资产，不联网、不查询数据库。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path


DAILY_CHART_LAYOUT_VERSION = 'buy_price_distribution_chart_v1'


def distribution_asset_versions(payload: dict, kind: str) -> tuple[str, str]:
    """Keep manifest generation and submission checks on the same layout versions."""
    if kind not in ('daily', 'weekly', 'monthly'):
        raise ValueError('Unsupported distribution report kind')
    layout = payload.get('trade_distribution', {}).get('layout_version')
    if layout is not None:
        if kind != 'daily' or layout != DAILY_CHART_LAYOUT_VERSION:
            raise ValueError('Unsupported distribution layout version')
        return 'daily_buy_distribution_chart_v1', 'research_cards_daily_buy_distribution_chart_v1'
    return (f'{kind}_buy_distribution_v2',
            'research_cards_buy_distribution_v2' if kind == 'daily'
            else f'research_cards_{kind}_buy_distribution_v2')


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal(raw: str, label: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid distribution {label}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"Invalid distribution {label}")
    return value


def _percent(value: Decimal | int, total: Decimal | int) -> str | None:
    if not total:
        return None
    return f"{(Decimal(value) * 100 / Decimal(total)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def report_identity(payload: dict) -> dict:
    """从总览本身确定完整 UTC 周期；不接受引用中的日期代替总览日期。"""
    if 'date_utc' in payload:
        stamp = date.fromisoformat(payload['date_utc']).isoformat()
        if stamp != payload['date_utc']:
            raise ValueError('Overview date must use YYYY-MM-DD')
        return {'kind': 'daily', 'target_utc': stamp, 'date_utc': stamp}
    current = payload.get('current', {})
    try:
        start = date.fromisoformat(current['start_date_utc'])
        end = date.fromisoformat(current['end_date_utc_exclusive'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Overview requires a complete UTC period') from exc
    kind = 'monthly' if start.day == end.day == 1 and 28 <= (end-start).days <= 31 else 'weekly'
    from polymarket_updown_period_distribution import period_dates
    _, target = period_dates(start, end, kind)
    return {'kind': kind, 'target_utc': target, 'start_date_utc': str(start),
            'end_date_utc_exclusive': str(end)}


_AUDIT_SUM_KEYS = ('transaction_groups', 'validated_groups', 'reconstructed_trades',
                   'buy_participating_trades', 'excluded_sell_only_trades', 'unresolved_groups',
                   'exact_value_groups', 'tolerated_value_groups', 'price_rounding_groups',
                   'source_row_count')


def _nonnegative_count(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError('Invalid distribution '+label)
    return value


def _validate_audit(audit: dict) -> None:
    for key in _AUDIT_SUM_KEYS:
        _nonnegative_count(audit.get(key), key)
    if (audit['unresolved_groups'] != 0 or audit['transaction_groups'] != audit['validated_groups']
            or audit['reconstructed_trades'] != audit['buy_participating_trades'] + audit['excluded_sell_only_trades']
            or audit['validated_groups'] != audit['exact_value_groups'] + audit['tolerated_value_groups'] + audit['price_rounding_groups']
            or audit['source_row_count'] != audit['transaction_groups'] + audit['reconstructed_trades']):
        raise ValueError('Distribution reconstruction audit totals mismatch')
    if audit.get('value_tolerance_usdc') != '0.000001' or audit.get('roundtrip_min_decimal_places') != 10:
        raise ValueError('Distribution reconstruction validation rule mismatch')
    _decimal(audit.get('max_absolute_value_difference_usdc'), 'maximum value difference')


def _validate_period_audits(data: dict) -> None:
    audit = data['validation']
    _validate_audit(audit)
    rows = data['daily_audits']
    for row in rows:
        _validate_audit(row.get('validation', {}))
        for key in ('total_markets', 'all_wallet_count', 'suspected_bot_wallets'):
            _nonnegative_count(row.get(key), 'daily '+key)
        if row['suspected_bot_wallets'] > row['all_wallet_count']:
            raise ValueError('Daily bot wallet count exceeds all wallets')
        digest = row.get('source_fingerprint_sha256', '')
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest):
            raise ValueError('Distribution daily source fingerprint missing')
    if sum(row['total_markets'] for row in rows) != data['total_markets']:
        raise ValueError('Distribution period market totals mismatch')
    for key in _AUDIT_SUM_KEYS:
        if sum(row['validation'][key] for row in rows) != audit[key]:
            raise ValueError('Distribution period audit sum mismatch: '+key)
    if _decimal(audit['max_absolute_value_difference_usdc'], 'maximum value difference') != max(
            _decimal(row['validation']['max_absolute_value_difference_usdc'], 'daily value difference') for row in rows):
        raise ValueError('Distribution period maximum value difference mismatch')
    for key in ('all_wallet_count', 'suspected_bot_wallets'):
        count = data[key]
        if not max(row[key] for row in rows) <= count <= sum(row[key] for row in rows):
            raise ValueError('Distribution period wallet union bounds mismatch')


def validate_distribution(data: dict, day: str | None = None) -> None:
    """检查给定日期、全系列 BUY 口径和五项指标的分母与加总。"""
    expected = {
        "date_basis": "market_end_utc",
        "market_scope": "all_crypto_updown_series", "series_slug": None,
        "scope": "buy_participation", "bot_scope": "buy_participation",
        "price_basis": "up_equivalent", "notional_basis": "up_equivalent_price_times_shares",
        "count_unit": "reconstructed_trades", "trade_share_denominator": "population_trade_count",
        "notional_share_denominator": "all_wallets_buy_notional",
    }
    if data.get('schema_version') == 3:
        from polymarket_updown_period_distribution import period_dates
        days, target = period_dates(date.fromisoformat(data['start_date_utc']),
                                   date.fromisoformat(data['end_date_utc_exclusive']), data['kind'])
        if data.get('target_utc') != target or data.get('covered_dates') != [str(stamp) for stamp in days]:
            raise ValueError('Distribution period coverage mismatch')
        if day is not None and day != target:
            raise ValueError('Distribution target period mismatch')
        if [row.get('date_utc') for row in data.get('daily_audits', [])] != data['covered_dates']:
            raise ValueError('Distribution daily audit coverage mismatch')
        rule = data.get('bot_rule', {})
        for key, value in {'classification':'first_hit_day_onward',
                           'classification_scope':'all_crypto_updown_series',
                           'first_seen_basis':'market_end_utc', 'future_labels_applied':False}.items():
            if rule.get(key) != value or (value is False and rule.get(key) is not False):
                raise ValueError('Distribution historical bot rule mismatch: '+key)
        if rule.get('max_mean_interval_seconds') != 60 or rule.get('min_span_minutes') != 90:
            raise ValueError('Distribution historical bot screening threshold mismatch')
        for key in ('total_markets', 'all_wallet_count', 'suspected_bot_wallets'):
            _nonnegative_count(data.get(key), key)
        if data['suspected_bot_wallets'] > data['all_wallet_count']:
            raise ValueError('Distribution bot wallet count exceeds all wallets')
        if data.get('reconstruction_method') != 'existing_db_first_row_anchor_plus_counterparty_rows_v1':
            raise ValueError('Distribution reconstruction method mismatch')
        _validate_period_audits(data)
    elif data.get('schema_version') == 2:
        expected['date_utc'] = day or data.get('date_utc')
        date.fromisoformat(expected['date_utc'])
    else:
        raise ValueError('Unsupported distribution schema')
    for key, value in expected.items():
        if data.get(key) != value:
            raise ValueError(f"Distribution {key} mismatch")
    series = data.get('included_series_slugs', [])
    if (not isinstance(series, list) or any(not isinstance(item, str) for item in series)
            or len(set(series)) != len(series) or data.get("series_count") != len(series)):
        raise ValueError("Distribution series count mismatch")
    validation = data.get("validation", {})
    if validation.get("unresolved_groups") != 0 or validation.get("validated_groups") != validation.get("transaction_groups"):
        raise ValueError("Distribution reconstruction is incomplete")
    populations = data.get("populations", {})
    if set(populations) != {"all_wallets", "suspected_bots"}:
        raise ValueError("Distribution populations mismatch")
    with localcontext() as context:
        context.prec = 100
        decimals = [_decimal(row[key], key) for population in populations.values()
                    for row in [population, *population.get('bins', [])]
                    for key in ('shares_exact', 'notional_usdc_exact')]
        if decimals:
            context.prec = max(100, max(value.adjusted() for value in decimals)
                               - min(value.as_tuple().exponent for value in decimals) + 30)
        market_notional = _decimal(populations["all_wallets"]["notional_usdc_exact"], "market notional")
        market_shares = _decimal(populations["all_wallets"]["shares_exact"], "all BUY shares")
        for name, population in populations.items():
            bins = population.get("bins", [])
            if len(bins) != 10:
                raise ValueError("Distribution requires ten price bands")
            count = population.get("trade_count")
            if type(count) is not int or count < 0:
                raise ValueError("Invalid distribution trade count")
            shares = _decimal(population["shares_exact"], "shares")
            notional = _decimal(population["notional_usdc_exact"], "notional")
            if sum(row["trade_count"] for row in bins) != count:
                raise ValueError("Distribution trade totals mismatch")
            if sum((_decimal(row["shares_exact"], "band shares") for row in bins), Decimal(0)) != shares:
                raise ValueError("Distribution share totals mismatch")
            if sum((_decimal(row["notional_usdc_exact"], "band notional") for row in bins), Decimal(0)) != notional:
                raise ValueError("Distribution notional totals mismatch")
            if population["notional_share_of_market_percent"] != _percent(notional, market_notional):
                raise ValueError("Distribution total percentage denominator mismatch")
            if ('share_of_all_buy_shares_percent' in population
                    and population['share_of_all_buy_shares_percent'] != _percent(shares, market_shares)):
                raise ValueError('Distribution total shares percentage denominator mismatch')
            for index, row in enumerate(bins):
                if (row.get("lower_cents"), row.get("upper_cents"), row.get("lower_inclusive"), row.get("upper_inclusive")) != (index * 10, (index + 1) * 10, True, index == 9):
                    raise ValueError("Distribution price bands mismatch")
                if type(row["trade_count"]) is not int or row["trade_count"] < 0:
                    raise ValueError("Invalid distribution band trade count")
                if row["trade_share_percent"] != _percent(row["trade_count"], count):
                    raise ValueError("Distribution trade percentage denominator mismatch")
                if ('share_of_all_buy_shares_percent' in row
                        and row['share_of_all_buy_shares_percent'] != _percent(_decimal(row['shares_exact'], 'band shares'), market_shares)):
                    raise ValueError('Distribution shares percentage denominator mismatch')
                if row["notional_share_of_market_percent"] != _percent(_decimal(row["notional_usdc_exact"], "band notional"), market_notional):
                    raise ValueError("Distribution notional percentage denominator mismatch")
        for all_row, bot_row in zip(populations["all_wallets"]["bins"], populations["suspected_bots"]["bins"]):
            if bot_row["trade_count"] > all_row["trade_count"] or any(
                _decimal(bot_row[key], key) > _decimal(all_row[key], key)
                for key in ("shares_exact", "notional_usdc_exact")
            ):
                raise ValueError("Suspected-bot distribution exceeds all-wallet distribution")
        if validation.get("buy_participating_trades") != populations["all_wallets"]["trade_count"]:
            raise ValueError("Distribution BUY coverage count mismatch")


def validate_buy_event_binding(payload: dict, data: dict | None) -> None:
    """新报告的总览与分布必须绑定同一 BUY 事件金额及全 BUY 分母。"""
    from polymarket_updown_buy_metrics import BASIS, amount, event_summary, percentage
    display = payload.get('display', {})
    marked = (payload.get('bot_panel_basis') == BASIS or 'buy_event_summary' in payload
              or display.get('bot_panel_basis') == BASIS)
    if not marked:
        return  # 历史包仍可读取和核对状态；提交时另作版本门禁。
    if data is None or payload.get('bot_panel_basis') != BASIS:
        raise ValueError('BUY event panel requires the same BUY distribution source')
    expected = event_summary(data)
    actual_summary = payload.get('buy_event_summary')
    copy_version = payload.get('copy_version') or display.get('copy_version')
    if copy_version:
        from polymarket_updown_poster_copy import COPY_VERSION
        if copy_version != COPY_VERSION or payload.get('copy_version') != COPY_VERSION or display.get('copy_version') != COPY_VERSION:
            raise ValueError('Overview approved copy version mismatch')
        summary_matches = actual_summary == expected
    else:
        # 旧BUY一致性报告允许缺少后来新增的份数占比字段，已有字段仍逐一核对。
        added = {'bot_shares_coverage_percent', 'shares_denominator'}
        required = set(expected) - added
        summary_matches = (isinstance(actual_summary, dict)
                           and required <= set(actual_summary) <= set(expected)
                           and all(actual_summary[key] == expected[key] for key in actual_summary))
    if not summary_matches:
        raise ValueError('Overview BUY event summary differs from distribution')
    expected_display = {
        'bot_panel_basis': BASIS,
        'bot_volume': amount(expected['suspected_bots']['notional_usdc_exact']),
        'bot_denominator': amount(expected['all_wallets']['notional_usdc_exact']),
        'bot_volume_share': percentage(expected['bot_up_notional_coverage_percent']),
    }
    for key, value in expected_display.items():
        if display.get(key) != value:
            raise ValueError('Overview BUY panel display differs from distribution: '+key)
    if copy_version:
        all_buy, bots = expected['all_wallets'], expected['suspected_bots']
        for name, row in (('all_buy', all_buy), ('bot_buy', bots)):
            wanted = {name+'_trades': f'{row["trade_count"]:,}',
                      name+'_shares': amount(row['shares_exact']),
                      name+'_notional': amount(row['notional_usdc_exact'])}
            for key, value in wanted.items():
                if display.get(key) != value:
                    raise ValueError('Overview approved copy metric mismatch: '+key)
        ratios = {'all_buy_trade_share': _percent(all_buy['trade_count'], all_buy['trade_count']),
                  'bot_buy_trade_share': expected['bot_trade_coverage_percent'],
                  'all_buy_shares_share': _percent(_decimal(all_buy['shares_exact'], 'all BUY shares'), _decimal(all_buy['shares_exact'], 'all BUY shares')),
                  'bot_buy_shares_share': expected['bot_shares_coverage_percent'],
                  'all_buy_notional_share': _percent(_decimal(all_buy['notional_usdc_exact'], 'all BUY notional'), _decimal(all_buy['notional_usdc_exact'], 'all BUY notional')),
                  'bot_buy_notional_share': expected['bot_up_notional_coverage_percent']}
        for key, value in ratios.items():
            if display.get(key) != percentage(value):
                raise ValueError('Overview approved copy ratio mismatch: '+key)
        current = payload['current']
        count = _nonnegative_count(current.get('wallets_meeting_daily_frequency_rule'), 'daily rule wallets')
        if count > current['unique_wallets'] or display.get('daily_rule_wallets') != f'{count:,}':
            raise ValueError('Overview daily frequency-rule wallet count mismatch')
        for population in data['populations'].values():
            if any('share_of_all_buy_shares_percent' not in row for row in [population, *population['bins']]):
                raise ValueError('Approved copy requires all BUY shares percentages')


def load_distribution(source: Path, payload: dict) -> dict | None:
    """按总览 JSON 的相对文件名及 SHA256 加载分布；缺失或变动时拒绝生成。"""
    reference = payload.get("trade_distribution")
    if reference is None:
        validate_buy_event_binding(payload, None)
        return None
    if not isinstance(reference, dict):
        raise ValueError("Distribution reference must be an object")
    identity = report_identity(payload)
    source = Path(source).resolve()
    from polymarket_updown_price_distribution import statistics_stem
    stem = statistics_stem(identity)
    paths = {}
    for key, suffix, hash_key in (("json_file", ".json", "json_sha256"), ("image_file", ".png", "image_sha256")):
        filename = reference.get(key)
        if filename != stem + suffix:
            raise ValueError("Distribution reference filename/period mismatch")
        path = source.parent / filename
        if _hash(path) != reference.get(hash_key):
            raise ValueError(f"Distribution asset changed: {key}")
        paths[key] = path
    data = json.loads(paths["json_file"].read_text(encoding="utf-8"))
    validate_distribution(data, identity['target_utc'])
    if data.get('schema_version') != (2 if identity['kind'] == 'daily' else 3):
        raise ValueError('Distribution schema does not match overview cadence')
    identity_keys = ('date_utc',) if identity['kind'] == 'daily' else tuple(identity)
    for key in identity_keys:
        if data.get(key) != identity[key] or reference.get(key) != identity[key]:
            raise ValueError('Distribution and overview period mismatch: '+key)
    for key in ("market_scope", "series_count", "count_unit", "price_basis", "notional_basis"):
        if reference.get(key) != data.get(key):
            raise ValueError(f"Distribution reference {key} mismatch")
    if payload["current"]["suspected_bot_wallets"] != data["suspected_bot_wallets"]:
        raise ValueError("Distribution and overview bot population mismatch")
    for overview_key, distribution_key in (('total_markets', 'total_markets'), ('unique_wallets', 'all_wallet_count')):
        if identity['kind'] != 'daily' or (overview_key in payload['current'] and distribution_key in data):
            if payload['current'].get(overview_key) != data.get(distribution_key):
                raise ValueError('Distribution and overview population mismatch: '+overview_key)
    if identity['kind'] != 'daily':
        overview_rule = payload.get('bot_rule', {})
        for key in ('classification', 'classification_scope', 'first_seen_basis', 'future_labels_applied',
                    'max_mean_interval_seconds', 'min_span_minutes'):
            if overview_rule.get(key) != data['bot_rule'].get(key):
                raise ValueError('Distribution and overview bot rule mismatch: '+key)
    validate_buy_event_binding(payload, data)
    return data
