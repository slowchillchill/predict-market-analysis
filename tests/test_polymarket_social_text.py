import builtins
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_social_text as text


def example_bundle():
    """Selected published Nov 10 totals; synthetic bins keep this fixture small."""
    all_buy = {"trade_count": 1252487, "shares_exact": "20019872.985133",
               "notional_usdc_exact": "9121356.0710582316566134"}
    bots = {"trade_count": 942009, "shares_exact": "14358425.43782",
            "notional_usdc_exact": "6718852.2412123612294763"}
    # All notional is in the first synthetic bin; percentages are recomputed,
    # not copied from potentially ambiguous legacy '*market_percent' fields.
    for population in (all_buy, bots):
        population["bins"] = [{"lower_cents": lower, "upper_cents": lower + 10,
                               "upper_inclusive": lower == 90,
                               "notional_usdc_exact": population["notional_usdc_exact"] if lower == 0 else "0"}
                              for lower in range(0, 100, 10)]
    return {"schema_version": 1, "status": "complete", "kind": "daily",
            "target_utc": "2026-11-10", "start_date_utc": "2026-11-10",
            "end_date_utc_exclusive": "2026-11-11",
            "source_versions": {"2026-11-10": {"dataset": "owner/raw-day", "version": 3}},
            "payload": {"date_basis": "market_end_utc",
                        "current": {"total_markets": 3298, "unique_wallets": 11123,
                                    "volume_micro_usdc": 10561633463775,
                                    "suspected_bot_wallets": 356,
                                    "wallets_meeting_daily_frequency_rule": 313},
                        "bot_rule": {"max_mean_interval_seconds": 60, "min_span_minutes": 90}},
            "distribution": {"series_count": 38, "bot_history": {"history_pending": True},
                             "populations": {"all_wallets": all_buy, "suspected_bots": bots}}}


