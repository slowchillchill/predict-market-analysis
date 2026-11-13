# Polymarket 加密货币 Up/Down 公开数据

[English](README.md) | **简体中文**

本项目公开收集 Polymarket 加密货币 Up/Down 市场的交易记录，覆盖 38 个市场系列，并按**市场结束日（UTC）**提供每日 SQLite 数据与日、周、月分析结果。你可以研究钱包活动、市场金额、买入价位分布，以及高频交易行为。

## 获取数据

| 入口 | 内容 |
| --- | --- |
| [Polymarket Crypto Updown Markets Analysis](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis) | 公开采集与分析 Notebook |
| [2026-11-09 原始日库，版本 2](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2) | 可下载、挂载和查询的完整 v2 日库示例 |
| [2026-11-10 原始日库，版本 3](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-10/versions/3) | 完整 v3 日库；保留官方系列中的不同真实合约及结算规则 |
| [2026-11 月度分析库，版本 3](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/3) | 日级特征、结果索引和已完成的统计 JSON |
| [开源代码仓库](https://github.com/slowchillchill/predict-market-analysis) | 数据结构、采集代码、统计方法与测试 |

原始日库命名为 `pm-updown-raw-YYYY-MM-DD`，分析库命名为 `pm-updown-analysis-YYYY-MM`，发布者为 `qiuqiuslowchillchill`。日期和月份均使用 UTC。

**2026 年 11 月 11 日恢复修复：**原始库格式 v3 允许未知的真实市场开始时间保留为 `NULL`，并增加采集与发布恢复行为。修复已通过 420 项回归和下述 Kaggle 恢复验收，有限生产恢复已结束，每日调度已恢复；历史范围仍未补齐。已发布的 11 月 9 日 v2 示例仍可读取，不会仅为更改格式版本号而强制重发。报告 JSON 保持 v1，分组、钱包和 BUY 指标口径不变。

以上 v2 示例核实于 **2026-11-10**，v3 示例核实于 **2026-11-11**，链接均固定版本。后续版本可能修正数据或增加已完成日期。

**钱包记录行不等于独立撮合成交，`group_id` 也不是 fill ID。**使用数据前，请先查看下方记录粒度、金额与日期定义。

## 快速使用

### 在 Kaggle Notebook 中查询 SQLite

SQLite 查询在 Kaggle Notebook 内执行，数据库留在 Kaggle 环境，无需下载到自己的电脑。本项目没有可直接连接的远程 SQL 端点。

在 Kaggle 创建自己的 Notebook，通过 **Add Input** 仅挂载**一个**原始日库。以下示例使用 2026-11-09 的版本 2；请在输入设置中确认版本。

```python
from pathlib import Path
import sqlite3
import pandas as pd

# 仅挂载一个日库，避免匹配到其他日期的数据文件。
db_path = next(Path('/kaggle/input').rglob('data.sqlite3'))
conn = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)

pd.read_sql_query(
    "SELECT name,type FROM sqlite_schema "
    "WHERE type IN ('table','view') ORDER BY type,name",
    conn,
)
```

下面的 SQL 可以逐条传给 `pd.read_sql_query(sql, conn)`，也可用于下载后的 SQLite 文件。

查看交易表字段：

```sql
PRAGMA table_info(trades);
```

先确认当天市场覆盖情况：

```sql
SELECT * FROM updown_coverage;
```

查询按项目方法派生的市场金额，单位为微 USDC：

```sql
SELECT date_utc, market_volume_micro_usdc
FROM updown_market_volume_daily;
```

查询钱包金额前十名：

```sql
SELECT wallet_rank, wallet, amount_micro_usdc
FROM updown_wallet_ranked
WHERE date_utc = '2026-11-09' AND wallet_rank <= 10
ORDER BY wallet_rank;
```

这个钱包排行累计 **BUY＋SELL**，与海报中的 **BUY-only** 钱包金额不同。查询结束后执行 `conn.close()`。SQLite 文件在数据集页面可能没有浏览预览，仍可通过 Notebook 查看其表、字段和内容。

### 使用 CLI 下载

也可以使用数据集页面的 **Download** 按钮，或安装 [Kaggle CLI](https://github.com/Kaggle/kaggle-cli)。CLI 需要 Python 3.11 或更新版本；在虚拟环境中安装，并按官方[认证说明](https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md#authentication)配置访问凭据。下面的命令固定数据集版本，避免后续更新改变你的输入。

```bash
python -m pip install kaggle

# 查看原始日库版本 2 中的文件。
kaggle datasets files qiuqiuslowchillchill/pm-updown-raw-2026-11-09/2

# 下载原始日库及其元数据。
kaggle datasets download \
  qiuqiuslowchillchill/pm-updown-raw-2026-11-09/2 \
  -p ./polymarket-2026-11-09 --unzip

# 现成日报：分析数据集版本 3，只下载这一个 JSON。
kaggle datasets download \
  qiuqiuslowchillchill/pm-updown-analysis-2026-11/3 \
  -f daily_2026-11-09.json -p ./polymarket-results --unzip
```

末尾的 `/2`、`/3` 是 Kaggle 数据集版本号；省略时会请求最新版本。查询下载的数据库时，可以将前面的 `db_path` 改为 `Path('./polymarket-2026-11-09/data.sqlite3').resolve()`，或使用 SQLite 客户端。

只需现成指标时，可以直接读取分析 JSON，不加载原始交易：

```python
import json
from decimal import Decimal
from pathlib import Path

report = json.loads(Path('./polymarket-results/daily_2026-11-09.json').read_text())
current = report['payload']['current']
print(report['status'], report['source_versions'])
print(Decimal(current['volume_micro_usdc']) / Decimal(1_000_000))
```

该固定版本的市场金额为 `9786444.035628` USDC。其他可用结果见**同一分析数据集版本**中的 `index.json`；例如 `results['daily:2026-11-09']['filename']` 给出相应文件名。

## 市场范围、更新与完整性

| 资产 | 周期 | 系列数 |
| --- | --- | ---: |
| BTC、ETH、SOL、XRP、DOGE、BNB、HYPE | 5 分钟、15 分钟、1 小时、4 小时、日线 | 35 |
| ZEC | 5 分钟、15 分钟、4 小时 | 3 |

全部系列启用后的配置为 **3,295 个计划时段／UTC 日**。同一时段可能有多个真实合约，因此实际市场数可以更多。历史计划时段按目录配置的首个市场结束时间调整，不能把 3,295 直接用于所有历史日期。系列定义见 [采集代码](scripts/polymarket_updown_daily.py)。

2026-11-10 官方完整目录实际有 **3,298 个市场**：BTC 在原 Binance 合约之外新增了 1 个日线及 2 个小时线 Chainlink TWAP 合约，condition 各不相同。这些合约仍属于原先的 38 个系列范围，采集程序会全部保留，并核对完整目录、计划时段及每个已发现条件的分页。详见[生产记录](docs/kaggle_migration.md)。

采集按日运行，先处理新日期，再从较早日期补采历史。**历史范围尚未补齐。**某日或某月的数据集存在，不表示整段历史已经完整；月度分析库名称也不表示当月所有日期或完整月报已就绪。采集安排不保证每天在某个精确时刻发布。

11 月 11 日的生产恢复任务（Notebook v13）先处理新日期，再重新处理 **2025-03-15 至 2025-04-02 已有的 19 个未完成日期**。当时其中 18 日已完成全部交易分页：6 日生成正式报告，12 日保留重建错误；3 月 31 日仍无法取得官方市场目录，历史游标保存为 **2025-04-03**。11 月 13 日生产 v24 验收后，游标已推进至 **2025-09-11**，见[当前验收记录](docs/kaggle_cloud_autonomy_20261113.md)。后续每日运行优先采集新日期，再从云端最新已确认的游标继续。未解决的日期保留为缺口，不标为完成，也不填零。

恢复机制默认每 **30 分钟**检查是否有新的已提交采集状态，仅在有变化时发布 `incomplete` 检查点；采集运行 **11 小时 30 分钟**后停止，给 12 小时会话上限预留发布时间。这些是可调整的工程参数，不是 Kaggle 使用规则或防封阈值。它不在每页后上传；中断后从最后**确认成功**的数据集版本恢复，上传失败或强制终止可能丢失超过 30 分钟的未发布进度。没有变化的月度快照也不反复发布。详情见 [恢复操作说明](docs/kaggle_migration.md)。

使用原始日库前，查看 `collection_status.json`：

- `status: complete`：目标市场覆盖与分页完成，并通过项目的正式统计重建检查。
- `status: incomplete`：可恢复的采集快照，可能缺市场或分页，不能当作完整日统计。
- 完整市场没有交易记录时，金额可以为零。未完成、缺失或查询没有返回行时，不能据此填零。

新采集快照记录 `coverage_policy: official_series_all_conditions_v1`。SQL 同时要求各适用系列目录分页读完、计划时段无缺口，以及全部已发现 condition 的交易分页完成。同一时段多出的合约不能抵消其他缺失时段；`updown_expected_slots`、`updown_discovery_status` 视图保留这些依据。旧的已完成 v2 输入仍按 `legacy_nominal_v1` 读取。

`updown_coverage` 提供整日覆盖，`updown_series_coverage` 提供逐系列覆盖。这里的完整性指目标范围内接口采集和项目统计检查，不是全量链上对账，也不保证上游接口返回了所有历史交易。

全部分页已取得，但统计重建未通过时，可以同时出现 `coverage.is_complete: 1` 和 `status: incomplete`。原始记录仍保留，`error` 说明暂不生成正式报告的原因。这本身不能证明上游数据错误。

周报和月报要求全部目标日期完成；缺失的比较周期或为零的比较基数显示为 `N/A`，不会用部分日期之和替代完整周期。

正式分析 JSON 的 `status` 为 `complete`，并通过 `source_versions` 引用原始日库的准确版本。机器人历史覆盖见 `payload.bot_history`，包括实际观察日期和历史是否仍待补齐。

## 文件与数据结构

### 原始日库

```text
pm-updown-raw-YYYY-MM-DD/
├── data.sqlite3
├── collection_status.json
├── README.md
├── migration_summary.json    # 由旧格式转换的日库附带；新采日期不一定有
└── publication.json          # 恢复修复的发布标记；旧示例不一定有
```

完成的 v2、v3 日库均有 `markets`、`collection_progress`、`trades` 三个实体表。已发布示例的 `PRAGMA user_version` 与 `collection_status.json.schema_version` 为 `2`；本次修复格式为 `3`，继续记录 `group_key` 和 `volume_method`。下方字段说明以 v3 为准：相比 v2，放宽 `markets.date_utc` 的非空约束，并增加可空结算元数据，不改变交易字段和分组口径。固定 v2 示例及早期未完成 v3 快照可能没有这些可选元数据列，应以所选版本的 `PRAGMA table_info(markets)` 为准。

恢复修复写出的快照另带 `publication.json` 小标记，用于上传返回 HTTP 499 等不确定结果时核对发布身份；不要求读者计算整库 SHA。旧的固定示例不因此被认为已经包含这个新文件。

下表未标为 `NOT NULL` 的字段可能为空，准确约束见 schema 源文件。

`incomplete` 日库可能另有 `pending_group_map`，用于未完成市场的分页续采。该表将交易哈希映射到短编号，不属于完成日库的数据模型；完成后删除。v2、v3 完成日库不含长交易哈希列，也没有独立 `market_volume` 实体表。

#### `markets`：市场元数据

| 字段 | SQLite 类型与约束 | 含义 |
| --- | --- | --- |
| `slug` | `TEXT PRIMARY KEY` | 市场标识；其他表通过它关联市场 |
| `date_utc` | `TEXT`，v3 可空 | 已知的**真实市场开始日**，UTC `YYYY-MM-DD`；未知为 `NULL`，不是日库分区日期；v2 为 `NOT NULL` |
| `market_id` | `TEXT` | 上游市场 ID |
| `condition_id` | `TEXT UNIQUE` | 上游 condition ID；尚未取得元数据时可为空 |
| `event_id` | `TEXT` | 所属 event ID |
| `title` | `TEXT` | 市场问题或标题 |
| `series_slug` | `TEXT` | 市场系列，例如 `btc-up-or-down-5m` |
| `event_start_time` | `TEXT`，可空 | 真实市场开始时间，UTC ISO 8601；未知为 `NULL`，不使用创建时间或结束时间推造 |
| `end_time` | `TEXT` | 市场结束时间，UTC ISO 8601；决定日库归属 |
| `resolution_source` | `TEXT`，可空 | API 提供的结算价格来源；未知为空 |
| `description` | `TEXT`，可空 | API 原始合约说明，保留其中的结算规则 |

#### `collection_progress`：分页状态

| 字段 | SQLite 类型与约束 | 含义 |
| --- | --- | --- |
| `market_slug` | `TEXT PRIMARY KEY`，外键至 `markets(slug)` | 对应市场 |
| `query_params` | `TEXT` | 交易查询参数的 JSON 文本 |
| `next_cursor` | `TEXT` | 下次分页游标；为空也可能表示尚未开始，需结合完成时间判断 |
| `committed_page` | `INTEGER NOT NULL DEFAULT 0` | 最后成功提交的页号，0 表示尚未提交分页 |
| `completed_at` | `TEXT` | 分页完成时间；为空表示尚未完成 |
| `last_error` | `TEXT` | 最近采集错误；完成后清除 |

#### `trades`：钱包视角记录

主键为 **`(market_slug, page_number, row_number)`**，表使用 `WITHOUT ROWID`。这三个字段标识该版本中的保存行，不表示全局成交 ID。

| 字段 | SQLite 类型与约束 | 含义 |
| --- | --- | --- |
| `market_slug` | `TEXT NOT NULL`，外键至 `markets(slug)` | 对应市场；主键的一部分 |
| `page_number` | `INTEGER NOT NULL` | 采集页号，从 1 开始；主键的一部分 |
| `row_number` | `INTEGER NOT NULL` | 页内行号，从 0 开始；主键的一部分 |
| `wallet` | `TEXT NOT NULL` | 小写钱包地址 |
| `group_id` | `INTEGER NOT NULL`，检查整数且大于 0 | 同一市场内的交易组短编号 |
| `timestamp` | `INTEGER NOT NULL` | 交易时间，Unix 秒 |
| `side` | `TEXT NOT NULL` | 钱包视角的方向，例如 `BUY`、`SELL` |
| `outcome` | `TEXT NOT NULL` | 结果方向，例如 `Up`、`Down` |
| `outcome_index` | `INTEGER NOT NULL` | 上游结果索引 |
| `token_id` | `TEXT NOT NULL` | 对应结果 token 的标识 |
| `raw_price` | `TEXT NOT NULL` | 保留十进制精度的价格字符串 |
| `raw_size` | `TEXT NOT NULL` | 保留十进制精度的份数字符串 |
| `amount_micro_usdc` | `INTEGER NOT NULL` | 每行 `raw_price × raw_size` 舍入后的整数微 USDC |

完整字段与约束以 [SQLite schema](sql/btc5m_schema.sql) 为准。

### 全系列 SQL 视图

日库提供以下七个 `updown_*` 视图，此外可保留原 BTC 5 分钟流程的兼容视图。

| 视图 | 关键字段与用途 |
| --- | --- |
| `updown_target_series` | `series_slug`、`expected_markets`、`first_end_time`：系列目录与配置的首个市场结束时间 |
| `updown_series_coverage` | `date_utc`、`series_slug`、`expected_markets`、`discovered_markets`、`completed_markets`、`committed_pages`、`is_complete` |
| `updown_coverage` | 按 `date_utc` 汇总上述覆盖数量，不含 `series_slug` |
| `updown_market_volume_daily` | `date_utc`、`market_volume_micro_usdc`：组内首行派生金额；未完整日为 `NULL` |
| `updown_wallet_daily` | `date_utc`、`wallet`、`amount_micro_usdc`：完整日期的钱包 BUY＋SELL 金额 |
| `updown_wallet_ranked` | 钱包日金额字段及 `wallet_rank`，按金额降序、地址升序排序 |
| `updown_daily_summary` | `date_utc`、`unique_wallets`、`wallet_total_micro_usdc`、`top200_micro_usdc`、`top200_share`、`market_volume_micro_usdc` |

`top200_share` 是 0～1 的比例，不是已经乘以 100 的百分数。SQL 定义见 [updown_daily.sql](sql/updown_daily.sql)。

### 月度分析库与报告 JSON

```text
pm-updown-analysis-YYYY-MM/
├── features.sqlite3
├── index.json
├── daily_YYYY-MM-DD.json
├── weekly_YYYY-MM-DD.json     # 日期为周一
├── monthly_YYYY-MM.json
├── progress.json             # 全局采集与历史状态；并非每个历史月份都有
└── publication.json          # 恢复修复的发布标记
```

周报归入最后一个包含日期所属月份，例如 `2026-10-26` 至 `2026-11-01` 的周结果归入 11 月分析库。只有完成的报告周期才会出现在结果索引中。

`features.sqlite3` 的三个表用于合并统计，不存储逐笔交易：

| 表 | 字段与类型 |
| --- | --- |
| `days` | `date_utc TEXT` 主键；`features_json`、`result_json` 为非空 `TEXT`，保存每日特征摘要与结果 |
| `wallet_days` | `(date_utc, wallet)` 为 `TEXT` 联合主键；`trade_count`、`first_timestamp`、`last_timestamp` 为非空 `INTEGER`；`amount_micro_usdc` 为非空 `TEXT`，保存 BUY-only 整数金额 |
| `daily_hits` | `(date_utc, wallet)` 为非空 `TEXT` 联合主键，记录当天命中规则的钱包 |

`wallet_days` 的活动次数和时间范围统计 BUY＋SELL；它的 `amount_micro_usdc` 与报告的 `payload.current.wallet_volume_micro_usdc` 都只统计 **BUY**。前者采用十进制整数字符串 `TEXT` 保存，与原始日库钱包视图的 BUY＋SELL 金额口径不同。`daily_hits` 记录当天首次或重复命中规则的钱包，不是当天全部沿用历史标签的钱包。字段定义见 [分析代码](scripts/polymarket_kaggle_analysis.py)。

报告 JSON 接口仍为 v1，与原始库格式 v2、v3 分开编号。已完成 v2 输入仍可保留在来源引用中。主要字段为：

- `kind`、`target_utc`、`start_date_utc`、`end_date_utc_exclusive`：日、周、月类型和 UTC 半开日期区间。
- `status`、`source_versions`：报告完成状态与原始日库版本引用。
- 日报的 `raw_schema_version`、`volume_method`，以及周月报按日期记录的 `raw_schema_versions`、`volume_methods`：来源格式与金额方法。
- `payload`：总览、比较周期、机器人规则和历史范围。
- `distribution`：重建后的 BUY 参与成交数量、份数、Up 等价名义金额与价位分布。

## 关键统计口径

**日期。**数据集日期和全系列视图按 **API 原始 `markets.end_time` 的 UTC 日期**归属；`markets.date_utc` 只在真实开始时间已知时保存开始日，v3 未知时为 `NULL`。`event_start_time` 原本就允许为空；不能把创建相关的 `startDate` 当作真实开始时间，也不能用 `end_time - 24 小时` 补造。选定市场的全部可得交易都会纳入，包括提前或跨日成交。原 `btc5m_*` 视图使用开始日，不能与 `updown_*` 直接混用。

**记录粒度。**`trades` 每行是上游返回的一条钱包视角记录。同一交易组可含汇总方与多个对手方记录。不能用原始行数或交易组数直接替代重建后的成交笔数，也不能按相同价格、数量删除看似重复的行。逐行金额求和不是单边市场金额，除以二也不是通用成交重建规则。

**分组键。**`group_id` 由市场内的交易哈希映射得到，只能在同一个市场和同一个日库版本内使用。它不是链上交易哈希、全局 ID 或独立 fill ID，也不保证跨重新采集或不同数据集版本保持一致。

**金额精度。**`1 USDC = 1,000,000 微 USDC`。每行金额采用十进制 `raw_price × raw_size`，按 `ROUND_HALF_UP` 舍入至整数微 USDC 后再求和。精确研究应保留整数或使用 Python `Decimal`，不要先将原始字符串转为二进制浮点数。

**市场金额。**当前只采集 `taker_only=false`，不再另取 `true` 路由。市场金额按 `(market_slug, group_id)` 分组，取保存页号、行号最早记录的金额再求和。这是项目的首行方法，不是官方接口永久排序或撮合结构保证。部分兼容字段仍使用 `volume_basis: taker_only` 这一历史指标名称，实际计算方法应查看 `volume_method`，当前为 `wallet_group_first_row_v1`。

**钱包金额与 BUY 分布。**原始钱包视图和 Top200 使用 BUY＋SELL；海报及月度特征中的钱包金额使用 BUY-only。BUY 分布按项目方法重建成交，将方向换为 Up 等价价格，再计算份数与名义金额。机器人参与统计覆盖至少含一个带标签 BUY 参与者的重建事件；其参与金额占比使用这些事件的 Up 等价名义金额，不是机器人钱包自身现金支出的占比。BUY 分布的金额和分母不能与钱包现金流或市场总额互换。

**机器人标签。**同一 UTC 市场结束日内，钱包活动记录数大于 1、首末跨度至少 90 分钟且平均间隔不超过 60 秒时，命中高频行为规则。活动特征包括 BUY＋SELL；标签从最早有效命中日起延续，较晚命中不应用于较早日期。历史补采可能发现更早命中并更新云端统计。这是行为筛选，不是身份验证，也不能确认钱包由机器人控制。

## 已发布 v2 的历史质量证据与复现

[2026-11-09 日库版本 2](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2)包含公开的 `migration_summary.json`，记录从版本 1 转为短编号格式的比较结果：

| 检查项 | 结果 |
| --- | ---: |
| 市场 | 3,295 |
| 钱包记录行 | 1,916,447 |
| 市场内交易组 | 686,988 |
| 与旧 `taker_only=true` 摘要金额不一致的市场 | 0 |
| 派生市场金额 | 9,786,444,035,628 微 USDC |
| 钱包特征、规则命中与报告业务值 | 转换前后不变 |
| SQLite 文件大小 | 547,946,496 → 378,290,176 字节，减少 30.96% |

旧数据集版本继续保留。这个文件大小变化与该日迁移检查，不能推断所有日期均无缺失或未来接口都符合首行方法。

v2 实现版本 [`c16edb8`](https://github.com/slowchillchill/predict-market-analysis/tree/c16edb8353aa78fe6229bc3e164f13788d1e1296)验收通过 **340 项测试**，覆盖分页事务、定向重试、旧格式兼容、金额精度、统计及图文生成。另做真实两会话续采试验：先保存一页，再从后续会话恢复三页，共保留 **3,209 行、1,200 组**，原记录和组号不变，完成后清空续采映射。这些保留为旧版 v2 的历史证据；本次 v3 的验收记录另列于下文。该样本不表示全历史已采齐。详见 [项目记录](docs/kaggle_migration.md)、[分组测试](tests/test_polymarket_trade_groups.py)和[迁移测试](tests/test_polymarket_kaggle_migration.py)。这些是项目自身的检查，不是独立审计。

## v3 恢复验收与当前部署

此前 v3 恢复修复通过 **420 项回归测试**。Kaggle 跨进程续采保留了前两个已保存分页的原始十进制值、页序和组号，持久映射保持连续。真实上传后模拟客户端 HTTP 499，分别验证了新建和更新发布的结果核对。已验证的现代样本共 **3,209 行、1,200 组**，实际从 v2 升级为 v3 后，通过正式统计导出和最终三表检查。

历史样本完成 **500 页、4,996 行**，但其中一个交易组未满足原有 BUY 重建条件，因此没有生成正式报告。将相同记录的分组列一一替换为旧键后，仍出现相同错误，其他交易字段未变。这本身不能证明上游数据错误。原始快照继续保留 `incomplete` 和错误说明；恢复验收通过不表示每个历史日期都能成功出报。

11 月 13 日的云端修复及合并前审查补修通过 **509 项测试**。生产 Notebook 第 23 版完成 11 月 12 日的 **3,295/3,295 个市场、4,605 页**，并基于已确认的月库 v285 生成文字稿。全新第 24 版会话复用此前未确认的 9 月 10 日 raw v1，在月库 v290 补齐引用，将历史游标推进至 9 月 11 日，并保留 100/101 的覆盖缺口；已有文字稿未改变。正式第 25 版使用 `RUN_ARGUMENTS=[]` 和 Kaggle 平台凭据。Daily 已恢复，开始日期为 11 月 13 日；本地四个报表定时器已停用。首次恢复后的 Scheduled v26 已核对启动与云端恢复，整次运行结果尚待验收。准确版本与证据见[本次修复记录](docs/kaggle_cloud_autonomy_20261113.md)。

为了复现结果，请同时记录**数据集名称与版本、日期范围、原始库格式版本、金额方法和所用代码提交**。分析结果中的 `source_versions` 可用于定位输入；固定版本后再比较研究结果，避免混用历史修订前后的统计。

## 许可、方法与贡献

- GitHub 软件主体采用 [MIT](LICENSE)；Kaggle 启动脚本依文件声明采用 Apache 2.0，第三方字体与依赖保留各自许可。
- 整理数据采用 CC0 1.0，声明仅适用于贡献者拥有的权利。数据来自 [Polymarket Gamma API](https://gamma-api.polymarket.com) 和 [Data API](https://data-api.polymarket.com)，仍应查看 [上游来源条款](https://polymarket.com/tos)。软件许可不授予额外的第三方数据或服务权利。
- 发现缺口、字段问题或口径差异时，请提交 Issue，并提供数据集版本、日期、市场标识、最小 SQL 或复现步骤及预期结果。请勿提交访问凭据。

进一步阅读：[市场发现与日报](docs/updown_daily.md)、[BUY 分布方法](docs/updown_bot_price_poster.md)、[X 与 LinkedIn 文字稿生成](docs/updown_publishing.md)、[部署与维护](docs/kaggle_migration.md)。

云端生产程序从已确认的 Kaggle 统计 JSON 生成英文 X 串联推文和 LinkedIn 动态。每个月度分析 Dataset 通过 `social.archive.json` 保存文字稿和生成状态，新会话先恢复已有内容，再生成缺少的周期。本地 `outputs/social/` 入口保留用于预览。LinkedIn 使用纵向指标块。旧图文和已发布记录保留；实际发布由用户另行签发给发布 AGENT。
