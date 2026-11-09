import copy
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import localcontext
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import polymarket_updown_captions as captions
import polymarket_updown_douyin as douyin
from test_polymarket_updown_douyin_distribution import distribution_fixture as large_distribution_fixture
from test_polymarket_updown_period_publishing_assets import period_distribution_fixture


def period_payload(data):
    start = date.fromisoformat(data['start_date_utc'])
    end = date.fromisoformat(data['end_date_utc_exclusive'])
    amount = 311592304995240 if data['kind'] == 'monthly' else 72097851622614
    day_count = (end-start).days
    daily = {str(start+timedelta(days=i)): amount//day_count for i in range(day_count)}
    daily[str(start)] += amount-sum(daily.values())
    current = dict(start_date_utc=str(start), end_date_utc_exclusive=str(end),
                   total_markets=data['total_markets'], unique_wallets=data['all_wallet_count'],
                   suspected_bot_wallets=data['suspected_bot_wallets'], volume_basis='taker_only',
                   volume_micro_usdc=amount, wallet_volume_micro_usdc=amount*2,
                   suspected_bot_volume_micro_usdc=amount, daily_volume_micro_usdc=daily)
    previous = dict(volume_micro_usdc=amount-1000000,
                    unique_wallets=max(1, current['unique_wallets']-1),
                    wallet_volume_micro_usdc=amount*2-2000000,
                    suspected_bot_volume_micro_usdc=amount-1000000)
    return dict(date_basis='market_end_utc', current=current, previous=previous,
                bot_rule=data['bot_rule'],
                scope_changes=[dict(series_slug='zec-up-or-down-5m', first_end_time='2026-08-04T21:40:00Z')],
                coverage_adjustment=dict(comparison_month='2026-08', excluded_unavailable_slots=214))


class PeriodCaptionCardsTests(unittest.TestCase):
    def data(self, kind):
        return period_distribution_fixture(kind)

    def captured_cards(self, payload, data):
        start = data['start_date_utc']
        end = str(date.fromisoformat(data['end_date_utc_exclusive'])-timedelta(days=1))
        records = {}
        original = douyin.Card.text
        def capture(card, text, x, y, size=32, color=douyin.INK, bold=False):
            records.setdefault(id(card.image), []).append((text, x, y, size))
            return original(card, text, x, y, size, color, bold)
        with patch.object(douyin.Card, 'text', capture):
            images = douyin.render_cards(payload, data['kind'], start, end, distribution_data=data)
        return images, list(records.values())

    def test_period_body_contains_buy_metrics_comparisons_scope_and_retained_labels(self):
        for kind, comparison in [('weekly', '较前周'), ('monthly', '较前月')]:
            data = self.data(kind)
            payload = period_payload(data)
            title, body = captions.render_zh(payload, distribution_data=data)
            for text in ('全部钱包买入', '疑似机器人买方参与成交', 'Trades', 'Shares',
                         'Up 等价名义金额', 'Trade % 分母为本图总成交笔数',
                         'Shares % 分母为全部 BUY 成交份数',
                         'Notional % 分母为全部 BUY 事件 Up 等价金额', comparison,
                         '首次命中当日及以后持续归类，不使用未来标签回算',
                         'ZEC 于 2026-08 加入', '排除 214 个不可用市场时段'):
                self.assertIn(text, body)
            self.assertIn(data['start_date_utc'], body)
            self.assertLessEqual(captions.utf16_length(title), 20)
            self.assertLessEqual(captions.utf16_length(body), 1000)
            self.assertNotIn('金额仅计入被标记当日', body)
            self.assertIn(f'历史首次命中后持续归类，本期活跃疑似机器人地址：{data["suspected_bot_wallets"]:,}个。', body)
            # A stray daily field cannot be presented as a weekly/monthly count.
            payload['current']['wallets_meeting_daily_frequency_rule'] = 334
            self.assertNotIn('当天符合上述规则', captions.render_zh(payload, distribution_data=data)[1])
            payload['previous'] = None
            self.assertIn('暂无可比', captions.render_zh(payload, distribution_data=data)[1])

    def test_six_cards_preserve_trend_and_bind_buy_event_panel(self):
        for kind in ('weekly', 'monthly'):
            data = self.data(kind)
            images, records = self.captured_cards(period_payload(data), data)
            self.assertEqual([im.size for im in images], [(1080, 1440)]*6)
            for index, page in enumerate(records, 1):
                self.assertIn((f'{index:02d} / 06', 880, 51, 25), page)
            texts = ['\n'.join(row[0] for row in page) for page in records]
            self.assertIn('成交额如何分布', texts[1])
            self.assertIn('每日市场成交额', texts[1])
            self.assertIn('高频地址的统计边界', texts[2])
            self.assertIn('Up 等价金额参与覆盖率', texts[2])
            self.assertNotIn('买入＋卖出', texts[2])
            self.assertIn('不是钱包自身支出', texts[2])
            self.assertIn('全部钱包买入分布', texts[3])
            self.assertIn('疑似机器人买入分布', texts[4])
            for text in texts[3:5]:
                for heading in ('Trades', 'Trade %', 'Shares', 'Shares %', 'USDC', 'Notional %'):
                    self.assertIn(heading, text)
            for phrase in ('首次命中当日及以后持续归类', '不使用未来标签回算', '范围变化：ZEC', '排除 214 个不可用市场时段'):
                self.assertIn(phrase, texts[5].replace('\n', ''))
            self.assertNotIn('NOTIONAL MIX', '\n'.join(texts))

    def test_month_sized_full_numbers_fit_and_rendering_rejects_wrong_period(self):
        with localcontext() as context:
            context.prec = 100
            data = large_distribution_fixture(31)
        data.update(kind='monthly', start_date_utc='2026-10-01', end_date_utc_exclusive='2026-11-01',
                    all_wallet_count=62341, total_markets=102145)
        data['bot_rule']['classification'] = 'first_hit_day_onward'
        payload = period_payload(data)
        images, records = self.captured_cards(payload, data)
        self.assertEqual(len(images), 6)
        for page in records:
            for text, x, y, size in page:
                self.assertGreaterEqual(x, 64)
                self.assertLessEqual(x+douyin.font(size).getlength(text), 1024)
                if 682 <= y <= 1060:
                    self.assertGreaterEqual(size, 24)
        bad = copy.deepcopy(data)
        bad['start_date_utc'] = '2026-09-01'
        with self.assertRaisesRegex(ValueError, 'period mismatch'):
            douyin.render_cards(payload, 'monthly', '2026-10-01', '2026-10-31', distribution_data=bad)

    def test_manifest_period_versions_order_and_source_preservation(self):
        for kind in ('weekly', 'monthly'):
            data = self.data(kind)
            with tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                payload = period_payload(data)
                stem = 'updown_'+kind+'_'+data['target_utc']
                source = folder/(stem+'.json')
                dist_stem = 'updown_buy_trade_distribution_all_series_'+kind+'_'+data['target_utc']
                dist_json = folder/(dist_stem+'.json')
                dist_image = folder/(dist_stem+'.png')
                dist_json.write_text(json.dumps(data))
                Image.new('RGB', (20, 20), 'green').save(dist_image)
                payload['trade_distribution'] = {key:data[key] for key in
                    ('kind','target_utc','start_date_utc','end_date_utc_exclusive','market_scope','series_count',
                     'count_unit','price_basis','notional_basis')}
                payload['trade_distribution'].update(json_file=dist_json.name, json_sha256=captions.sha256(dist_json),
                    image_file=dist_image.name, image_sha256=captions.sha256(dist_image))
                source.write_text(json.dumps(payload))
                Image.new('RGB', (20, 20)).save(source.with_suffix('.png'))
                source.with_name(stem+'_tweet.md').write_text('Current period BUY distribution\n')
                before = {path:path.read_bytes() for path in folder.iterdir()}
                manifest = captions.write_sidecars(source, output_dir=folder/'preview')
                self.assertTrue(all(path.read_bytes() == value for path,value in before.items()))
                for name in ('x', 'linkedin', 'tiktok'):
                    self.assertEqual(manifest['platforms'][name]['asset_version'], kind+'_buy_distribution_v2')
                    self.assertEqual(len(manifest['platforms'][name]['image_paths']), 2)
                    self.assertEqual(manifest['platforms'][name]['image_purposes'][0], kind.title()+' overview')
                item = manifest['platforms']['douyin']
                self.assertEqual(item['asset_version'], 'research_cards_'+kind+'_buy_distribution_v2')
                self.assertEqual(item['image_purposes'], ['本期总览', '每日市场成交额趋势', '高频地址解释',
                                                         '全部钱包买入分布', '疑似机器人买入分布', '数据与方法'])
                self.assertEqual([Path(path).name for path in item['image_paths']],
                                 [stem+f'_douyin_{i:02d}.png' for i in range(1,7)])
                self.assertEqual(item['image_sha256s'], [captions.sha256(Path(path)) for path in item['image_paths']])


if __name__ == '__main__':
    unittest.main()
