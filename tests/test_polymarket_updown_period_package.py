import contextlib
import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import polymarket_updown_weekly_poster as weekly
import polymarket_updown_monthly_poster as monthly
import polymarket_updown_period_package as package
from polymarket_updown_period_distribution import load_period_data


class PeriodPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, kind):
        start = date(2026,11,2) if kind == 'weekly' else date(2026,10,1)
        end = start+timedelta(days=7) if kind == 'weekly' else date(2026,11,1)
        source = self.root/(kind+'.sqlite3')
        db = weekly.daily.daily.connect_database(source)
        db.executescript("DROP VIEW updown_target_series; CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) AS VALUES ('btc',1,NULL);")
        with db:
            for offset in range((end-start).days):
                day = start+timedelta(days=offset)
                slug = str(day)
                db.execute('INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) VALUES (?,?,?,?,?)',
                           (slug,str(day-timedelta(days=1)),slug,'btc',f'{day}T00:00:00Z'))
                db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')",(slug,))
                for index,(wallet,side) in enumerate([('buyer','BUY'),('counterparty','SELL')]):
                    db.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (slug,1,index,wallet,1,1700000000,side,'Up',0,'token','.25','4',1000000))
        labels = {'buyer':str(start)}
        stats = load_period_data(db,start,end,kind=kind,bot_first_seen=labels)
        current = monthly.period.load_period(db,start,end,bot_first_seen=labels)
        db.close()
        return source,start,end,labels,stats,current

    def args(self, kind, source, start, output):
        return (['--week-start',str(start)] if kind=='weekly' else ['--month',start.strftime('%Y-%m')])+['--db',str(source),'--output-dir',str(output)]

    def test_default_main_creates_complete_package_with_final_paths_and_shared_data(self):
        for kind,module in [('weekly',weekly),('monthly',monthly)]:
            with self.subTest(kind=kind):
                source,start,end,labels,stats,current = self.fixture(kind)
                original = source.read_bytes()
                output = self.root/(kind+'_output')
                with patch('polymarket_updown_bot_registry.load_first_seen',return_value=labels), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(self.args(kind,source,start,output)),0)
                self.assertEqual(source.read_bytes(),original)
                stem = ('updown_weekly_'+str(start)+'_'+str(end-timedelta(days=1)) if kind=='weekly' else 'updown_monthly_'+start.strftime('%Y-%m'))
                payload = json.loads((output/(stem+'.json')).read_text())
                self.assertEqual(payload['current'],current)
                reference = payload['trade_distribution']
                self.assertEqual(reference['kind'],kind)
                self.assertEqual(reference['start_date_utc'],str(start))
                self.assertEqual(reference['end_date_utc_exclusive'],str(end))
                self.assertEqual(json.loads((output/reference['json_file']).read_text()),stats)
                tweet = (output/(stem+'_tweet.md')).read_text()
                self.assertTrue(tweet.isascii())
                self.assertLessEqual(len(tweet),280)
                self.assertIn('All BUY:',tweet)
                self.assertIn('Suspected bot BUY:',tweet)
                self.assertIn('of all BUY notional.',tweet)
                self.assertNotIn('of wallet volume',tweet)
                manifest = json.loads((output/(stem+'_publish.json')).read_text())
                for key in ('source_path','image_path','english_path'):
                    self.assertEqual(Path(manifest[key]).parent,output)
                    self.assertTrue(Path(manifest[key]).is_file())
                for name,item in manifest['platforms'].items():
                    self.assertEqual(len(item['image_paths']),6 if name=='douyin' else 2)
                    for path in item['image_paths']:
                        self.assertEqual(Path(path).parent,output)
                        self.assertTrue(Path(path).is_file())
                self.assertEqual(len(list(output.iterdir())),13)
                self.assertFalse(list(self.root.glob('.'+stem+'.*')))

    def test_distribution_connection_starts_after_overview_connection_is_closed(self):
        source,start,end,labels,_,_ = self.fixture('weekly')
        overview_connections = []
        original_overview = weekly.load_week
        original_distribution = weekly.load_period_distribution
        def overview(db,*args,**kwargs):
            self.assertTrue(db.in_transaction)
            overview_connections.append(db)
            return original_overview(db,*args,**kwargs)
        def distribution(*args,**kwargs):
            with self.assertRaises(sqlite3.ProgrammingError):
                overview_connections[0].execute('SELECT 1')
            self.assertEqual(args[-1],labels)
            return original_distribution(*args,**kwargs)
        with patch('polymarket_updown_bot_registry.load_first_seen',return_value=labels), \
             patch.object(weekly,'load_week',side_effect=overview), \
             patch.object(weekly,'load_period_distribution',side_effect=distribution),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(weekly.main(self.args('weekly',source,start,self.root/'connection_output')),0)

    def test_distribution_tweet_or_sidecar_failure_keeps_existing_outputs(self):
        source,start,_,labels,_,_ = self.fixture('weekly')
        output = self.root/'existing'
        output.mkdir()
        stem = 'updown_weekly_2026-11-02_2026-11-08'
        sentinel = output/(stem+'.json')
        sentinel.write_text('{"old":true}\n')
        original = sentinel.read_bytes()
        for function,behavior in [('load_period_distribution',{'side_effect':ValueError('bad reconstruction')}),
                                  ('render_tweet',{'return_value':'x'*281}),
                                  ('write_sidecars',{'side_effect':ValueError('bad cards')})]:
            with self.subTest(function=function), patch('polymarket_updown_bot_registry.load_first_seen',return_value=labels), \
                 patch.object(weekly,function,**behavior),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(weekly.main(self.args('weekly',source,start,output)),2)
                self.assertEqual(sentinel.read_bytes(),original)
                self.assertEqual(list(output.iterdir()),[sentinel])
                self.assertFalse(list(self.root.glob('.'+stem+'.*')))

    def test_tweet_budget_scope_zero_and_raw_rounding(self):
        source,start,end,labels,stats,current = self.fixture('monthly')
        note = 'Scope: ZEC added during Aug 2026.'
        text = monthly.render_tweet(current,None,[],stats)
        self.assertIn('31 trades | 124.00 shares | 31.00 USDC',text)
        self.assertIn('bot BUY event coverage=100.00%',text)
        self.assertLessEqual(len(text),280)
        with localcontext() as context:
            context.prec = 100
            # Preserve valid totals and percentages while stressing monthly string lengths.
            large = copy.deepcopy(stats)
            for population in large['populations'].values():
                population['trade_count'] *= 1000000
                for key in ('shares_exact','notional_usdc_exact'):
                    population[key] = str(Decimal(population[key])*1000000)
                for row in population['bins']:
                    row['trade_count'] *= 1000000
                    for key in ('shares_exact','notional_usdc_exact'):
                        row[key] = str(Decimal(row[key])*1000000)
            audit_keys = ('transaction_groups','validated_groups','reconstructed_trades','buy_participating_trades',
                          'excluded_sell_only_trades','unresolved_groups','exact_value_groups','tolerated_value_groups',
                          'price_rounding_groups','source_row_count')
            for audit in [large['validation'],*[row['validation'] for row in large['daily_audits']]]:
                for key in audit_keys:
                    audit[key] *= 1000000
        text = package.render_distribution_tweet(current,large,'monthly',scope_note=note)
        self.assertIn(note,text)
        self.assertIn('trades',text)
        self.assertIn('shares',text)
        self.assertIn('of all BUY notional.',text)
        self.assertLessEqual(len(text),280)
        with sqlite3.connect(source) as db:
            db.execute('DELETE FROM trades')
            db.commit()
            stats = load_period_data(db,start,end,kind='monthly',bot_first_seen=labels)
        current.update(volume_micro_usdc=None,unique_wallets=0)
        text = package.render_distribution_tweet(current,stats,'monthly')
        self.assertIn('Market vol N/A USDC',text)
        self.assertIn('bot BUY event coverage=N/A of all BUY notional.',text)
        self.assertEqual(package._compact(Decimal('1005')),'1.01K')
        self.assertEqual(package._compact(Decimal('1004.999999999999999999999999999999999999')),'1.00K')

    def test_unrepresentable_scope_or_wrong_period_fails_without_truncation(self):
        _,_,_,_,stats,current = self.fixture('weekly')
        with self.assertRaisesRegex(ValueError,'no truncation'):
            package.render_distribution_tweet(current,stats,'weekly',scope_note='Scope: '+'A'*281)
        changed = {**current,'start_date_utc':'2026-11-03'}
        with self.assertRaisesRegex(ValueError,'periods differ'):
            package.render_distribution_tweet(changed,stats,'weekly')


if __name__ == '__main__':
    unittest.main()
