import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_price_distribution as distribution


DAY = date(2026, 11, 7)
ROOT = Path(__file__).resolve().parents[1]
FIRST = 1700000000  # 故意不在目标日内，统计仍按市场结束日归属。


class PriceDistributionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "sql/btc5m_schema.sql").read_text())
        self.db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) "
                        "AS VALUES ('btc',1,NULL),('eth',1,NULL)")
        for slug, series in (("m-btc", "btc"), ("m-eth", "eth")):
            self.db.execute("INSERT INTO markets(slug,date_utc,condition_id,series_slug,end_time) "
                            "VALUES (?,'2026-11-06',?,?,'2026-11-07T00:00:00Z')", (slug, slug, series))
            self.db.execute("INSERT INTO collection_progress(market_slug,completed_at) VALUES (?,'done')", (slug,))
        self.db.commit()
        self.group_id = 0

    def tearDown(self):
        self.db.close()

    def group(self, anchor, details, *, stamp=FIRST + 2700, slug="m-btc"):
        """anchor/details: (wallet, side, outcome, price, size)."""
        self.group_id += 1
        tx = f"tx-{self.group_id}"
        with self.db:
            for i, (wallet, side, outcome, price, size) in enumerate([anchor, *details]):
                self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (slug, self.group_id, i, wallet, tx, stamp, side, outcome,
                                 0 if outcome == "Up" else 1, f"token-{outcome}", price, size, 0))
        return tx

    def bot(self, wallet="bot", *, side="BUY", slug="m-btc", start=0, count=91):
        for i in range(start, start + count):
            self.group((wallet, side, "Up", "0", "1"),
                       [(f"human-{wallet}-{i}", "SELL" if side == "BUY" else "BUY", "Up", "0", "1")],
                       stamp=FIRST + i * 60, slug=slug)

    def load(self, **kwargs):
        return distribution.load_data(self.db, DAY, **kwargs)

    def test_complementary_buys_count_one_trade_and_one_set_of_shares(self):
        self.bot("a")
        self.bot("b")
        self.group(("a", "BUY", "Down", ".95", "10"), [("b", "BUY", "Up", ".05", "10")])
        result = self.load()
        self.assertEqual(result["suspected_bot_wallets"], 2)
        for population in result["populations"].values():
            self.assertEqual(population["trade_count"], 183)
            self.assertEqual(population["shares_exact"], "192")
            self.assertEqual(population["notional_usdc_exact"], "0.5")
            self.assertEqual(population["notional_share_of_market_percent"], "100.00")

    def test_anchor_bot_buy_expands_to_each_counterparty(self):
        self.bot()
        self.group(("bot", "BUY", "Up", ".2", "10"),
                   [("h1", "SELL", "Up", ".2", "2"),
                    ("h2", "BUY", "Down", ".8", "3"),
                    ("h3", "SELL", "Up", ".2", "5")])
        result = self.load()
        bots = result["populations"]["suspected_bots"]
        self.assertEqual(bots["trade_count"], 94)
        self.assertEqual(bots["shares_exact"], "101")
        self.assertEqual(bots["notional_usdc_exact"], "2")
        self.assertEqual(bots["bins"][2]["trade_count"], 3)
        self.assertEqual(bots["bins"][2]["shares_exact"], "10")

    def test_identical_counterparty_rows_are_distinct_reconstructed_trades(self):
        self.bot()
        child = ("human", "SELL", "Up", ".2", "5")
        self.group(("bot", "BUY", "Up", ".2", "10"), [child, child])
        bots = self.load()["populations"]["suspected_bots"]
        self.assertEqual(bots["bins"][2]["trade_count"], 2)
        self.assertEqual(bots["bins"][2]["shares_exact"], "10")
        self.assertEqual(bots["bins"][2]["notional_usdc_exact"], "2")

    def test_bot_notional_uses_all_wallets_denominator(self):
        self.bot()
        self.group(("bot", "BUY", "Up", ".2", "10"), [("h1", "SELL", "Up", ".2", "10")])
        self.group(("h2", "BUY", "Up", ".8", "10"), [("h3", "SELL", "Up", ".8", "10")])
        result = self.load()
        all_wallets = result["populations"]["all_wallets"]
        bots = result["populations"]["suspected_bots"]
        self.assertEqual(all_wallets["notional_usdc_exact"], "10")
        self.assertEqual(bots["notional_usdc_exact"], "2")
        self.assertEqual(bots["notional_share_of_market_percent"], "20.00")
        self.assertEqual(bots["bins"][2]["notional_share_of_market_percent"], "20.00")
        self.assertEqual(all_wallets["bins"][2]["notional_share_of_market_percent"], "20.00")
        self.assertEqual(bots["bins"][2]["trade_share_percent"], "1.09")
        self.assertEqual(all_wallets["bins"][2]["trade_share_percent"], "1.08")
        self.assertEqual(result["notional_share_denominator"], "all_wallets_buy_notional")
        self.assertEqual(result["shares_share_denominator"], "all_wallets_buy_event_shares")
        self.assertEqual(all_wallets["share_of_all_buy_shares_percent"], "100.00")
        self.assertEqual(bots["share_of_all_buy_shares_percent"], "90.99")
        self.assertEqual(bots["bins"][2]["share_of_all_buy_shares_percent"], "9.01")
        self.assertEqual(all_wallets["bins"][2]["share_of_all_buy_shares_percent"], "9.01")

    def test_down_trade_notional_is_up_equivalent_not_buyers_cash(self):
        self.group(("buyer", "BUY", "Down", ".8", "10"), [("seller", "SELL", "Down", ".8", "10")])
        population = self.load()["populations"]["all_wallets"]
        self.assertEqual(population["notional_usdc_exact"], "2")
        self.assertEqual(population["bins"][2]["trade_count"], 1)
        self.assertEqual(population["bins"][2]["shares_exact"], "10")

    def test_sell_only_merge_is_excluded_and_sell_only_bot_does_not_qualify(self):
        self.bot(side="SELL")
        self.group(("bot", "SELL", "Up", ".2", "10"), [("human", "SELL", "Down", ".8", "10")])
        result = self.load()
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["populations"]["all_wallets"]["trade_count"], 91)
        self.assertEqual(result["populations"]["suspected_bots"]["trade_count"], 0)
        self.assertEqual(result["validation"]["reconstructed_trades"], 92)
        self.assertEqual(result["validation"]["excluded_sell_only_trades"], 1)

    def test_bin_boundaries_are_disjoint_and_include_one(self):
        prices = [str(Decimal(i) / 10) for i in range(11)]
        prices.append("0.09999999999999999999999999999999999999999999999999999999999")
        for i, price in enumerate(prices):
            self.group((f"a{i}", "BUY", "Up", price, "1"), [(f"b{i}", "SELL", "Up", price, "1")])
        with localcontext() as context:
            context.prec = 2
            result = self.load()
        population = result["populations"]["all_wallets"]
        self.assertEqual([row["trade_count"] for row in population["bins"]], [2] + [1] * 8 + [2])
        for price in prices:
            p = Decimal(price)
            matches = [row for row in population["bins"]
                       if Decimal(row["lower_cents"]) / 100 <= p
                       and (p < Decimal(row["upper_cents"]) / 100
                            or (row["upper_inclusive"] and p <= 1))]
            self.assertEqual(len(matches), 1, price)
        self.assertEqual(sum(Decimal(row["shares_exact"]) for row in population["bins"]), Decimal(population["shares_exact"]))

    def test_accumulates_unrounded_fractional_amounts_and_shares(self):
        for i in range(2):
            self.group((f"a{i}", "BUY", "Up", ".6", ".000001"),
                       [(f"b{i}", "SELL", "Up", ".6", ".000001")])
        population = self.load()["populations"]["all_wallets"]
        self.assertEqual(population["notional_usdc_exact"], "0.0000012")
        self.assertEqual(population["shares_exact"], "0.000002")
        self.assertEqual(population["bins"][6]["notional_usdc_exact"], "0.0000012")

    def test_decimal_precision_expands_for_up_conversion_and_large_shares(self):
        price = "0." + "9" * 150
        self.group(("a", "BUY", "Down", price, "1e100"),
                   [("b", "SELL", "Down", price, "1e100")])
        population = self.load()["populations"]["all_wallets"]
        self.assertEqual(population["notional_usdc_exact"], "0." + "0" * 49 + "1")
        self.assertEqual(population["shares_exact"], "1" + "0" * 100)
        self.assertEqual(population["bins"][0]["trade_count"], 1)

    def test_notional_percentage_uses_half_up_rounding(self):
        self.assertEqual(distribution._amount_percent(Decimal("2.01"), Decimal("200")), "1.01")
        self.assertIsNone(distribution._amount_percent(Decimal(0), Decimal(0)))

    def test_empty_or_zero_notional_populations_do_not_fake_percentages(self):
        result = self.load()
        for population in result["populations"].values():
            self.assertEqual(population["trade_count"], 0)
            self.assertEqual(population["shares_exact"], "0")
            self.assertEqual(population["notional_usdc_exact"], "0")
            self.assertIsNone(population["notional_share_of_market_percent"])
            self.assertIsNone(population["share_of_all_buy_shares_percent"])
            self.assertTrue(all(row["share_of_all_buy_shares_percent"] is None for row in population["bins"]))
            self.assertTrue(all(row["trade_share_percent"] is None for row in population["bins"]))
        self.group(("a", "BUY", "Up", "0", "3"), [("b", "SELL", "Up", "0", "3")])
        population = self.load()["populations"]["all_wallets"]
        self.assertEqual(population["bins"][0]["trade_share_percent"], "100.00")
        self.assertEqual(population["bins"][0]["share_of_all_buy_shares_percent"], "100.00")
        self.assertIsNone(population["bins"][0]["notional_share_of_market_percent"])

    def test_invalid_groups_fail_instead_of_silently_dropping(self):
        cases = [
            (("a", "BUY", "Up", ".2", "1"), []),
            (("a", "BUY", "Up", ".2", "2"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "BUY", "Up", ".2", "1"), [("b", "BUY", "Up", ".2", "1")]),
            (("a", "BUY", "Up", ".2", "1"), [("b", "SELL", "Up", ".2000011", "1")]),
            (("a", "BUY", "Up", "NaN", "1"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "BUY", "Up", "1.01", "1"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "BUY", "Up", ".2", "0"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "BUY", "Up", ".2", "-1"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "BUY", "Up", ".2", "Infinity"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "OTHER", "Up", ".2", "1"), [("b", "SELL", "Up", ".2", "1")]),
            (("a", "BUY", "Yes", ".2", "1"), [("b", "SELL", "Up", ".2", "1")]),
        ]
        for anchor, details in cases:
            with self.subTest(anchor=anchor, details=details):
                with self.db:
                    self.db.execute("DELETE FROM trades")
                self.group(anchor, details)
                with self.assertRaises(distribution.InvalidReconstruction):
                    self.load()

    def test_timestamp_and_value_tolerance_checks(self):
        self.group(("a", "BUY", "Up", ".2", "1"), [("b", "SELL", "Up", ".2000005", "1")])
        result = self.load()
        self.assertEqual(result["validation"]["tolerated_value_groups"], 1)
        self.assertEqual(result["populations"]["all_wallets"]["notional_usdc_exact"], "0.2000005")
        with self.db:
            self.db.execute("UPDATE trades SET timestamp=timestamp+1 WHERE row_number=1")
        with self.assertRaises(distribution.InvalidReconstruction):
            self.load()

    def test_large_notional_anchor_price_roundtrip_accepts_only_rounding_difference(self):
        self.group(("a", "BUY", "Up", ".1234567891", "1000000"),
                   [("b", "SELL", "Up", ".1234567890", "490000"),
                    ("c", "SELL", "Up", ".1234567891", "510000")])
        result = self.load()
        self.assertEqual(result["validation"]["price_rounding_groups"], 1)
        self.assertEqual(result["validation"]["exact_value_groups"], 0)
        self.assertEqual(result["validation"]["tolerated_value_groups"], 0)
        self.assertEqual(result["validation"]["roundtrip_min_decimal_places"], 10)
        self.assertEqual(result["validation"]["value_tolerance_usdc"], "0.000001")
        self.assertEqual(result["validation"]["max_absolute_value_difference_usdc"], "0.000049")
        population = result["populations"]["all_wallets"]
        self.assertEqual(population["trade_count"], 2)
        self.assertEqual(population["shares_exact"], "1000000")
        self.assertEqual(population["notional_usdc_exact"], "123456.789051")
        with self.db:
            self.db.execute("UPDATE trades SET raw_price='.1234567892' WHERE row_number=0")
        with self.assertRaises(distribution.InvalidReconstruction):
            self.load()

    def test_down_anchor_roundtrip_checks_original_down_price(self):
        self.group(("a", "BUY", "Down", ".8765432109", "1000000"),
                   [("b", "SELL", "Down", ".8765432110", "490000"),
                    ("c", "SELL", "Down", ".8765432109", "510000")])
        result = self.load()
        self.assertEqual(result["validation"]["price_rounding_groups"], 1)
        self.assertEqual(result["populations"]["all_wallets"]["notional_usdc_exact"], "123456.789051")

    def test_roundtrip_does_not_accept_short_price_using_coarse_precision(self):
        self.group(("a", "BUY", "Up", ".5", "1000000"),
                   [("b", "SELL", "Up", ".50000000009", "1000000")])
        with self.assertRaises(distribution.InvalidReconstruction):
            self.load()

    def test_roundtrip_preserves_more_than_ten_original_decimal_places(self):
        self.group(("a", "BUY", "Up", ".123456789100", "1000000"),
                   [("b", "SELL", "Up", ".1234567891044", "1000000")])
        with self.assertRaises(distribution.InvalidReconstruction):
            self.load()

    def test_cross_market_bot_features_and_end_date_scope(self):
        self.bot(slug="m-btc", start=0, count=46)
        self.bot(slug="m-eth", start=46, count=45)
        result = self.load()
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["populations"]["suspected_bots"]["trade_count"], 91)
        self.assertEqual(result["market_scope"], "all_crypto_updown_series")
        self.assertEqual(result["included_series_slugs"], ["btc", "eth"])
        self.assertEqual(result["series_count"], 2)
        self.assertEqual(result["total_markets"], 2)
        selected = self.load(series_slug="btc")
        # 展示只选 BTC 时，历史标签仍来自全部加密市场。
        self.assertEqual(selected["suspected_bot_wallets"], 1)
        self.assertEqual(selected["populations"]["all_wallets"]["trade_count"], 46)
        self.assertEqual(selected["market_scope"], "selected_series")
        self.assertEqual(selected["included_series_slugs"], ["btc"])
        self.assertEqual(selected["series_count"], 1)
        self.assertEqual(selected["total_markets"], 1)
        self.assertEqual(result["date_basis"], "market_end_utc")

    def test_first_hit_day_onward_persists_without_future_backfill(self):
        self.group(("old-bot", "BUY", "Down", ".8", "10"),
                   [("human", "SELL", "Down", ".8", "10")])
        self.group(("future-bot", "BUY", "Up", ".7", "10"),
                   [("human2", "SELL", "Up", ".7", "10")])
        result = self.load(bot_first_seen={"old-bot": "2026-11-01", "future-bot": "2026-11-08"})
        self.assertEqual(result["suspected_bot_wallets"], 1)
        bot = result["populations"]["suspected_bots"]
        self.assertEqual(bot["trade_count"], 1)
        self.assertEqual(bot["shares_exact"], "10")
        self.assertEqual(bot["notional_usdc_exact"], "2")
        self.assertEqual(result["bot_rule"]["classification"], "first_hit_day_onward")
        self.assertFalse(result["bot_rule"]["future_labels_applied"])

    def test_same_day_first_hit_is_included_and_inactive_history_bots_not_counted(self):
        self.group(("bot", "BUY", "Up", ".3", "5"), [("human", "SELL", "Up", ".3", "5")])
        result = self.load(bot_first_seen={"bot": str(DAY), "inactive": "2026-01-01"})
        self.assertEqual(result["suspected_bot_wallets"], 1)
        self.assertEqual(result["populations"]["suspected_bots"]["trade_count"], 1)

    def test_default_labels_use_shared_history_registry(self):
        self.group(("bot", "BUY", "Up", ".3", "5"), [("human", "SELL", "Up", ".3", "5")])
        with mock.patch("polymarket_updown_bot_registry.load_first_seen", return_value={"bot": "2026-11-01"}) as registry:
            result = self.load()
        registry.assert_called_once_with(self.db, DAY)
        self.assertEqual(result["populations"]["suspected_bots"]["trade_count"], 1)

    def test_incomplete_selected_series_and_outer_transaction_rejected(self):
        with self.db:
            self.db.execute("UPDATE collection_progress SET completed_at=NULL WHERE market_slug='m-eth'")
        self.assertEqual(self.load(series_slug="btc")["total_markets"], 1)
        with self.assertRaises(distribution.source.IncompleteDay):
            self.load()
        with self.assertRaises(ValueError):
            self.load(series_slug="unknown")
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "外层数据库事务"):
            self.load(series_slug="btc")
        self.db.rollback()

    def test_metadata_changes_rejected(self):
        original = distribution.source._snapshot
        calls = 0

        def changed(db, day, series_slug=None):
            nonlocal calls
            result = original(db, day, series_slug)
            calls += 1
            return result if calls == 1 else (result[0], result[1] + (("changed",),))

        with mock.patch.object(distribution.source, "_snapshot", side_effect=changed):
            with self.assertRaises(distribution.source.DataChangedError):
                self.load(bot_first_seen={})

    def test_readonly_database_remains_unchanged_and_uses_only_selects(self):
        self.group(("a", "BUY", "Up", ".2", "1"), [("b", "SELL", "Up", ".2", "1")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.sqlite3"
            writer = sqlite3.connect(path)
            self.db.backup(writer)
            before = path.read_bytes()
            reader = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            queries = []
            reader.set_trace_callback(queries.append)
            try:
                result = distribution.load_data(reader, DAY, bot_first_seen={})
                self.assertEqual(result["populations"]["all_wallets"]["trade_count"], 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(reader.in_transaction)
                self.assertTrue(all(query.lstrip().upper().startswith("SELECT") for query in queries))
                with writer:
                    writer.execute("UPDATE collection_progress SET last_error='changed'")
            finally:
                reader.close()
                writer.close()


if __name__ == "__main__":
    unittest.main()
