# Polymarket BTC 5 分钟数据使用指南

本指南汇总 2026-09-17 完成的逐笔下载、SQLite 离线统计和疑似机器人筛选方法。
只覆盖 BTC Up/Down 5 分钟系列（`btc-up-or-down-5m`）。以下命令均在仓库根目录执行。

## 1. 安装

需要 Python 3.10 或更新版本；直接执行 SQL 示例还需要 `sqlite3` 命令行工具。

```bash
python3 -m venv .venv-btc5m
.venv-btc5m/bin/python -m pip install -r requirements-btc5m.txt
sqlite3 --version
```

下载通过本机香港代理进行。运行下面命令，在提示后输入实际代理 URL，
格式为 `socks5h://127.0.0.1:端口`；代理地址和凭据只保存在当前终端变量中。

```bash
read -r -p '请输入本机香港代理 URL：' BTC5M_HK_PROXY
```

## 2. 启动下载与断点续传

下载 UTC 2026-09-15、2026-09-16 两天所属市场的全部成交：

```bash
.venv-btc5m/bin/python scripts/polymarket_btc5m_daily.py collect \
  --start-date 2026-09-15 \
  --end-date 2026-09-17 \
  --proxy "$BTC5M_HK_PROXY" \
  --db data/raw/btc5m.sqlite3
```

`--start-date` 包含当天，`--end-date` 不包含当天。日期按市场的 `eventStartTime`
所属 UTC 日确定，下载这些市场的所有成交，包括提前或跨日成交。

按 `Ctrl+C` 可以中断；再次执行相同命令，从最后提交的分页继续，已完成市场自动跳过。
每页明细与游标在同一个事务中提交，重跑不会重复保存已提交页。
同一数据库一次只运行一个采集进程。

采集使用 `/v2/trades`，设置 `taker_only=false`、`filter_type=TOKENS`、
`filter_amount=1e-18`、`limit=1000`，跟随游标至结束，没有 4,000 条截断。
这里的“完整”指上述条件下采集时接口返回的全部分页，未作全量链上对账。

查看采集覆盖情况：

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
SELECT date_utc, expected_markets, discovered_markets,
       completed_markets, committed_pages, is_complete
FROM btc5m_coverage
WHERE date_utc >= '2026-09-15' AND date_utc < '2026-09-17'
ORDER BY date_utc;
SQL
```

每天应发现并完成 288 个市场，`is_complete=1` 表示完成。日期没有返回行表示尚无采集记录。
有缺口时重新运行下载命令；也可用下面的 `report` 命令查看未完成市场和错误。

## 3. 查询成交额、去重钱包数和 Top10

最简单的方法是生成日汇总 CSV、Top10 CSV 和中文报告，全程离线：

```bash
python3 scripts/polymarket_btc5m_daily.py report \
  --date 2026-09-15 --db data/raw/btc5m.sqlite3 --output-dir outputs/btc5m

python3 scripts/polymarket_btc5m_daily.py report \
  --date 2026-09-16 --db data/raw/btc5m.sqlite3 --output-dir outputs/btc5m
```

终端直接显示结果，并为每个完整日生成：

- `outputs/btc5m/btc5m_daily_日期.csv`：去重钱包数、钱包成交总额、Top10 合计及占比。
- `outputs/btc5m/btc5m_top10_日期.csv`：Top10 钱包地址及成交额。
- `outputs/btc5m/btc5m_daily_日期.md`：中文报告及采集覆盖情况。

缺口日只生成进度报告，暂不生成金额统计。命令退出码 `0` 表示完成，`2` 表示有缺口，
`130` 表示下载被用户中断。`report` 只需要 Python 标准库，不需要代理。

也可以直接查询两天的成交额和去重钱包数：

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
SELECT date_utc,
       unique_wallets,
       printf('%d.%06d', wallet_total_micro_usdc / 1000000,
                        wallet_total_micro_usdc % 1000000) AS wallet_total_usdc
FROM btc5m_daily_summary
WHERE date_utc >= '2026-09-15' AND date_utc < '2026-09-17'
ORDER BY date_utc;
SQL
```

查询某一天 Top10 钱包的成交额：

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
SELECT wallet_rank, wallet,
       printf('%d.%06d', amount_micro_usdc / 1000000,
                        amount_micro_usdc % 1000000) AS amount_usdc
