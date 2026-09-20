import contextlib
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_updown_yesterday as workflow


class YesterdayTests(unittest.TestCase):
    def setUp(self):
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def test_utc_year_boundary_and_midnight_during_collection(self):
        calls = []
        with patch.object(workflow, "datetime") as clock, \
                patch.object(workflow.daily, "main") as collect, \
                patch.object(workflow.poster, "main") as poster:
            clock.now.return_value = datetime(2027, 1, 1, 23, 59, tzinfo=timezone.utc)

            def downloaded(argv):
                calls.append(("collect", argv))
                clock.now.return_value = datetime(2027, 1, 2, tzinfo=timezone.utc)
                return 0

            collect.side_effect = downloaded
            poster.side_effect = lambda argv: calls.append(("poster", argv)) or 0
            result = workflow.main([
                "--proxy", "socks5h://127.0.0.1:12345",
                "--db", "local data/shared.sqlite3", "--output-dir", "local output",
            ])
            self.assertEqual(result, 0)
            clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(calls, [
            ("collect", ["collect", "--start-date", "2026-12-31", "--end-date", "2027-01-01",
                         "--proxy", "socks5h://127.0.0.1:12345", "--db", "local data/shared.sqlite3"]),
            ("poster", ["--date", "2026-12-31", "--db", "local data/shared.sqlite3",
                        "--output-dir", "local output"]),
        ])

    def test_incomplete_failed_or_interrupted_collection_never_generates_poster(self):
        for code in (2, 1, 130):
            with self.subTest(code=code), \
                    patch.object(workflow.daily, "main", return_value=code), \
                    patch.object(workflow.poster, "main") as poster:
                self.assertEqual(workflow.main(["--proxy", "http://localhost:12345"]), code)
                poster.assert_not_called()

    def test_poster_result_and_existing_database_default_are_preserved(self):
        with patch.object(workflow.daily, "main", return_value=0) as collect, \
                patch.object(workflow.poster, "main", return_value=2) as poster:
            self.assertEqual(workflow.main([]), 2)
            collection_args = collect.call_args.args[0]
            poster_args = poster.call_args.args[0]
            self.assertEqual(collection_args[collection_args.index("--proxy") + 1],
                             "socks5h://127.0.0.1:20810")
            self.assertEqual(collection_args[-1], str(workflow.daily.core.DEFAULT_DB))
            self.assertEqual(poster_args[3], str(workflow.daily.core.DEFAULT_DB))
            self.assertEqual(poster_args[-1], str(workflow.daily.core.ROOT / "outputs/posters"))

    def test_unhandled_collection_error_does_not_start_poster(self):
        with patch.object(workflow.daily, "main", side_effect=RuntimeError("download failed")), \
                patch.object(workflow.poster, "main") as poster:
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                workflow.main(["--proxy", "http://localhost:12345"])
            poster.assert_not_called()

    def test_keyboard_interrupt_returns_130(self):
        with patch.object(workflow.daily, "main", side_effect=KeyboardInterrupt), \
                patch.object(workflow.poster, "main") as poster:
            self.assertEqual(workflow.main(["--proxy", "http://localhost:12345"]), 130)
            poster.assert_not_called()


if __name__ == "__main__":
    unittest.main()
