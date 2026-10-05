-- 单边市场成交额与钱包明细分开；仅为本次请求范围内的市场创建进度。
CREATE TABLE IF NOT EXISTS market_volume (
    market_slug TEXT PRIMARY KEY REFERENCES markets(slug),
    amount_micro_usdc INTEGER NOT NULL DEFAULT 0,
    committed_page INTEGER NOT NULL DEFAULT 0,
    next_cursor TEXT,
    completed_at TEXT,
    last_error TEXT
);
