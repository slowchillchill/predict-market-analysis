import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import polymarket_updown_distribution_assets as assets
import polymarket_updown_poster as poster
from polymarket_updown_buy_metrics import BASIS, event_summary, share_percent
from polymarket_updown_poster_copy import COPY_VERSION
import test_polymarket_updown_captions as caption_fixtures
from test_polymarket_updown_distribution_publishing import distribution_fixture


def approved_fixture():
    data = distribution_fixture()
    total = data['populations']['all_wallets']['shares_exact']
    for population in data['populations'].values():
        for row in [population, *population['bins']]:
            row['share_of_all_buy_shares_percent'] = share_percent(row['shares_exact'], total)
    payload = caption_fixtures.CaptionTests().sample()
    payload['current'].update(total_markets=1, wallets_meeting_daily_frequency_rule=1)
    payload.update(bot_panel_basis=BASIS, buy_event_summary=event_summary(data), copy_version=COPY_VERSION)
    payload['display'] = poster.display_values(poster.PosterData(date(2026, 9, 30),
        payload['current'], payload['previous']), data)
    return payload, data


class ApprovedCopyAssetsTests(unittest.TestCase):
    def test_approved_copy_binds_all_three_ratios_and_daily_rule_count(self):
        payload, data = approved_fixture()
        assets.validate_distribution(data, '2026-09-30')
        assets.validate_buy_event_binding(payload, data)
        self.assertEqual(payload['display']['bot_buy_shares_share'], '50.00%')
        self.assertEqual(payload['display']['all_buy_shares_share'], '100.00%')
        self.assertEqual(payload['display']['daily_rule_wallets'], '1')

    def test_own_group_shares_denominator_is_rejected(self):
        payload, data = approved_fixture()
        data['populations']['suspected_bots']['bins'][0]['share_of_all_buy_shares_percent'] = '10.00'
        with self.assertRaisesRegex(ValueError, 'shares percentage denominator'):
            assets.validate_distribution(data)

    def test_display_or_summary_cannot_hide_missing_shares_coverage(self):
        for change in (lambda p: p['display'].update(bot_buy_shares_share='100.00%'),
                       lambda p: p['buy_event_summary'].pop('bot_shares_coverage_percent'),
                       lambda p: p['display'].pop('bot_buy_shares_share')):
            payload, data = approved_fixture()
            change(payload)
            with self.assertRaises(ValueError):
                assets.validate_buy_event_binding(payload, data)

    def test_missing_band_shares_percentage_is_rejected_for_approved_copy(self):
        payload, data = approved_fixture()
        data['populations']['suspected_bots']['bins'][0].pop('share_of_all_buy_shares_percent')
        with self.assertRaisesRegex(ValueError, 'requires all BUY shares percentages'):
            assets.validate_buy_event_binding(payload, data)

    def test_copy_version_and_rule_counter_must_agree_with_source(self):
        for change in (lambda p: p.update(copy_version='unapproved'),
                       lambda p: p['display'].update(daily_rule_wallets='3'),
                       lambda p: p['current'].pop('wallets_meeting_daily_frequency_rule')):
            payload, data = approved_fixture()
            change(payload)
            with self.assertRaises(ValueError):
                assets.validate_buy_event_binding(payload, data)

    def test_old_buy_bound_report_can_be_read_without_new_optional_ratios(self):
        payload, data = approved_fixture()
        payload.pop('copy_version')
        payload['display'].pop('copy_version')
        for key in ('bot_shares_coverage_percent', 'shares_denominator'):
            payload['buy_event_summary'].pop(key)
        for population in data['populations'].values():
            for row in [population, *population['bins']]:
                row.pop('share_of_all_buy_shares_percent')
        assets.validate_distribution(data)
        assets.validate_buy_event_binding(payload, data)


if __name__ == '__main__':
    unittest.main()
