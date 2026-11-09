import copy
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import polymarket_updown_captions as captions
import polymarket_updown_distribution_assets as assets
import polymarket_updown_douyin as douyin
import polymarket_publish_state as state
from polymarket_updown_buy_metrics import BASIS, amount, event_summary, percentage
import test_polymarket_updown_captions as caption_fixtures
from test_polymarket_updown_period_captions_douyin import period_payload
import test_polymarket_updown_period_publishing_assets as asset_fixtures
from test_polymarket_updown_period_publishing_assets import period_distribution_fixture


def bind(payload, data):
    summary = event_summary(data)
    payload.update(bot_panel_basis=BASIS, buy_event_summary=summary)
    payload['display'] = dict(bot_panel_basis=BASIS,
        bot_volume=amount(summary['suspected_bots']['notional_usdc_exact']),
        bot_denominator=amount(summary['all_wallets']['notional_usdc_exact']),
        bot_volume_share=percentage(summary['bot_up_notional_coverage_percent']))
    return payload


class BuyOnlyPublicationTests(unittest.TestCase):
    def test_previous_explicit_or_unknown_basis_never_inherits_current_buy_basis(self):
        payload = {'bot_volume_basis':'wallet_buys_only', 'current':{},
                   'previous':{'wallet_volume_basis':'wallet_buys_plus_sells'}}
        self.assertTrue(douyin.wallet_buy_only(payload))
        self.assertTrue(douyin.wallet_buy_only(payload, payload['current']))
        self.assertFalse(douyin.wallet_buy_only(payload, payload['previous']))
        self.assertFalse(douyin.wallet_buy_only(payload, {}))
        self.assertFalse(douyin.wallet_buy_only(payload, {'bot_volume_basis':'wallet_buys_plus_sells'}))
        self.assertTrue(douyin.wallet_buy_only(payload, {'wallet_volume_basis':'wallet_buys_only'}))

    def test_buy_only_legacy_caption_uses_buy_wallet_denominator(self):
        payload = caption_fixtures.CaptionTests().sample()
        payload['current']['wallet_volume_basis'] = 'wallet_buys_only'
        _, text = captions.render_zh(payload)
        self.assertIn('钱包侧 BUY 原价金额占比：9.50%', text)
        self.assertIn('分母为全部钱包 BUY 原价金额', text)
        self.assertNotIn('买入＋卖出', text)
        self.assertNotIn('买入与卖出', text)

    def test_distribution_caption_names_event_coverage_not_wallet_expenditure(self):
        data = period_distribution_fixture('monthly')
        payload = period_payload(data)
        payload['current']['suspected_bot_volume_micro_usdc'] = 12012037493586
        _, text = captions.render_zh(payload, distribution_data=data)
        self.assertIn('疑似机器人买方参与成交', text)
        summary = event_summary(data)
        self.assertIn('占全部 BUY 成交份数：'+percentage(summary['bot_shares_coverage_percent']), text)
        self.assertIn('占全部 BUY Up 等价金额：'+percentage(summary['bot_up_notional_coverage_percent']), text)
        self.assertIn('不代表机器人钱包自身支出', text)
        self.assertIn('Shares % 分母为全部 BUY 成交份数', text)
        self.assertIn('Notional % 分母为全部 BUY 事件 Up 等价金额', text)
        self.assertNotIn('12,012,037.49', text)
        self.assertNotIn('买入＋卖出', text)
        self.assertLessEqual(captions.utf16_length(text), 1000)

    def test_period_third_card_uses_distribution_amount_and_has_no_raw_pp_change(self):
        data = period_distribution_fixture('weekly')
        payload = period_payload(data)
        payload['current']['suspected_bot_volume_micro_usdc'] = 12012037493586
        records = {}
        original = douyin.Card.text
        def capture(card, text, x, y, size=32, color=douyin.INK, bold=False):
            records.setdefault(id(card.image), []).append(text)
            return original(card, text, x, y, size, color, bold)
        with patch.object(douyin.Card, 'text', capture):
            images = douyin.render_cards(payload, 'weekly', data['start_date_utc'],
                str(date.fromisoformat(data['end_date_utc_exclusive'])-timedelta(days=1)), distribution_data=data)
        self.assertEqual(len(images), 6)
        third = '\n'.join(list(records.values())[2])
        self.assertIn('Up 等价金额参与覆盖率', third)
        self.assertIn(amount(data['populations']['suspected_bots']['notional_usdc_exact']), third)
        self.assertIn(amount(data['populations']['all_wallets']['notional_usdc_exact']), third)
        self.assertIn('不是钱包自身支出', third)
        self.assertNotIn('12,012,037.49', third)
        self.assertNotIn('买入＋卖出', third)
        self.assertNotIn('个百分点', third)

    def test_cross_page_binding_rejects_old_amount_denominator_or_share(self):
        data = period_distribution_fixture()
        valid = bind(period_payload(data), data)
        assets.validate_buy_event_binding(valid, data)
        for key, wrong in [('bot_volume', '12,012,037.49'), ('bot_denominator', '8,977,047.76'),
                           ('bot_volume_share', '66.19%'), ('bot_panel_basis', 'wallet_buys_plus_sells')]:
            payload = copy.deepcopy(valid)
            payload['display'][key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError):
                assets.validate_buy_event_binding(payload, data)
        changed = copy.deepcopy(valid)
        changed['buy_event_summary']['suspected_bots']['notional_usdc_exact'] = '12012037.493586'
        with self.assertRaises(ValueError):
            assets.validate_buy_event_binding(changed, data)
        with self.assertRaises(ValueError):
            assets.validate_buy_event_binding(valid, None)

    def test_legacy_buy_wallet_panel_does_not_require_event_distribution(self):
        payload = caption_fixtures.CaptionTests().sample()
        payload.update(bot_panel_basis='wallet_buys_only', bot_volume_basis='wallet_buys_only')
        payload['display'] = {'bot_panel_basis':'wallet_buys_only'}
        assets.validate_buy_event_binding(payload, None)

    def test_load_distribution_checks_binding_after_matching_source_reference(self):
        fixture = asset_fixtures.PeriodPublishingAssetsTests()
        with tempfile.TemporaryDirectory() as temp:
            source, payload, data = fixture.write_source(Path(temp))
            bind(payload, data)
            self.assertEqual(assets.load_distribution(source, payload), data)
            payload['display']['bot_volume'] = '12,012,037.49'
            with self.assertRaisesRegex(ValueError, 'display differs'):
                assets.load_distribution(source, payload)

    def test_legacy_distribution_cannot_be_newly_submitted_but_can_be_reconciled(self):
        fixture = asset_fixtures.PeriodPublishingAssetsTests()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, data = fixture.write_source(root)
            manifest, _ = fixture.manifest(source, payload)
            with patch.object(state, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'mixed-basis'):
                    state.record(manifest, 'x', 'submitting', verified_absent=True)
                self.assertFalse((root/'outputs/publish_state').exists())
                recorded = state.record(manifest, 'x', 'published', post_url='https://example.com/existing', evidence='existing post checked')
                self.assertEqual(json.loads(recorded.read_text())['status'], 'published')

    def test_buy_consistent_distribution_can_pass_submission_guard(self):
        fixture = asset_fixtures.PeriodPublishingAssetsTests()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, data = fixture.write_source(root)
            bind(payload, data)
            source.write_text(json.dumps(payload))
            manifest, _ = fixture.manifest(source, payload)
            with patch.object(state, 'ROOT', root):
                recorded = state.record(manifest, 'x', 'submitting', verified_absent=True)
                self.assertEqual(json.loads(recorded.read_text())['status'], 'submitting')


if __name__ == '__main__':
    unittest.main()
