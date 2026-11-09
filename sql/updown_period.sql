-- 按市场结束日选取区间；成交时间不参与过滤。
-- CROSS JOIN 固定先选市场再按成交主键读取，避免每一天都扫描全部历史成交。
-- 保留 BUY+SELL 特征用于钱包活动统计，仅金额限制为 BUY。
SELECT date(m.end_time) AS date_utc, t.wallet, m.series_slug,
       COUNT(*) AS trade_count, MIN(t.timestamp) AS first_timestamp,
       MAX(t.timestamp) AS last_timestamp,
       SUM(CASE WHEN t.side='BUY' THEN t.amount_micro_usdc ELSE 0 END) AS amount_micro_usdc
FROM markets m
JOIN updown_target_series s ON s.series_slug=m.series_slug
CROSS JOIN trades t ON t.market_slug=m.slug
WHERE date(m.end_time)>=:start AND date(m.end_time)<:end
GROUP BY date(m.end_time), t.wallet, m.series_slug;
