import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import polymarket_updown_monthly_poster as monthly


class MonthlyTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            CREATE TABLE updown_coverage(date_utc TEXT,is_complete INTEGER,completed_markets INTEGER);
            CREATE TABLE updown_target_series(series_slug TEXT,first_end_time TEXT);
            CREATE TABLE markets(slug TEXT,series_slug TEXT,end_time TEXT);
            CREATE TABLE trades(market_slug TEXT,wallet TEXT,timestamp INTEGER,amount_micro_usdc INTEGER);
            INSERT INTO updown_target_series VALUES ('btc-up-or-down-5m',NULL);
            INSERT INTO updown_target_series VALUES ('zec-up-or-down-5m','2026-08-04T21:40:00+00:00');
        ''')
        for offset in range(61):
            day=date(2026,8,1)+timedelta(days=offset)
            self.db.execute('INSERT INTO updown_coverage VALUES (?,1,1)',(str(day),))

    def tearDown(self):
        self.db.close()

    def trade(self,day,wallet,amount):
        slug=str(self.db.execute('SELECT COUNT(*) FROM markets').fetchone()[0])
        self.db.execute('INSERT INTO markets VALUES (?, ?, ?)',(slug,'btc-up-or-down-5m',day+'T00:00:00Z'))
        # 成交时间在统计月份外，仍随市场结束日归属。
        self.db.execute('INSERT INTO trades VALUES (?, ?, 1, ?)',(slug,wallet,amount))

    def load(self):
        return monthly.period.load_period(self.db,date(2026,9,1),date(2026,10,1))

    def sample(self,month='2026-09',amount=600_000_000_000_000):
        start=monthly.month_start(month)
        end=monthly.next_month(start)
        days=(end-start).days
        volumes={str(start+timedelta(days=i)):amount//days for i in range(days)}
        volumes[str(start)]+=amount-sum(volumes.values())
        return {'start_date_utc':str(start),'end_date_utc_exclusive':str(end),
                'total_markets':3295*days,'volume_micro_usdc':amount,'unique_wallets':40000,
                'suspected_bot_wallets':1200,'suspected_bot_volume_micro_usdc':amount//2,
                'daily_volume_micro_usdc':volumes,'asset_volume_micro_usdc':{'BTC':amount}}

    def test_natural_month_boundaries_leap_year_and_year_change(self):
        for today,start,end in [(date(2026,10,1),date(2026,9,1),date(2026,10,1)),
                                (date(2026,1,1),date(2025,12,1),date(2026,1,1)),
                                (date(2024,3,1),date(2024,2,1),date(2024,3,1)),
                                (date(2026,3,31),date(2026,2,1),date(2026,3,1))]:
            with self.subTest(today=today):
                self.assertEqual(monthly.previous_month_start(today),start)
                self.assertEqual(monthly.next_month(start),end)
        self.assertEqual(monthly.days_in_period(self.sample('2024-02')),29)
        self.assertEqual(monthly.days_in_period(self.sample('2026-02')),28)

    def test_month_union_and_market_end_boundaries(self):
        self.trade('2026-08-31','previous',900_000_000)
        self.trade('2026-09-01','same',1_000_000)
        self.trade('2026-09-30','same',2_000_000)
        self.trade('2026-09-30','other',4_000_000)
        self.trade('2026-10-01','next',900_000_000)
        data=self.load()
        self.assertEqual(data['unique_wallets'],2)
        self.assertEqual(data['volume_micro_usdc'],7_000_000)
        self.assertEqual(len(data['daily_volume_micro_usdc']),30)
        self.assertEqual(data['daily_volume_micro_usdc']['2026-09-30'],6_000_000)

    def test_daily_average_uses_each_month_length(self):
        current=self.sample(amount=30_000_000)
        previous=self.sample('2026-08',31_000_000)
        values=monthly.display_values(current,previous)
        self.assertEqual(values['average_daily_volume'],'1.00')
        self.assertEqual(values['volume_change'][0],'-3.23%')
        self.assertEqual(values['average_daily_volume_change'][0],'0.00%')
        self.assertEqual(monthly.average_change(current,None)[0],'N/A')
        self.assertEqual(monthly.average_change(current,self.sample('2026-08',0))[0],'N/A')

    def test_missing_target_day_and_previous_month(self):
        self.db.execute("DELETE FROM updown_coverage WHERE date_utc='2026-09-30'")
        with self.assertRaises(monthly.daily.IncompleteDay):self.load()
        self.assertIsNone(monthly.period.load_period(self.db,date(2026,9,1),date(2026,10,1),required=False))
        self.assertEqual(monthly.scope_changes(self.db,date(2026,9,1),date(2026,11,1)),[])
        scope=monthly.scope_changes(self.db,date(2026,8,1),date(2026,10,1))
        self.assertIn('ZEC added during Aug 2026',monthly.scope_note(scope))

    def test_tweet_length_missing_baseline_zero_and_large_numbers(self):
        scope=monthly.scope_changes(self.db,date(2026,8,1),date(2026,10,1))
        normal=self.sample()
        zero=self.sample(amount=0)
        zero.update(unique_wallets=0,suspected_bot_wallets=0)
        large=self.sample(amount=2**63-1)
        large.update(unique_wallets=2**63-1,suspected_bot_wallets=2**63-1)
        small={**self.sample('2026-08',1),'unique_wallets':1}
        for current,prior in [(normal,self.sample('2026-08')),(normal,None),(normal,zero),
                              (zero,zero),(large,small),(large,None)]:
            for changes in ([],scope):
                with self.subTest(current=current,prior=prior,scope=changes):
                    text=monthly.render_tweet(current,prior,changes)
                    self.assertTrue(text.isascii())
                    self.assertLessEqual(len(text),280)
                    self.assertIn('wallet volume',text)
                    self.assertIn('unique wallets',text)
                    self.assertIn('of volume',text)
                    if changes:self.assertIn('ZEC added during Aug 2026',text)

    def test_render_31_days_large_values_and_zero(self):
        for current in (self.sample('2026-08'),self.sample('2026-08',2**63-1),self.sample('2026-02',0)):
            image,boxes=monthly.render(current,None,[])
            self.assertEqual(image.size,(1600,2000))
            self.assertEqual(image.mode,'RGB')
            for box in boxes:
                left,top,right,bottom=box['bounds']
                with self.subTest(text=box['text']):
                    self.assertGreaterEqual(left,0)
                    self.assertGreaterEqual(top,0)
                    self.assertLessEqual(right,1600)
                    self.assertLessEqual(bottom,2000)

    def test_cli_outputs_share_data_and_leave_database_read_only(self):
        self.trade('2026-09-01','same',1_000_000)
        self.trade('2026-08-01','same',1_000_000)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'data.sqlite3'
            target=sqlite3.connect(path)
            self.db.commit()
            self.db.backup(target)
            target.close()
            original=path.read_bytes()
            output=Path(folder)/'posters'
            with contextlib.redirect_stdout(io.StringIO()):
                status=monthly.main(['--month','2026-09','--db',str(path),'--output-dir',str(output)])
            self.assertEqual(status,0)
            stem='updown_monthly_2026-09'
            self.assertEqual({p.name for p in output.iterdir()},{stem+'.png',stem+'.json',stem+'_tweet.md'})
            payload=json.loads((output/(stem+'.json')).read_text())
            self.assertEqual(payload['current']['volume_micro_usdc'],1_000_000)
            self.assertIsNotNone(payload['previous'])
            self.assertEqual(len(payload['scope_changes']),1)
            self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':unittest.main()
