import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import polymarket_updown_captions as captions
import polymarket_updown_distribution_assets as assets
import polymarket_publish_state as state
import polymarket_updown_poster as poster
import polymarket_updown_yesterday as workflow
import test_polymarket_updown_poster_distribution as pipeline_fixtures
import test_polymarket_updown_distribution_publishing as publishing_fixtures
from test_polymarket_updown_buy_only_publication import bind


class DailyChartPipelineTests(unittest.TestCase):
    def test_legacy_and_approved_layout_versions_are_selected_by_source(self):
        for kind in ('daily', 'weekly', 'monthly'):
            self.assertEqual(assets.distribution_asset_versions({}, kind), (
                f'{kind}_buy_distribution_v2',
                'research_cards_buy_distribution_v2' if kind == 'daily'
                else f'research_cards_{kind}_buy_distribution_v2'))
        payload = {'trade_distribution': {'layout_version': assets.DAILY_CHART_LAYOUT_VERSION}}
        self.assertEqual(assets.distribution_asset_versions(payload, 'daily'), (
            'daily_buy_distribution_chart_v1', 'research_cards_daily_buy_distribution_chart_v1'))
        for kind in ('weekly', 'monthly'):
            with self.assertRaisesRegex(ValueError, 'layout version'):
                assets.distribution_asset_versions(payload, kind)
        with self.assertRaisesRegex(ValueError, 'layout version'):
            assets.distribution_asset_versions({'trade_distribution': {'layout_version': 'unknown'}}, 'daily')

    def test_default_yesterday_entry_generates_approved_complete_package(self):
        fixture = pipeline_fixtures.DailyPosterDistributionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.overview.current['wallets_meeting_daily_frequency_rule'] = 0
        before = fixture.path.read_bytes()
        with patch.object(workflow, 'datetime') as clock, \
                patch.object(workflow.daily, 'main', return_value=0) as collect, \
                patch.object(poster, 'load_data', return_value=fixture.overview), \
                contextlib.redirect_stdout(io.StringIO()):
            clock.now.return_value = datetime(2026, 11, 8, tzinfo=timezone.utc)
            self.assertEqual(workflow.main(['--db', str(fixture.path),
                                           '--output-dir', str(fixture.output)]), 0)
        collect.assert_called_once()
        args = collect.call_args.args[0]
        self.assertEqual(args[args.index('--start-date')+1], '2026-11-07')
        self.assertEqual(args[args.index('--end-date')+1], '2026-11-08')
        self.assertEqual(before, fixture.path.read_bytes())
        source = fixture.output / 'updown_2026-11-07.json'
        payload = json.loads(source.read_text())
        self.assertEqual(payload['trade_distribution']['layout_version'], assets.DAILY_CHART_LAYOUT_VERSION)
        distribution = assets.load_distribution(source, payload)
        assets.validate_buy_event_binding(payload, distribution)
        manifest = json.loads(source.with_name(source.stem+'_publish.json').read_text())
        self.assertEqual(len(list(fixture.output.iterdir())), 11)
        for platform in ('x', 'linkedin', 'tiktok'):
            item = manifest['platforms'][platform]
            self.assertEqual(item['asset_version'], 'daily_buy_distribution_chart_v1')
            self.assertEqual(len(item['image_paths']), 2)
        self.assertEqual(manifest['platforms']['douyin']['asset_version'],
                         'research_cards_daily_buy_distribution_chart_v1')
        self.assertEqual(len(manifest['platforms']['douyin']['image_paths']), 4)
        self.assertLessEqual(len(manifest['platforms']['x']['body']), 280)
        self.assertIn('Bot-buyer subset:', manifest['platforms']['x']['body'])
        self.assertIn('Notional=Up-equivalent price*shares', manifest['platforms']['x']['body'])
        with Image.open(fixture.output / payload['trade_distribution']['image_file']) as image:
            self.assertEqual(image.size, (1600, 2000))
            image.verify()

    def test_submission_checks_accept_new_versions_and_reject_stale_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, data = publishing_fixtures.DistributionPublishingTests().source(root)
            bind(payload, data)
            payload['trade_distribution']['layout_version'] = assets.DAILY_CHART_LAYOUT_VERSION
            source.write_text(json.dumps(payload))
            manifest = captions.write_sidecars(source)
            path = source.with_name(source.stem+'_publish.json')
            with patch.object(state, 'ROOT', root):
                for platform, legacy in [('x', 'daily_buy_distribution_v2'),
                                         ('douyin', 'research_cards_buy_distribution_v2')]:
                    current = manifest['platforms'][platform]['asset_version']
                    manifest['platforms'][platform]['asset_version'] = legacy
                    path.write_text(json.dumps(manifest))
                    with self.assertRaisesRegex(ValueError, 'asset version|image set'):
                        state.record(path, platform, 'failed')
                    manifest['platforms'][platform]['asset_version'] = current
                    path.write_text(json.dumps(manifest))
                    saved = state.record(path, platform, 'failed')
                    self.assertEqual(json.loads(saved.read_text())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
