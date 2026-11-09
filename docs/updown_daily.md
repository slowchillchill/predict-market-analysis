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

每个完整 UTC 日按全部系列启用后的安排应有 3,295 个市场：5 分钟 2,304 个、15 分钟 768 个、
小时线 168 个、4 小时 48 个、日线 7 个。按系列分别检查覆盖，缺少系列或分页未完成时不生成完整日金额。
已核实的系列历史启用边界由同一目录维护，上线之前的时段不计入预期，上线首日按实际时段计算。
其他未经核实的缺口仍会列出，不把缺失市场计作零成交。

### 已核实的历史启用边界

2026-10-01 核对官方系列元数据、按结束时间排序的历史事件列表和首期详情：ZEC 三个系列均在
2026-08-04 创建，第一期市场结束时间如下。这里使用结束时间而非系列创建时间决定首日市场数。

| 系列 | 第一期 UTC 结束时间 | 2026-08-04 预期市场数 |
| --- | --- | ---: |
| ZEC 5 分钟 | 2026-08-04 21:40 | 28 |
| ZEC 15 分钟 | 2026-08-04 21:45 | 9 |
| ZEC 4 小时 | 2026-08-05 00:00 | 0 |

对应官方详情：[5 分钟首期](https://gamma-api.polymarket.com/events/slug/zec-updown-5m-1785879300)、
[15 分钟首期](https://gamma-api.polymarket.com/events/slug/zec-updown-15m-1785879000)、
[4 小时首期](https://gamma-api.polymarket.com/events/slug/zec-updown-4h-1785873600)。

`scripts/polymarket_updown_daily.py` 中的 `SERIES_FIRST_END_UTC` 是这些边界的来源，
`updown_target_series` 是供离线查询使用的数据库投影。采集入口会更新已有 `updown_*` 视图，
日、周、月报告均使用更新后的每日覆盖定义。月报比较期间涉及系列上线时会注明范围变化。

## 下载与续传

### 一次运行：下载 UTC 昨日并生成海报和推文

安装 `requirements-btc5m.txt` 和 `requirements-poster.txt` 中的依赖后直接执行。
脚本内置香港代理地址 `socks5h://127.0.0.1:20810`：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_yesterday.py
```

脚本在启动时用系统时钟确定 UTC 昨日，固定采集区间为昨日 00:00 至今日 00:00（不含上限）。
下载跨过午夜也不会改变本次海报日期；系统时钟应准确。代理用于网络请求，脚本不切换 VPN 节点，
也不检测代理出口所在地。其他环境可用 `--proxy` 覆盖默认地址，`--db` 和 `--output-dir` 可指定数据库和输出目录。

采集复用现有数据库与续传逻辑，已完成市场不重新下载成交。只有采集入口成功返回后才运行海报入口，
并自动传入 `--with-distribution`：生成 `updown_YYYY-MM-DD.png`、`updown_YYYY-MM-DD.json`、
`updown_YYYY-MM-DD_tweet.md`，以及 `updown_buy_trade_distribution_all_series_YYYY-MM-DD.png` 和同名 JSON。
两套图文共用启动时固定的目标日期、数据库及输出目录；分布覆盖数据库中的全部加密系列。
采集有缺口、失败或中断时，本次不进入生成步骤，原有输出文件保留。退出码沿用两个入口的结果。
分布完整性或重建校验失败时海报入口返回 `2`，不新写本次图文；总览发布侧文件在图文全部生成后才写入。

每日全系列分布页现使用已批准的图表版：顶部三张卡片显示两组 Trades、Shares、Up 等价名义金额及机器人买方参与比例；下方两张横向条形图按十美分价档显示成交笔数、本组笔数占比、Up 等价名义金额（USDC）及占全部 BUY 名义金额的比例。每档 Shares 完整保留在分布 JSON。总览引用记录 `layout_version=buy_price_distribution_chart_v1`。英文推文和抖音四卡同步生成，清单最后更新；每日期共 11 个文件。采集、成交重建和持续机器人标签规则沿用原口径。

当前用户级 `polymarket-updown-yesterday.timer` 每天 Asia/Tokyo 09:30 执行上述原入口。入口及默认路径保持一致，更新脚本后下一次定时运行会自动使用新版，无需增加第二个定时任务。

只下载 UTC 昨日；环比使用数据库已有的前日数据，前日缺失时沿用海报的 `N/A` 规则。
同一天重跑可继续下载，完成后覆盖该日期的输出；跨 UTC 日期重跑会自动改为新的昨日，补旧日期请使用下方指定日期命令。

### 指定日期下载

依赖及香港代理要求沿用 [BTC 指南](../README_BTC5M.md)。在仓库根目录运行：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_daily.py collect \
  --start-date 2026-09-19 --end-date 2026-09-20 \
  --proxy socks5h://127.0.0.1:20810 \
  --db data/raw/btc5m.sqlite3

```

通过 Gamma `/events/keyset` 按实际系列和结束时间发现市场，将返回的 `next_cursor`
作为下一页的 `after_cursor`，直到没有后续游标，并在本地排除上限日期的市场。
列表扫描后，对计划内尚未发现的时段，通过 `/events/slug/{slug}` 详情接口补查。实际复现过
列表遗漏、详情仍可查询的停用市场；`active` 和 `closed` 状态不改变市场结束日统计归属。
详情返回的系列和市场结束时间仍按相同规则处理，404 或请求失败保留为覆盖缺口。
小时线和日线的候选名称按纽约时区构造，短周期名称按市场开始时间戳构造。

钱包成交查询沿用 `/v2/trades`、`taker_only=false`、`filter_type=TOKENS`、`filter_amount=1e-18`、
`limit=1000`。跟随游标直到 `next_cursor=null`，保留每条返回记录，不按交易哈希去重。
市场成交额另以 `taker_only=true` 查询，逐笔金额按同样规则舍入后求和；每笔成交仅计吃单方一次。
`market_volume` 保存各市场的单边金额与独立分页进度，逐页原子累计，已完成市场不重复查询。
“完整”指采集时这些参数下官方接口三年窗口内的全部分页，未作全量链上对账。
采集命令成功要求钱包与单边分页都完成，原 `is_complete` 仍表示钱包采集覆盖，
`market_volume_is_complete` 表示单边覆盖。

直接复用 `data/raw/btc5m.sqlite3`；已下载三天的 BTC 5 分钟市场、成交和进度保留。
相同市场已完成则跳过，未完成则从已提交游标续传。按 `Ctrl+C` 中断后可重跑同一命令。
每页成交与进度原子提交；游标过期时只重建该未完成市场。数据库一次运行一个采集进程。

`markets.date_utc` 保留原市场开始日语义；新 `updown_*` 视图从 `end_time` 推导结束日，
不复制成交明细。原 `btc5m_*` 视图只包含 BTC 5 分钟，并继续按开始日统计。
新入口首次连接会安装这些视图，不需要手动导入 SQL。

## 离线统计

需要发布英文数据海报时，使用[海报生成脚本](updown_poster.md)。输入日期即可生成固定版式的 PNG，
包含市场数、成交额、去重钱包数、环比及疑似机器人指标。

每日海报与买入分布共用 `first_hit_day_onward` 标签：从当前数据库最早相关市场结束日开始，
每天将全部加密系列的同一钱包 BUY＋SELL 记录合并，首次满足记录数大于 1、平均间隔≤60秒、
首末跨度≥90分钟时标记。从首次命中当日及以后持续归为疑似机器人，不用未来命中回算更早日期。
同一钱包之后即使当天未再达到频率条件，也仍纳入相应机器人统计。
历史未完整采集的日期只提供现库首次观察证据，不能据此确定真实首次操作日；
相关覆盖信息与标签规则在分布 JSON 的 `bot_history`／`bot_rule` 中保留。

```bash
python3 scripts/polymarket_updown_daily.py report \
  --date 2026-09-17 --db data/raw/btc5m.sqlite3 --output-dir outputs/updown
```

生成中文报告、日汇总 CSV 和前 200 钱包 CSV。钱包成交额为买入支出加卖出收入，
每条 `price × size` 按 `ROUND_HALF_UP` 舍入至微 USDC 后求和，与既有统计计算一致。
该分母是目标市场的全部钱包成交额，不应与只计单边的其他平台成交量直接混用。
日汇总另含 `market_volume_micro_usdc` 与 `market_volume_usdc`。未采单边的历史日保留钱包统计，
市场成交额显示缺失，不用零替代。钱包采集缺口日仍只输出覆盖报告。报告只读，不联网。

本次从 2026-10-06 定时任务开始采集 UTC 2026-10-05 结束市场，不补采此前单边数据。

### 单独导出买入分布统计

```bash
python3 scripts/polymarket_updown_daily.py distribution \
  --date 2026-11-07 --db data/raw/btc5m.sqlite3 --output-dir outputs/updown
```

仅用 Python 标准库执行只读短查询，输出 `updown_buy_trade_distribution_all_series_2026-11-07.json`。
原始成交记录不改动；两个群体分别统计买入参与的重建成交、Shares、Up 等价名义金额及比例。
重建按每市场、交易哈希组首条汇总记录与后续对手明细配对，每条明细形成一笔成交；
汇总行不另计，双方均为机器人买入也只计一次。日、周、月分布使用同一个内核与同一套持续标签。
成交比例以各群体笔数为分母，名义金额比例以全部钱包 BUY 总名义金额为分母。
Up 等价名义金额不替代总览中每笔只计吃单方一次的市场成交额。
不完整或不满足重建规则时返回 `2`，不导出新文件。详细规则见[买入分布说明](updown_bot_price_poster.md)。

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
