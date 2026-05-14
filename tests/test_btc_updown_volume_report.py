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
        row, raw = report.normalize_event(
            event,
            market_type="hourly",
            series_slug="btc-up-or-down-hourly",
            month_utc="2026-02",
            window_start=datetime(2026, 2, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(row["date_utc"], "2026-02-01")
        self.assertEqual(row["market_type"], "hourly")
        self.assertEqual(row["condition_id"], "0xabc")
        self.assertEqual(row["volume"], "12.345")
        self.assertEqual(row["volume_source"], "market.volumeClob")
        self.assertEqual(raw["event"]["id"], "event-1")

    def test_aggregation_uses_decimal_and_rejects_duplicate_condition_ids(self):
        rows = [
            {
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "condition_id": "0x1",
                "volume": "0.1",
            },
            {
                "date_utc": "2026-02-01",
                "month_utc": "2026-02",
                "market_type": "hourly",
                "condition_id": "0x2",
                "volume": "0.2",
            },
        ]
        daily = report.aggregate_daily(rows)
        monthly = report.aggregate_monthly(daily)
        self.assertEqual(daily[0]["total_volume"], "0.3")
        self.assertEqual(monthly[0]["total_volume"], "0.3")
        with self.assertRaises(ValueError):
            report.validate_unique_condition_ids(rows + [dict(rows[0])])


if __name__ == "__main__":
    unittest.main()
