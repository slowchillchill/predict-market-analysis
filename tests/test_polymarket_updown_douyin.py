import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_captions as captions
import polymarket_updown_douyin as douyin
import polymarket_publish_state as state
import test_polymarket_updown_captions as fixtures


class DouyinCardsTests(unittest.TestCase):
    def weekly(self):
        p=fixtures.CaptionTests().sample()
        del p['date_utc']
        p['current'].update(start_date_utc='2026-10-05', end_date_utc_exclusive='2026-10-12',
                            unique_wallets=26033, volume_micro_usdc=71556399966739,
                            wallet_volume_micro_usdc=145689164415100,
                            suspected_bot_wallets=713, suspected_bot_volume_micro_usdc=79041080992151,
                            daily_volume_micro_usdc={f'2026-10-{i:02}':v for i,v in zip(range(5,12),
                                [11631778794624,10180903773848,9740846925656,10892891651305,
                                 10555454267721,9306504599715,9248019949489])})
        p['previous'].update(unique_wallets=25820,volume_micro_usdc=72056703330507,
                             wallet_volume_micro_usdc=144537915760000,
                             suspected_bot_volume_micro_usdc=77931244176000)
        return p

    def source_files(self, folder, payload=None):
        source=folder/'updown_weekly_2026-10-05_2026-10-11.json'
        source.write_text(json.dumps(payload or self.weekly()))
        Image.new('RGB',(20,20)).save(source.with_suffix('.png'))
        source.with_name(source.stem+'_tweet.md').write_text('Unchanged English\n')
        return source

    def test_insight_and_extrema_are_data_driven(self):
        p=self.weekly()
        title,body=captions.render_zh(p)
        self.assertTrue(body.startswith('地址数增加，成交额小幅下降。'))
        self.assertIn('71,556,399.97 USDC',body)
        self.assertIn('26,033 个',body)
        self.assertIn('54.25%',body)
        self.assertIn('同一地址跨日出现只计一次',body)
        self.assertIn('最高：10-05；最低：10-11',body)
        p['previous']['unique_wallets']=0
        self.assertNotIn('地址数增加',douyin.observation(p)[0])
        p=self.weekly(); p['current']['daily_volume_micro_usdc']['2026-10-07']=None
        note=douyin.extrema_note(douyin.series(p,'2026-10-05','2026-10-11'))
        self.assertIn('缺口',note); self.assertNotIn('最高',note)
        with self.assertRaises(ValueError):
            douyin.series(self.weekly(),'2026-10-06','2026-10-12')

    def test_ties_zero_and_rounded_stability(self):
        self.assertIn('并列',douyin.extrema_note([('2026-10-05',10),('2026-10-06',10),('2026-10-07',0)]))
        self.assertEqual(douyin.extrema_note([('2026-10-05',0),('2026-10-06',0)]),'所列日期成交额相同。')
        p=self.weekly();p['current']['unique_wallets']=p['previous']['unique_wallets']
        p['current']['volume_micro_usdc']=p['previous']['volume_micro_usdc']+1
        self.assertEqual(douyin.observation(p)[0],'地址数持平，成交额持平')

    def test_preview_keeps_existing_artifacts_and_other_platform_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=self.source_files(folder)
            old_caption=folder/(source.stem+'_caption_zh.md');old_caption.write_text('Already published caption')
            old_manifest=folder/(source.stem+'_publish.json');old_manifest.write_text('Already published manifest')
            paths=[source,source.with_suffix('.png'),folder/(source.stem+'_tweet.md'),old_caption,old_manifest]
            original=[p.read_bytes() for p in paths]
            result=captions.write_sidecars(source,output_dir=folder/'preview')
            self.assertEqual(original,[p.read_bytes() for p in paths])
            for platform in ('x','linkedin','tiktok'):
                item=result['platforms'][platform]
                self.assertEqual(item['body'],'Unchanged English\n')
                old_hash=hashlib.sha256(json.dumps({'title':item['title'],'body':item['body'],
                    'image_sha256':result['image_sha256']},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                self.assertEqual(item['content_sha256'],old_hash)
                self.assertNotIn('image_paths',item)
            dy=result['platforms']['douyin']
            self.assertEqual(len(dy['image_paths']),4)
            for path,digest in zip(dy['image_paths'],dy['image_sha256s']):
                self.assertEqual(captions.sha256(Path(path)),digest)
                with Image.open(path) as image:
                    self.assertEqual(image.size,(1080,1440));image.verify()
            self.assertLessEqual(captions.utf16_length(dy['body']),1000)

    def test_card_tampering_and_order_block_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=self.source_files(folder)
            result=captions.write_sidecars(source,output_dir=folder/'preview')
            manifest=folder/'preview'/(source.stem+'_publish.json')
            path=Path(result['platforms']['douyin']['image_paths'][2])
            path.write_bytes(b'corrupted image')
            with patch.object(state,'ROOT',folder):
                with self.assertRaisesRegex(ValueError,'card changed'):
                    state.record(manifest,'douyin','blocked')
                # Other platforms still validate only their original assets.
                state.record(manifest,'x','submitting',verified_absent=True)
            result=captions.write_sidecars(source,output_dir=folder/'preview')
            dy=result['platforms']['douyin']
            dy['image_paths'].reverse();dy['image_sha256s'].reverse()
            manifest.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError,'order or caption'):
                state.record(manifest,'douyin','blocked')

    def test_daily_missing_and_monthly_scope_render_without_fabrication(self):
        p=fixtures.CaptionTests().sample();p['current']['volume_micro_usdc']=None
        self.assertEqual(len(douyin.render_cards(p,'daily','2026-09-30','2026-09-30')),4)
        p=self.weekly();p['current'].update(start_date_utc='2024-02-01',end_date_utc_exclusive='2024-03-01')
        p['current']['daily_volume_micro_usdc']={f'2024-02-{i:02}':0 if i==5 else i*10**12 for i in range(1,30)}
        p['current']['daily_volume_micro_usdc']['2024-02-15']=None
        p['scope_changes']=[{'series_slug':'zec-updown-5m','first_end_time':'2024-01-05T00:00:00Z'}]
        p['coverage_adjustment']={'comparison_month':'2024-01','excluded_unavailable_slots':214}
        self.assertEqual(len(douyin.render_cards(p,'monthly','2024-02-01','2024-02-29')),4)


if __name__=='__main__': unittest.main()
