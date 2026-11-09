import copy
import json
import sys
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import polymarket_updown_captions as captions
import polymarket_updown_douyin as douyin
from polymarket_updown_distribution_assets import DAILY_CHART_LAYOUT_VERSION
import test_polymarket_updown_douyin_distribution as card_fixtures
import test_polymarket_updown_distribution_publishing as publishing_fixtures


class DailyChartCardsTests(unittest.TestCase):
    def payload(self):
        payload = card_fixtures.BuyDistributionCardsTests().payload()
        payload['trade_distribution'] = {'layout_version': DAILY_CHART_LAYOUT_VERSION}
        return payload

    def render(self, data, payload=None):
        records = {}
        original = douyin.Card.text
        def capture(card, text, x, y, size=32, color=douyin.INK, bold=False):
            records.setdefault(id(card.image), []).append((text, x, y, size))
            return original(card, text, x, y, size, color, bold)
        with patch.object(douyin.Card, 'text', capture):
            images = douyin.render_cards(payload or self.payload(), 'daily', '2026-11-07',
                                         '2026-11-07', distribution_data=data)
        return images, list(records.values())

    def test_daily_chart_keeps_exact_notional_and_all_buy_denominator_per_band(self):
        data = card_fixtures.distribution_fixture()
        images, records = self.render(data)
        self.assertEqual([image.size for image in images], [(1080, 1440)] * 4)
        for page, name in zip(records[1:3], ('all_wallets', 'suspected_bots')):
            text = '\n'.join(row[0] for row in page)
            population = data['populations'][name]
            all_buy = data['populations']['all_wallets']
            for heading in ('笔数分布', '笔数·本组%', '名义USDC·全BUY%'):
                self.assertIn(heading, text)
            self.assertNotIn('Shares %', text)
            self.assertIn('每档成交份数见数据 JSON；汇总份数见上方。', text)
            self.assertIn('全BUY份数 '+douyin.distribution_percent(douyin.share_percent(
                population['shares_exact'], all_buy['shares_exact'])), text)
            self.assertIn(douyin.exact_amount(population['shares_exact']), text)
            for index, row in enumerate(population['bins']):
                rendered = [entry for entry in page if entry[2] == 680+index*39]
                self.assertEqual([entry[0] for entry in rendered], [
                    f'{index*10}–<{(index+1)*10}' if index < 9 else '90–100',
                    f'{row["trade_count"]:,} · '+douyin.distribution_percent(douyin.share_percent(
                        row['trade_count'], population['trade_count'])),
                    douyin.exact_amount(row['notional_usdc_exact'])+' · '+douyin.distribution_percent(
                        douyin.share_percent(row['notional_usdc_exact'], all_buy['notional_usdc_exact'])),
                ])
                self.assertTrue(all(entry[3] >= 24 for entry in rendered))
                self.assertGreaterEqual(rendered[1][1], 435)
                self.assertGreaterEqual(rendered[2][1], 712)
                self.assertLessEqual(rendered[1][1]+douyin.font(24).getlength(rendered[1][0]), 690)
                self.assertLessEqual(rendered[2][1]+douyin.font(24).getlength(rendered[2][0]), 1016)

    def test_zero_buy_shows_na_for_unknown_ratios_and_never_divides_by_zero(self):
        data = card_fixtures.distribution_fixture()
        for population in data['populations'].values():
            population.update(trade_count=0, shares_exact='0', notional_usdc_exact='0',
                              notional_share_of_market_percent=None)
            for row in population['bins']:
                row.update(trade_count=0, trade_share_percent=None, shares_exact='0',
                           notional_usdc_exact='0', notional_share_of_market_percent=None)
        _, records = self.render(data)
        for page in records[1:3]:
            text = '\n'.join(row[0] for row in page)
            self.assertIn('全BUY笔数 N/A', text)
            self.assertIn('全BUY份数 N/A', text)
            self.assertIn('0 · N/A', text)
            self.assertIn('0.00 · N/A', text)
            self.assertNotIn('0 · 0.00%', text)
        self.assertEqual(douyin._distribution_chart_axis(data), (Decimal(15), (0, 5, 10, 15)))

    def test_one_hundred_percent_uses_one_shared_containing_axis_and_full_size_digits(self):
        with localcontext() as context:
            context.prec = 100
            data = card_fixtures.distribution_fixture(31)
        for population in data['populations'].values():
            for index, row in enumerate(population['bins']):
                row.update(trade_count=population['trade_count'] if index == 2 else 0,
                           shares_exact=population['shares_exact'] if index == 2 else '0',
                           notional_usdc_exact=population['notional_usdc_exact'] if index == 2 else '0')
        ceiling, ticks = douyin._distribution_chart_axis(data)
        self.assertEqual(ceiling, 100)
        self.assertEqual(ticks, (0, 25, 50, 75, 100))
        _, records = self.render(data)
        for page in records[1:3]:
            rendered = [entry for entry in page if entry[2] == 680+2*39]
            self.assertIn('100.00%', rendered[1][0])
            self.assertTrue(all(entry[3] == 24 for entry in rendered))
            self.assertIn('100%', '\n'.join(entry[0] for entry in page))

    def test_layout_marker_is_daily_only_and_legacy_cards_keep_table(self):
        self.assertTrue(douyin.daily_distribution_chart(self.payload(), 'daily'))
        for kind in ('weekly', 'monthly'):
            self.assertFalse(douyin.daily_distribution_chart(self.payload(), kind))
        legacy = self.payload()
        del legacy['trade_distribution']['layout_version']
        _, records = self.render(card_fixtures.distribution_fixture(), legacy)
        text = '\n'.join(row[0] for row in records[1])
        self.assertIn('Shares %', text)
        self.assertNotIn('笔数分布', text)

    def test_daily_chart_caption_explains_moved_shares_and_manifest_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source, payload, data = publishing_fixtures.DistributionPublishingTests().source(folder)
            payload['trade_distribution']['layout_version'] = DAILY_CHART_LAYOUT_VERSION
            source.write_text(json.dumps(payload))
            manifest = captions.write_sidecars(source)
            for platform in ('x', 'linkedin', 'tiktok'):
                self.assertEqual(manifest['platforms'][platform]['asset_version'],
                                 'daily_buy_distribution_chart_v1')
            item = manifest['platforms']['douyin']
            self.assertEqual(item['asset_version'], 'research_cards_daily_buy_distribution_chart_v1')
            self.assertEqual(len(item['image_paths']), 4)
            self.assertIn('金额%以全部 BUY 的 Up 等价名义金额为分母', item['body'])
            self.assertIn('每档份数见配套 JSON', item['body'])
            self.assertLessEqual(captions.utf16_length(item['body']), 1000)
            self.assertLessEqual(captions.utf16_length(item['title']), 20)
            payload['trade_distribution'].pop('layout_version')
            legacy_body = captions.render_zh(payload, distribution_data=data)[1]
            self.assertNotIn('每档份数见配套 JSON', legacy_body)
            self.assertIn('Shares % 分母为全部 BUY 成交份数', legacy_body)


if __name__ == '__main__':
    unittest.main()
