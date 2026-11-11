PRAGMA foreign_keys = ON;

-- 未发现的标准时段也占一行，元数据保持 NULL，不能当作零成交。
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    date_utc TEXT,
    market_id TEXT,
    condition_id TEXT UNIQUE,
    event_id TEXT,
    title TEXT,
    series_slug TEXT,
    event_start_time TEXT,
    end_time TEXT,
    resolution_source TEXT,
    description TEXT
);
CREATE INDEX IF NOT EXISTS markets_date ON markets(date_utc);
CREATE INDEX IF NOT EXISTS markets_series_end ON markets(series_slug,end_time);

CREATE TABLE IF NOT EXISTS collection_progress (
    market_slug TEXT PRIMARY KEY REFERENCES markets(slug),
    query_params TEXT,
    next_cursor TEXT,
    committed_page INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS trades (
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
) WITHOUT ROWID;

-- 仅供未完成市场跨页续采；最后一页和完成进度在同一事务中清除此市场映射。
-- 普通持久表，随 incomplete 日库发布；不能使用 TEMP TABLE。
CREATE TABLE IF NOT EXISTS pending_group_map (
    market_slug TEXT NOT NULL REFERENCES markets(slug),
    transaction_hash TEXT NOT NULL,
    group_id INTEGER NOT NULL CHECK(typeof(group_id)='integer' AND group_id>0),
    PRIMARY KEY (market_slug, transaction_hash),
    UNIQUE (market_slug, group_id)
) WITHOUT ROWID;

PRAGMA user_version = 3;
