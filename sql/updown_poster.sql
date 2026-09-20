-- 市场结束日归属与 updown_* 视图一致；提前和跨日成交均随市场计入。
-- 仅对已完成的日期汇总。机器人条件由海报入口传入，与页脚使用同一组参数。
WITH wallet_features AS (
    SELECT date(m.end_time) AS date_utc, t.wallet,
           COUNT(*) AS trade_count,
           MAX(t.timestamp)-MIN(t.timestamp) AS span_seconds,
           SUM(t.amount_micro_usdc) AS amount_micro_usdc
    FROM markets m
    JOIN updown_target_series s ON s.series_slug=m.series_slug
    JOIN updown_coverage c ON c.date_utc=date(m.end_time) AND c.is_complete
    JOIN trades t ON t.market_slug=m.slug
    WHERE date(m.end_time) IN (:day, :previous_day)
    GROUP BY date(m.end_time), t.wallet
), classified AS (
    SELECT *, trade_count>1
        AND span_seconds<=:max_interval_seconds*(trade_count-1)
        AND span_seconds>=:min_span_seconds AS suspected_bot
    FROM wallet_features
)
SELECT c.date_utc, c.completed_markets AS total_markets,
       COUNT(w.wallet) AS unique_wallets,
       COALESCE(SUM(w.amount_micro_usdc), 0) AS volume_micro_usdc,
       COALESCE(SUM(w.suspected_bot), 0) AS suspected_bot_wallets,
       COALESCE(SUM(CASE WHEN w.suspected_bot THEN w.amount_micro_usdc ELSE 0 END), 0)
           AS suspected_bot_volume_micro_usdc
FROM updown_coverage c
LEFT JOIN classified w ON w.date_utc=c.date_utc
WHERE c.date_utc IN (:day, :previous_day) AND c.is_complete
GROUP BY c.date_utc, c.completed_markets
ORDER BY c.date_utc;
