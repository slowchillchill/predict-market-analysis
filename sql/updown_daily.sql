-- 日期均为市场 end_time 所属 UTC 日；原 markets.date_utc 仍保留开始日。
-- updown_target_series 由采集入口的系列目录生成，供离线查询使用。
-- 采集初始化时更新已有视图，使历史范围修订同时作用于日、周、月报告。
DROP VIEW IF EXISTS updown_daily_summary;
DROP VIEW IF EXISTS updown_market_volume_daily;
DROP VIEW IF EXISTS updown_wallet_ranked;
DROP VIEW IF EXISTS updown_wallet_daily;
DROP VIEW IF EXISTS updown_coverage;
DROP VIEW IF EXISTS updown_series_coverage;
CREATE VIEW IF NOT EXISTS updown_series_coverage AS
WITH days AS (
    SELECT DISTINCT date(m.end_time) AS date_utc
    FROM markets m JOIN updown_target_series s ON s.series_slug=m.series_slug
    WHERE m.end_time IS NOT NULL
), expected AS (
    SELECT d.date_utc, s.series_slug,
        CASE WHEN s.first_end_time IS NULL OR date(s.first_end_time)<d.date_utc THEN s.expected_markets
             WHEN date(s.first_end_time)>d.date_utc THEN 0
             ELSE (CAST(strftime('%s', d.date_utc, '+1 day') AS INTEGER)
                   - CAST(strftime('%s', s.first_end_time) AS INTEGER)
                   + 86400/s.expected_markets - 1) / (86400/s.expected_markets)
        END AS expected_markets
    FROM days d CROSS JOIN updown_target_series s
)
SELECT s.date_utc, s.series_slug, s.expected_markets,
       COUNT(m.condition_id) AS discovered_markets,
       COUNT(p.completed_at) AS completed_markets,
       COALESCE(SUM(p.committed_page), 0) AS committed_pages,
       COUNT(v.completed_at) AS market_volume_completed_markets,
       COALESCE(SUM(v.committed_page), 0) AS market_volume_committed_pages,
       COUNT(m.condition_id)=s.expected_markets AND
           COUNT(v.completed_at)=COUNT(m.condition_id) AS market_volume_is_complete,
       COUNT(m.condition_id)=s.expected_markets AND
           COUNT(p.completed_at)=COUNT(m.condition_id) AS is_complete
FROM expected s
LEFT JOIN markets m ON m.series_slug=s.series_slug AND date(m.end_time)=s.date_utc
LEFT JOIN collection_progress p ON p.market_slug=m.slug
LEFT JOIN market_volume v ON v.market_slug=m.slug
GROUP BY s.date_utc, s.series_slug, s.expected_markets;

CREATE VIEW IF NOT EXISTS updown_coverage AS
SELECT date_utc, SUM(expected_markets) AS expected_markets,
       SUM(discovered_markets) AS discovered_markets,
       SUM(completed_markets) AS completed_markets,
       SUM(committed_pages) AS committed_pages, MIN(is_complete) AS is_complete,
       SUM(market_volume_completed_markets) AS market_volume_completed_markets,
       SUM(market_volume_committed_pages) AS market_volume_committed_pages,
       MIN(market_volume_is_complete) AS market_volume_is_complete
FROM updown_series_coverage GROUP BY date_utc;

CREATE VIEW IF NOT EXISTS updown_market_volume_daily AS
SELECT c.date_utc,
       CASE WHEN c.market_volume_is_complete THEN COALESCE(SUM(v.amount_micro_usdc), 0)
            ELSE NULL END AS market_volume_micro_usdc
FROM updown_coverage c
LEFT JOIN markets m ON date(m.end_time)=c.date_utc
    AND m.series_slug IN (SELECT series_slug FROM updown_target_series)
LEFT JOIN market_volume v ON v.market_slug=m.slug
GROUP BY c.date_utc;

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
           / NULLIF(SUM(w.amount_micro_usdc), 0) AS top200_share,
       v.market_volume_micro_usdc
FROM updown_coverage c LEFT JOIN updown_wallet_ranked w ON w.date_utc=c.date_utc
LEFT JOIN updown_market_volume_daily v ON v.date_utc=c.date_utc
WHERE c.is_complete GROUP BY c.date_utc;