FROM btc5m_wallet_ranked
WHERE date_utc = '2026-09-16' AND wallet_rank <= 10
ORDER BY wallet_rank;
SQL
```

以上视图只返回采集完整的日期。查询指定地址时，可将 Top10 示例中的
`wallet_rank <= 10` 替换为 `wallet = '小写钱包地址'`。

统计口径：

- 日期随市场开始日归属，提前和跨日成交仍计入该市场所属日期。
- **钱包成交额 = 买入支出 + 卖出收入**。买入 1.75、卖出 3.25，合计为 5 USDC。
- **钱包成交总额**汇总所有参与钱包，包含买卖双方各自的成交金额，不等于单边市场成交量。
- 每笔 `价格 × 份数` 通过十进制运算四舍五入至 0.000001 USDC，以整数微 USDC 保存并求和。
- 钱包地址统一为小写，跨当天全部目标市场去重。Top10 先跨市场累计，再按金额降序、地址升序排名。
- Top10 占比以当天钱包成交总额为分母；不足十个钱包时全部列出，总额为零时占比为空。

## 4. 查询疑似机器人数量

目前通过下面的只读 SQL 按需筛选；`report` 命令不会自动输出机器人数量。
按**实际成交时间的 UTC 日期**，分别计算每个钱包的三个参数，三项同时满足时标为疑似机器人：

| 参数 | 计算方式 | 初始筛选阈值 |
| --- | --- | --- |
| 平均成交间隔 | `(最后成交时间 − 最早成交时间) / (成交记录数 − 1)` | ≤60 秒 |
| 首末成交跨度 | 最后成交时间 − 最早成交时间 | ≥6 小时 |
| 参与市场数 | 当天有成交的不同 `market_slug` 数量 | ≥48 个 |

跨度包含中间的空闲时间，48 个市场也不要求连续。成交记录按原始明细逐条计数，
不按交易哈希去重。只有一笔成交的钱包不命中。
这是两天样本支持的初始筛选规则，不能确认身份；未命中的钱包也不能直接认定为真人。

先按第 2 节检查采集覆盖，再运行下列命令。修改日期可查询其他时段，修改
`parameters` 中的 `60`、`6`、`48` 可比较不同阈值：

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
WITH parameters AS (
    SELECT 60 AS max_avg_interval_seconds,
           6 AS min_span_hours,
           48 AS min_markets
), wallet_day AS (
    SELECT date(timestamp, 'unixepoch') AS date_utc,
           wallet,
           COUNT(*) AS trade_count,
           MAX(timestamp) - MIN(timestamp) AS span_seconds,
           COUNT(DISTINCT market_slug) AS market_count
    FROM trades
    WHERE timestamp >= CAST(strftime('%s', '2026-09-15') AS INTEGER)
      AND timestamp < CAST(strftime('%s', '2026-09-17') AS INTEGER)
    GROUP BY date(timestamp, 'unixepoch'), wallet
)
SELECT date_utc,
       COUNT(*) AS observed_unique_wallets,
       SUM(CASE WHEN trade_count > 1
                 AND span_seconds <= max_avg_interval_seconds * (trade_count - 1)
                 AND span_seconds >= min_span_hours * 3600
                 AND market_count >= min_markets
                THEN 1 ELSE 0 END) AS suspected_bot_wallets
FROM wallet_day CROSS JOIN parameters
GROUP BY date_utc
ORDER BY date_utc;
SQL
```

`observed_unique_wallets` 是本地已下载数据中、按实际成交日去重的钱包数；
`suspected_bot_wallets` 是其中满足三个条件的钱包数。没有成交记录的日期不会返回行。
这条查询直接读取本地明细，不会自动检查采集缺口，不能把部分数据的结果称为完整日统计。

机器人分析与第 3 节日报的日期口径不同，因此钱包数可以不同。
分析只覆盖已下载市场；相邻日期市场可能存在提前或跨日成交，
完成某日所属的 288 个市场，不代表已经覆盖该实际成交日所有相邻市场的成交。
下面结果对应已验收的两天市场数据。

## 5. 两天数据的已验证结果

按市场开始日归属的日统计：

| UTC 市场开始日 | 完成市场数 | 逐笔记录数 | 去重钱包数 | 钱包成交总额（USDC） |
| --- | ---: | ---: | ---: | ---: |
| 2026-09-15 | 288 | 1,406,687 | 8,767 | 15,260,434.779976 |
| 2026-09-16 | 288 | 1,350,542 | 8,646 | 14,151,087.511605 |

按实际成交日筛选，使用 60 秒、6 小时、48 个市场的规则：

| UTC 实际成交日 | 本地样本去重钱包数 | 疑似机器人钱包数 |
| --- | ---: | ---: |
| 2026-09-15 | 8,766 | 168 |
| 2026-09-16 | 8,645 | 161 |

两天都命中的钱包有 135 个，两天至少命中一次的去重钱包有 194 个。
日数量相加不是跨日去重数量。

数据库合计保存 576 个市场、3,044 页、2,757,229 条明细，大小为
**783,720,448 字节，约 783.7 MB（747.4 MiB）**。这是 SQLite 文件本身的大小，
不含虚拟环境和导出报表。首次下载耗时约 28 分 44 秒，实际耗时随网络及数据量变化。

`--db` 可指定其他数据库路径；数据库和生成报表不提交到 Git。
数据库结构、采集边界及测试说明见 [详细文档](docs/btc5m_daily.md)，
真实采集和离线核对见 [验收记录](reports/btc5m_acceptance_2026-09-15_2026-09-16.md)。
