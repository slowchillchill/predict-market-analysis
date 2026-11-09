import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP, localcontext
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_bot_price_poster as poster


ALL_COUNTS = [87410, 60644, 56477, 54759, 74191, 100323, 74212, 64752, 71458, 107232]
BOT_COUNTS = [53376, 43157, 40844, 39400, 51536, 64973, 55302, 48038, 52305, 66618]
ALL_SHARES = ["2219239.488611", "718036.328569", "657276.591227", "617585.855013",
              "879254.272361", "1226537.100264", "860025.291025", "745626.582334",
              "822216.415752", "2295644.757889"]
BOT_SHARES = ["932159.426444", "484513.280805", "452131.617453", "426568.437022",
              "548777.555503", "725753.361047", "604816.840477", "523508.644759",
              "575962.061986", "1142074.768370"]
ALL_NOTIONALS = ["54780.9546692500359422", "102881.1824041012580157", "160638.3710929621227621",
                 "213602.4559088443348650", "396782.3673770875092146", "659646.9701990887856787",
                 "554119.6798141248223487", "554412.9262070365748107", "694524.5073339421470745",
                 "2227254.5326618013761871"]
BOT_NOTIONALS = ["33282.6375121188358973", "69915.1798910154280483", "110495.2251720506856313",
                 "147564.4176899468045473", "245867.7454190785451188", "393617.8092819311765723",
                 "389865.6736681325558988", "389547.1542820373285975", "486791.9829139728136278",
                 "1099292.9547058711551254"]


def _percent(value, denominator):
    if not denominator:
        return None
    with localcontext() as context:
        context.prec = 100
        return f"{(Decimal(value) * 100 / Decimal(denominator)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def example_data(*, empty=False, all_series=False):
    with localcontext() as context:
        context.prec = 100
        market_total = sum(map(Decimal, ALL_NOTIONALS)) if not empty else Decimal(0)
        populations = {}
        for name, counts, shares, notionals in (
            ("all_wallets", ALL_COUNTS, ALL_SHARES, ALL_NOTIONALS),
            ("suspected_bots", BOT_COUNTS, BOT_SHARES, BOT_NOTIONALS),
        ):
            if empty:
                counts, shares, notionals = [0] * 10, ["0"] * 10, ["0"] * 10
            total_count = sum(counts)
            total_notional = sum(map(Decimal, notionals))
            populations[name] = {
                "trade_count": total_count,
                "shares_exact": str(sum(map(Decimal, shares))),
                "notional_usdc_exact": str(total_notional),
                "notional_share_of_market_percent": _percent(total_notional, market_total),
                "bins": [
                    {"lower_cents": i * 10, "upper_cents": (i + 1) * 10,
                     "lower_inclusive": True, "upper_inclusive": i == 9,
                     "interval_cents": f"[{i * 10}, {(i + 1) * 10}{']' if i == 9 else ')'}",
                     "trade_count": count, "trade_share_percent": _percent(count, total_count),
                     "shares_exact": shares[i], "notional_usdc_exact": notionals[i],
                     "notional_share_of_market_percent": _percent(notionals[i], market_total)}
                    for i, count in enumerate(counts)
                ],
            }
    return {
        "schema_version": 2, "date_utc": "2026-11-07", "date_basis": "market_end_utc",
        "series_slug": None if all_series else "btc-up-or-down-5m", "scope": "buy_participation",
        "market_scope": "all_crypto_updown_series" if all_series else "single_series",
        "series_count": 38 if all_series else 1,
        "total_markets": 3295 if all_series else 288,
        "all_wallet_count": (10271 if all_series else 7538) if not empty else 0,
        "suspected_bot_wallets": (331 if all_series else 201) if not empty else 0,
        "trade_share_denominator": "population_trade_count",
        "notional_share_denominator": "all_wallets_buy_notional",
        "populations": populations,
    }


def assert_distribution_headers(test, boxes):
    """Check each full heading in its own column, including wrapped lines."""
    for panel_top in (326, 788):
        for heading, (left, width) in zip(poster.copy.DISTRIBUTION_COLUMNS, poster.COLUMNS):
            lines = [box for box in boxes
                     if panel_top + 50 <= box['bounds'][1] / poster.style.SCALE < panel_top + 115
                     and left - 1 <= box['bounds'][0] / poster.style.SCALE
                     and box['bounds'][2] / poster.style.SCALE <= left + width + 1]
            lines.sort(key=lambda box: box['bounds'][1])
            test.assertEqual(' '.join(box['text'] for box in lines), heading)


