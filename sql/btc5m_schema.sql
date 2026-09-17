PRAGMA foreign_keys = ON;

-- 未发现的标准时段也占一行，元数据保持 NULL，不能当作零成交。
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    date_utc TEXT NOT NULL,
    market_id TEXT,
    condition_id TEXT UNIQUE,
    event_id TEXT,
    title TEXT,
    series_slug TEXT,
    event_start_time TEXT,
    end_time TEXT
);
CREATE INDEX IF NOT EXISTS markets_date ON markets(date_utc);

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
    transaction_hash TEXT NOT NULL,
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
