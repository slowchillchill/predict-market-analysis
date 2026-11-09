import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import polymarket_updown_captions as captions
import polymarket_updown_distribution_assets as assets
import polymarket_publish_state as state
import test_polymarket_updown_captions as fixtures


def distribution_fixture():
    data = dict(schema_version=2, date_utc='2026-09-30', date_basis='market_end_utc',
                market_scope='all_crypto_updown_series', series_slug=None,
                included_series_slugs=['btc-up-or-down-5m'], series_count=1, total_markets=1,
                scope='buy_participation', bot_scope='buy_participation',
                price_basis='up_equivalent', notional_basis='up_equivalent_price_times_shares',
                count_unit='reconstructed_trades', trade_share_denominator='population_trade_count',
                notional_share_denominator='all_wallets_buy_notional', suspected_bot_wallets=1,
                bot_rule=dict(max_mean_interval_seconds=60, min_span_minutes=90),
                validation=dict(unresolved_groups=0, transaction_groups=100, validated_groups=100,
                                buy_participating_trades=100), populations={})
    for name, divisor in [('all_wallets', 1), ('suspected_bots', 2)]:
        bins = [dict(lower_cents=i*10, upper_cents=(i+1)*10, lower_inclusive=True,
                     upper_inclusive=i == 9, trade_count=10//divisor,
                     trade_share_percent='10.00', shares_exact=str(100//divisor),
                     notional_usdc_exact=str((5+10*i)/divisor),
                     notional_share_of_market_percent=f'{(1+2*i)/divisor:.2f}') for i in range(10)]
        data['populations'][name] = dict(trade_count=100//divisor, shares_exact=str(1000//divisor),
            notional_usdc_exact=str(500//divisor),
            notional_share_of_market_percent=f'{100/divisor:.2f}', bins=bins)
    return data


class DistributionPublishingTests(unittest.TestCase):
    def source(self, folder):
        source = folder/'updown_2026-09-30.json'
        payload = fixtures.CaptionTests().sample()
        data = distribution_fixture()
        stem = 'updown_buy_trade_distribution_all_series_2026-09-30'
        distribution_json = folder/(stem+'.json')
        distribution_image = folder/(stem+'.png')
        distribution_json.write_text(json.dumps(data))
        Image.new('RGB', (20, 20), 'blue').save(distribution_image)
        payload['trade_distribution'] = {key: data[key] for key in
            ('date_utc', 'market_scope', 'series_count', 'count_unit', 'price_basis', 'notional_basis')}
        payload['trade_distribution'].update(json_file=distribution_json.name,
            json_sha256=captions.sha256(distribution_json), image_file=distribution_image.name,
            image_sha256=captions.sha256(distribution_image))
        source.write_text(json.dumps(payload))
        Image.new('RGB', (20, 20)).save(source.with_suffix('.png'))
        (folder/'updown_2026-09-30_tweet.md').write_text('All BUY: 100 trades | 1,000 shares | 500 USDC\nSuspected bot BUY: 50 trades | 500 shares | 250 USDC\n')
        return source, payload, data

    def test_chinese_contains_new_metrics_and_explicit_denominators(self):
        payload = fixtures.CaptionTests().sample()
        title, body = captions.render_zh(payload, distribution_data=distribution_fixture())
        for text in ('全部钱包买入', '疑似机器人买方参与成交', '成交笔数（Trades）：100',
                     '成交份数（Shares）：1,000.00', '500.00 USDC', '50.00%',
                     'Trades 不是唯一订单数', 'Trade % 分母为本图总成交笔数',
                     'Shares % 分母为全部 BUY 成交份数',
                     '占全部 BUY 成交份数：50.00%',
                     '占全部 BUY Up 等价金额：50.00%',
                     'Notional % 分母为全部 BUY 事件 Up 等价金额', '最后一档含100美分'):
            self.assertIn(text, body)
        self.assertLessEqual(captions.utf16_length(title), 20)
        self.assertLessEqual(captions.utf16_length(body), 1000)

    def test_daily_retained_active_count_does_not_claim_same_day_qualification(self):
        payload = fixtures.CaptionTests().sample()
        data = distribution_fixture()
        data['bot_rule']['classification'] = 'first_hit_day_onward'
        data['suspected_bot_wallets'] = 1189
        payload['current'].update(unique_wallets=10444, suspected_bot_wallets=1189,
                                  wallets_meeting_daily_frequency_rule=334)
        _, body = captions.render_zh(payload, distribution_data=data)
        self.assertIn('历史首次命中后持续归类，本期活跃疑似机器人地址：1,189个。', body)
        self.assertIn('当天符合上述规则：334个地址。', body)
        self.assertNotIn('分钟，共1,189个地址', body)
        self.assertLessEqual(captions.utf16_length(body), 1000)
        del payload['current']['wallets_meeting_daily_frequency_rule']
        self.assertNotIn('当天符合上述规则', captions.render_zh(payload, distribution_data=data)[1])

    def test_manifest_links_two_english_images_and_four_new_chinese_cards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.source(root)
            before = source.read_bytes()
            manifest = captions.write_sidecars(source)
            self.assertEqual(source.read_bytes(), before)
            for platform in ('x', 'linkedin', 'tiktok'):
                item = manifest['platforms'][platform]
                self.assertEqual(item['asset_version'], 'daily_buy_distribution_v2')
                self.assertEqual(len(item['image_paths']), 2)
                self.assertEqual(item['image_sha256s'], [captions.sha256(Path(path)) for path in item['image_paths']])
                self.assertEqual(Path(item['image_paths'][1]).name, payload['trade_distribution']['image_file'])
            douyin = manifest['platforms']['douyin']
            self.assertEqual(douyin['asset_version'], 'research_cards_buy_distribution_v2')
            self.assertEqual(douyin['image_purposes'], ['本期总览', '全部钱包买入分布', '疑似机器人买入分布', '数据与方法'])
            self.assertEqual(len(douyin['image_paths']), 4)
            with patch.object(state, 'ROOT', root):
                path = state.record(root/'updown_2026-09-30_publish.json', 'x', 'failed')
            self.assertEqual(json.loads(path.read_text())['image_paths'], manifest['platforms']['x']['image_paths'])

    def test_bad_scope_boundaries_totals_and_denominators_rejected(self):
        changes = [lambda d: d.update(market_scope='single_series'),
                   lambda d: d.update(bot_scope='all_participation'),
                   lambda d: d['populations']['all_wallets']['bins'][0].update(upper_inclusive=True),
                   lambda d: d['populations']['all_wallets'].update(shares_exact='1001'),
                   lambda d: d['populations']['suspected_bots']['bins'][0].update(notional_share_of_market_percent='1.00'),
                   lambda d: d['validation'].update(unresolved_groups=1)]
        for change in changes:
            data = distribution_fixture()
            change(data)
            with self.assertRaises(ValueError):
                assets.validate_distribution(data, '2026-09-30')

    def test_missing_stale_or_mismatched_reference_fails_before_cards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.source(root)
            for field, wrong in [('json_sha256', 'bad'), ('image_file', '../other.png'), ('date_utc', '2026-09-29')]:
                changed = copy.deepcopy(payload)
                changed['trade_distribution'][field] = wrong
                source.write_text(json.dumps(changed))
                with patch('polymarket_updown_douyin.write_cards') as write_cards:
                    with self.assertRaises(ValueError):
                        captions.build_manifest(source)
                    write_cards.assert_not_called()

    def test_state_rejects_modified_distribution_image_and_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.source(root)
            captions.write_sidecars(source)
            manifest = root/'updown_2026-09-30_publish.json'
            image = root/payload['trade_distribution']['image_file']
            original = image.read_bytes()
            Image.new('RGB', (20, 20), 'red').save(image)
            with patch.object(state, 'ROOT', root):
                with self.assertRaises(ValueError):
                    state.record(manifest, 'x', 'failed')
                image.write_bytes(original)
                (root/payload['trade_distribution']['json_file']).write_text('{}')
                with self.assertRaises(ValueError):
                    state.record(manifest, 'douyin', 'failed')

    def test_state_rejects_reordered_english_attachments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _, _ = self.source(root)
            result = captions.write_sidecars(source)
            item = result['platforms']['x']
            item['image_paths'].reverse()
            item['image_sha256s'].reverse()
            manifest = root/'updown_2026-09-30_publish.json'
            manifest.write_text(json.dumps(result))
            with patch.object(state, 'ROOT', root):
                with self.assertRaises(ValueError):
                    state.record(manifest, 'x', 'failed')


if __name__ == '__main__':
    unittest.main()
