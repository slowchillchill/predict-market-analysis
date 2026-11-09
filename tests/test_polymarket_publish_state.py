import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_captions as captions
import polymarket_publish_state as state
import test_polymarket_updown_captions as fixtures

class PublishStateTests(unittest.TestCase):
    def test_no_duplicate_submission_or_downgrade_and_hash_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'updown_2026-09-30.json'
            source.write_text(json.dumps(fixtures.CaptionTests().sample()))
            Image.new('RGB',(20,20)).save(source.with_suffix('.png'))
            tweet=root/'updown_2026-09-30_tweet.md';tweet.write_text('English\n')
            captions.write_sidecars(source)
            manifest=root/'updown_2026-09-30_publish.json'
            with patch.object(state,'ROOT',root):
                with self.assertRaises(ValueError): state.record(manifest,'x','submitting')
                path=state.record(manifest,'x','submitting',verified_absent=True)
                with self.assertRaises(ValueError): state.record(manifest,'x','submitting',verified_absent=True)
                state.record(manifest,'x','pending_review',post_url='https://example.com/post')
                with self.assertRaises(ValueError): state.record(manifest,'x','submitting',verified_absent=True)
                with self.assertRaises(ValueError): state.record(manifest,'x','published')
                state.record(manifest,'x','published',post_url='https://example.com/post',evidence='test evidence')
                self.assertEqual(json.loads(path.read_text())['status'],'published')
                with self.assertRaises(ValueError): state.record(manifest,'x','failed')
                state.record(manifest,'linkedin','submitting',verified_absent=True)
                tweet.write_text('Changed source')
                with self.assertRaises(ValueError): state.record(manifest,'linkedin','pending_review')

    def test_douyin_fallback_is_explicit_and_tiktok_stays_strict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'updown_2026-09-30.json'
            source.write_text(json.dumps(fixtures.CaptionTests().sample()))
            Image.new('RGB', (20, 20)).save(source.with_suffix('.png'))
            (root/'updown_2026-09-30_tweet.md').write_text('English\n')
            captions.write_sidecars(source)
            manifest = root/'updown_2026-09-30_publish.json'
            music = {'title': 'Available song', 'artist': 'Artist', 'duration': '01:00'}
            with patch.object(state, 'ROOT', root):
                for invalid in (None, {}, music):
                    with self.assertRaises(ValueError):
                        state.record(manifest, 'douyin', 'submitting', music=invalid, verified_absent=True)
                music['selection_reason'] = 'configured_candidates_unavailable'
                with self.assertRaises(ValueError):
                    state.record(manifest, 'tiktok', 'submitting', music=music, verified_absent=True)
                path = state.record(manifest, 'douyin', 'submitting', music=music, verified_absent=True)
                self.assertEqual(json.loads(path.read_text())['music'], music)
                with self.assertRaises(ValueError):
                    state.record(manifest, 'douyin', 'submitting', music=music, verified_absent=True)

if __name__=='__main__':unittest.main()
