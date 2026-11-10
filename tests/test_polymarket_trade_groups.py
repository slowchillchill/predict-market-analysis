import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_btc5m_daily as core
import polymarket_trade_groups as groups
import polymarket_updown_daily as up

ROOT = Path(__file__).resolve().parents[1]


def trade(tx, wallet="wallet", price=".3", size="2"):
    return {"transaction_hash": tx, "proxy_wallet": wallet, "price": price, "size": size,
            "timestamp": 1, "side": "BUY", "outcome": "Up", "outcome_index": 0, "token_id": "up"}


def page(rows, cursor=None):
    return {"data": rows, "pagination": {"next_cursor": cursor}}


def seed(db, slug):
    with db:
        db.execute("INSERT INTO markets(slug,date_utc,condition_id) VALUES (?,'2026-11-09',?)", (slug, slug))
        db.execute("INSERT INTO collection_progress(market_slug,query_params) VALUES (?,?)",
                   (slug, '{"condition":"'+slug+'"}'))


class TradeGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "work.sqlite3"
        self.db = core.connect_database(self.path)
        self.addCleanup(lambda: self.db.close())
        seed(self.db, "a")
        seed(self.db, "b")

    def table_names(self, db=None):
        return {row[0] for row in (db or self.db).execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def test_page_group_ids_are_integer_first_seen_order_and_scoped_to_market(self):
        core.commit_page(self.db, "a", 1, page([trade("x"), trade("x"), trade("y")], "next"))
        core.commit_page(self.db, "b", 1, page([trade("y")]))
        self.assertEqual(groups.group_column(self.db), "group_id")
        rows = self.db.execute("SELECT market_slug,group_id,typeof(group_id) FROM trades ORDER BY market_slug,page_number,row_number").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("a", 1, "integer"), ("a", 1, "integer"),
                                                       ("a", 2, "integer"), ("b", 1, "integer")])
        mapping = self.db.execute("SELECT market_slug,transaction_hash,group_id FROM pending_group_map ORDER BY transaction_hash").fetchall()
        self.assertEqual([tuple(row) for row in mapping], [("a", "x", 1), ("a", "y", 2)])
        self.assertNotIn("market_volume", self.table_names())

    def test_published_incomplete_copy_resumes_cross_page_group_ids(self):
        core.commit_page(self.db, "a", 1, page([trade("x"), trade("y")], "page-2"))
        snapshot = Path(self.temp.name) / "published.sqlite3"
        with closing(sqlite3.connect(snapshot)) as copy:
            self.db.backup(copy)
        with closing(core.connect_database(snapshot)) as resumed:
            core.commit_page(resumed, "a", 2, page([trade("x"), trade("z"), trade("y")]))
            self.assertEqual([row[0] for row in resumed.execute("SELECT group_id FROM trades WHERE market_slug='a' ORDER BY page_number,row_number")],
                             [1, 2, 1, 3, 2])
            self.assertEqual(resumed.execute("SELECT COUNT(*) FROM pending_group_map WHERE market_slug='a'").fetchone()[0], 0)
            self.assertIsNotNone(resumed.execute("SELECT completed_at FROM collection_progress WHERE market_slug='a'").fetchone()[0])

    def test_failed_page_rolls_back_mapping_rows_and_cursor_together(self):
        bad = trade("y")
        del bad["size"]
        with self.assertRaises(KeyError):
            core.commit_page(self.db, "a", 1, page([trade("x"), bad], "page-2"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pending_group_map").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT committed_page FROM collection_progress WHERE market_slug='a'").fetchone()[0], 0)
        core.commit_page(self.db, "a", 1, page([trade("z")]))
        self.assertEqual(self.db.execute("SELECT group_id FROM trades").fetchone()[0], 1)

    def test_invalid_cursor_resets_only_current_incomplete_market_mapping(self):
        core.commit_page(self.db, "a", 1, page([trade("old")], "expired"))
        core.commit_page(self.db, "b", 1, page([trade("other")], "other-page"))
        client = Mock()
        client.get.side_effect = [core.FetchError("cursor", invalid_cursor=True), page([trade("new")])]
        core.collect_market(self.db, client, "a")
        rows = self.db.execute("SELECT market_slug,group_id FROM trades ORDER BY market_slug").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("a", 1), ("b", 1)])
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT market_slug,transaction_hash FROM pending_group_map")],
                         [("b", "other")])
        self.assertNotIn("cursor", client.get.call_args.args[1])

    def test_group_id_check_refuses_hash_text_in_new_table(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO trades VALUES ('a',1,0,'wallet','0xhash',1,'BUY','Up',0,'up','.3','2',600000)")
        self.db.rollback()

    def test_completed_market_cannot_append_after_mapping_is_removed(self):
        core.commit_page(self.db, "a", 1, page([trade("x")]))
        with self.assertRaisesRegex(ValueError, "整市场重采"):
            core.commit_page(self.db, "a", 2, page([trade("x")]))

    def test_finalize_refuses_partial_then_removes_map_and_erases_hash_bytes(self):
        transaction_hash = "0x" + "abcdef1234567890" * 4
        core.commit_page(self.db, "a", 1, page([trade(transaction_hash)], "page-2"))
        with self.assertRaises(ValueError):
            groups.finalize_database(self.db)
        self.assertIn("pending_group_map", self.table_names())
        core.commit_page(self.db, "a", 2, page([trade(transaction_hash)]))
        core.commit_page(self.db, "b", 1, page([]))
        groups.finalize_database(self.db)
        self.assertNotIn("pending_group_map", self.table_names())
        self.assertNotIn(transaction_hash.encode(), self.path.read_bytes())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 2)

    def test_finalize_refuses_incomplete_expected_market_coverage(self):
        core.commit_page(self.db, "a", 1, page([]))
        core.commit_page(self.db, "b", 1, page([]))
        self.db.execute("CREATE VIEW updown_target_series(series_slug,expected_markets,first_end_time) AS VALUES ('test',3,NULL)")
        with self.db:
            self.db.execute("UPDATE markets SET series_slug='test',end_time='2026-11-09T00:00:00Z'")
        self.db.executescript((ROOT / "sql/updown_daily.sql").read_text())
        with self.assertRaisesRegex(ValueError, "覆盖"):
            groups.finalize_database(self.db)

    def test_new_incomplete_database_cannot_resume_without_persistent_mapping(self):
        core.commit_page(self.db, "a", 1, page([trade("x")], "page-2"))
        self.db.execute("DROP TABLE pending_group_map")
        with self.assertRaisesRegex(ValueError, "持久成交组映射"):
            groups.upgrade_database(self.db)

    def legacy_database(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        db.executescript((ROOT / "tests/fixtures/btc5m_legacy_schema.sql").read_text())
        seed(db, "complete")
        seed(db, "partial")
        core.commit_page(db, "complete", 1, page([trade("x"), trade("y"), trade("x")]))
        core.commit_page(db, "partial", 1, page([trade("y"), trade("x"), trade("y")], "page-2"))
        db.executescript("CREATE TABLE market_volume(market_slug TEXT PRIMARY KEY,amount_micro_usdc INTEGER);"
                         "INSERT INTO market_volume VALUES ('complete',123);"
                         "CREATE VIEW updown_old_volume AS SELECT * FROM market_volume;")
        return path, db

    def test_upgrade_preserves_rows_precision_order_and_only_partial_hash_mapping(self):
        path, db = self.legacy_database()
        with closing(db):
            before = [tuple(row) for row in db.execute("SELECT * FROM trades ORDER BY market_slug,page_number,row_number")]
            self.assertTrue(groups.upgrade_database(db))
            self.assertEqual(groups.group_column(db), "group_id")
            self.assertFalse(groups.upgrade_database(db))
            after = [tuple(row) for row in db.execute("SELECT * FROM trades ORDER BY market_slug,page_number,row_number")]
            self.assertEqual([row[:4]+row[5:] for row in before], [row[:4]+row[5:] for row in after])
            self.assertEqual([row[4] for row in after], [1, 2, 1, 1, 2, 1])
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM pending_group_map ORDER BY group_id")],
                             [("partial", "y", 1), ("partial", "x", 2)])
            self.assertNotIn("market_volume", self.table_names(db))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='updown_old_volume'").fetchone())
        with closing(up.connect_database(path)) as resumed:
            core.commit_page(resumed, "partial", 2, page([trade("x"), trade("z")]))
            self.assertEqual([row[0] for row in resumed.execute("SELECT group_id FROM trades WHERE market_slug='partial' ORDER BY page_number,row_number")],
                             [1, 2, 1, 2, 3])

    def test_upgrade_failure_rolls_back_table_mapping_and_old_view_changes(self):
        _, db = self.legacy_database()
        with closing(db):
            create = groups._new_trade_table

            def fail_copy(connection):
                create(connection)
                connection.execute("CREATE TRIGGER fail_upgrade BEFORE INSERT ON trades_grouped_upgrade "
                                   "BEGIN SELECT RAISE(ABORT,'simulated failure'); END")

            with patch.object(groups, "_new_trade_table", side_effect=fail_copy), self.assertRaises(sqlite3.IntegrityError):
                groups.upgrade_database(db)
            self.assertEqual(groups.group_column(db), "transaction_hash")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 6)
            self.assertIn("market_volume", self.table_names(db))
            self.assertNotIn("pending_group_map", self.table_names(db))
            self.assertNotIn("trades_grouped_upgrade", self.table_names(db))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM updown_old_volume").fetchone()[0], 1)

    def test_local_preserved_database_guard_does_not_open_it(self):
        with patch.object(core.sqlite3, "connect", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(ValueError, "保留不动"):
                core.connect_database(core.DEFAULT_DB)
        fake = Mock()
        fake.execute.return_value = [(0, "main", str(core.DEFAULT_DB))]
        with self.assertRaisesRegex(ValueError, "保留不动"):
            groups.upgrade_database(fake)
        fake.execute.assert_called_once_with("PRAGMA database_list")


if __name__ == "__main__":
    unittest.main()
