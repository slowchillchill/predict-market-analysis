import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import polymarket_updown_distribution_assets as assets
import polymarket_publish_state as state
from polymarket_updown_bot_registry import RULE
from polymarket_updown_price_distribution import statistics_stem
from test_polymarket_updown_distribution_publishing import distribution_fixture


def period_distribution_fixture(kind='weekly'):
    """Exact synthetic period, including the same complete audits as the producer."""
    start, end = ((date(2026, 11, 2), date(2026, 11, 9)) if kind == 'weekly'
                  else (date(2026, 10, 1), date(2026, 11, 1)))
    days = [str(start+timedelta(days=i)) for i in range((end-start).days)]
    data = distribution_fixture()
    data.pop('date_utc')
    data.update(schema_version=3, kind=kind,
                target_utc=f'{start}_{end-timedelta(days=1)}' if kind == 'weekly' else f'{start:%Y-%m}',
                start_date_utc=str(start), end_date_utc_exclusive=str(end), covered_dates=days,
                total_markets=len(days), all_wallet_count=3,
                reconstruction_method='existing_db_first_row_anchor_plus_counterparty_rows_v1',
                bot_rule=dict(RULE), bot_history={})
    audit = dict(transaction_groups=100, validated_groups=100, reconstructed_trades=100,
                 buy_participating_trades=100, excluded_sell_only_trades=0, unresolved_groups=0,
                 exact_value_groups=100, tolerated_value_groups=0, price_rounding_groups=0,
                 source_row_count=200, value_tolerance_usdc='0.000001',
                 roundtrip_min_decimal_places=10, max_absolute_value_difference_usdc='0')
    data['daily_audits'] = [dict(date_utc=day, total_markets=1, all_wallet_count=3,
        suspected_bot_wallets=1, source_fingerprint_sha256='0'*64, validation=copy.deepcopy(audit)) for day in days]
    data['validation'] = {key: value*len(days) if key in assets._AUDIT_SUM_KEYS else value
                          for key, value in audit.items()}
    for population in data['populations'].values():
        for row in [population, *population['bins']]:
            row['trade_count'] *= len(days)
            for key in ('shares_exact', 'notional_usdc_exact'):
                row[key] = str(Decimal(row[key])*len(days))
    return data


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PeriodPublishingAssetsTests(unittest.TestCase):
    def write_source(self, root, kind='weekly'):
        data = period_distribution_fixture(kind)
        source = root/f'updown_{kind}_{data["target_utc"]}.json'
        json_path = root/(statistics_stem(data)+'.json')
        image_path = json_path.with_suffix('.png')
        json_path.write_text(json.dumps(data))
        Image.new('RGB', (20, 20), 'blue').save(image_path)
        ref_keys = ('kind', 'target_utc', 'start_date_utc', 'end_date_utc_exclusive',
                    'market_scope', 'series_count', 'count_unit', 'price_basis', 'notional_basis')
        reference = {key: data[key] for key in ref_keys}
        reference.update(json_file=json_path.name, json_sha256=_sha(json_path),
                         image_file=image_path.name, image_sha256=_sha(image_path))
        payload = dict(date_basis='market_end_utc', bot_rule=dict(RULE), trade_distribution=reference,
                       current=dict(start_date_utc=data['start_date_utc'],
                                    end_date_utc_exclusive=data['end_date_utc_exclusive'],
                                    total_markets=data['total_markets'], unique_wallets=3,
                                    suspected_bot_wallets=1))
        source.write_text(json.dumps(payload))
        Image.new('RGB', (20, 20), 'white').save(source.with_suffix('.png'))
        source.with_name(source.stem+'_tweet.md').write_text('Period BUY distribution\n')
        return source, payload, data

    def manifest(self, source, payload):
        identity = assets.report_identity(payload)
        kind = identity['kind']
        image = source.with_suffix('.png')
        english = source.with_name(source.stem+'_tweet.md')
        english_images = [str(image), str(source.parent/payload['trade_distribution']['image_file'])]
        cards = []
        for i in range(1, 7):
            path = source.with_name(source.stem+f'_douyin_{i:02}.png')
            Image.new('RGB', (20, 20), (i, i, i)).save(path)
            cards.append(str(path))
        platforms = {}
        for platform in ('x', 'linkedin', 'tiktok', 'douyin'):
            paths = cards if platform == 'douyin' else english_images
            item = dict(title='', body='Period BUY distribution\n', image_paths=paths,
                        image_sha256s=[_sha(path) for path in paths],
                        asset_version=f'research_cards_{kind}_buy_distribution_v2' if platform == 'douyin'
                                      else f'{kind}_buy_distribution_v2')
            self.rehash(item)
            platforms[platform] = item
        manifest = dict(schema_version=1, kind=kind, target_utc=identity['target_utc'],
                        image_path=str(image), image_sha256=_sha(image),
                        source_path=str(source), source_sha256=_sha(source),
                        english_path=str(english), english_sha256=_sha(english), platforms=platforms)
        path = source.with_name(source.stem+'_publish.json')
        path.write_text(json.dumps(manifest))
        return path, manifest

    def rehash(self, item):
        item['content_sha256'] = hashlib.sha256(json.dumps(
            {key: item[key] for key in ('title', 'body', 'image_sha256s')},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def test_load_week_and_month_validate_all_three_period_identities(self):
        for kind in ('weekly', 'monthly'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                source, payload, data = self.write_source(Path(temp), kind)
                self.assertEqual(assets.load_distribution(source, payload), data)
                self.assertEqual(assets.report_identity(payload)['kind'], kind)
                for key, wrong in [('target_utc', 'other'), ('kind', 'daily'),
                                   ('start_date_utc', '2026-01-01'),
                                   ('end_date_utc_exclusive', '2027-01-01')]:
                    changed = copy.deepcopy(payload)
                    changed['trade_distribution'][key] = wrong
                    with self.assertRaises(ValueError):
                        assets.load_distribution(source, changed)

    def test_schema_three_rejects_incomplete_audits_bad_sums_and_future_rule(self):
        changes = [lambda d: d['covered_dates'].pop(),
                   lambda d: d['daily_audits'][0].update(date_utc='2026-01-01'),
                   lambda d: d['daily_audits'][0].update(source_fingerprint_sha256='bad'),
                   lambda d: d['daily_audits'][0]['validation'].update(unresolved_groups=1),
                   lambda d: d['validation'].update(source_row_count=1),
                   lambda d: d.update(total_markets=1),
                   lambda d: d.update(all_wallet_count=1000),
                   lambda d: d['bot_rule'].update(future_labels_applied=True),
                   lambda d: d['bot_rule'].update(classification='daily'),
                   lambda d: d.update(reconstruction_method='transaction_hash_only')]
        for mutate in changes:
            data = period_distribution_fixture()
            mutate(data)
            with self.assertRaises(ValueError):
                assets.validate_distribution(data)

    def test_overview_population_or_rule_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source, payload, _ = self.write_source(Path(temp))
            for key in ('unique_wallets', 'total_markets', 'suspected_bot_wallets'):
                changed = copy.deepcopy(payload)
                changed['current'][key] += 1
                with self.assertRaises(ValueError):
                    assets.load_distribution(source, changed)
            changed = copy.deepcopy(payload)
            changed['bot_rule']['classification'] = 'daily'
            with self.assertRaises(ValueError):
                assets.load_distribution(source, changed)

    def test_wrong_period_payload_missing_or_changed_distribution_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.write_source(root)
            changed = copy.deepcopy(payload)
            changed['current']['start_date_utc'] = '2026-11-09'
            changed['current']['end_date_utc_exclusive'] = '2026-11-16'
            with self.assertRaises(ValueError):
                assets.load_distribution(source, changed)
            path = root/payload['trade_distribution']['json_file']
            path.write_text('{}')
            with self.assertRaises(ValueError):
                assets.load_distribution(source, payload)

    def test_state_accepts_two_english_images_and_six_chinese_cards(self):
        for kind in ('weekly', 'monthly'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, payload, _ = self.write_source(root, kind)
                path, manifest = self.manifest(source, payload)
                with patch.object(state, 'ROOT', root):
                    for platform in ('x', 'linkedin', 'tiktok', 'douyin'):
                        recorded = state.record(path, platform, 'failed')
                        result = json.loads(recorded.read_text())
                        self.assertEqual(result['image_paths'], manifest['platforms'][platform]['image_paths'])

    def test_state_rejects_wrong_version_missing_card_and_recalculated_wrong_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.write_source(root)
            path, original = self.manifest(source, payload)
            for platform, mutation in [
                ('x', lambda item: item.update(asset_version='daily_buy_distribution_v2')),
                ('douyin', lambda item: item.update(asset_version='research_cards_buy_distribution_v2')),
                ('douyin', lambda item: (item['image_paths'].pop(), item['image_sha256s'].pop())),
                ('x', lambda item: (item['image_paths'].reverse(), item['image_sha256s'].reverse())),
                ('douyin', lambda item: (item['image_paths'].reverse(), item['image_sha256s'].reverse())),
            ]:
                manifest = copy.deepcopy(original)
                item = manifest['platforms'][platform]
                mutation(item)
                self.rehash(item)
                path.write_text(json.dumps(manifest))
                with patch.object(state, 'ROOT', root), self.assertRaises(ValueError):
                    state.record(path, platform, 'failed')

    def test_state_rejects_wrong_target_changed_body_or_image_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.write_source(root)
            path, original = self.manifest(source, payload)
            wrong_target = copy.deepcopy(original)
            wrong_target['target_utc'] = 'other'
            path.write_text(json.dumps(wrong_target))
            with patch.object(state, 'ROOT', root), self.assertRaises(ValueError):
                state.record(path, 'x', 'failed')
            changed = copy.deepcopy(original)
            changed['platforms']['douyin']['body'] = 'stale content hash'
            path.write_text(json.dumps(changed))
            with patch.object(state, 'ROOT', root), self.assertRaises(ValueError):
                state.record(path, 'douyin', 'failed')
            path.write_text(json.dumps(original))
            Image.new('RGB', (20, 20), 'red').save(original['platforms']['douyin']['image_paths'][4])
            with patch.object(state, 'ROOT', root), self.assertRaises(ValueError):
                state.record(path, 'douyin', 'failed')
            self.assertFalse((root/'outputs/publish_state').exists())

    def test_state_rejects_foreign_overview_even_with_updated_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, payload, _ = self.write_source(root)
            path, manifest = self.manifest(source, payload)
            foreign = root/'updown_weekly_2026-11-09_2026-11-15.png'
            Image.new('RGB', (20, 20), 'green').save(foreign)
            manifest.update(image_path=str(foreign), image_sha256=_sha(foreign))
            manifest['platforms']['x']['image_paths'][0] = str(foreign)
            manifest['platforms']['x']['image_sha256s'][0] = _sha(foreign)
            self.rehash(manifest['platforms']['x'])
            path.write_text(json.dumps(manifest))
            with patch.object(state, 'ROOT', root), self.assertRaises(ValueError):
                state.record(path, 'x', 'failed')


if __name__ == '__main__':
    unittest.main()
