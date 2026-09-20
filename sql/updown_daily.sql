-- 日期均为市场 end_time 所属 UTC 日；原 markets.date_utc 仍保留开始日。
-- updown_target_series 由采集入口的系列目录生成，供离线查询使用。
CREATE VIEW IF NOT EXISTS updown_series_coverage AS
WITH days AS (
    SELECT DISTINCT date(m.end_time) AS date_utc
    FROM markets m JOIN updown_target_series s ON s.series_slug=m.series_slug
    WHERE m.end_time IS NOT NULL
)
SELECT d.date_utc, s.series_slug, s.expected_markets,
       COUNT(m.condition_id) AS discovered_markets,
       COUNT(p.completed_at) AS completed_markets,
       COALESCE(SUM(p.committed_page), 0) AS committed_pages,
       COUNT(m.condition_id)=s.expected_markets AND
           COUNT(p.completed_at)=COUNT(m.condition_id) AS is_complete
FROM days d CROSS JOIN updown_target_series s
LEFT JOIN markets m ON m.series_slug=s.series_slug AND date(m.end_time)=d.date_utc
LEFT JOIN collection_progress p ON p.market_slug=m.slug
GROUP BY d.date_utc, s.series_slug, s.expected_markets;

CREATE VIEW IF NOT EXISTS updown_coverage AS
SELECT date_utc, SUM(expected_markets) AS expected_markets,
       SUM(discovered_markets) AS discovered_markets,
       SUM(completed_markets) AS completed_markets,
       SUM(committed_pages) AS committed_pages, MIN(is_complete) AS is_complete
FROM updown_series_coverage GROUP BY date_utc;

CREATE VIEW IF NOT EXISTS updown_wallet_daily AS
SELECT date(m.end_time) AS date_utc, t.wallet,
       SUM(t.amount_micro_usdc) AS amount_micro_usdc
FROM markets m JOIN updown_target_series s ON s.series_slug=m.series_slug
JOIN trades t ON t.market_slug=m.slug
JOIN updown_coverage c ON c.date_utc=date(m.end_time) AND c.is_complete
GROUP BY date(m.end_time), t.wallet;

CREATE VIEW IF NOT EXISTS updown_wallet_ranked AS
SELECT date_utc, wallet, amount_micro_usdc,
       ROW_NUMBER() OVER (
           PARTITION BY date_utc ORDER BY amount_micro_usdc DESC, wallet ASC
       ) AS wallet_rank
FROM updown_wallet_daily;

CREATE VIEW IF NOT EXISTS updown_daily_summary AS
SELECT c.date_utc, COUNT(w.wallet) AS unique_wallets,
       COALESCE(SUM(w.amount_micro_usdc), 0) AS wallet_total_micro_usdc,
       COALESCE(SUM(CASE WHEN w.wallet_rank<=200 THEN w.amount_micro_usdc ELSE 0 END), 0)
           AS top200_micro_usdc,
       1.0 * SUM(CASE WHEN w.wallet_rank<=200 THEN w.amount_micro_usdc ELSE 0 END)
           / NULLIF(SUM(w.amount_micro_usdc), 0) AS top200_share
FROM updown_coverage c LEFT JOIN updown_wallet_ranked w ON w.date_utc=c.date_utc
WHERE c.is_complete GROUP BY c.date_utc;
