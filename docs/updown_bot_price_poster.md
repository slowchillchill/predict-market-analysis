# 买入成交价位分布海报（日／周／月）

一张 1600 × 2000 英文海报，包含全部 BUY 和疑似机器人买方参与两组价位分布。
每日全系列采用已批准的三张摘要卡和上下双横条：摘要显示两组精确 Trades、Shares、Up 等价名义金额，以及三项机器人买方参与占比。每档显示成交笔数、本组笔数占比、Up 等价名义金额（USDC）和占全部 BUY 名义金额的比例；每档 Shares 及其比例保留在 JSON。
条形从零起，长度表示本组笔数占比，两组共用刻度；无 BUY 时显示 N/A。周/月及显式单系列图继续使用数值表，完整保留每档 Shares 及其比例。日报沿用深蓝背景，周版使用酒红与香槟金，月版使用米白与墨绿；均采用 Inter 字体。
只读取现有 SQLite 数据，不调用外部数据、采集或发布流程。

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_bot_price_poster.py --date 2026-11-07
```

默认读取数据库全部加密 Up/Down 系列。当前为 38 个系列，2026-11-07 共 3295 个市场；
海报与 JSON 均明确记录系列数量和市场数量，不默认限制为 BTC 5M。

默认保存到 `outputs/previews/buy_trade_distribution_all_series_2026-11-07_v3/`：

- `updown_buy_trade_distribution_all_series_2026-11-07.png`
- `updown_buy_trade_distribution_all_series_2026-11-07.json`

支持 `--db PATH`、`--output-dir PATH`；单日仅显式传入 `--series` 时缩小成交展示范围，
疑似机器人历史识别仍覆盖数据库全部加密 Up/Down 系列。
同目录重跑覆盖该日期的 v3 输出，之前试作保留。

日报总览批准文案版本为 `buy_activity_approved_v1`；每日分布版式为 `buy_price_distribution_chart_v1`，由 `scripts/polymarket_updown_distribution_chart.py` 渲染，总览 JSON 的 `trade_distribution.layout_version` 记录该值。总览与分布页均明确使用 `All BUY Trades` 和 `Trades With Suspected Bot Buyers`，避免将参与事件量称为钱包自身支出或持仓。群体与每档 JSON 的 `share_of_all_buy_shares_percent` 都以全部 BUY 的未舍入 `shares_exact` 为分母，零分母显示 N/A。

## 周／月分布页

同一入口支持 `--week-start YYYY-MM-DD` 和 `--month YYYY-MM`，与 `--date` 三选一。
周起日必须为 UTC 周一，月为完整 UTC 自然月；周／月固定覆盖全部加密系列，不能指定 `--series`。

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_bot_price_poster.py \
  --week-start 2026-11-02 \
  --output-dir outputs/previews/weekly_buy_distribution_2026-11-02_2026-11-08_v1

.venv-btc5m/bin/python scripts/polymarket_updown_bot_price_poster.py \
  --month 2026-10 \
  --output-dir outputs/previews/monthly_buy_distribution_2026-10_v1
```

分别生成下列 PNG 和同名 JSON：

- `updown_buy_trade_distribution_all_series_weekly_2026-11-02_2026-11-08.png`
- `updown_buy_trade_distribution_all_series_monthly_2026-10.png`

周／月逐日调用与日报相同的成交重建和去重内核，再合并十档未舍入的笔数、份数和名义金额。
周期占比重新计算，不相加或平均每日百分比；周期钱包数取并集，不相加每日钱包数。
周期 JSON 记录起日、排除上界的结束日、覆盖日期、每日审计和历史标签来源。
周／月分布页已接入原周／月脚本的默认流程，同时生成新版英文推文、中文正文、抖音六卡与发布清单。上述独立入口仍可只生成分布页；原周／月入口使用 `--without-distribution` 可保留旧包。

## 每日流程接入

`polymarket_updown_yesterday.py` 采集成功后，默认调用原总览海报入口并传入 `--with-distribution`。
分布页和统计 JSON 写入同一个每日输出目录（默认 `outputs/posters/`），不再局限于试作目录。
手动一次生成总览和分布：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_poster.py \
  --date 2026-11-07 --with-distribution --output-dir outputs/posters
```

只需统计 JSON、无需 Pillow 时：

```bash
python3 scripts/polymarket_updown_daily.py distribution \
  --date 2026-11-07 --output-dir outputs/updown
