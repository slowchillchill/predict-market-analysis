-- 普通非物化视图；不保存市场或每日金额缓存。金额单位均为微 USDC。
CREATE VIEW IF NOT EXISTS btc5m_coverage AS
SELECT m.date_utc, 288 AS expected_markets,
       COUNT(m.condition_id) AS discovered_markets,
       COUNT(p.completed_at) AS completed_markets,
       COALESCE(SUM(p.committed_page), 0) AS committed_pages,
       COUNT(*) = 288 AND COUNT(p.completed_at) = 288 AS is_complete
FROM markets m LEFT JOIN collection_progress p ON p.market_slug = m.slug
GROUP BY m.date_utc;

-- 完整日才进入统计视图；缺失或失败的市场不会被计为零成交。
CREATE VIEW IF NOT EXISTS btc5m_wallet_daily AS
SELECT m.date_utc, t.wallet, SUM(t.amount_micro_usdc) AS amount_micro_usdc
FROM trades t JOIN markets m ON m.slug = t.market_slug
JOIN btc5m_coverage c ON c.date_utc = m.date_utc AND c.is_complete
GROUP BY m.date_utc, t.wallet;

CREATE VIEW IF NOT EXISTS btc5m_wallet_ranked AS
SELECT date_utc, wallet, amount_micro_usdc,
       ROW_NUMBER() OVER (
           PARTITION BY date_utc ORDER BY amount_micro_usdc DESC, wallet ASC
       ) AS wallet_rank
FROM btc5m_wallet_daily;

CREATE VIEW IF NOT EXISTS btc5m_daily_summary AS
SELECT c.date_utc, COUNT(w.wallet) AS unique_wallets,
       COALESCE(SUM(w.amount_micro_usdc), 0) AS wallet_total_micro_usdc,
       COALESCE(SUM(CASE WHEN w.wallet_rank <= 10 THEN w.amount_micro_usdc ELSE 0 END), 0)
           AS top10_micro_usdc,
       1.0 * SUM(CASE WHEN w.wallet_rank <= 10 THEN w.amount_micro_usdc ELSE 0 END)
           / NULLIF(SUM(w.amount_micro_usdc), 0) AS top10_share
FROM btc5m_coverage c
LEFT JOIN btc5m_wallet_ranked w ON w.date_utc = c.date_utc
WHERE c.is_complete
GROUP BY c.date_utc;

-- sqlite3 命令行示例（先设置 .parameter set :date "'2026-09-16'"）：
-- SELECT * FROM btc5m_coverage WHERE date_utc = :date;
-- SELECT * FROM btc5m_wallet_daily WHERE date_utc = :date
-- ORDER BY amount_micro_usdc DESC, wallet ASC;
-- SELECT * FROM btc5m_daily_summary WHERE date_utc = :date;
-- SELECT * FROM btc5m_wallet_ranked WHERE date_utc = :date AND wallet_rank <= 10
-- ORDER BY wallet_rank;
