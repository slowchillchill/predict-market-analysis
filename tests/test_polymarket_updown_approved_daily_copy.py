import copy
import sys
import unittest
from datetime import date
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_poster as poster
import polymarket_updown_poster_copy as approved


def fixture():
    current = dict(total_markets=3295, unique_wallets=10444,
                   suspected_bot_wallets=1189, wallets_meeting_daily_frequency_rule=334,
                   volume_micro_usdc=8977047755386, wallet_volume_micro_usdc=15600030915319,
                   suspected_bot_volume_micro_usdc=10310519989466)
    previous = dict(volume_micro_usdc=8624333260445, unique_wallets=10271)
    distribution = dict(series_count=38, scope='buy_participation', bot_scope='buy_participation',
                        price_basis='up_equivalent', count_unit='reconstructed_trades',
                        notional_basis='up_equivalent_price_times_shares', populations={
        'all_wallets':dict(trade_count=1308320, shares_exact='17529049.772681',
                           notional_usdc_exact='8749963.6746142221732089'),
        'suspected_bots':dict(trade_count=1126684, shares_exact='14675595.816832',
                              notional_usdc_exact='7354703.80250785451022')})
    return poster.PosterData(date(2026,11,8), current, previous), distribution


class ApprovedDailyCopyTests(unittest.TestCase):
    def test_six_metrics_and_context_bind_the_unrounded_source(self):
        data, distribution = fixture()
        values = poster.display_values(data, distribution)
        self.assertEqual(values['copy_version'], approved.COPY_VERSION)
        self.assertEqual(values['market_end_date'], 'Nov 8, 2026 · UTC Market End Date')
        self.assertEqual(values['scope_label'], '38 Series · 3,295 Markets')
        self.assertEqual(values['daily_rule_wallets'], '334')
        expected = {
            'all_buy_trades':'1,308,320', 'bot_buy_trades':'1,126,684',
            'all_buy_trade_share':'100.00%', 'bot_buy_trade_share':'86.12%',
            'all_buy_shares':'17,529,049.77', 'bot_buy_shares':'14,675,595.82',
            'all_buy_shares_share':'100.00%', 'bot_buy_shares_share':'83.72%',
            'all_buy_notional':'8,749,963.67', 'bot_buy_notional':'7,354,703.80',
            'all_buy_notional_share':'100.00%', 'bot_buy_notional_share':'84.05%',
        }
        for key, value in expected.items():
            self.assertEqual(values[key], value, key)
        self.assertEqual(values['bot_volume'], values['bot_buy_notional'])
        self.assertEqual(values['bot_denominator'], values['all_buy_notional'])

    def test_approved_copy_is_drawn_without_overlap_or_truncation(self):
        data, distribution = fixture()
        values = poster.display_values(data, distribution)
        image, boxes = poster.render_poster(values)
        self.assertEqual(image.size, (1600,2000))
        self.assertEqual(image.mode, 'RGB')
        logical = {box.get('source_text', box['text']) for box in boxes}
        for text in (approved.BRAND, approved.OVERVIEW_TITLE, approved.MARKET_CONTEXT_TITLE,
                     approved.SUMMARY_TITLE, *approved.CONTEXT_LABELS,
                     *approved.SUMMARY_COLUMNS, *approved.SUMMARY_METRICS,
                     *approved.OVERVIEW_NOTES):
            self.assertIn(text, logical)
        for key in ('all_buy_trades','bot_buy_trades','bot_buy_trade_share','all_buy_shares',
                    'bot_buy_shares','bot_buy_shares_share','all_buy_notional',
                    'bot_buy_notional','bot_buy_notional_share'):
            self.assertIn(values[key], logical)
        self.assertNotIn('Wallet Trading Volume', logical)
        self.assertNotIn('12,012,037.49', logical)
        for box in boxes:
            left, top, right, bottom = box['bounds']
            self.assertGreaterEqual(left, 100)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, 1485)
            self.assertLessEqual(bottom, 2000)
            self.assertGreaterEqual(box['font_size_px'], 21)
        for a,b in combinations(boxes,2):
            x1,y1,x2,y2 = a['bounds']; u1,v1,u2,v2 = b['bounds']
            self.assertFalse(min(x2,u2)>max(x1,u1) and min(y2,v2)>max(y1,v1),
                             f"Text collision: {a['text']} / {b['text']}")
        for box in boxes:
            if box.get('source_text') in (approved.GROUP_ALL, approved.GROUP_BOTS):
                self.assertLess(box['bounds'][3], 690*poster.SCALE,
                                'A column heading must clear the header rule')

    def test_zero_event_denominators_do_not_claim_100_percent(self):
        data, distribution = fixture()
        for population in distribution['populations'].values():
            population.update(trade_count=0, shares_exact='0', notional_usdc_exact='0')
        values = poster.display_values(data, distribution)
        for key in ('all_buy_trade_share','bot_buy_trade_share','all_buy_shares_share',
                    'bot_buy_shares_share','all_buy_notional_share','bot_buy_notional_share'):
            self.assertEqual(values[key], 'N/A')

    def test_no_distribution_keeps_the_explicit_wallet_buy_legacy_page(self):
        data, _ = fixture()
        values = poster.display_values(data)
        self.assertNotIn('copy_version', values)
        self.assertEqual(values['bot_panel_basis'], 'wallet_buys_only')
        self.assertEqual(values['bot_volume'], '10,310,519.99')
        _, boxes = poster.render_poster(values)
        self.assertIn('Market Overview', {box['text'] for box in boxes})


if __name__ == '__main__':
    unittest.main()
