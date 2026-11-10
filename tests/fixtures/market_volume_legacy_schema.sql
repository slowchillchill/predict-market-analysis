-- Explicit legacy API cache, never loaded by the new collector.
CREATE TABLE market_volume (
    market_slug TEXT PRIMARY KEY REFERENCES markets(slug),
    amount_micro_usdc INTEGER NOT NULL DEFAULT 0,
    committed_page INTEGER NOT NULL DEFAULT 0,
    next_cursor TEXT,
    completed_at TEXT,
    last_error TEXT
);
