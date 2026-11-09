import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_price_distribution as daily
import polymarket_updown_period_distribution as period

ROOT = Path(__file__).resolve().parents[1]
START = date(2026, 11, 2)
END = START + timedelta(days=7)


class PeriodDistributionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "sql/btc5m_schema.sql").read_text())
        self.db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) "
                        "AS VALUES ('btc',1,NULL),('eth',1,NULL)")
        self.sequence = 0
        self.add_days(START, END)

    def tearDown(self):
        self.db.close()

    def add_days(self, start, end):
        with self.db:
            for offset in range((end-start).days):
                day = start + timedelta(days=offset)
                for coin in ("btc", "eth"):
                    slug = f"{coin}-{day}"
                    self.db.execute("INSERT OR IGNORE INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                                    "VALUES (?,?,?,?,?)", (slug, str(day-timedelta(days=1)), slug, coin, f"{day}T00:00:00Z"))
                    self.db.execute("INSERT OR IGNORE INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))

    def group(self, day, anchor, details, *, coin="btc", tx="same-hash"):
        self.sequence += 1
        # 同一哈希可出现在不同市场；同组有多条外观相同的明细也必须保留。
        with self.db:
            for row_number, (wallet, side, outcome, price, size) in enumerate([anchor, *details]):
                self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{coin}-{day}", self.sequence, row_number, wallet, tx, 1700000000,
                     side, outcome, 0 if outcome == "Up" else 1, "token", price, size, 0))

    def load(self, first_seen=None, **kwargs):
        return period.load_period_data(self.db, START, END, kind="weekly",
                                       bot_first_seen={} if first_seen is None else first_seen, **kwargs)

    def test_daily_and_period_use_same_dedup_and_persistent_labels(self):
        days = [START + timedelta(days=i) for i in range(3)]
        child = ("human", "SELL", "Up", ".2", "5")
        for day in days:
            self.group(day, ("bot", "BUY", "Up", ".2", "10"), [child, child])
        # 对手BUY与汇总BUY镜像只是一笔，且相同hash跨市场不能合并。
        self.group(days[1], ("bot", "BUY", "Down", ".8", "10"),
                   [("other-bot", "BUY", "Up", ".2", "10")], coin="eth")
        labels = {"bot": str(days[1]), "other-bot": str(days[1]), "inactive": str(START)}
        result = self.load(labels)
        self.assertEqual(result["all_wallet_count"], 3)
        self.assertEqual(result["suspected_bot_wallets"], 2)
        self.assertEqual(result["populations"]["all_wallets"]["trade_count"], 7)
        self.assertEqual(result["populations"]["suspected_bots"]["trade_count"], 5)
        self.assertEqual(result["populations"]["suspected_bots"]["shares_exact"], "30")
        for name in ("all_wallets", "suspected_bots"):
            day_data = [daily.load_data(self.db, day, bot_first_seen=labels)["populations"][name]
                        for day in period.period_dates(START, END, "weekly")[0]]
            population = result["populations"][name]
            self.assertEqual(population["trade_count"], sum(row["trade_count"] for row in day_data))
            self.assertEqual(Decimal(population["shares_exact"]), sum(Decimal(row["shares_exact"]) for row in day_data))
            self.assertEqual(Decimal(population["notional_usdc_exact"]), sum(Decimal(row["notional_usdc_exact"]) for row in day_data))
        self.assertEqual(result["validation"]["buy_participating_trades"], 7)
        self.assertEqual(result["validation"]["transaction_groups"], 4)
        self.assertEqual(result["covered_dates"], [str(START+timedelta(days=i)) for i in range(7)])
        self.assertNotIn("date_utc", result)
        self.assertEqual(result["bot_rule"]["classification"], "first_hit_day_onward")

    def test_percentages_recomputed_from_raw_totals_not_mean_daily_percentages(self):
        self.group(START, ("bot", "BUY", "Up", ".2", ".01"), [("h", "SELL", "Up", ".2", ".01")])
        self.group(START+timedelta(days=1), ("human", "BUY", "Up", ".8", "1"),
                   [("h2", "SELL", "Up", ".8", "1")])
        result = self.load({"bot": str(START)})
        all_rows = result["populations"]["all_wallets"]["bins"]
        bot_rows = result["populations"]["suspected_bots"]["bins"]
        self.assertEqual(all_rows[2]["trade_share_percent"], "50.00")
        self.assertEqual(all_rows[2]["notional_share_of_market_percent"], "0.25")
        self.assertEqual(bot_rows[2]["trade_share_percent"], "100.00")
        self.assertEqual(bot_rows[2]["notional_share_of_market_percent"], "0.25")
        self.assertEqual(all_rows[2]["share_of_all_buy_shares_percent"], "0.99")
        self.assertEqual(bot_rows[2]["share_of_all_buy_shares_percent"], "0.99")
        self.assertEqual(result["populations"]["all_wallets"]["share_of_all_buy_shares_percent"], "100.00")
        self.assertEqual(result["populations"]["suspected_bots"]["share_of_all_buy_shares_percent"], "0.99")
        self.assertEqual(result["shares_share_denominator"], "all_wallets_buy_event_shares")
        self.assertEqual(result["populations"]["all_wallets"]["notional_usdc_exact"], "0.802")
        self.assertEqual(result["populations"]["suspected_bots"]["notional_usdc_exact"], "0.002")

    def test_decimal_sums_not_rounded_under_caller_precision(self):
        price = "0." + "9"*150
        for day in (START, START+timedelta(days=1)):
            self.group(day, ("a", "BUY", "Down", price, "1e100"), [("b", "SELL", "Down", price, "1e100")])
        with localcontext() as context:
            context.prec = 2
            result = self.load()
        self.assertEqual(result["populations"]["all_wallets"]["shares_exact"], "2"+"0"*100)
        self.assertEqual(result["populations"]["all_wallets"]["notional_usdc_exact"], "0."+"0"*49+"2")

    def test_full_calendar_periods_and_compatible_names(self):
        for start, end, kind, target in [
            (date(2025,12,29), date(2026,1,5), "weekly", "2025-12-29_2026-01-04"),
            (date(2024,2,1), date(2024,3,1), "monthly", "2024-02"),
            (date(2026,4,1), date(2026,5,1), "monthly", "2026-04"),
            (date(2026,10,1), date(2026,11,1), "monthly", "2026-10"),
            (date(2026,12,1), date(2027,1,1), "monthly", "2026-12")]:
            with self.subTest(start=start):
                days, result_target = period.period_dates(start, end, kind)
                self.assertEqual(result_target, target)
                self.assertEqual(len(days), (end-start).days)
                payload = dict(kind=kind, start_date_utc=str(start), end_date_utc_exclusive=str(end))
                self.assertEqual(daily.statistics_stem(payload), f"updown_buy_trade_distribution_all_series_{kind}_{target}")
        self.assertEqual(daily.statistics_stem(dict(date_utc="2026-11-07",series_slug=None)),
                         "updown_buy_trade_distribution_all_series_2026-11-07")
        for start, end, kind in [(START,END+timedelta(days=1),"weekly"),
                                  (START+timedelta(days=1),END+timedelta(days=1),"weekly"),
                                  (START,END,"monthly"), (START,END,"daily")]:
            with self.assertRaises(ValueError):
                period.period_dates(start,end,kind)

    def test_month_empty_population_and_audit_metadata(self):
        start, end = date(2026,10,1), date(2026,11,1)
        self.add_days(start,end)
        result = period.load_period_data(self.db,start,end,kind="monthly",bot_first_seen={})
        self.assertEqual(result["total_markets"], 62)
        self.assertEqual(len(result["daily_audits"]), 31)
        self.assertEqual(result["all_wallet_count"], 0)
        self.assertTrue(all(len(row["source_fingerprint_sha256"]) == 64 for row in result["daily_audits"]))
        for population in result["populations"].values():
            self.assertEqual(population["trade_count"], 0)
            self.assertIsNone(population["notional_share_of_market_percent"])
            self.assertIsNone(population["share_of_all_buy_shares_percent"])
        with tempfile.TemporaryDirectory() as folder:
            path = daily.save_statistics(result, Path(folder))
            self.assertEqual(path.name, "updown_buy_trade_distribution_all_series_monthly_2026-10.json")
            self.assertEqual(json.loads(path.read_text()), result)

    def test_target_day_missing_or_changed_anywhere_rejected(self):
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug=?", (f"btc-{START}",))
        with self.assertRaises(daily.source.IncompleteDay):
            self.load()
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at='done'")
        def progress(message):
            if f"{START+timedelta(days=1)} 完成" in message:
                with self.db:
                    self.db.execute("UPDATE collection_progress SET completed_at='changed' WHERE market_slug=?", (f"btc-{START}",))
        with self.assertRaises(daily.source.DataChangedError):
            self.load(progress=progress)

    def test_no_outer_transaction_and_registry_loaded_once_for_full_period(self):
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError,"外层数据库事务"):
            self.load()
        self.db.rollback()
        with mock.patch("polymarket_updown_bot_registry.load_first_seen", return_value={}) as load:
            period.load_period_data(self.db,START,END,kind="weekly")
        self.assertEqual(load.call_count, 1)
        self.assertEqual(load.call_args.args, (self.db, END-timedelta(days=1)))


if __name__ == "__main__":
    unittest.main()
