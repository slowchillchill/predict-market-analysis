"""持久化短成交组标识；兼容只读旧哈希日库与工作副本升级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 2
_PRESERVED_LOCAL_DATABASE = Path(__file__).resolve().parents[1] / "data/raw/btc5m.sqlite3"
_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_group_map (
    market_slug TEXT NOT NULL REFERENCES markets(slug),
    transaction_hash TEXT NOT NULL,
    group_id INTEGER NOT NULL CHECK(typeof(group_id)='integer' AND group_id>0),
    PRIMARY KEY (market_slug, transaction_hash),
    UNIQUE (market_slug, group_id)
) WITHOUT ROWID
"""


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def group_column(db: sqlite3.Connection) -> str:
    """只读返回受控列名；用于新旧日库共用统计读取路径。"""
    columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
    if "group_id" in columns:
        return "group_id"
    if "transaction_hash" in columns:
        return "transaction_hash"
    raise ValueError("交易表缺少 group_id 或旧 transaction_hash")


def ensure_pending_map(db: sqlite3.Connection) -> None:
    # 必须是普通持久表，不能用 TEMP TABLE；暂停发布后仍需恢复哈希映射。
    db.execute(_PENDING_SCHEMA)


def _protect_local_database(db: sqlite3.Connection) -> None:
    for _, name, path in db.execute("PRAGMA database_list"):
        if name == "main" and path and Path(path).resolve() == _PRESERVED_LOCAL_DATABASE.resolve():
            raise ValueError("本地旧数据库保留不动；只能升级 Kaggle 工作副本或临时测试数据库")


def _drop_old_views(db: sqlite3.Connection) -> None:
    # 升级后由 connect_database 重建项目视图，避免旧视图引用已移除的金额实体表。
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='view' "
             "AND (name LIKE 'btc5m_%' OR name LIKE 'updown_%') AND name<>'updown_target_series'")]
    for name in names:
        db.execute('DROP VIEW "' + name.replace('"', '""') + '"')


def _new_trade_table(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE trades_grouped_upgrade (
            market_slug TEXT NOT NULL REFERENCES markets(slug),
            page_number INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            wallet TEXT NOT NULL,
            group_id INTEGER NOT NULL CHECK(typeof(group_id)='integer' AND group_id>0),
            timestamp INTEGER NOT NULL,
            side TEXT NOT NULL,
            outcome TEXT NOT NULL,
            outcome_index INTEGER NOT NULL,
            token_id TEXT NOT NULL,
            raw_price TEXT NOT NULL,
            raw_size TEXT NOT NULL,
            amount_micro_usdc INTEGER NOT NULL,
            PRIMARY KEY (market_slug, page_number, row_number)
        ) WITHOUT ROWID
    """)


def upgrade_database(db: sqlite3.Connection) -> bool:
    """升级工作副本，不改原行、页序或金额；返回是否转换旧结构。

    旧分析视图会被移除，调用方随后通过 collector.connect_database 重建。
    此函数不做 VACUUM；未完成市场仍须保留普通持久映射表。
    """
    _protect_local_database(db)
    if db.in_transaction:
        raise ValueError("升级前须结束外层事务")
    if not _table_exists(db, "trades"):
        return False
    old_hash = group_column(db) == "transaction_hash"
    old_volume = _table_exists(db, "market_volume")
    if not old_hash:
        if not _table_exists(db, "pending_group_map") and db.execute("""
            SELECT 1 FROM trades t JOIN collection_progress p ON p.market_slug=t.market_slug
            WHERE p.completed_at IS NULL LIMIT 1
        """).fetchone():
            raise ValueError("未完成市场缺少持久成交组映射，不能靠内存重新分配组号")
        with db:
            db.execute("BEGIN IMMEDIATE")
            if old_volume:
                _drop_old_views(db)
                db.execute("DROP TABLE market_volume")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return old_volume
    complete = {row[0] for row in db.execute("SELECT market_slug FROM collection_progress WHERE completed_at IS NOT NULL")}
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TABLE IF EXISTS pending_group_map")
        ensure_pending_map(db)
        _new_trade_table(db)
        current_market, mapping = None, {}
        cursor = db.execute("SELECT market_slug,page_number,row_number,wallet,transaction_hash,timestamp,"
                            "side,outcome,outcome_index,token_id,raw_price,raw_size,amount_micro_usdc "
                            "FROM trades ORDER BY market_slug,page_number,row_number")
        try:
            for row in cursor:
                slug, transaction_hash = row[0], row[4]
                if slug != current_market:
                    current_market, mapping = slug, {}
                if transaction_hash not in mapping:
                    mapping[transaction_hash] = len(mapping) + 1
                    if slug not in complete:
                        db.execute("INSERT INTO pending_group_map VALUES (?,?,?)",
                                   (slug, transaction_hash, mapping[transaction_hash]))
                converted = (*tuple(row[:4]), mapping[transaction_hash], *tuple(row[5:]))
                db.execute("INSERT INTO trades_grouped_upgrade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", converted)
        finally:
            cursor.close()
        _drop_old_views(db)
        db.execute("DROP TABLE trades")
        db.execute("ALTER TABLE trades_grouped_upgrade RENAME TO trades")
        if old_volume:
            db.execute("DROP TABLE market_volume")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return True


def finalize_database(db: sqlite3.Connection) -> None:
    """只为完整日库移除空映射表并压实文件；未完成日库必须可续采。"""
    _protect_local_database(db)
    if db.in_transaction:
        raise ValueError("压实前须结束外层事务")
    if group_column(db) != "group_id":
        raise ValueError("旧哈希日库须先转换，才能发布新格式")
    catalog = db.execute("SELECT 1 FROM sqlite_master WHERE name='updown_target_series' AND type='view'").fetchone()
    scope = "JOIN updown_target_series s ON s.series_slug=m.series_slug " if catalog else ""
    found = db.execute("SELECT 1 FROM markets m " + scope + "LIMIT 1").fetchone()
    unfinished = db.execute("SELECT 1 FROM markets m " + scope +
                           "LEFT JOIN collection_progress p ON p.market_slug=m.slug "
                           "WHERE m.condition_id IS NULL OR p.completed_at IS NULL LIMIT 1").fetchone()
    if not found or unfinished:
        raise ValueError("仍有未发现或未完成的目标市场，不能移除续采映射")
    if catalog:
        coverage = db.execute("SELECT is_complete FROM updown_coverage").fetchall()
        if not coverage or not all(row[0] for row in coverage):
            raise ValueError("目标市场覆盖尚未完整，不能发布最终日库")
    if _table_exists(db, "pending_group_map") and db.execute("SELECT 1 FROM pending_group_map LIMIT 1").fetchone():
        raise ValueError("成交组映射尚未清空，不能发布最终日库")
    with db:
        db.execute("DROP TABLE IF EXISTS pending_group_map")
    db.execute("VACUUM")
