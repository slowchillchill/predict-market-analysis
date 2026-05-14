import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "btc_updown_volume_report.py"
spec = importlib.util.spec_from_file_location("btc_updown_volume_report", SCRIPT_PATH)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class BtcUpdownVolumeReportTests(unittest.TestCase):
    def tearDown(self):
        report.configure_asset("btc")

    def test_series_slug_maps_market_type_including_4hour(self):
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-5m"), "5min")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-15m"), "15min")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-hourly"), "hourly")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-4h"), "4hour")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-daily"), "daily")

    def test_configure_eth_maps_series_and_output_paths(self):
        report.configure_asset("eth")
        self.assertEqual(report.market_type_for_series_slug("eth-up-or-down-5m"), "5min")
        self.assertEqual(report.market_type_for_series_slug("eth-up-or-down-15m"), "15min")
        self.assertEqual(report.market_type_for_series_slug("eth-up-or-down-hourly"), "hourly")
        self.assertEqual(report.market_type_for_series_slug("eth-up-or-down-4h"), "4hour")
        self.assertEqual(report.market_type_for_series_slug("eth-up-or-down-daily"), "daily")
        self.assertEqual(
            report.DETAIL_PATH.name,
            "eth_updown_market_detail_by_platform_2026-02_2026-04.csv",
        )
        self.assertEqual(
            report.DISCOVERY_REPORT_PATH.name,
            "kalshi_eth_series_discovery_2026-02_2026-04.md",
        )

    def test_configure_asset_sets_wallet_output_paths(self):
        report.configure_asset("btc")
        self.assertEqual(
            report.POLYMARKET_TRADES_RAW_PATH.name,
            "btc_updown_polymarket_trades_2026-02_2026-04.jsonl.gz",
        )
        self.assertEqual(
            report.WALLET_MARKET_PATH.name,
            "btc_updown_wallet_activity_by_market_2026-02_2026-04.csv",
        )
        self.assertEqual(
            report.WALLET_DAILY_PATH.name,
            "btc_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv",
        )
        self.assertEqual(
            report.WALLET_MONTHLY_PATH.name,
            "btc_updown_wallet_activity_monthly_by_platform_2026-02_2026-04.csv",
        )

        report.configure_asset("eth")
        self.assertEqual(
            report.POLYMARKET_TRADES_RAW_PATH.name,
            "eth_updown_polymarket_trades_2026-02_2026-04.jsonl.gz",
        )
        self.assertEqual(
            report.WALLET_MARKET_PATH.name,
            "eth_updown_wallet_activity_by_market_2026-02_2026-04.csv",
        )
        self.assertEqual(
            report.WALLET_DAILY_PATH.name,
            "eth_updown_wallet_activity_daily_by_platform_2026-02_2026-04.csv",
        )

    def test_half_open_month_filter_excludes_end_boundary(self):
        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 1, tzinfo=timezone.utc)
        self.assertTrue(report.is_in_half_open_window("2026-02-28T23:59:59Z", start, end))
        self.assertFalse(report.is_in_half_open_window("2026-03-01T00:00:00Z", start, end))
        self.assertFalse(report.is_in_half_open_window("2026-01-31T23:59:59Z", start, end))

    def test_fallback_filter_requires_exact_current_series(self):
        event = {
            "series": [
                {"slug": "btc-up-or-down-hourly"},
                {"slug": "btc-up-or-down-daily"},
            ]
        }
        self.assertTrue(report.event_has_series_slug(event, "btc-up-or-down-hourly"))
        self.assertFalse(report.event_has_series_slug(event, "btc-up-or-down-4h"))

    def test_volume_priority_uses_clob_num_volume_then_event(self):
        event = {"volume": "40"}
        self.assertEqual(
            report.choose_volume({"volumeClob": "10", "volumeNum": "20", "volume": "30"}, event),
            (Decimal("10"), "market.volumeClob"),
        )
        self.assertEqual(
            report.choose_volume({"volumeNum": "20", "volume": "30"}, event),
            (Decimal("20"), "market.volumeNum"),
        )
        self.assertEqual(
            report.choose_volume({"volume": "30"}, event),
            (Decimal("30"), "market.volume"),
        )
        self.assertEqual(report.choose_volume({}, event), (Decimal("40"), "event.volume"))
        self.assertEqual(report.choose_volume({}, {}), (Decimal("0"), "missing_as_zero"))

    def test_normalize_polymarket_trade_requires_wallet_condition_and_size(self):
        condition_id = "0x" + "a" * 64
        trade = {
            "proxyWallet": "0x0000000000000000000000000000000000000001",
            "conditionId": condition_id,
            "size": "12.5",
            "price": "0.61",
            "timestamp": 1770000000,
            "side": "BUY",
            "outcome": "Up",
            "transactionHash": "0xhash",
        }
        normalized = report.normalize_polymarket_trade(trade, condition_id)
        self.assertEqual(normalized["wallet"], "0x0000000000000000000000000000000000000001")
        self.assertEqual(normalized["condition_id"], condition_id)
        self.assertEqual(normalized["size"], Decimal("12.5"))
        self.assertEqual(
            report.polymarket_trade_key(normalized),
            (
                "0xhash",
                "0x0000000000000000000000000000000000000001",
                condition_id,
                "Up",
                1770000000,
                "BUY",
                "12.5",
                "0.61",
            ),
        )

    def test_normalize_polymarket_trade_rejects_wrong_condition(self):
        trade = {
            "proxyWallet": "0x0000000000000000000000000000000000000001",
            "conditionId": "0x" + "b" * 64,
            "size": "1",
            "timestamp": 1770000000,
        }
        with self.assertRaises(ValueError):
            report.normalize_polymarket_trade(trade, "0x" + "a" * 64)

    def test_normalize_polymarket_position_requires_wallet_and_condition(self):
        condition_id = "0x" + "a" * 64
        position = {
            "proxyWallet": "0x0000000000000000000000000000000000000001",
            "conditionId": condition_id,
            "asset": "123",
            "outcome": "Up",
            "outcomeIndex": 0,
            "totalBought": 12.5,
        }
        normalized = report.normalize_polymarket_position(position, condition_id)
        self.assertEqual(normalized["wallet"], "0x0000000000000000000000000000000000000001")
        self.assertEqual(normalized["condition_id"], condition_id)
        self.assertEqual(normalized["total_bought"], Decimal("12.5"))

        position["conditionId"] = "0x" + "b" * 64
        with self.assertRaises(ValueError):
            report.normalize_polymarket_position(position, condition_id)

    def test_fetch_polymarket_trades_for_market_dedupes_and_sets_taker_only(self):
        condition_id = "0x" + "a" * 64
        calls = []

        def fake_get(base_url, path, params):
            calls.append((base_url, path, params.copy()))
            if params["offset"] == 0:
                return [
                    {
                        "proxyWallet": "0x0000000000000000000000000000000000000001",
                        "conditionId": condition_id,
                        "size": "1",
                        "timestamp": 1770000000,
                        "transactionHash": "0x1",
                    },
                    {
                        "proxyWallet": "0x0000000000000000000000000000000000000001",
                        "conditionId": condition_id,
                        "size": "1",
                        "timestamp": 1770000000,
                        "transactionHash": "0x1",
                    },
                ]
            return []

        original = report.get_json_list
        report.get_json_list = fake_get
        try:
            trades, status = report.fetch_polymarket_trades_for_market(condition_id, taker_only=False)
        finally:
            report.get_json_list = original

        self.assertEqual(status, "complete")
        self.assertEqual(len(trades), 1)
        self.assertEqual(calls[0][0], report.POLYMARKET_DATA_API_BASE)
        self.assertEqual(calls[0][1], "/trades")
        self.assertEqual(calls[0][2]["market"], condition_id)
        self.assertEqual(calls[0][2]["takerOnly"], "false")

    def test_fetch_polymarket_trades_for_market_marks_truncated_at_max_offset(self):
        condition_id = "0x" + "a" * 64

        def fake_get(base_url, path, params):
            start = params["offset"]
            return [
                {
                    "proxyWallet": f"0x{i:040x}",
                    "conditionId": condition_id,
                    "size": "1",
                    "timestamp": 1770000000 + start + i,
                    "transactionHash": f"0x{start + i}",
                }
                for i in range(report.POLYMARKET_TRADE_LIMIT)
            ]

        original = report.get_json_list
        report.get_json_list = fake_get
        try:
            trades, status = report.fetch_polymarket_trades_for_market(condition_id, taker_only=True)
        finally:
            report.get_json_list = original

        self.assertEqual(status, "truncated")
        expected_pages = report.POLYMARKET_MAX_TRADE_OFFSET // report.POLYMARKET_TRADE_LIMIT + 1
        self.assertEqual(len(trades), report.POLYMARKET_TRADE_LIMIT * expected_pages)

    def test_fetch_polymarket_trades_for_markets_batches_and_groups_results(self):
        condition_ids = ["0x" + "a" * 64, "0x" + "b" * 64]
        calls = []

        def fake_get(base_url, path, params):
            calls.append((base_url, path, params.copy()))
            return [
                {
                    "proxyWallet": "0x0000000000000000000000000000000000000001",
                    "conditionId": condition_ids[0],
                    "size": "1",
                    "timestamp": 1770000000,
                    "transactionHash": "0x1",
                },
                {
                    "proxyWallet": "0x0000000000000000000000000000000000000002",
                    "conditionId": condition_ids[1],
                    "size": "2",
                    "timestamp": 1770000001,
                    "transactionHash": "0x2",
                },
            ]

        original = report.get_json_list
        report.get_json_list = fake_get
        try:
            grouped = report.fetch_polymarket_trades_for_markets(condition_ids, taker_only=True)
        finally:
            report.get_json_list = original

        self.assertEqual(calls[0][0], report.POLYMARKET_DATA_API_BASE)
        self.assertEqual(calls[0][1], "/trades")
        self.assertEqual(calls[0][2]["market"], ",".join(condition_ids))
        self.assertEqual(calls[0][2]["takerOnly"], "true")
        self.assertEqual(
            [trade["wallet"] for trade in grouped[condition_ids[0]]["trades"]],
            ["0x0000000000000000000000000000000000000001"],
        )
        self.assertEqual(
            [trade["wallet"] for trade in grouped[condition_ids[1]]["trades"]],
            ["0x0000000000000000000000000000000000000002"],
        )
        self.assertEqual(grouped[condition_ids[0]]["status"], "complete")
        self.assertEqual(grouped[condition_ids[1]]["status"], "complete")

    def test_fetch_polymarket_trades_for_markets_splits_truncated_batches(self):
        condition_ids = ["0x" + "a" * 64, "0x" + "b" * 64]
        calls = []
        original_limit = report.POLYMARKET_TRADE_LIMIT
        original_max_offset = report.POLYMARKET_MAX_TRADE_OFFSET

        def fake_get(base_url, path, params):
            calls.append(params["market"])
            requested = params["market"].split(",")
            if len(requested) > 1:
                return [
                    {
                        "proxyWallet": f"0x{i + 1:040x}",
                        "conditionId": requested[i],
                        "size": "1",
                        "timestamp": 1770000000 + i,
                        "transactionHash": f"0x{i}",
                    }
                    for i in range(2)
                ]
            return [
                {
                    "proxyWallet": "0x0000000000000000000000000000000000000001",
                    "conditionId": requested[0],
                    "size": "1",
                    "timestamp": 1770000000,
                    "transactionHash": "0x1",
                }
            ]

        original_get = report.get_json_list
        report.get_json_list = fake_get
        report.POLYMARKET_TRADE_LIMIT = 2
        report.POLYMARKET_MAX_TRADE_OFFSET = 0
        try:
            grouped = report.fetch_polymarket_trades_for_markets(condition_ids, taker_only=True)
        finally:
            report.get_json_list = original_get
            report.POLYMARKET_TRADE_LIMIT = original_limit
            report.POLYMARKET_MAX_TRADE_OFFSET = original_max_offset

        self.assertEqual(calls, [",".join(condition_ids), condition_ids[0], condition_ids[1]])
        self.assertEqual(grouped[condition_ids[0]]["status"], "complete")
        self.assertEqual(grouped[condition_ids[1]]["status"], "complete")

    def test_fetch_polymarket_positions_for_market_paginates_per_outcome(self):
        condition_id = "0x" + "a" * 64
        calls = []
        original_limit = report.POLYMARKET_POSITIONS_LIMIT
        original_get = report.get_json_list
        report.POLYMARKET_POSITIONS_LIMIT = 2

        def fake_get(base_url, path, params):
            calls.append((base_url, path, params.copy()))
            offset = params["offset"]
            if offset == 0:
                return [
                    {
                        "positions": [
                            {
                                "proxyWallet": "0x0000000000000000000000000000000000000001",
                                "conditionId": condition_id,
                                "asset": "up",
                                "outcomeIndex": 0,
                                "totalBought": "1",
                            },
                            {
                                "proxyWallet": "0x0000000000000000000000000000000000000002",
                                "conditionId": condition_id,
                                "asset": "up",
                                "outcomeIndex": 0,
                                "totalBought": "2",
                            },
                        ]
                    },
                    {"positions": []},
                ]
            return [
                {
                    "positions": [
                        {
                            "proxyWallet": "0x0000000000000000000000000000000000000003",
                            "conditionId": condition_id,
                            "asset": "up",
                            "outcomeIndex": 0,
                            "totalBought": "3",
                        }
                    ]
                }
            ]

        report.get_json_list = fake_get
        try:
            fetched = report.fetch_polymarket_positions_for_market(condition_id)
        finally:
            report.get_json_list = original_get
            report.POLYMARKET_POSITIONS_LIMIT = original_limit

        self.assertEqual([call[2]["offset"] for call in calls], [0, 2])
        self.assertEqual(calls[0][0], report.POLYMARKET_DATA_API_BASE)
        self.assertEqual(calls[0][1], "/v1/market-positions")
        self.assertEqual(fetched["status"], "positions_complete")
        self.assertEqual([position["wallet"] for position in fetched["positions"]], ["0x0000000000000000000000000000000000000001", "0x0000000000000000000000000000000000000002", "0x0000000000000000000000000000000000000003"])

    def test_normalize_event_validates_title_outcomes_and_window(self):
        event = {
            "id": "event-1",
            "slug": "bitcoin-up-or-down-february-1-12pm-et",
            "title": "Bitcoin Up or Down - February 1, 12PM ET",
            "endDate": "2026-02-01T17:00:00Z",
            "volume": "99",
            "series": [{"slug": "btc-up-or-down-hourly"}],
            "markets": [
                {
                    "id": "market-1",
                    "slug": "market-slug",
                    "conditionId": "0xabc",
                    "outcomes": '["Up", "Down"]',
                    "volumeClob": "12.345",
                    "volumeNum": "13",
                    "closedTime": "2026-02-01T17:00:01Z",
                }
            ],
        }
        row, raw = report.normalize_polymarket_event(
            event,
            market_type="hourly",
            series_slug="btc-up-or-down-hourly",
            month_utc="2026-02",
            window_start=datetime(2026, 2, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(row["date_utc"], "2026-02-01")
        self.assertEqual(row["market_type"], "hourly")
        self.assertEqual(row["platform"], "polymarket")
        self.assertEqual(row["platform_market_id"], "0xabc")
        self.assertEqual(row["volume"], "12.345")
        self.assertEqual(row["volume_source"], "market.volumeClob")
        self.assertEqual(row["comparable"], "true")
        self.assertEqual(raw["event"]["id"], "event-1")

    def test_normalize_eth_event_validates_ethereum_title(self):
        report.configure_asset("eth")
        event = {
            "id": "event-eth-1",
            "slug": "ethereum-up-or-down-february-1-12pm-et",
            "title": "Ethereum Up or Down - February 1, 12PM ET",
            "endDate": "2026-02-01T17:00:00Z",
            "series": [{"slug": "eth-up-or-down-hourly"}],
            "markets": [
                {
                    "id": "market-eth-1",
                    "conditionId": "0xeth",
                    "outcomes": '["Up", "Down"]',
                    "volumeClob": "7",
                }
            ],
        }
        row, _ = report.normalize_polymarket_event(
            event,
            market_type="hourly",
            series_slug="eth-up-or-down-hourly",
            month_utc="2026-02",
            window_start=datetime(2026, 2, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(row["platform_series"], "eth-up-or-down-hourly")
        self.assertEqual(row["title"], "Ethereum Up or Down - February 1, 12PM ET")

    def test_kalshi_classifier_accepts_exact_15min_and_rejects_related_products(self):
        exact = report.classify_kalshi_series(
            {"ticker": "KXBTC15M", "title": "Bitcoin price up down", "frequency": "fifteen_min"}
        )
        self.assertEqual(exact["classification"], "exact_direction")
        self.assertEqual(exact["market_type"], "15min")
        self.assertEqual(exact["comparable"], "true")

        for ticker in ("KXBTCD", "BTCD", "BTCD-B", "KXBTC"):
            classified = report.classify_kalshi_series({"ticker": ticker, "title": "Bitcoin price Above/below"})
            self.assertEqual(classified["classification"], "excluded_related_product")
            self.assertEqual(classified["comparable"], "false")

    def test_eth_kalshi_classifier_accepts_exact_15min_and_rejects_legacy_products(self):
        report.configure_asset("eth")
        exact = report.classify_kalshi_series(
            {"ticker": "KXETH15M", "title": "ETH 15M price up down", "frequency": "fifteen_min"}
        )
        self.assertEqual(exact["classification"], "exact_direction")
        self.assertEqual(exact["market_type"], "15min")
        self.assertEqual(exact["comparable"], "true")

        for ticker in ("ETH", "ETHATH", "ETHETF", "ETHMAXY", "ETHMINY", "KXETHD", "ETHD", "KXETH"):
            classified = report.classify_kalshi_series({"ticker": ticker, "title": "Ethereum range"})
            self.assertEqual(classified["classification"], "excluded_related_product")
            self.assertEqual(classified["comparable"], "false")

        cross_asset = report.classify_kalshi_series({"ticker": "BTCETHRETURN", "title": "BTC vs. ETH performance"})
        self.assertEqual(cross_asset["comparable"], "false")
        self.assertNotEqual(cross_asset["classification"], "irrelevant")

    def test_eth_kalshi_candidate_predicate_rejects_substring_false_positives(self):
        report.configure_asset("eth")
        false_positives = [
            ("KXHEGSETH", "Hegseth Senate Yeas"),
            ("KXTETHERPAUSE", "Tether pause"),
            ("KXBETHELSEAT", "GA Supreme Court: Bethel seat winner?"),
            ("KXYCAITOGETHER", "Altman and Musk on stage together"),
            ("KXMOSTSTREAMEDSOMETHINGBEAUTIFUL", "Most streamed song on Miley Cyrus's Something Beautiful"),
        ]
        for ticker, title in false_positives:
            classified = report.classify_kalshi_series({"ticker": ticker, "title": title})
            self.assertEqual(classified["classification"], "irrelevant")

    def test_kalshi_half_open_filter_excludes_month_end_boundary(self):
        before_boundary = report.normalize_kalshi_market(
            {
                "ticker": "KXBTC15M-26FEB282345-45",
                "event_ticker": "KXBTC15M-26FEB282345",
                "close_time": "2026-02-28T23:45:00Z",
                "status": "finalized",
                "market_type": "binary",
                "strike_type": "greater_or_equal",
                "title": "BTC price up in next 15 mins?",
                "rules_primary": "If the simple average is at least the simple average, resolves yes.",
                "volume_fp": "10.00",
            },
            "15min",
            "KXBTC15M",
        )
        self.assertIsNotNone(before_boundary)
        row, _ = before_boundary
        self.assertEqual(row["month_utc"], "2026-02")

        at_global_end = report.normalize_kalshi_market(
            {
                "ticker": "KXBTC15M-26MAY010000-00",
                "event_ticker": "KXBTC15M-26MAY010000",
                "close_time": "2026-05-01T00:00:00Z",
                "status": "finalized",
                "market_type": "binary",
                "strike_type": "greater_or_equal",
                "title": "BTC price up in next 15 mins?",
                "rules_primary": "If the simple average is at least the simple average, resolves yes.",
                "volume_fp": "10.00",
            },
            "15min",
            "KXBTC15M",
        )
        self.assertIsNone(at_global_end)

    def test_kalshi_directional_market_accepts_legacy_target_title(self):
        self.assertTrue(
            report.is_kalshi_directional_market(
                {
                    "title": "BTC 15 min \u00b7 $70,054.23 target",
                    "market_type": "binary",
                    "strike_type": "greater_or_equal",
                },
                "KXBTC15M",
            )
        )

    def test_eth_kalshi_directional_market_accepts_exact_target_title(self):
        report.configure_asset("eth")
        self.assertTrue(
            report.is_kalshi_directional_market(
                {
                    "title": "ETH 15M price up down",
                    "market_type": "binary",
                    "strike_type": "greater_or_equal",
                },
                "KXETH15M",
            )
        )

    def test_kalshi_window_routes_split_on_cutoff(self):
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        end = datetime(2026, 4, 1, tzinfo=timezone.utc)
        cutoff = datetime(2026, 3, 15, tzinfo=timezone.utc)
        self.assertEqual(
            report.kalshi_window_routes(start, end, cutoff),
            [
                ("historical", start, cutoff),
                ("live", cutoff, end),
            ],
        )

    def test_kalshi_live_fetch_rejects_repeated_cursor(self):
        calls = []

        def fake_kalshi_get(path, params=None):
            calls.append((path, params or {}))
            return {
                "cursor": "same-cursor",
                "markets": [
                    {
                        "ticker": f"market-{len(calls)}",
                        "close_time": "2026-04-01T00:15:00Z",
                    }
                ],
            }

        original = report.kalshi_get
        report.kalshi_get = fake_kalshi_get
        try:
            with self.assertRaises(report.FetchError):
                report.fetch_kalshi_live_markets(
                    "KXBTC15M",
                    datetime(2026, 4, 1, tzinfo=timezone.utc),
                    datetime(2026, 5, 1, tzinfo=timezone.utc),
                )
        finally:
            report.kalshi_get = original

    def test_aggregation_uses_decimal_and_rejects_duplicate_platform_market_ids(self):
        rows = [
            {
                "platform": "polymarket",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "comparable": "true",
                "platform_market_id": "0x1",
                "volume": "0.1",
            },
            {
                "platform": "kalshi",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "15min",
                "comparable": "true",
                "platform_market_id": "KXBTC15M-1",
                "volume": "0.2",
            },
            {
                "platform": "kalshi",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "15min",
                "comparable": "false",
                "platform_market_id": "KXBTCD-1",
                "volume": "1000",
            },
        ]
        platform_daily = report.aggregate_platform_daily(rows)
        combined_daily = report.aggregate_combined_daily(rows)
        combined_monthly = report.aggregate_combined_monthly(rows)
        kalshi_platform_daily = [
            row for row in platform_daily if row["platform"] == "kalshi" and row["market_type"] == "15min"
        ][0]
        self.assertEqual(kalshi_platform_daily["total_volume"], "1000.2")
        self.assertEqual(combined_daily[0]["total_volume"], "0.2")
        self.assertEqual(combined_monthly[0]["total_volume"], "0.2")
        with self.assertRaises(ValueError):
            report.validate_unique_platform_market_ids(rows + [dict(rows[0])])

    def test_wallet_market_daily_and_monthly_aggregation_dedupes_wallets_within_bucket(self):
        detail_rows = [
            {
                "platform": "polymarket",
                "month_utc": "2026-02",
                "date_utc": "2026-02-01",
                "market_type": "hourly",
                "platform_series": "btc-up-or-down-hourly",
                "platform_market_id": "0x" + "a" * 64,
                "title": "Bitcoin Up or Down - February 1",
                "volume": "100",
            },
            {
                "platform": "polymarket",
                "month_utc": "2026-02",
                "date_utc": "2026-02-01",
                "market_type": "hourly",
                "platform_series": "btc-up-or-down-hourly",
                "platform_market_id": "0x" + "b" * 64,
                "title": "Bitcoin Up or Down - February 1 1PM",
                "volume": "50",
            },
        ]
        trades_by_market = {
            "0x" + "a" * 64: {
                "participant_trades": [
                    {"wallet": "0x1", "size": Decimal("5"), "transaction_hash": "0xpaired", "raw": {}},
                    {"wallet": "0x2", "size": Decimal("5"), "transaction_hash": "0xpaired", "raw": {}},
                    {"wallet": "0x3", "size": Decimal("10"), "transaction_hash": "0xsolo", "raw": {}},
                ],
                "participant_status": "complete",
                "taker_trades": [
                    {"wallet": "0x2", "size": Decimal("5"), "transaction_hash": "0xpaired", "raw": {}},
                    {"wallet": "0x3", "size": Decimal("10"), "transaction_hash": "0xsolo", "raw": {}},
                ],
                "taker_status": "truncated",
            },
            "0x" + "b" * 64: {
                "participant_trades": [
                    {"wallet": "0x1", "size": Decimal("5"), "transaction_hash": "0xother", "raw": {}},
                    {"wallet": "0x4", "size": Decimal("15"), "transaction_hash": "0xother", "raw": {}},
                ],
                "participant_status": "complete",
                "taker_trades": [
                    {"wallet": "0x4", "size": Decimal("15"), "transaction_hash": "0xother", "raw": {}},
                ],
                "taker_status": "complete",
            },
        }
        market_rows, daily_rows, monthly_rows = report.aggregate_wallet_activity(detail_rows, trades_by_market)
        self.assertEqual(market_rows[0]["active_wallet_count"], "3")
        self.assertEqual(market_rows[0]["participant_trade_count"], "3")
        self.assertEqual(market_rows[0]["taker_trade_count"], "2")
        self.assertEqual(market_rows[0]["trade_history_volume"], "15")
        self.assertEqual(market_rows[0]["top10_wallet_volume"], "15")
        self.assertEqual(market_rows[0]["top10_wallet_volume_share"], "1")
        self.assertEqual(market_rows[0]["platform_reported_volume"], "100")
        self.assertEqual(market_rows[0]["volume_gap"], "85")
        self.assertEqual(market_rows[0]["data_status"], "truncated")
        self.assertEqual(daily_rows[0]["active_wallet_count"], "4")
        self.assertEqual(daily_rows[0]["participant_trade_count"], "5")
        self.assertEqual(daily_rows[0]["taker_trade_count"], "3")
        self.assertEqual(daily_rows[0]["trade_history_volume"], "30")
        self.assertEqual(monthly_rows[0]["active_wallet_count"], "4")
        self.assertEqual(monthly_rows[0]["trade_history_volume"], "30")

    def test_load_cached_detail_rows_reads_current_platform_detail_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_detail_path = report.DETAIL_PATH
            report.DETAIL_PATH = Path(tmpdir) / "btc_updown_market_detail_by_platform_2026-02_2026-04.csv"
            report.DETAIL_PATH.write_text(
                ",".join(report.DETAIL_FIELDS)
                + "\n"
                + ",".join(
                    [
                        "polymarket",
                        "2026-02",
                        "2026-02-01",
                        "hourly",
                        "true",
                        "btc-up-or-down-hourly",
                        "event-1",
                        "0x" + "a" * 64,
                        "Bitcoin Up or Down - February 1",
                        "market-1",
                        "2026-02-01T01:00:00Z",
                        "closed",
                        "",
                        "100",
                        "market.volumeClob",
                        "100",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                cached = report.load_cached_detail_rows()
            finally:
                report.DETAIL_PATH = original_detail_path

        self.assertIsNotNone(cached)
        detail_rows, raw_records, anomalies = cached
        self.assertEqual(detail_rows[0]["platform"], "polymarket")
        self.assertEqual(detail_rows[0]["platform_market_id"], "0x" + "a" * 64)
        self.assertEqual(raw_records[0]["platform"], "polymarket")
        self.assertIn("Loaded platform detail from local cache", anomalies[0])

    def test_kalshi_wallet_rows_are_not_available(self):
        platform_rows = [
            {
                "platform": "kalshi",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "15min",
                "total_volume": "123",
            }
        ]
        rows = report.build_kalshi_wallet_unavailable_rows(platform_rows)
        self.assertEqual(rows[0]["platform"], "kalshi")
        self.assertEqual(rows[0]["date_utc"], "2026-02-01")
        self.assertEqual(rows[0]["data_status"], "not_available")
        self.assertEqual(rows[0]["unavailable_reason"], "kalshi_public_trades_have_no_wallet_identifier")

    def test_polymarket_status_only_wallet_rows_are_not_generated(self):
        report.configure_asset("btc")
        platform_rows = [
            {
                "platform": "polymarket",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "total_volume": "123",
            }
        ]
        detail_rows = [
            {
                "platform": "polymarket",
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "platform_series": "btc-up-or-down-hourly",
                "platform_market_id": "0x" + "a" * 64,
                "title": "Bitcoin Up or Down - February 1",
                "volume": "123",
            }
        ]

        daily_rows = report.build_polymarket_wallet_not_generated_rows(platform_rows)
        market_rows = report.build_polymarket_wallet_market_not_generated_rows(detail_rows)

        self.assertEqual(daily_rows[0]["platform"], "polymarket")
        self.assertEqual(daily_rows[0]["date_utc"], "2026-02-01")
        self.assertEqual(daily_rows[0]["active_wallet_count"], "")
        self.assertEqual(daily_rows[0]["platform_reported_volume"], "123")
        self.assertEqual(daily_rows[0]["data_status"], "not_generated")
        self.assertEqual(
            daily_rows[0]["unavailable_reason"],
            "polymarket_trade_history_generation_not_run",
        )
        self.assertEqual(market_rows[0]["platform_market_id"], "0x" + "a" * 64)
        self.assertEqual(market_rows[0]["data_status"], "not_generated")

    def test_generate_report_status_only_skips_polymarket_trade_fetch(self):
        detail_rows = [
            {
                "platform": "polymarket",
                "month_utc": "2026-02",
                "date_utc": "2026-02-01",
                "market_type": "hourly",
                "comparable": "true",
                "platform_series": "btc-up-or-down-hourly",
                "platform_event_id": "event-1",
                "platform_market_id": "0x" + "a" * 64,
                "title": "Bitcoin Up or Down - February 1",
                "subtitle": "",
                "end_time_utc": "2026-02-01T01:00:00Z",
                "status": "closed",
                "result": "",
                "volume": "123",
                "volume_source": "market.volumeClob",
                "raw_volume": "123",
            }
        ]

        def fail_collect(_detail_rows):
            raise AssertionError("status-only must not fetch Polymarket wallet trades")

        with tempfile.TemporaryDirectory() as tmpdir:
            original_root = report.ROOT
            original_collect = report.collect_polymarket_wallet_activity
            original_load_cached = report.load_cached_detail_rows
            report.ROOT = Path(tmpdir)
            report.configure_asset("btc")
            report.collect_polymarket_wallet_activity = fail_collect
            report.load_cached_detail_rows = lambda: (detail_rows, [{"platform": "polymarket"}], [])
            try:
                generated = report.generate_report(wallet_mode="status-only")
                with gzip.open(report.POLYMARKET_TRADES_RAW_PATH, "rt", encoding="utf-8") as input_file:
                    raw_wallet_lines = input_file.readlines()
            finally:
                report.ROOT = original_root
                report.collect_polymarket_wallet_activity = original_collect
                report.load_cached_detail_rows = original_load_cached
                report.configure_asset("btc")

        wallet_market_rows = generated[5]
        wallet_daily_rows = generated[6]
        self.assertEqual(wallet_market_rows[0]["data_status"], "not_generated")
        self.assertEqual(wallet_daily_rows[0]["data_status"], "not_generated")
        self.assertEqual(raw_wallet_lines, [])

    def test_collect_polymarket_wallet_positions_streams_raw_and_aggregates(self):
        detail_rows = [
            {
                "platform": "polymarket",
                "month_utc": "2026-02",
                "date_utc": "2026-02-01",
                "market_type": "hourly",
                "platform_series": "btc-up-or-down-hourly",
                "platform_market_id": "0x" + "a" * 64,
                "title": "Bitcoin Up or Down - February 1",
                "volume": "20",
            }
        ]

        def fake_fetch(condition_id):
            return {
                "positions": [
                    {
                        "wallet": "0x1",
                        "condition_id": condition_id,
                        "asset": "up",
                        "outcome_index": "0",
                        "total_bought": Decimal("5"),
                        "raw": {"conditionId": condition_id},
                    },
                    {
                        "wallet": "0x2",
                        "condition_id": condition_id,
                        "asset": "down",
                        "outcome_index": "1",
                        "total_bought": Decimal("10"),
                        "raw": {"conditionId": condition_id},
                    },
                ],
                "status": "positions_complete",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = report.POLYMARKET_TRADES_RAW_PATH
            original_fetch = report.fetch_polymarket_positions_for_market
            original_workers = report.POLYMARKET_POSITIONS_WORKERS
            report.POLYMARKET_TRADES_RAW_PATH = Path(tmpdir) / "wallet_raw.jsonl.gz"
            report.fetch_polymarket_positions_for_market = fake_fetch
            report.POLYMARKET_POSITIONS_WORKERS = 1
            try:
                market_rows, daily_rows, monthly_rows, anomalies = report.collect_polymarket_wallet_positions(
                    detail_rows
                )
            finally:
                report.POLYMARKET_TRADES_RAW_PATH = original_path
                report.fetch_polymarket_positions_for_market = original_fetch
                report.POLYMARKET_POSITIONS_WORKERS = original_workers

            with gzip.open(Path(tmpdir) / "wallet_raw.jsonl.gz", "rt", encoding="utf-8") as input_file:
                raw_records = [json.loads(line) for line in input_file]

        self.assertEqual(market_rows[0]["active_wallet_count"], "2")
        self.assertEqual(market_rows[0]["participant_trade_count"], "2")
        self.assertEqual(market_rows[0]["trade_history_volume"], "15")
        self.assertEqual(market_rows[0]["top10_wallet_volume_share"], "1")
        self.assertEqual(market_rows[0]["data_status"], "positions_complete")
        self.assertEqual(daily_rows[0]["active_wallet_count"], "2")
        self.assertEqual(monthly_rows[0]["trade_history_volume"], "15")
        self.assertEqual(raw_records[0]["source"], "market_positions")
        self.assertEqual(anomalies, [])

    def test_collect_polymarket_wallet_activity_streams_raw_and_aggregates(self):
        detail_rows = [
            {
                "platform": "polymarket",
                "month_utc": "2026-02",
                "date_utc": "2026-02-01",
                "market_type": "hourly",
                "platform_series": "btc-up-or-down-hourly",
                "platform_market_id": "0x" + "a" * 64,
                "title": "Bitcoin Up or Down - February 1",
                "volume": "10",
            }
        ]

        def fake_fetch(condition_ids):
            condition_id = condition_ids[0]
            trade = {"wallet": "0x1", "size": Decimal("3"), "raw": {"conditionId": condition_id}}
            return (
                {condition_id: {"trades": [trade], "status": "complete"}},
                {condition_id: {"trades": [trade], "status": "complete"}},
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = report.POLYMARKET_TRADES_RAW_PATH
            original_fetch = report.fetch_polymarket_trade_batch
            report.POLYMARKET_TRADES_RAW_PATH = Path(tmpdir) / "wallet_raw.jsonl.gz"
            report.fetch_polymarket_trade_batch = fake_fetch
            try:
                market_rows, daily_rows, monthly_rows, anomalies = report.collect_polymarket_wallet_activity(
                    detail_rows
                )
            finally:
                report.POLYMARKET_TRADES_RAW_PATH = original_path
                report.fetch_polymarket_trade_batch = original_fetch

            with gzip.open(Path(tmpdir) / "wallet_raw.jsonl.gz", "rt", encoding="utf-8") as input_file:
                raw_records = [json.loads(line) for line in input_file]

        self.assertEqual(market_rows[0]["active_wallet_count"], "1")
        self.assertEqual(daily_rows[0]["trade_history_volume"], "3")
        self.assertEqual(monthly_rows[0]["top10_wallet_volume_share"], "1")
        self.assertEqual(len(raw_records), 2)
        self.assertEqual(raw_records[0]["platform_market_id"], "0x" + "a" * 64)
        self.assertEqual(anomalies, [])

    def test_markdown_report_includes_wallet_activity_section(self):
        report.configure_asset("btc")
        text = report.build_markdown_report(
            detail_rows=[],
            platform_daily_rows=[],
            platform_monthly_rows=[],
            combined_daily_rows=[],
            combined_monthly_rows=[],
            wallet_daily_rows=[
                {
                    "asset": "btc",
                    "platform": "polymarket",
                    "date_utc": "2026-02-01",
                    "month_utc": "2026-02",
                    "market_type": "hourly",
                    "active_wallet_count": "3",
                    "participant_trade_count": "6",
                    "taker_trade_count": "4",
                    "trade_history_volume": "10",
                    "top10_wallet_volume": "9",
                    "top10_wallet_volume_share": "0.9",
                    "platform_reported_volume": "11",
                    "volume_gap": "1",
                    "volume_gap_pct": "0.0909",
                    "data_status": "complete",
                    "unavailable_reason": "",
                },
            ],
            wallet_monthly_rows=[
                {
                    "asset": "btc",
                    "platform": "polymarket",
                    "month_utc": "2026-02",
                    "market_type": "hourly",
                    "active_wallet_count": "3",
                    "participant_trade_count": "6",
                    "taker_trade_count": "4",
                    "trade_history_volume": "10",
                    "top10_wallet_volume": "9",
                    "top10_wallet_volume_share": "0.9",
                    "platform_reported_volume": "11",
                    "volume_gap": "1",
                    "volume_gap_pct": "0.0909",
                    "data_status": "complete",
                    "unavailable_reason": "",
                },
                {
                    "asset": "btc",
                    "platform": "kalshi",
                    "month_utc": "2026-02",
                    "market_type": "15min",
                    "active_wallet_count": "",
                    "participant_trade_count": "",
                    "taker_trade_count": "",
                    "trade_history_volume": "",
                    "top10_wallet_volume": "",
                    "top10_wallet_volume_share": "",
                    "platform_reported_volume": "5",
                    "volume_gap": "",
                    "volume_gap_pct": "",
                    "data_status": "not_available",
                    "unavailable_reason": "kalshi_public_trades_have_no_wallet_identifier",
                },
            ],
            anomalies=[],
            kalshi_cutoff=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        self.assertIn("## Wallet Activity Daily", text)
        self.assertIn("## Wallet Activity Monthly", text)
        self.assertIn("| polymarket | 2026-02-01 | hourly | 3 | 0.9 | complete |", text)
        self.assertIn("| polymarket | 2026-02 | hourly | 3 | 0.9 | complete |", text)
        self.assertIn("kalshi_public_trades_have_no_wallet_identifier", text)


if __name__ == "__main__":
    unittest.main()
