"""The overview and distribution must share one BUY event monetary grain."""
import copy
import sys
import unittest
from datetime import date
from decimal import localcontext
from itertools import combinations
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_buy_metrics as metrics
import polymarket_updown_poster as daily
import polymarket_updown_weekly_poster as weekly
import polymarket_updown_monthly_poster as monthly


def fixture():
    return {'scope':'buy_participation','bot_scope':'buy_participation','price_basis':'up_equivalent',
            'count_unit':'reconstructed_trades','notional_basis':'up_equivalent_price_times_shares',
            'populations':{
                'all_wallets':{'trade_count':1308320,'shares_exact':'17529049.772681',
                              'notional_usdc_exact':'8749963.6746142221732089'},
                'suspected_bots':{'trade_count':1126684,'shares_exact':'14675595.816832',
                                  'notional_usdc_exact':'7354703.80250785451022'}}}


class BuyEventBindingsTests(unittest.TestCase):
    def current(self):
        return {'start_date_utc':'2026-11-02','end_date_utc_exclusive':'2026-11-09',
                'total_markets':3295,'unique_wallets':10444,'suspected_bot_wallets':1189,
                'volume_micro_usdc':8977047755386,'wallet_volume_micro_usdc':15600030915319,
                'suspected_bot_volume_micro_usdc':10310519989466}

    def test_shared_summary_preserves_raw_precision_and_names_event_coverage(self):
        data=fixture();summary=metrics.event_summary(data)
        self.assertEqual(summary['basis'],'reconstructed_buy_event_up_equivalent')
        self.assertEqual(summary['side'],'BUY')
        self.assertEqual(summary['suspected_bots']['notional_usdc_exact'],'7354703.80250785451022')
        self.assertEqual(summary['bot_trade_coverage_percent'],'86.12')
        self.assertEqual(summary['bot_shares_coverage_percent'],'83.72')
        self.assertEqual(summary['bot_up_notional_coverage_percent'],'84.05')
        self.assertEqual(summary['shares_denominator'],'all_wallets_buy_event_shares')
        self.assertEqual(summary['notional_denominator'],'all_wallets_buy_event_up_notional')
        summary['suspected_bots']['trade_count']=1
        self.assertEqual(data['populations']['suspected_bots']['trade_count'],1126684)

    def test_all_overview_cadences_bind_same_buy_numerator_and_denominator(self):
        current=self.current();data=fixture()
        displays=[daily.display_values(daily.PosterData(date(2026,11,8),current,None),data),
                  weekly.display_values(current,None,data),monthly.display_values(current,None,data)]
        for values in displays:
            self.assertEqual(values['bot_panel_basis'],metrics.BASIS)
            self.assertEqual(values['bot_volume'],'7,354,703.80')
            self.assertEqual(values['bot_denominator'],'8,749,963.67')
            self.assertEqual(values['bot_volume_share'],'84.05%')
            self.assertNotEqual(values['bot_volume'],'10,310,519.99')
        # Prior raw wallet ratios cannot stand in for prior reconstructed-event coverage.
        previous={**current,'suspected_bot_volume_micro_usdc':1}
        self.assertEqual(weekly.display_values(current,previous,data)['bot_share_change'][0],'N/A')
        self.assertEqual(monthly.display_values(current,previous,data)['bot_share_change'][0],'N/A')

    def test_legacy_mode_exposes_buy_only_wallet_basis(self):
        values=daily.display_values(daily.PosterData(date(2026,11,8),self.current(),None))
        self.assertEqual(values['bot_panel_basis'],'wallet_buys_only')
        self.assertEqual(values['bot_volume'],'10,310,519.99')
        self.assertEqual(values['bot_denominator'],'15,600,030.92')
        self.assertEqual(values['bot_volume_share'],'66.09%')

    def test_invalid_scope_or_population_values_fail(self):
        for change in [lambda d:d.update(bot_scope='all_participation'),
                       lambda d:d['populations']['suspected_bots'].update(trade_count=2000000),
                       lambda d:d['populations']['all_wallets'].update(shares_exact='NaN')]:
            data=fixture();change(data)
            with self.assertRaises(ValueError):metrics.event_summary(data)

    def test_zero_denominators_stay_undefined(self):
        data=fixture()
        for row in data['populations'].values():row.update(trade_count=0,shares_exact='0',notional_usdc_exact='0')
        summary=metrics.event_summary(data)
        self.assertIsNone(summary['bot_trade_coverage_percent'])
        self.assertIsNone(summary['bot_shares_coverage_percent'])
        self.assertIsNone(summary['bot_up_notional_coverage_percent'])

    def test_share_percent_uses_exact_inputs_and_half_up_independent_of_context(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(metrics.share_percent('2.01','200'),'1.01')
            self.assertEqual(metrics.share_percent('2.009999999999999999999999','200'),'1.00')
            self.assertEqual(metrics.share_percent('14675595.816832','17529049.772681'),'83.72')
            self.assertEqual(metrics.share_percent('1e150','2e150'),'50.00')
        self.assertIsNone(metrics.share_percent('0','0'))

    def test_event_mode_panels_show_matching_denominator_without_old_wallet_amount(self):
        current=self.current()
        current.update(wallet_volume_micro_usdc=18147197539959,
                       suspected_bot_volume_micro_usdc=12012037493586,
                       daily_volume_micro_usdc={f'2026-11-{day:02}':1000000000000 for day in range(2,9)},
                       asset_volume_micro_usdc={'BTC':8977047755386})
        data=fixture()
        daily_image,daily_boxes=daily.render_poster(daily.display_values(daily.PosterData(date(2026,11,8),current,None),data))
        weekly_image,weekly_boxes=weekly.render(current,None,data)
        monthly_current={**current,'start_date_utc':'2026-10-01','end_date_utc_exclusive':'2026-11-01',
                         'daily_volume_micro_usdc':{f'2026-10-{day:02}':1000000000000 for day in range(1,32)}}
        monthly_image,monthly_boxes=monthly.render(monthly_current,None,[],data)
        for image,boxes,top in [(daily_image,daily_boxes,838),(weekly_image,weekly_boxes,1030),(monthly_image,monthly_boxes,1086)]:
            self.assertEqual(image.size,(1600,2000))
            labels='\n'.join(row['text'] for row in boxes)
            self.assertIn('7,354,703.80',labels)
            self.assertIn('8,749,963.67',labels)
            self.assertIn('84.05%',labels)
            self.assertNotIn('12,012,037.49',labels)
            panel=[row for row in boxes if row['bounds'][1]>=top*daily.SCALE]
            for a,b in combinations(panel,2):
                x1,y1,x2,y2=a['bounds'];u1,v1,u2,v2=b['bounds']
                self.assertFalse(min(x2,u2)>max(x1,u1) and min(y2,v2)>max(y1,v1),
                                 f"Text collision: {a['text']} / {b['text']}")


if __name__=='__main__':unittest.main()
