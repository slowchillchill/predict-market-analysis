import copy
import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import polymarket_updown_distribution_chart as chart
import polymarket_updown_bot_price_poster as poster
from polymarket_updown_buy_metrics import amount, share_percent
from test_polymarket_updown_bot_price_poster import example_data


def refresh_totals(data):
    """Keep edited boundary fixtures internally consistent, like source statistics."""
    with localcontext() as context:
        context.prec = 100
        for population in data['populations'].values():
            population['trade_count'] = sum(row['trade_count'] for row in population['bins'])
            for field in ('shares_exact', 'notional_usdc_exact'):
                population[field] = str(sum(Decimal(row[field]) for row in population['bins']))
        all_notional = data['populations']['all_wallets']['notional_usdc_exact']
        for population in data['populations'].values():
            population['notional_share_of_market_percent'] = share_percent(population['notional_usdc_exact'], all_notional)
            for row in population['bins']:
                row['trade_share_percent'] = share_percent(row['trade_count'], population['trade_count'])
                row['notional_share_of_market_percent'] = share_percent(row['notional_usdc_exact'], all_notional)
    return data


class DailyDistributionChartTests(unittest.TestCase):
    def test_daily_chart_preserves_exact_amounts_and_uses_two_different_denominators(self):
        data = example_data(all_series=True)
        before = copy.deepcopy(data)
        canvas, summary = chart.render(data)
        self.assertEqual(data, before)
        self.assertEqual(canvas.image.size, (1600, 2000))
        self.assertEqual(canvas.image.mode, 'RGB')
        self.assertEqual(len(canvas.bars), 20)
        self.assertEqual(canvas.axis_max_percent, 15)
        all_notional = data['populations']['all_wallets']['notional_usdc_exact']
        for bar in canvas.bars:
            population = data['populations'][bar['group']]
            row = next(row for row in population['bins'] if row['interval_cents'] == bar['interval_cents'])
            self.assertEqual(bar['denominator'], population['trade_count'])
            self.assertEqual(bar['notional_denominator_usdc_exact'], all_notional)
            self.assertEqual(bar['notional_usdc_display'], amount(row['notional_usdc_exact']))
            self.assertEqual(bar['notional_share_of_all_buy_percent'], share_percent(row['notional_usdc_exact'], all_notional))
            expected_width = Decimal(bar['trade_count']) * 100 / population['trade_count'] / 15 * 510
            self.assertLessEqual(abs(Decimal(bar['bounds'][2]-bar['bounds'][0])-expected_width), Decimal('.5'))
        self.assertEqual(summary['bot_shares_coverage_percent'], share_percent(
            data['populations']['suspected_bots']['shares_exact'], data['populations']['all_wallets']['shares_exact']))
        self.assertEqual(len([box for box in canvas.boxes if box['role'] == 'bin_metric']), 20)
        self.assertEqual(len([box for box in canvas.boxes if box['role'] == 'bin_notional']), 20)

    def test_titles_stay_on_one_line_and_amounts_retain_readable_font_sizes(self):
        canvas, _ = chart.render(example_data(all_series=True))
        for role, expected in [('title', ['BUY Price Distribution']),
                               ('panel_title', ['All BUY Trades', 'Trades With Suspected Bot Buyers'])]:
            self.assertEqual([box['text'] for box in canvas.boxes if box['role'] == role], expected)
        for box in canvas.boxes:
            if box['role'] == 'bin_metric':
                self.assertEqual(box['size_px'], 25)
            elif box['role'] == 'bin_notional':
                self.assertEqual(box['size_px'], 24)
            left, top, right, bottom = box['bounds']
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, 1600)
            self.assertLessEqual(bottom, 2000)

    def test_any_daily_date_is_rendered_from_the_source(self):
        data = example_data(all_series=True)
        data['date_utc'] = '2031-02-03'
        _, boxes = poster.render_poster(data)
        self.assertIn('03 FEB 2031 · UTC MARKET END DATE', [box['text'] for box in boxes])

    def test_zero_buy_day_has_no_bars_or_invented_percentages(self):
        canvas, summary = chart.render(example_data(all_series=True, empty=True))
        self.assertIsNone(summary['bot_trade_coverage_percent'])
        self.assertIsNone(summary['bot_shares_coverage_percent'])
        self.assertIsNone(summary['bot_up_notional_coverage_percent'])
        self.assertFalse(any(bar['drawn'] for bar in canvas.bars))
        self.assertTrue(all(bar['unrounded_share_percent'] is None for bar in canvas.bars))
        texts = [box['text'] for box in canvas.boxes]
        self.assertEqual(texts.count('No BUY trades in this group'), 2)
        self.assertEqual(texts.count('N/A'), 3)
        self.assertEqual(texts.count('0  ·  N/A'), 20)
        self.assertEqual(texts.count('0.00  ·  N/A'), 20)
        self.assertNotIn('0%', texts)
        self.assertNotIn('0.00%', texts)

    def test_empty_bot_group_uses_na_for_group_mix_but_valid_zero_global_amount_share(self):
        data = example_data(all_series=True)
        for row in data['populations']['suspected_bots']['bins']:
            row.update(trade_count=0, shares_exact='0', notional_usdc_exact='0')
        canvas, summary = chart.render(refresh_totals(data))
        bot_bars = [bar for bar in canvas.bars if bar['group'] == 'suspected_bots']
        self.assertTrue(all(bar['trade_share_percent'] is None and not bar['drawn'] for bar in bot_bars))
        self.assertTrue(all(bar['notional_share_of_all_buy_percent'] == '0.00' for bar in bot_bars))
        self.assertEqual(summary['bot_trade_coverage_percent'], '0.00')
        self.assertEqual(summary['bot_up_notional_coverage_percent'], '0.00')

    def test_single_bin_concentration_expands_both_charts_to_the_same_hundred_percent_axis(self):
        data = example_data(all_series=True)
        for population in data['populations'].values():
            for i, row in enumerate(population['bins']):
                row['trade_count'] = population['trade_count'] if i == 0 else 0
                row['shares_exact'] = population['shares_exact'] if i == 0 else '0'
                row['notional_usdc_exact'] = population['notional_usdc_exact'] if i == 0 else '0'
        canvas, _ = chart.render(refresh_totals(data))
        self.assertEqual(canvas.axis_max_percent, 100)
        nonzero = [bar for bar in canvas.bars if bar['drawn']]
        self.assertEqual(len(nonzero), 2)
        self.assertTrue(all(bar['bounds'][2]-bar['bounds'][0] == 510 for bar in nonzero))
        self.assertEqual([box['text'] for box in canvas.boxes].count('100%'), 2)

    def test_using_bot_notional_as_the_bin_denominator_is_rejected(self):
        data = example_data(all_series=True)
        bots = data['populations']['suspected_bots']
        row = bots['bins'][0]
        row['notional_share_of_market_percent'] = share_percent(row['notional_usdc_exact'], bots['notional_usdc_exact'])
        with self.assertRaisesRegex(ValueError, 'notional-share denominator'):
            chart.render(data)

    def test_notional_uses_exact_decimal_values_and_half_up_display(self):
        data = example_data(all_series=True)
        data['populations']['suspected_bots']['bins'][0]['notional_usdc_exact'] = '123456.785'
        canvas, _ = chart.render(refresh_totals(data))
        first = next(bar for bar in canvas.bars if bar['group'] == 'suspected_bots')
        self.assertEqual(first['notional_usdc_display'], '123,456.79')
        self.assertEqual(first['notional_share_of_all_buy_percent'], share_percent('123456.785', data['populations']['all_wallets']['notional_usdc_exact']))

    def test_daily_larger_values_are_complete_without_font_shrinking(self):
        data = example_data(all_series=True)
        with localcontext() as context:
            context.prec = 100
            for population in data['populations'].values():
                for row in population['bins']:
                    row['trade_count'] *= 10
                    for field in ('shares_exact', 'notional_usdc_exact'):
                        row[field] = str(Decimal(row[field])*10)
        canvas, _ = chart.render(refresh_totals(data))
        totals = [box for box in canvas.boxes if box['role'] == 'exact_total']
        self.assertEqual(len(totals), 6)
        self.assertTrue(all(box['size_px'] == 25 for box in totals))


if __name__ == '__main__':
    unittest.main()
