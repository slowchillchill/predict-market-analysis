import copy
from decimal import Decimal, ROUND_HALF_UP
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_douyin as douyin
import test_polymarket_updown_captions as fixtures


def distribution_fixture(scale=1):
    # Exact values from the inspected all-series 2026-11-07 reconstruction.
    populations = {
        'all_wallets': (
            [134868, 99360, 96126, 109691, 132383, 168425, 129834, 117268, 110756, 159114],
            ['3018838.743973', '1185105.76641', '1102867.112963', '1279716.744095',
             '1605110.820931', '2039993.502974', '1553419.853441', '1430152.049113',
             '1303903.433473', '3236347.136482'],
            ['80442.3757362992656067', '169990.4619531636671645', '271215.4733189419315711',
             '443458.8885988484515146', '718936.188497090978359', '1101127.0124502334740416',
             '999353.4955051492532786', '1063793.1092571392800498', '1100998.3519529565965902',
             '3138139.9986317388067914']),
        'suspected_bots': (
            [90050, 78177, 77074, 88593, 102007, 126156, 106260, 95837, 89158, 107787],
            ['1539682.961126', '915729.455837', '877801.668711', '1024133.28952',
             '1204200.914369', '1514272.604364', '1253238.458869', '1153339.659047',
             '1037022.090119', '1768725.438455'],
            ['55303.0717612059451608', '132183.6831160380256606', '215946.7809500119521285',
             '354666.9280069320835018', '538659.0428691115781258', '821331.1392190234827455',
             '806389.8151431598118418', '858053.0406691266155456', '875684.6080729903827182',
             '1699790.2561028117519533']),
    }
    data = dict(date_utc='2026-11-07', market_scope='all_crypto_updown_series', series_count=38,
                total_markets=3295, scope='buy_participation', bot_scope='buy_participation',
                suspected_bot_wallets=331, bot_rule=dict(max_mean_interval_seconds=60, min_span_minutes=90),
                populations={})
    market_amount = sum(map(Decimal, populations['all_wallets'][2]))*scale
    def share(numerator, denominator):
        return str((numerator*100/denominator).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    for name, (counts, shares, amounts) in populations.items():
        counts = [value*scale for value in counts]
        shares = [Decimal(value)*scale for value in shares]
        amounts = [Decimal(value)*scale for value in amounts]
        total_count = sum(counts)
        bins = [dict(lower_cents=i*10, upper_cents=(i+1)*10, trade_count=count,
                     trade_share_percent=share(Decimal(count), Decimal(total_count)),
                     shares_exact=str(quantity), notional_usdc_exact=str(amount),
                     notional_share_of_market_percent=share(amount, market_amount))
                for i, (count, quantity, amount) in enumerate(zip(counts, shares, amounts))]
        data['populations'][name] = dict(trade_count=total_count, shares_exact=str(sum(shares)),
            notional_usdc_exact=str(sum(amounts)),
            notional_share_of_market_percent=share(sum(amounts), market_amount), bins=bins)
    return data


class BuyDistributionCardsTests(unittest.TestCase):
    def payload(self):
        payload = fixtures.CaptionTests().sample()
        payload['date_utc'] = '2026-11-07'
        payload['current'].update(unique_wallets=10271, volume_micro_usdc=8624333260000,
                                  wallet_volume_micro_usdc=17248666520000,
                                  suspected_bot_wallets=331,
                                  suspected_bot_volume_micro_usdc=8700000000000)
        return payload

    def captured_cards(self, data):
        records = {}
        original = douyin.Card.text
        def capture(card, text, x, y, size=32, color=douyin.INK, bold=False):
            records.setdefault(id(card.image), []).append((text, x, y, size))
            return original(card, text, x, y, size, color, bold)
        with patch.object(douyin.Card, 'text', capture):
            images = douyin.render_cards(self.payload(), 'daily', '2026-11-07', '2026-11-07',
                                         distribution_data=data)
        return images, list(records.values())

    def test_six_metrics_ten_bands_totals_and_distinct_denominators(self):
        data = distribution_fixture()
        images, records = self.captured_cards(data)
        self.assertEqual([image.size for image in images], [(1080, 1440)]*4)
        for page, population in ((records[1], 'all_wallets'), (records[2], 'suspected_bots')):
            text = '\n'.join(entry[0] for entry in page)
            item = data['populations'][population]
            all_buy = data['populations']['all_wallets']
            self.assertIn('全BUY笔数 '+douyin.distribution_percent(douyin.share_percent(
                item['trade_count'], all_buy['trade_count'])), text)
            self.assertIn('全BUY份数 '+douyin.distribution_percent(douyin.share_percent(
                item['shares_exact'], all_buy['shares_exact'])), text)
            for heading in ('Trades', 'Trade %', 'Shares', 'Shares %', 'USDC', 'Notional %'):
                self.assertIn(heading, text)
            self.assertIn('38 个系列 · 3,295 个市场', text)
            self.assertIn('笔数%：本组总笔数为分母。', text)
            self.assertIn('份数%：全部 BUY 成交份数为分母。', text)
            self.assertIn('金额%：全部 BUY 的 Up 等价金额为分母。', text)
            for index, row in enumerate(data['populations'][population]['bins']):
                expected = [f'{index*10}–<{(index+1)*10}' if index<9 else '90–100',
                            f'{row["trade_count"]:,}', douyin.distribution_percent(row['trade_share_percent']),
                            douyin.exact_amount(row['shares_exact']),
                            douyin.distribution_percent(douyin.share_percent(row['shares_exact'], data['populations']['all_wallets']['shares_exact'])),
                            douyin.exact_amount(row['notional_usdc_exact']),
                            douyin.distribution_percent(row['notional_share_of_market_percent'])]
                rendered_row = [entry for entry in page if entry[2] == 682+index*42]
                self.assertEqual([entry[0] for entry in rendered_row], expected)
                self.assertTrue(all(entry[3]>=24 for entry in rendered_row))
        all_text = '\n'.join(entry[0] for entry in records[1])
        bot_text = '\n'.join(entry[0] for entry in records[2])
        for value in ('1,257,825', '17,755,455.16', '9,087,455.36', '100.00%'):
            self.assertIn(value, all_text)
        for value in ('961,099', '12,288,146.54', '6,358,008.37', '69.96%'):
            self.assertIn(value, bot_text)
        self.assertNotIn('占地址侧买入＋卖出金额', bot_text)
        self.assertNotIn('NOTIONAL MIX', all_text+bot_text)
        overview = '\n'.join(entry[0] for entry in records[0])
        self.assertIn('10,271', overview)
        self.assertIn('8,624,333.26', overview)

    def test_methods_cover_only_buy_reconstruction_scope_and_rounding(self):
        _, records = self.captured_cards(distribution_fixture())
        methods = ''.join(entry[0] for entry in records[3])
        for phrase in ('数据库全部加密 Up/Down 市场', 'UTC 市场结束日', '本地汇总记录与对手明细',
                       '任一买方为已标记地址', '每笔只计一次', '不是唯一订单数',
                       'Down 用 1−原价', 'Up 等价价×成交份数', '吃单方市场成交额不同',
                       '各图总笔数为分母', '份数%以全部 BUY 成交份数为分母',
                       '全部 BUY 事件 Up 等价金额为分母', '末档含100¢',
                       '总计取未舍入值', '间隔≤60秒', '跨度≥90分钟', '不能确认自动程序'):
            self.assertIn(phrase, methods)
        self.assertEqual(douyin.exact_amount('2.345'), '2.35')
        self.assertEqual(douyin.exact_amount('2.34499999999'), '2.34')
        self.assertEqual(douyin.exact_amount(None), 'N/A')

    def test_single_band_accepts_one_hundred_percent_at_full_digit_size(self):
        # Degenerate distributions are valid, including a full month in one band.
        data = distribution_fixture(31)
        for population in data['populations'].values():
            for index, row in enumerate(population['bins']):
                row.update(trade_count=population['trade_count'] if index == 2 else 0,
                    trade_share_percent='100.00' if index == 2 else '0.00',
                    shares_exact=population['shares_exact'] if index == 2 else '0',
                    notional_usdc_exact=population['notional_usdc_exact'] if index == 2 else '0',
                    notional_share_of_market_percent=(population['notional_share_of_market_percent']
                                                       if index == 2 else '0.00'))
        images, records = self.captured_cards(data)
        for page in records[1:3]:
            row = [entry for entry in page if entry[2] == 682+2*42]
            self.assertEqual(row[2][0], '100.00%')
            self.assertTrue(all(entry[3] >= 24 for entry in row))
            for text, x, _, size in row:
                self.assertGreaterEqual(x, 64)
                self.assertLessEqual(x+douyin.font(size).getlength(text), 1016)
        self.assertEqual(len(images), 4)
        # The same minimum percent-column width also leaves room for bold totals.
        card = douyin.Card('日报', '2026-11-07', 1, '比例边界')
        for bold in (False, True):
            douyin._right_text(card, '100.00%', 394, 682, size=24, left=292, bold=bold)

    def test_changed_bin_data_changes_only_its_distribution_card(self):
        data = distribution_fixture()
        original, _ = self.captured_cards(data)
        changed = copy.deepcopy(data)
        changed['populations']['suspected_bots']['bins'][0]['shares_exact'] = '1539682.965'
        changed['populations']['suspected_bots']['bins'][0]['notional_usdc_exact'] = '55303.075'
        after, _ = self.captured_cards(changed)
        self.assertEqual([before.tobytes() != now.tobytes() for before, now in zip(original, after)],
                         [False, False, True, False])

    def test_realistic_large_values_stay_inside_distinct_columns(self):
        # One observed day and a seven-times-larger stress fixture retain full digits.
        for scale in (1, 7):
            with self.subTest(scale=scale):
                images, records = self.captured_cards(distribution_fixture(scale))
                for page in records:
                    for text, x, y, size in page:
                        self.assertGreaterEqual(x, 64)
                        self.assertLessEqual(x+douyin.font(size).getlength(text), 1024)
                        if 682 <= y <= 1060:
                            self.assertGreaterEqual(size, 24)
                self.assertEqual(len(images), 4)
        with tempfile.TemporaryDirectory() as temp:
            paths = douyin.write_cards(self.payload(), 'daily', '2026-11-07', '2026-11-07',
                Path(temp), 'updown_2026-11-07', distribution_data=distribution_fixture())
            self.assertEqual(len(paths), 4)
            for index, path in enumerate(paths, 1):
                self.assertEqual(path.name, f'updown_2026-11-07_douyin_{index:02d}.png')
                with Image.open(path) as image:
                    self.assertEqual(image.size, (1080, 1440))
                    image.verify()

    def test_legacy_cards_unchanged_and_wrong_scope_rejected(self):
        payload = self.payload()
        legacy = douyin.render_cards(payload, 'daily', '2026-11-07', '2026-11-07')
        explicit_none = douyin.render_cards(payload, 'daily', '2026-11-07', '2026-11-07',
                                            distribution_data=None)
        self.assertEqual([image.tobytes() for image in legacy], [image.tobytes() for image in explicit_none])
        for kind in ('weekly', 'monthly'):
            start, end = ('2026-11-01', '2026-11-07') if kind == 'weekly' else ('2026-10-01', '2026-10-31')
            before = douyin.render_cards(payload, kind, start, end)
            after = douyin.render_cards(payload, kind, start, end, distribution_data=None)
            self.assertEqual([image.tobytes() for image in before], [image.tobytes() for image in after])
            with self.assertRaisesRegex(ValueError, 'period mismatch'):
                douyin.render_cards(payload, kind, start, end, distribution_data=distribution_fixture())
        for field, wrong in (('market_scope', 'single_series'), ('scope', 'all_participation'),
                             ('bot_scope', 'all_participation')):
            data = distribution_fixture(); data[field] = wrong
            with self.assertRaisesRegex(ValueError, 'all-crypto BUY'):
                douyin.render_cards(payload, 'daily', '2026-11-07', '2026-11-07', distribution_data=data)


if __name__ == '__main__':
    unittest.main()
