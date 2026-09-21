-- 按市场结束日选取整周；成交时间不参与日期过滤。
-- 保留每日、钱包、系列粒度，供跨系列机器人判断和币种贡献汇总。
SELECT date(m.end_time) AS date_utc, t.wallet, m.series_slug,
       COUNT(*) AS trade_count, MIN(t.timestamp) AS first_timestamp,
       MAX(t.timestamp) AS last_timestamp,
       SUM(t.amount_micro_usdc) AS amount_micro_usdc
FROM markets m
JOIN updown_target_series s ON s.series_slug=m.series_slug
JOIN trades t ON t.market_slug=m.slug
WHERE date(m.end_time)>=:start AND date(m.end_time)<:end
GROUP BY date(m.end_time), t.wallet, m.series_slug;
