-- Explicit legacy hash schema, used only by backwards-compatibility fixtures.
PRAGMA foreign_keys = ON;
CREATE TABLE markets (
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
CREATE INDEX markets_date ON markets(date_utc);
CREATE TABLE collection_progress (
    market_slug TEXT PRIMARY KEY REFERENCES markets(slug),
    query_params TEXT,
    next_cursor TEXT,
    committed_page INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    last_error TEXT
);
CREATE TABLE trades (
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