```

两个入口共用同一个统计实现与 JSON 导出器。总览 JSON 中 `trade_distribution` 引用新增图文及其 SHA256，
没有把 Up 等价名义金额写入原总览市场成交额。含分布的新版日报会同步生成相应英文推文、
中文正文和抖音四卡，图片附件以发布清单为准。周／月默认新版保留原总览与趋势，另加双表分布页及对应推文；英文平台为两图、抖音为六卡，详见[发布规则](updown_publishing.md)。

## 计数与买入范围

日期按市场结束日 UTC 归属，包含目标市场的提前和跨日成交。
日、周、月共用 `first_hit_day_onward` 疑似机器人标签。系统从当前数据库最早的相关市场结束日
开始逐日观察全部加密 Up/Down 系列，将同一钱包当天的 BUY＋SELL 记录跨系列合并。
某日记录数大于 1、平均间隔不超过 60 秒且首末跨度至少 90 分钟时，该钱包首次命中规则。
从首次命中当日开始，后续日期即使不再满足频率条件，也持续按疑似机器人处理。
未来日期的命中不能回写首次命中之前的统计。它只是行为筛选，不能确认身份。

历史名单由 `polymarket_updown_bot_registry.py` 统一构建；源数据库只读，派生缓存位于
`outputs/statistics/`，使用每日市场／采集进度指纹和规则版本验证。某日首次命中日期表示
**现库首次观察证据**，不是钱包真实的首次机器人操作日期。历史缺失或未采完整的日期
保留在 `bot_history` 审计中，不将未知历史当作已证明的非机器人，也不虚构更早标签。
本次历史观察起于 2026-08-01；2026-08-05、2026-08-06 和 2026-11-01 的历史覆盖不完整，
这些日期只能提供现有记录中的观察证据。

每个市场、交易哈希组按原分页位置排序，首行为汇总方，其余行为对手明细。
校验首行份数等于所有明细份数之和、经济方向相反、时间一致，且归一化 Up 价值误差
不超过 0.000001 USDC。超过固定容差时，只允许可严格还原的汇总均价舍入尾差：
将明细反推均价按汇总价原始小数精度（至少十位）舍入，必须等于存储汇总价。
这种情况在 JSON 中单独记录，不改变明细价格或计入统计的名义金额。
每条明细关联汇总方，重建一笔成交；首行不另计一次。
不同分页位置的相同字段明细仍分别保留。计数属于按现库存储结构重建的成交，不承诺原生逐笔 ID。

全部钱包图要求汇总方或明细方至少一方 BUY，双 SELL 不纳入。
机器人图要求至少一方是已标记机器人且该方 BUY；只有机器人 SELL 而买方非机器人的成交不纳入。
双方均为机器人 BUY 时也只计一笔。两图使用相同规则，机器人图是全部钱包图的子集。

现库没有订单 ID，无法识别同一订单的多次成交。用户已选择用 **Trades（成交笔数）** 展示计数，
不把它标为独立订单数。

## 图表字段及分母

| 字段 | 定义 |
| --- | --- |
| Up price | 明细为 Up 时用原价，为 Down 时用 `1 − 原价` |
| Trades | 该价档的 BUY 参与重建成交笔数 |
| Trade % | 该档笔数 ÷ 当前图全部 BUY 参与成交笔数 |
| Shares | 每笔入选明细的成交份数，仅累计一侧 |
| Notional (USDC) | Up 等价价格 × 明细成交份数，仅累计一侧 |
| Notional % | 该档名义金额 ÷ **全部钱包 BUY 总名义金额** |

两组 Notional % 共用全钱包 BUY 金额分母。
全部钱包总金额占比为 100%；机器人总金额占比等于机器人金额占全部钱包金额的比例，
不强制显示 100%。机器人图的 Trade % 仍以机器人图自己的总成交笔数为分母。

Up 等价金额用于统一价格坐标，例如 Down 买入 95¢ 归到 Up 5¢；它不是该 Down 买家的原始支付金额。
名义金额不采用合约每份 1 USDC 的面值，也不另加另一方金额或手续费。

价格用 Decimal 在显示舍入前分档，十档均包含下界、不包含上界，最后 `[90,100]` 美分包含 100¢。
JSON 每档保存 `lower_inclusive`、`upper_inclusive` 和 `interval_cents`，避免区间歧义。
份数和金额全程精确累计，JSON 保存十进制字符串。海报金额、Shares 和比例分别四舍五入至两位。
各行单独舍入的显示值之和可能与总计有微小差异；总计和比例始终由未舍入值计算。

## 完整性和验证

只检查所选系列的钱包分页完整性，不依赖单边金额汇总表。
按市场执行短 SELECT，不持有全日读事务，读取前后核对市场与采集进度。
出现不完整日期、无法识别的方向、无效价格或份数、不平衡组、读取期间数据变化时返回 `2`，不生成新图文。
无成交或名义金额分母为零时，相关比例为 `null`，海报显示 `N/A`。成功返回 `0`。

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -p 'test_polymarket_updown_*price*.py' -v
```
