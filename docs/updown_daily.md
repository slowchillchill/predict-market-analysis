# 加密货币 Up/Down 全系列采集

范围采用[市场系列清单](../reports/crypto_updown_market_types_2026-09-17.md)中的 Polymarket
38 个系列：BTC、ETH、SOL、XRP、DOGE、BNB、HYPE 的 5 分钟、15 分钟、1 小时、4 小时和日线，
以及 ZEC 的 5 分钟、15 分钟和 4 小时。

## 日期口径

日期是**市场结束日（UTC）**，由 Gamma 市场的 `endDate` 确定；包含日期下限，不包含上限。
例如 UTC 2026-09-17 的范围是 `2026-09-17T00:00:00Z <= endDate < 2026-09-18T00:00:00Z`。
选择这些市场后，下载其全部成交，包括市场开始前和跨日成交，不按成交时间过滤。
这里的结束时间是市场规定的 `endDate`，不是平台确认结算结果的时间。

9 月 16 日 23:55 开始、9 月 17 日 00:00 结束的 5 分钟市场属于 9 月 17 日；
9 月 17 日 23:55 开始、9 月 18 日 00:00 结束的市场属于 9 月 18 日。
日线市场可能跨两个 UTC 日期，仍按相同规则处理。

每个完整 UTC 日按当前系列安排应有 3,295 个市场：5 分钟 2,304 个、15 分钟 768 个、
小时线 168 个、4 小时 48 个、日线 7 个。按系列分别检查覆盖，缺少系列或分页未完成时不生成完整日金额。
将来系列调整或历史上未开放的日期可能达不到该数量；报告会列出实际缺口，不把缺失市场计作零成交。

## 下载与续传

依赖及香港代理要求沿用 [BTC 指南](../README_BTC5M.md)。在仓库根目录运行：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_daily.py collect \
  --start-date 2026-09-19 --end-date 2026-09-20 \
  --proxy socks5h://127.0.0.1:20810 \
  --db data/raw/btc5m.sqlite3

```

通过 Gamma 按实际系列和结束时间发现市场，处理全部事件分页，并在本地排除上限日期的市场。
成交查询沿用 `/v2/trades`、`taker_only=false`、`filter_type=TOKENS`、`filter_amount=1e-18`、
`limit=1000`。跟随游标直到 `next_cursor=null`，保留每条返回记录，不按交易哈希去重。
“完整”指采集时这些参数下官方接口三年窗口内的全部分页，未作全量链上对账。

直接复用 `data/raw/btc5m.sqlite3`；已下载三天的 BTC 5 分钟市场、成交和进度保留。
相同市场已完成则跳过，未完成则从已提交游标续传。按 `Ctrl+C` 中断后可重跑同一命令。
每页成交与进度原子提交；游标过期时只重建该未完成市场。数据库一次运行一个采集进程。

`markets.date_utc` 保留原市场开始日语义；新 `updown_*` 视图从 `end_time` 推导结束日，
不复制成交明细。原 `btc5m_*` 视图只包含 BTC 5 分钟，并继续按开始日统计。
新入口首次连接会安装这些视图，不需要手动导入 SQL。

## 离线统计

需要发布英文数据海报时，使用[海报生成脚本](updown_poster.md)。输入日期即可生成固定版式的 PNG，
包含市场数、成交额、去重钱包数、环比及疑似机器人指标。

```bash
python3 scripts/polymarket_updown_daily.py report \
  --date 2026-09-17 --db data/raw/btc5m.sqlite3 --output-dir outputs/updown
```

生成中文报告、日汇总 CSV 和前 200 钱包 CSV。钱包成交额为买入支出加卖出收入，
每条 `price × size` 按 `ROUND_HALF_UP` 舍入至微 USDC 后求和，与既有统计计算一致。
该分母是目标市场的全部钱包成交额，不应与只计单边的其他平台成交量直接混用。
缺口日只输出覆盖报告。报告以只读方式打开数据库，不联网。

退出码 `0` 表示完整，`2` 表示仍有缺口，`130` 表示采集被中断。

```sql
-- 日期均为 UTC 市场结束日。
SELECT * FROM updown_coverage WHERE date_utc='2026-09-17';
SELECT * FROM updown_series_coverage
WHERE date_utc='2026-09-17' ORDER BY series_slug;
SELECT * FROM updown_daily_summary WHERE date_utc='2026-09-17';
SELECT * FROM updown_wallet_ranked
WHERE date_utc='2026-09-17' AND wallet_rank<=200 ORDER BY wallet_rank;
```

官方接口说明：[Gamma 事件列表](https://docs.polymarket.com/api-reference/events/list-events)、
[成交分页](https://docs.polymarket.com/api-reference/feeds/list-trades)。
