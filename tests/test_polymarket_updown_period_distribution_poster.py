import contextlib
import io
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date
from decimal import Decimal, localcontext
from itertools import combinations
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import polymarket_updown_bot_price_poster as poster
from test_polymarket_updown_bot_price_poster import assert_distribution_headers, example_data


def period_fixture(kind, *, factor=31):
    data = example_data(all_series=True)
    data.pop('date_utc')
    data.update(schema_version=3, kind=kind, target_utc='2026-10' if kind == 'monthly' else '2026-11-02_2026-11-08',
                start_date_utc='2026-10-01' if kind == 'monthly' else '2026-11-02',
                end_date_utc_exclusive='2026-11-01' if kind == 'monthly' else '2026-11-09',
                bot_rule=dict(classification='first_hit_day_onward'),
                total_markets=3295*factor, all_wallet_count=62341, suspected_bot_wallets=1730)
    with localcontext() as context:
        context.prec = 100
        for group in data['populations'].values():
            for row in [group, *group['bins']]:
                row['trade_count'] *= factor
                for key in ('shares_exact', 'notional_usdc_exact'):
                    row[key] = str(Decimal(row[key])*factor)
    return data


class PeriodDistributionPosterTests(unittest.TestCase):
    def assert_layout(self, image, boxes):
        self.assertEqual(image.size, (1600, 2000))
        self.assertEqual(image.mode, 'RGB')
        for box in boxes:
            left, top, right, bottom = box['bounds']
            self.assertGreaterEqual(left, 0, box['text'])
            self.assertGreaterEqual(top, 0, box['text'])
            self.assertLessEqual(right, image.width, box['text'])
            self.assertLessEqual(bottom, image.height, box['text'])
        for a, b in combinations(boxes, 2):
            al, at, ar, ab = a['bounds']
            bl, bt, br, bb = b['bounds']
            self.assertFalse(al < br and bl < ar and at < bb and bt < ab,
                             f"Overlapping text: {a['text']} / {b['text']}")

    def test_week_month_themes_keep_seven_columns_and_approved_notes(self):
        original_colors = (poster.style.WHITE, poster.style.MUTED, poster.style.BASE, poster.style.LINE)
        captures = []
        original_text = poster.style.PosterCanvas.text
        def capture(canvas, text, x, y, size, **kwargs):
            captures.append((text, kwargs.get('color'), size))
            return original_text(canvas, text, x, y, size, **kwargs)
        for kind, label in [('weekly', 'Nov 2–8, 2026 · UTC Market End Date'),
                            ('monthly', 'Oct 2026 · UTC Market End Date')]:
            captures.clear()
            with patch.object(poster.style.PosterCanvas, 'text', capture):
                image, boxes = poster.render_poster(period_fixture(kind))
            self.assert_layout(image, boxes)
            texts = [box['text'] for box in boxes]
            self.assertIn(label, texts)
            self.assertIn(kind.upper(), texts)
            normalized = ' '.join(texts)
            assert_distribution_headers(self, boxes)
            self.assertNotIn('NOTIONAL MIX', texts)
            self.assertEqual(texts.count('[0,10)'), 2)
            self.assertEqual(texts.count('[90,100]'), 2)
            self.assertIn('Only matches containing a BUY are included.', normalized)
            self.assertIn('for all BUY trades.', normalized)
            self.assertEqual(next(color for text, color, _ in captures if text == poster.copy.BRAND), poster.THEMES[kind].ink)
            self.assertEqual(next(color for text, color, _ in captures if text == poster.copy.GROUP_ALL), poster.THEMES[kind].ink)
            self.assertNotEqual(poster.THEMES[kind].ink, poster.style.WHITE)
        self.assertEqual(original_colors, (poster.style.WHITE, poster.style.MUTED, poster.style.BASE, poster.style.LINE))

    def test_monthly_large_numbers_remain_complete_and_readable(self):
        data = period_fixture('monthly', factor=100)
        image, boxes = poster.render_poster(data)
        self.assert_layout(image, boxes)
        texts = [box['text'] for box in boxes]
        for group in data['populations'].values():
            self.assertIn(poster.number_text(group['shares_exact']), texts)
            self.assertIn(poster.number_text(group['notional_usdc_exact']), texts)
            self.assertIn(f"{group['trade_count']:,}", texts)
        for box in boxes:
            top = box['bounds'][1]/poster.style.SCALE
            if 452 <= top <= 730 or 914 <= top <= 1192:
                # A 20-point table produces at least 18 physical pixels of ink.
                self.assertGreaterEqual(box['bounds'][3]-box['bounds'][1], 18, box['text'])

    def test_period_dates_are_complete_and_cross_year_label_is_explicit(self):
        data = period_fixture('weekly')
        data.update(start_date_utc='2025-12-29', end_date_utc_exclusive='2026-01-05')
        self.assertEqual(poster.period_label(data), ('weekly', 'Dec 29, 2025 – Jan 4, 2026 · UTC'))
        self.assert_layout(*poster.render_poster(data))
        data.update(start_date_utc='2026-11-03', end_date_utc_exclusive='2026-11-10')
        with self.assertRaises(ValueError): poster.render_poster(data)
        data = period_fixture('monthly')
        data.update(start_date_utc='2024-02-01', end_date_utc_exclusive='2024-03-01')
        self.assertEqual(poster.period_label(data), ('monthly', 'Feb 2024 · UTC'))
        data['end_date_utc_exclusive'] = '2024-02-29'
        with self.assertRaises(ValueError): poster.render_poster(data)

    def test_week_month_cli_use_read_only_period_loader(self):
        for option, value, kind, start, end in [
            ('--week-start', '2026-11-02', 'weekly', date(2026, 11, 2), date(2026, 11, 9)),
            ('--month', '2026-10', 'monthly', date(2026, 10, 1), date(2026, 11, 1)),
        ]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                db_path = root/'source.sqlite3'
                with sqlite3.connect(db_path) as db:
                    db.execute('CREATE TABLE sentinel(value TEXT)')
                before = db_path.read_bytes()
                data = period_fixture(kind)
                def load(db, actual_start, actual_end, *, kind, progress):
                    self.assertEqual((actual_start, actual_end), (start, end))
                    self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0], 1)
                    self.assertFalse(db.in_transaction)
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute("INSERT INTO sentinel VALUES ('bad')")
                    progress('fixture complete')
                    return data
                loader = Mock(side_effect=load)
                module = types.SimpleNamespace(load_period_data=loader)
                with patch.dict(sys.modules, {'polymarket_updown_period_distribution': module}), \
                        patch.object(poster, 'write_outputs', return_value={'png': root/'a.png', 'json': root/'a.json'}) as export, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = poster.main([option, value, '--db', str(db_path), '--output-dir', str(root/'preview')])
                self.assertEqual(result, 0)
                self.assertEqual(loader.call_args.kwargs['kind'], kind)
                export.assert_called_once_with(data, root/'preview')
                self.assertEqual(db_path.read_bytes(), before)

    def test_invalid_cli_periods_fail_before_opening_database(self):
        invalid = [('--week-start', '2026-11-03'), ('--month', '2026-1'), ('--month', '2026-13')]
        for option, value in invalid:
            with self.subTest(value=value), patch.object(poster.sqlite3, 'connect') as connect, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                poster.main([option, value])
            connect.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            poster.main(['--month', '2026-10', '--series', 'btc-up-or-down-5m'])


if __name__ == '__main__':
    unittest.main()
