# BTC 5 分钟逐笔采集与日统计

入口为 `scripts/polymarket_btc5m_daily.py`，只覆盖 Polymarket 的
`btc-up-or-down-5m` 系列。逐笔明细保存在 SQLite，钱包与日汇总由普通 SQL
和非物化视图按需计算。旧脚本、历史报表及缓存保持原样。

## 安装和运行

需要 Python 3.10 或更新版本，采集依赖 `requests[socks]`：

```bash
python3 -m venv .venv-btc5m
.venv-btc5m/bin/python -m pip install -r requirements-btc5m.txt
```

将本机香港代理 URL 保存到当前终端的 `BTC5M_HK_PROXY` 环境变量；例如采用
`socks5h://127.0.0.1:端口` 格式，端口替换为实际配置。代理地址和凭据不写入仓库。
采集会话显式设置 HTTP 与 HTTPS 代理，关闭环境代理自动继承；代理不可用时请求失败，
不回退直连。网络请求、数据库写入均为单线程，使用一个数据库连接。

```bash
.venv-btc5m/bin/python scripts/polymarket_btc5m_daily.py collect \
  --start-date 2026-09-15 --end-date 2026-09-17 \
  --proxy "$BTC5M_HK_PROXY" --db data/raw/btc5m.sqlite3

python3 scripts/polymarket_btc5m_daily.py report \
  --date 2026-09-15 --db data/raw/btc5m.sqlite3 --output-dir outputs/btc5m

python3 scripts/polymarket_btc5m_daily.py report \
  --date 2026-09-16 --db data/raw/btc5m.sqlite3 --output-dir outputs/btc5m
```

结束日期不包含在采集范围内。省略 `--db` 时使用 `data/raw/btc5m.sqlite3`。
`report` 只读打开现有数据库，不加载 `requests`，不访问网络。
默认输出目录为 `outputs/btc5m`；数据库与生成文件均被 Git 忽略。

每个完整日生成 `btc5m_daily_日期.csv`、`btc5m_top10_日期.csv` 和
`btc5m_daily_日期.md`。采集结束时输出市场数、页数、逐笔记录数、数据库大小与本次耗时。
命令退出码为 `0` 表示完成、`2` 表示仍有缺口、`130` 表示采集被用户中断。

缺口日只生成中文进度报告，逐项列出未完成市场与错误；不生成日金额或 Top10 CSV，
并清除输出目录内同名旧 CSV。SQL 统计视图也只返回覆盖完整的日期。

## 统计口径

- 每个 UTC 日枚举 288 个标准开始时刻，按 slug 批量查询 Gamma，核对系列与
  `eventStartTime`。市场的创建时间 `startDate` 不用于日期归属。
- 所有成交均随市场的 `eventStartTime` 所属 UTC 日期统计，包括提前成交和跨日成交。
- 钱包成交额为实际买入支出加实际卖出收入。例如卖出收入 3.25、买入支出 1.75，
  钱包成交额是 5 USDC。买卖方向、Up/Down 方向均不互相抵消。
- 日总额明确称为“钱包成交总额”：同一撮合中买方和卖方各自的钱包成交都计入，
  不等同于单边市场成交量或净资金流。
- 单笔金额由接口的原始十进制 `price × size` 计算，按 `ROUND_HALF_UP` 舍入到
  0.000001 USDC，以整数微 USDC 保存。原始价格、份数另存为文本，可重新计算。
- 钱包地址统一转为小写，跨当天全部市场去重并累计成交额。Top10 按金额降序、
  地址升序排序，不足十个时全部列出；占比的分母是钱包成交总额，总额为零时占比为空。

## 数据源和完整性边界

依据 [官方 Data API v2 文档](https://data-api.polymarket.com/v2/docs) 的 `/v2/trades` 合同：

```text
condition=<市场 conditionId>
limit=1000
taker_only=false
filter_type=TOKENS
filter_amount=1e-18
cursor=<上一页 pagination.next_cursor，仅续页提供>
```

每页都重发相同过滤参数，直到 `pagination.next_cursor` 为 `null`，不以页长判断结束。
接口默认的 0.01 份过滤不适用本采集；也没有旧入口的 4,000 条上限。
不调用官方累计成交额接口，不保存市场或日累计金额缓存。

官方市场查询只提供固定的近三年窗口，且忽略 `start`/`end`；这里不按成交时间过滤。
首版指定验收日期为 UTC 2026-09-15、2026-09-16。所谓“完整”仅指采集时上述查询条件下
官方接口的全部分页，不代表全量链上对账，也不保证包含接口后来补录的成交。

每次请求超时 30 秒，最多尝试 3 次；网络异常、无效 JSON、HTTP 429 和 5xx 可重试。
优先遵守服务端 `Retry-After`（秒数或 HTTP 日期），否则等待 `0.5 × 尝试次数` 秒。
重试用尽的市场保留进度和失败信息，继续处理其他市场；再次运行同一命令即可续传。

## 数据库与续传

`sql/btc5m_schema.sql` 定义三张表：

| 表 | 保存内容 |
| --- | --- |
| `markets` | 标准时段 slug、UTC 日期及已核实的市场编号、conditionId、事件编号、标题、系列、起止时间；未发现时元数据为 NULL |
| `trades` | 市场、页号、页内序号、钱包、交易哈希、时间、买卖方向、结果及编号、代币编号、原始价格、原始份数、整数金额 |
| `collection_progress` | 实际过滤参数、下一页游标、已提交页号、完成时间、最后错误 |

每页明细与下一页游标在同一个 SQLite 事务中提交。主键为市场、页号和页内序号，
不使用交易哈希或成交字段去重；同一交易内的多笔真实成交、字段完全相同的多条记录均保留。
该保证针对采集重试，不擅自消除源接口可能存在的重复。

中断后读取最后提交的游标。若接口明确返回游标失效，原子清除当前未完成市场的旧页和游标，
从首页重采；其他市场不受影响。已完成市场默认跳过。一次只运行一个采集进程写入同一数据库。

## 直接使用 SQL

`sql/btc5m_daily.sql` 随数据库初始化安装非物化视图。整数金额单位都是微 USDC；
显示为 USDC 时除以 1,000,000。Python SQLite 调用应使用参数绑定。
使用 `sqlite3` 命令行时，要保留日期内层引号，避免被当作减法表达式：

```sql
.parameter set :date "'2026-09-16'"

SELECT * FROM btc5m_coverage WHERE date_utc = :date;

SELECT wallet, amount_micro_usdc
FROM btc5m_wallet_daily WHERE date_utc = :date
ORDER BY amount_micro_usdc DESC, wallet ASC;

SELECT unique_wallets, wallet_total_micro_usdc, top10_micro_usdc, top10_share
FROM btc5m_daily_summary WHERE date_utc = :date;

SELECT wallet_rank, wallet, amount_micro_usdc
FROM btc5m_wallet_ranked WHERE date_utc = :date AND wallet_rank <= 10
ORDER BY wallet_rank;
```

日 CSV 中 `wallet_total_usdc` 是钱包成交总额，`top10_usdc` 是 Top10 合计，
`top10_share` 为 0 到 1 的比例；`*_micro_usdc` 保留整数金额便于核对。

## 验证

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

新增测试覆盖 UTC 日期边界、买卖金额相加、逐笔舍入、跨市场钱包去重、同哈希与相同字段记录保留、
超过 4,000 条的游标分页、短页继续分页、事务提交前失败、提交后中断、游标失效重采、
普通失败保留断点、Top10 并列、零成交、零金额、缺口抑制统计，以及离线报告重复生成。
