import builtins
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import polymarket_publish_state as state


class TextPublishStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'updown_2026-11-10.json'
        self.bundle = {
            'schema_version': 1, 'status': 'complete', 'kind': 'daily',
            'target_utc': '2026-11-10', 'start_date_utc': '2026-11-10',
            'end_date_utc_exclusive': '2026-11-11',
            'payload': {'date_basis': 'market_end_utc'},
        }
        self.manifest = {
            'schema_version': 2, 'format': 'text', 'kind': 'daily',
            'target_utc': '2026-11-10', 'start_date_utc': '2026-11-10',
            'end_date_utc_exclusive': '2026-11-11', 'date_basis': 'market_end_utc',
            'source_path': str(self.source), 'platforms': {},
        }
        for platform, posts in [('x', ['1/2 First post', '2/2 Second post']),
                                ('linkedin', ['A vertical report\n\nMarket volume: 10.00 USDC'])]:
            paths = []
            for number, post in enumerate(posts, 1):
                path = self.root / f'{platform}_{number}.txt'
                path.write_text(post, encoding='utf-8')
                paths.append(str(path))
            item = {'language': 'en', 'title': '', 'body': posts[0],
                    'text_paths': paths, 'text_sha256s': [state.sha256(path) for path in paths]}
            if platform == 'x':
                item['posts'] = posts
            item['content_sha256'] = state.text_content_sha256(item, platform)
            self.manifest['platforms'][platform] = item
        self.manifest_path = self.root / 'updown_2026-11-10_publish.json'
        self.save()

    def save(self):
        self.source.write_text(json.dumps(self.bundle), encoding='utf-8')
        self.manifest['source_sha256'] = state.sha256(self.source)
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')

    def record(self, platform, status, **kwargs):
        with patch.object(state, 'ROOT', self.root):
            return state.record(self.manifest_path, platform, status, **kwargs)

    def test_text_submission_needs_no_images_music_or_pillow_and_records_all_thread_links(self):
        original_import = builtins.__import__
        def no_media_import(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.') or name == 'polymarket_updown_captions':
                raise AssertionError('Text publication imported a media dependency')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=no_media_import):
            with self.assertRaisesRegex(ValueError, 'verified-absent'):
                self.record('x', 'submitting')
            path = self.record('x', 'submitting', verified_absent=True)
            self.record('x', 'unknown', post_urls=['https://x.com/owner/status/1'])
            urls = ['https://x.com/owner/status/1', 'https://x.com/owner/status/2']
            self.record('x', 'published', post_urls=urls, evidence='Verified both posts')
            data = json.loads(path.read_text())
            self.assertEqual(data['post_urls'], urls)
            self.assertEqual(data['post_url'], urls[0])
            self.assertEqual(data['posts'], self.manifest['platforms']['x']['posts'])
            self.assertNotIn('image_sha256', data)
            self.assertNotIn('music', data)
            self.assertEqual([item['status'] for item in data['history']],
                             ['submitting', 'unknown', 'published'])
            self.record('linkedin', 'submitting', verified_absent=True)
            linkedin_path = self.record('linkedin', 'published',
                post_url='https://www.linkedin.com/feed/update/urn:li:activity:1',
                evidence='Verified public post')
            self.assertEqual(json.loads(linkedin_path.read_text())['format'], 'text')

    def test_x_published_requires_distinct_urls_for_every_post_and_evidence(self):
        for kwargs in [
            {'post_url': 'https://x.com/owner/status/1', 'evidence': 'one'},
            {'post_urls': ['https://x.com/owner/status/1'], 'evidence': 'one'},
            {'post_urls': ['same', 'same'], 'evidence': 'duplicate'},
            {'post_urls': ['one', 'two']},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.record('x', 'published', **kwargs)

    def test_same_identity_keeps_old_publication_guard_and_cannot_downgrade(self):
        directory = self.root / 'outputs/publish_state'
        directory.mkdir(parents=True)
        path = directory / 'daily_2026-11-10_x.json'
        old = {'status': 'published', 'content_sha256': 'old-image-package',
               'post_url': 'https://x.com/owner/status/old'}
        path.write_text(json.dumps(old))
        with self.assertRaisesRegex(ValueError, 'reconciled'):
            self.record('x', 'submitting', verified_absent=True)
        with self.assertRaisesRegex(ValueError, 'Never downgrade'):
            self.record('x', 'failed')
        self.assertEqual(json.loads(path.read_text()), old)

    def test_changed_source_text_post_order_and_period_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        for change in ('source', 'text', 'order', 'period'):
            with self.subTest(change=change):
                self.manifest = copy.deepcopy(original)
                for item in self.manifest['platforms'].values():
                    for path, post in zip(item['text_paths'], item.get('posts', [item['body']])):
                        Path(path).write_text(post)
                self.save()
                if change == 'source':
                    self.source.write_text('{}')
                elif change == 'text':
                    Path(self.manifest['platforms']['x']['text_paths'][0]).write_text('Changed')
                elif change == 'order':
                    self.manifest['platforms']['x']['text_paths'].reverse()
                    self.manifest['platforms']['x']['text_sha256s'].reverse()
                    self.manifest_path.write_text(json.dumps(self.manifest))
                else:
                    self.manifest['target_utc'] = '2026-11-09'
                    self.manifest_path.write_text(json.dumps(self.manifest))
                with self.assertRaises(ValueError):
                    self.record('x', 'submitting', verified_absent=True)

    def test_weekly_and_monthly_state_identity_matches_legacy_package_names(self):
        for kind, start, end, target in [
            ('weekly', '2026-11-02', '2026-11-09', '2026-11-02_2026-11-08'),
            ('monthly', '2026-11-01', '2026-12-01', '2026-11'),
        ]:
            with self.subTest(kind=kind):
                self.bundle.update(kind=kind, start_date_utc=start,
                                   end_date_utc_exclusive=end,
                                   target_utc=start if kind == 'weekly' else target)
                self.manifest.update(kind=kind, start_date_utc=start,
                                     end_date_utc_exclusive=end, target_utc=target)
                self.save()
                path = self.record('linkedin', 'submitting', verified_absent=True)
                self.assertEqual(path.name, f'{kind}_{target}_linkedin.json')

    def test_removed_platforms_are_not_accepted_by_text_packages(self):
        for platform in ('douyin', 'tiktok'):
            with self.subTest(platform=platform), self.assertRaisesRegex(ValueError, 'only X and LinkedIn'):
                self.record(platform, 'submitting', verified_absent=True)


if __name__ == '__main__':
    unittest.main()