class SocialTextTests(unittest.TestCase):
    def test_daily_values_denominators_and_distinct_platform_layout(self):
        bundle = example_bundle()
        original = copy.deepcopy(bundle)
        copy_out = text.render_text(bundle, {"dataset": "owner/analysis-month", "version": 100})
        self.assertEqual(bundle, original)
        self.assertEqual(set(copy_out), {"x", "linkedin"})
        self.assertEqual(len(copy_out["x"]["posts"]), 3)
        self.assertTrue(all(text.x_weighted_length(post) <= 280 for post in copy_out["x"]["posts"]))
        first, second, third = copy_out["x"]["posts"]
        self.assertIn("Markets | 3,298", first)
        self.assertIn("10,561,633.46", first)
        self.assertIn("owner/analysis-month/versions/100", first)
        self.assertIn("1,252,487 | 942,009", second)
        self.assertIn("9,121,356.07 | 6,718,852.24", second)
        self.assertIn("75.21%", third)
        self.assertIn("71.72%", third)
        self.assertIn("73.66%", third)
        body = copy_out["linkedin"]["body"]
        self.assertNotIn("|", body)
        self.assertIn("Active historically flagged addresses: 356", body)
        self.assertIn("Meeting the screen on this date: 313", body)
        self.assertIn("[0,10) cents: 100.00% / 73.66%", body)
        self.assertIn("total all-BUY Up-equivalent notional", body)
        self.assertIn("not those wallets' own spending", body)
        self.assertIn("Historical coverage is incomplete.", body)
        self.assertNotIn("previous day", body)
        self.assertLessEqual(text.utf16_length(body), 3000)

    def test_daily_weekly_monthly_use_actual_dates_and_series_count(self):
        for kind, start, end in (("daily", "2026-11-10", "2026-11-11"),
                                 ("weekly", "2026-11-02", "2026-11-09"),
                                 ("monthly", "2026-10-01", "2026-11-01")):
            with self.subTest(kind=kind):
                bundle = example_bundle()
                bundle.update(kind=kind, start_date_utc=start, end_date_utc_exclusive=end)
                bundle["distribution"]["series_count"] = 2
                if kind != "daily":
                    bundle["payload"]["current"].pop("wallets_meeting_daily_frequency_rule")
                reference = None if kind == "daily" else {"dataset": "owner/analysis-period", "version": 100}
                rendered = text.render_text(bundle, reference)
                self.assertIn("Official series: 2", rendered["linkedin"]["body"])
                self.assertIn("owner/raw-day/versions/3" if kind == "daily" else "owner/analysis-period/versions/100",
                              rendered["x"]["posts"][0])
                if kind != "daily":
                    self.assertIn("to 2026-" + ("11-08" if kind == "weekly" else "10-31"), rendered["linkedin"]["body"])
                    self.assertNotIn("Meeting the screen on this date", rendered["linkedin"]["body"])

    def test_period_report_cannot_point_to_only_one_raw_day_as_its_source(self):
        for kind, start, end in (("weekly", "2026-11-02", "2026-11-09"),
                                 ("monthly", "2026-10-01", "2026-11-01")):
            with self.subTest(kind=kind):
                bundle = example_bundle()
                bundle.update(kind=kind, start_date_utc=start, end_date_utc_exclusive=end)
                for reference in (None, {"dataset": "owner/analysis"}, {"version": 100}):
                    with self.assertRaisesRegex(ValueError, "--source-dataset and --source-version"):
                        text.render_text(bundle, reference)
                bundle["source_reference"] = {"dataset": "owner/analysis", "version": 100}
                rendered = text.render_text(bundle)
                self.assertIn("owner/analysis/versions/100", rendered["x"]["posts"][0])
                self.assertNotIn("owner/raw-day", rendered["linkedin"]["body"])

    def test_daily_fallback_uses_target_date_instead_of_first_source_date(self):
        bundle = example_bundle()
        bundle["source_versions"]["2026-11-09"] = {"dataset": "owner/previous-day", "version": 2}
        rendered = text.render_text(bundle)
        self.assertIn("owner/raw-day/versions/3", rendered["x"]["posts"][0])
        self.assertNotIn("owner/previous-day", rendered["linkedin"]["body"])
        del bundle["source_versions"]["2026-11-10"]
        with self.assertRaisesRegex(ValueError, "fixed dataset version"):
            text.render_text(bundle)

    def test_zero_buy_denominators_and_unknown_market_volume_remain_na(self):
        bundle = example_bundle()
        bundle["payload"]["current"]["volume_micro_usdc"] = None
        for population in bundle["distribution"]["populations"].values():
            population.update(trade_count=0, shares_exact="0", notional_usdc_exact="0")
            for row in population["bins"]:
                row["notional_usdc_exact"] = "0"
        rendered = text.render_text(bundle)
        body = rendered["linkedin"]["body"]
        self.assertIn("Market volume, counted once (USDC): N/A", body)
        self.assertIn("Events: 0 (N/A of all BUY events)", body)
        self.assertIn("[90,100] cents: N/A / N/A", body)
        self.assertIn("Total: N/A / N/A", body)

    def test_larger_period_totals_keep_all_fields_and_fit_x_limit(self):
        bundle = example_bundle()
        current = bundle["payload"]["current"]
        current.update(total_markets=10**18, unique_wallets=10**17,
                       volume_micro_usdc=10**36, suspected_bot_wallets=10**16,
                       wallets_meeting_daily_frequency_rule=10**15)
        for population in bundle["distribution"]["populations"].values():
            population.update(trade_count=10**17, shares_exact="1e25", notional_usdc_exact="1e24")
            population["bins"][0]["notional_usdc_exact"] = "1e24"
        rendered = text.render_text(bundle)
        combined = "\n".join(rendered["x"]["posts"])
        self.assertIn("T", combined)
        self.assertIn("Shares |", combined)
        self.assertIn("not wallet spending", combined)
        self.assertTrue(all(text.x_weighted_length(post) <= 280 for post in rendered["x"]["posts"]))

    def test_text_does_not_import_image_renderers_or_open_databases(self):
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == "PIL" or name.startswith("polymarket_updown_poster") or name == "sqlite3":
                raise AssertionError("Text renderer loaded an image/database dependency")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded_import):
            self.assertIn("linkedin", text.render_text(example_bundle()))

    def test_fixed_source_is_required_and_completed_history_is_not_called_pending(self):
        bundle = example_bundle()
        bundle["distribution"]["bot_history"]["history_pending"] = False
        rendered = text.render_text(bundle)
        self.assertNotIn("history incomplete", "\n".join(rendered["x"]["posts"]))
        self.assertNotIn("Historical coverage is incomplete.", rendered["linkedin"]["body"])
        bundle["source_versions"] = {}
        with self.assertRaisesRegex(ValueError, "fixed dataset version"):
            text.render_text(bundle)
        with self.assertRaisesRegex(ValueError, "positive"):
            text.render_text(bundle, {"dataset": "owner/analysis", "version": 0})

    def test_incomplete_reports_and_non_ascii_x_text_are_rejected(self):
        bundle = example_bundle()
        bundle["status"] = "incomplete"
        with self.assertRaisesRegex(ValueError, "completed"):
            text.render_text(bundle)
        with self.assertRaisesRegex(ValueError, "ASCII"):
            text.x_weighted_length("中文")
        self.assertEqual(text.x_weighted_length("source https://example.com/a/very/long/path"), 30)
        self.assertEqual(text.utf16_length("A😀"), 3)

    def test_price_bins_cannot_be_silently_dropped_or_misaligned(self):
        bundle = example_bundle()
        bundle["distribution"]["populations"]["suspected_bots"]["bins"].pop()
        with self.assertRaisesRegex(ValueError, "ten bins"):
            text.render_text(bundle)
        bundle = example_bundle()
        bundle["distribution"]["populations"]["suspected_bots"]["bins"][0]["lower_cents"] = 1
        with self.assertRaisesRegex(ValueError, "differ between populations"):
            text.render_text(bundle)

    def test_missing_history_is_unknown_not_claimed_complete_or_incomplete(self):
        bundle = example_bundle()
        bundle["distribution"]["bot_history"] = {}
        rendered = text.render_text(bundle)
        self.assertIn("history unverified", "\n".join(rendered["x"]["posts"]))
        self.assertIn("Historical coverage is not specified.", rendered["linkedin"]["body"])
        self.assertNotIn("Historical coverage is incomplete.", rendered["linkedin"]["body"])


if __name__ == "__main__":
    unittest.main()