class BotPricePosterTests(unittest.TestCase):
    def test_both_buy_panels_have_explicit_metric_labels_and_reference_values(self):
        image, boxes = poster.render_poster(example_data())
        self.assertEqual(image.size, (1600, 2000))
        self.assertEqual(image.mode, "RGB")
        texts = [box["text"] for box in boxes]
        for title in (poster.copy.GROUP_ALL, poster.copy.GROUP_BOTS):
            self.assertIn(title, texts)
        normalized = ' '.join(texts)
        assert_distribution_headers(self, boxes)
        self.assertNotIn("RECORDS", texts)
        self.assertNotIn("SHARE", texts)
        self.assertIn("Nov 7, 2026 · UTC Market End Date", texts)
        self.assertIn("BTC 5M · 1 Series · 288 Markets", texts)
        for value in ("751,458", "515,549", "11,041,442.68", "6,416,265.99",
                      "5,618,643.95", "3,366,240.78", "59.91%"):
            self.assertIn(value, texts)
        self.assertIn('Shares and notional percentages use the corresponding totals for all BUY trades.', normalized)
        self.assertIn('Trade mix uses each group’s total trades.', normalized)

    def test_reference_layout_has_no_clipping_or_overlapping_text(self):
        image, boxes = poster.render_poster(example_data())
        for box in boxes:
            left, top, right, bottom = box["bounds"]
            self.assertGreaterEqual(left, 0, box["text"])
            self.assertGreaterEqual(top, 0, box["text"])
            self.assertLessEqual(right, image.width, box["text"])
            self.assertLessEqual(bottom, image.height, box["text"])
        for a, b in combinations(boxes, 2):
            al, at, ar, ab = a["bounds"]
            bl, bt, br, bb = b["bounds"]
            self.assertFalse(al < br and bl < ar and at < bb and bt < ab,
                             f"Overlapping text: {a['text']} / {b['text']}")

    def test_shares_and_notional_use_exact_fields_and_round_half_up(self):
        self.assertEqual(poster.number_text("123456.785"), "123,456.79")
        self.assertEqual(poster.number_text("42.125"), "42.13")
        data = example_data()
        row = data["populations"]["suspected_bots"]["bins"][0]
        row.update(shares_exact="42.125", notional_usdc_exact="123456.785")
        _, boxes = poster.render_poster(data)
        texts = [box["text"] for box in boxes]
        self.assertIn("42.13", texts)
        self.assertIn("123,456.79", texts)
        # Amount text comes from the exact amount, not the rounded percentage.
        self.assertIn("0.59%", texts)

    def test_all_series_daily_scope_and_two_chart_panels_are_explicit(self):
        _, boxes = poster.render_poster(example_data(all_series=True))
        texts = [box["text"] for box in boxes]
        self.assertIn("07 NOV 2026 · UTC MARKET END DATE", texts)
        self.assertFalse(any('BTC 5M' in text for text in texts))
        self.assertIn("ALL CRYPTO UP/DOWN  ·  38 SERIES  ·  3,295 MARKETS", texts)
        self.assertEqual(texts.count("0–<10"), 2)
        self.assertEqual(texts.count("90–100"), 2)
        self.assertEqual(texts.count("Trades · Group %"), 2)
        self.assertEqual(texts.count("Notional USDC · All BUY %"), 2)

    def test_daily_trade_share_bars_do_not_restore_notional_mix(self):
        _, boxes = poster.render_poster(example_data(all_series=True))
        texts = [box["text"] for box in boxes]
        self.assertNotIn("NOTIONAL MIX", texts)
        self.assertEqual(texts.count("Trade mix · %"), 2)
        self.assertEqual(texts.count("0%"), 2)
        self.assertEqual(texts.count("15%"), 2)
        self.assertIn('Group %: each group’s trades. USDC %: all BUY Up-equivalent notional. Cards: % of all BUY.', texts)

    def test_empty_panels_show_na_and_zero_numeric_totals(self):
        _, boxes = poster.render_poster(example_data(empty=True))
        texts = [box["text"] for box in boxes]
        self.assertEqual(texts.count("N/A"), 66)
        self.assertNotIn("100.00%", texts)
        self.assertEqual(texts.count("0.00"), 44)
        self.assertEqual(texts.count("Total"), 2)

    def test_approved_daily_chart_copy_and_summary_shares_ratio_are_complete(self):
        data = example_data(all_series=True)
        _, boxes = poster.render_poster(data)
        normalized = ' '.join(box['text'] for box in boxes)
        self.assertIn(poster.copy.BRAND, normalized)
        self.assertIn('BUY Price Distribution', normalized)
        self.assertIn('Full per-bin matched shares remain in the accompanying data JSON.', normalized)
        self.assertIn('Shares and notional describe participation, not holdings or wallet spending.', normalized)
        denominator = data['populations']['all_wallets']['shares_exact']
        expected = poster.percentage_text(poster.buy_metrics.share_percent(
            data['populations']['suspected_bots']['shares_exact'], denominator))
        coverage = [box['text'] for box in boxes if box.get('role') == 'coverage']
        self.assertEqual(len(coverage), 3)
        self.assertEqual(coverage[1], expected)
        totals = [box['text'] for box in boxes if box.get('role') == 'exact_total']
        self.assertIn(poster.number_text(denominator), totals)
        self.assertIn(poster.number_text(data['populations']['suspected_bots']['shares_exact']), totals)
        self.assertEqual(poster.buy_metrics.share_percent('14675595.816832','17529049.772681'),'83.72')

    def test_cli_is_read_only_and_only_writes_new_preview_png_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "source.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE sentinel(value TEXT)")
            before = database.read_bytes()
            data = example_data()

            def load(db, day, *, series_slug=None):
                self.assertEqual(str(day), "2026-11-07")
                self.assertEqual(series_slug, "btc-up-or-down-5m")
                self.assertFalse(db.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO sentinel VALUES ('should fail')")
                return data

            with patch.object(poster.distribution, "load_data", side_effect=load), \
                    patch.object(poster.style, "main") as original_main, \
                    patch.object(poster.style, "write_sidecars") as sidecars, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = poster.main(["--date", "2026-11-07", "--db", str(database),
                                      "--series", "btc-up-or-down-5m", "--output-dir", str(root / "preview")])
            self.assertEqual(result, 0)
            self.assertEqual(database.read_bytes(), before)
            original_main.assert_not_called()
            sidecars.assert_not_called()
            files = sorted(path.name for path in (root / "preview").iterdir())
            self.assertEqual(files, ["updown_buy_trade_distribution_btc_5m_2026-11-07.json",
                                     "updown_buy_trade_distribution_btc_5m_2026-11-07.png"])
            self.assertEqual(json.loads((root / "preview" / files[0]).read_text()), data)
            with Image.open(root / "preview" / files[1]) as image:
                self.assertEqual(image.size, (1600, 2000))
                self.assertEqual(image.mode, "RGB")

    def test_default_output_uses_v3_directory_and_passes_full_series_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "source.sqlite3"
            sqlite3.connect(database).close()
            with patch.object(poster.distribution, "load_data", return_value=example_data(all_series=True)) as loader, \
                    patch.object(poster.style, "ROOT", root), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = poster.main(["--date", "2026-11-07", "--db", str(database)])
            self.assertEqual(result, 0)
            self.assertEqual(loader.call_args.kwargs, {"series_slug": None})
            folder = root / "outputs/previews/buy_trade_distribution_all_series_2026-11-07_v3"
            self.assertEqual(sorted(path.name for path in folder.iterdir()),
                             ["updown_buy_trade_distribution_all_series_2026-11-07.json",
                              "updown_buy_trade_distribution_all_series_2026-11-07.png"])

    def test_invalid_or_missing_data_returns_two_without_output(self):
        errors = [ValueError("incomplete"), ValueError("unbalanced group"), sqlite3.OperationalError("busy")]
        for error in errors:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "source.sqlite3"
                sqlite3.connect(database).close()
                output = Path(tmp) / "preview"
                with patch.object(poster.distribution, "load_data", side_effect=error), \
                        contextlib.redirect_stderr(io.StringIO()):
                    result = poster.main(["--date", "2026-11-07", "--db", str(database),
                                          "--output-dir", str(output)])
                self.assertEqual(result, 2)
                self.assertFalse(output.exists())
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            root = Path(tmp)
            result = poster.main(["--date", "2026-11-07", "--db", str(root / "missing.sqlite3"),
                                  "--output-dir", str(root / "preview")])
            self.assertEqual(result, 2)
            self.assertFalse((root / "missing.sqlite3").exists())
            self.assertFalse((root / "preview").exists())


if __name__ == "__main__":
    unittest.main()
