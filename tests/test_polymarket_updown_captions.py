import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_captions as captions

class CaptionTests(unittest.TestCase):
    def sample(self):
        return {'date_basis':'market_end_utc','date_utc':'2026-09-30',
                'current':{'volume_basis':'taker_only','volume_micro_usdc':123456789,'wallet_volume_micro_usdc':246913578,'unique_wallets':3,'suspected_bot_wallets':1,'suspected_bot_volume_micro_usdc':23456789},
                'previous':{'volume_micro_usdc':100000000,'unique_wallets':4},
                'bot_rule':{'max_mean_interval_seconds':60,'min_span_minutes':90}}

    def test_exact_amount_comparison_and_zero(self):
        p=self.sample()
        _,text=captions.render_zh(p)
        self.assertIn('123.46 USDC（较前日 +23.46%）',text)
        self.assertIn('3 个（较前日 -25.00%）',text)
        p['previous']=None
        self.assertIn('暂无可比前日数据',captions.render_zh(p)[1])
        p['previous']={'volume_micro_usdc':0,'unique_wallets':0}
        p['current']['volume_micro_usdc']=0
        p['current']['wallet_volume_micro_usdc']=0
        self.assertIn('前日基数为0',captions.render_zh(p)[1])
        self.assertIn('钱包成交额为0',captions.render_zh(p)[1])
        self.assertEqual(captions.percent(-1,10000000,signed=True),'0.00%')

    def test_market_volume_missing_and_legacy_wallet_caption(self):
        p=self.sample()
        p['current']['volume_micro_usdc']=None
        text=captions.render_zh(p)[1]
        self.assertIn('市场成交额：N/A USDC',text)
        self.assertIn('单边市场成交额数据不完整',text)
        self.assertIn('占钱包成交额 9.50%',text)
        self.assertIn('较前日 -25.00%',text)
        p=self.sample()
        del p['current']['volume_basis']
        del p['current']['wallet_volume_micro_usdc']
        text=captions.render_zh(p)[1]
        self.assertIn('钱包交易量：123.46 USDC',text)
        self.assertNotIn('市场成交额：',text)
        self.assertIn('占钱包成交额 19.00%',text)

    def test_calendar_and_scope(self):
        p=self.sample(); del p['date_utc']
        p['current'].update(start_date_utc='2024-02-01',end_date_utc_exclusive='2024-03-01')
        self.assertEqual(captions.period_info(p),('monthly','2024-02','2024-02-01','2024-02-29'))
        p['current'].update(start_date_utc='2025-12-29',end_date_utc_exclusive='2026-01-05')
        self.assertEqual(captions.period_info(p)[1],'2025-12-29_2026-01-04')
        p['scope_changes']=[{'series_slug':'zec-up-or-down-5m','first_end_time':'2026-08-04T21:40:00Z'}]
        p['coverage_adjustment']={'comparison_month':'2026-08','excluded_unavailable_slots':[{}]*214}
        text=captions.render_zh(p)[1]
        self.assertIn('去重交易钱包：3 个',text)
        self.assertIn('ZEC 于 2026-08 加入',text)
        self.assertIn('排除 214 个',text)

    def test_sidecars_preserve_sources_and_reject_oversize(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'updown_2026-09-30.json'
            source.write_text(json.dumps(self.sample()))
            Image.new('RGB',(20,20)).save(source.with_suffix('.png'))
            tweet=root/'updown_2026-09-30_tweet.md'; tweet.write_text('Original English\n')
            paths=[source,source.with_suffix('.png'),tweet]
            before=[p.read_bytes() for p in paths]
            result=captions.write_sidecars(source)
            self.assertEqual(before,[p.read_bytes() for p in paths])
            self.assertEqual(result['platforms']['tiktok']['body'],'Original English\n')
            self.assertEqual(result['platforms']['x']['body'],'Original English\n')
            self.assertLessEqual(captions.utf16_length(result['platforms']['douyin']['title']),20)
            tweet.write_text('x'*4001)
            with self.assertRaises(ValueError): captions.write_sidecars(source)
            p=self.sample(); p['date_utc']='2026-09-29'; source.write_text(json.dumps(p))
            with self.assertRaises(ValueError): captions.build_manifest(source)

if __name__=='__main__': unittest.main()
