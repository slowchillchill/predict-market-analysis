"""BUY 重建成交参与指标；不将事件覆盖额解释成钱包自身支出。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

BASIS = 'reconstructed_buy_event_up_equivalent'


def _decimal(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Invalid BUY event '+label) from exc
    if not number.is_finite() or number < 0:
        raise ValueError('Invalid BUY event '+label)
    return number


def share_percent(value, total):
    """Return a HALF_UP percentage from exact values, or None for a zero total."""
    value = _decimal(value, 'percentage numerator')
    total = _decimal(total, 'percentage denominator')
    if total == 0:
        return None
    with localcontext() as context:
        context.prec = max(100, len(value.as_tuple().digits)+len(total.as_tuple().digits)
                           + abs(value.adjusted())+abs(total.adjusted())+10)
        return f'{(value*100/total).quantize(Decimal(".01"),rounding=ROUND_HALF_UP):.2f}'


def _percent(value, total):
    return share_percent(value, total)


def event_summary(distribution: dict) -> dict:
    """从同一份 BUY 分布直接取总值，保持金额、参与分子与全 BUY 分母一致。"""
    for key, expected in [('scope','buy_participation'),('bot_scope','buy_participation'),
                          ('price_basis','up_equivalent'),('count_unit','reconstructed_trades'),
                          ('notional_basis','up_equivalent_price_times_shares')]:
        if key in distribution and distribution[key] != expected:
            raise ValueError('Unsupported BUY event summary '+key)
    populations = {}
    for name in ('all_wallets','suspected_bots'):
        row = distribution['populations'][name]
        count = row['trade_count']
        if type(count) is not int or count < 0:
            raise ValueError('Invalid BUY event trade count')
        _decimal(row['shares_exact'],name+' shares')
        _decimal(row['notional_usdc_exact'],name+' notional')
        populations[name] = {key:row[key] for key in ('trade_count','shares_exact','notional_usdc_exact')}
    all_buy, bots = populations['all_wallets'],populations['suspected_bots']
    if any(_decimal(bots[key],key)>_decimal(all_buy[key],key)
           for key in ('trade_count','shares_exact','notional_usdc_exact')):
        raise ValueError('Bot BUY event participation exceeds all BUY events')
    return {'basis':BASIS,'side':'BUY',**populations,
            'bot_trade_coverage_percent':_percent(bots['trade_count'],all_buy['trade_count']),
            'bot_shares_coverage_percent':share_percent(bots['shares_exact'],all_buy['shares_exact']),
            'bot_up_notional_coverage_percent':_percent(_decimal(bots['notional_usdc_exact'],'bot notional'),
                                                       _decimal(all_buy['notional_usdc_exact'],'all notional')),
            'shares_denominator':'all_wallets_buy_event_shares',
            'notional_denominator':'all_wallets_buy_event_up_notional'}


def amount(value) -> str:
    with localcontext() as context:
        context.prec = 100
        return f'{_decimal(value,"amount").quantize(Decimal(".01"),rounding=ROUND_HALF_UP):,.2f}'


def percentage(value) -> str:
    return 'N/A' if value is None else value+'%'


def coverage_note(share: str, *, dense=False) -> str:
    # This is event coverage weighted by Up notional, not wallets' own cash share.
    return f'Notional=Up price*shares; bot BUY event coverage={share} of all BUY notional.'
