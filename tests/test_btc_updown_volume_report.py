import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "btc_updown_volume_report.py"
spec = importlib.util.spec_from_file_location("btc_updown_volume_report", SCRIPT_PATH)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class BtcUpdownVolumeReportTests(unittest.TestCase):
    def test_series_slug_maps_market_type_including_4hour(self):
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-5m"), "5min")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-15m"), "15min")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-hourly"), "hourly")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-4h"), "4hour")
        self.assertEqual(report.market_type_for_series_slug("btc-up-or-down-daily"), "daily")

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


if __name__ == "__main__":
    unittest.main()
