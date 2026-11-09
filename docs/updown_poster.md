# 加密货币 Up/Down 英文海报

`scripts/polymarket_updown_poster.py` 读取本地数据库，复用已确认的深蓝 Web3 背景，
生成 1600 × 2000、4:5 竖版 PNG 和配套英文推文。日期、英文文字和数字由程序排版，日常运行完全离线。

## 安装与运行

在仓库根目录安装海报依赖；现有采集环境可以继续使用：

```bash
.venv-btc5m/bin/python -m pip install -r requirements-poster.txt
```

生成 UTC 2026-09-20 结束的目标市场海报：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_poster.py --date 2026-09-20
```

之后只需替换日期。完整参数示例：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_poster.py \
  --date 2026-09-20 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/posters --with-distribution
```

输出三个对应日期的文件，重复生成同一日期会更新对应文件：

- `outputs/posters/updown_2026-09-20.png`：可用于 X 的英文海报。
- `outputs/posters/updown_2026-09-20.json`：当前日、前日的未舍入数据、机器人规则和海报显示值。
- `outputs/posters/updown_2026-09-20_tweet.md`：可直接粘贴到 X 的完整英文推文正文。

加入 `--with-distribution` 时，同目录还生成全部加密系列的买入分布页：

- `updown_buy_trade_distribution_all_series_2026-09-20.png`。
- `updown_buy_trade_distribution_all_series_2026-09-20.json`。

每日分布页使用已批准的三张摘要卡和上下双横向条形图。卡片保留两组精确 Trades、Shares、Up 等价名义金额及三项机器人参与比例；每档显示成交笔数、本组笔数占比、Up 等价名义金额（USDC）及占全部 BUY 名义金额的比例。条形长度表示本组笔数占比，两图从零起、使用相同刻度。每档 Shares 及其占全部 BUY 份数的比例保留在完整 JSON，不在主图逐档展开。不含 NOTIONAL MIX 图。含分布时，总览机器人模块与分布页共用同一套 BUY 事件汇总、Up 等价金额及对应全部 BUY 分母；总览的吃单方市场成交额保留为独立指标。
详见[分布统计与版式](updown_bot_price_poster.md)。

含分布的每日总览现使用已批准英文文案，版本为 `buy_activity_approved_v1`，存于总览 JSON 顶层和 `display.copy_version`。正文集中定义在 `scripts/polymarket_updown_poster_copy.py`，主标题为 `BUY Trade Activity`。两组汇总显示笔数、份数、Up 等价名义金额，以及各自占全部 BUY 的三项比例。历史标签活跃钱包数与当天达到频率规则的钱包数分别列示，后者由原 count/span 条件独立计数，不改变机器人名单。

分布 JSON 的 `share_of_all_buy_shares_percent` 在群体和每档都以全部 BUY 的未舍入成交份数为分母。图中的 `Group %` 以本组总笔数为分母；`Notional USDC · All BUY %` 以全部 BUY 的 Up 等价名义金额为分母。显示百分比逐项舍入，因此显示行之和可能与未舍入值计算的合计存在舍入差异。

昨日自动流程默认传入该参数。手动省略参数可继续仅生成原总览。
总览 JSON 的 `trade_distribution` 保存新增 JSON、PNG 的相对文件名、SHA256 和口径。
每日图表版同时保存 `trade_distribution.layout_version=buy_price_distribution_chart_v1`，由 `scripts/polymarket_updown_distribution_chart.py` 排版。日、周、月共用的分布入口只对每日全系列启用此版式，周/月及单系列诊断图继续使用原表格版。
总览读取事务关闭后，分布以独立只读连接执行短查询；分布校验失败返回 `2`，不会新写本次图文。
发布侧文件在所有新资产和引用写入后生成。含分布时，正文展示两组 BUY 的 Trades、Shares 和 Up 等价事件金额；机器人比例明确表示其买方参与的成交覆盖率（以 Up 等价金额加权），不称为钱包自身资金贡献率。正文按280字符预算排版。
抖音日报四卡为总览、全部钱包买入分布、疑似机器人买入分布、数据与方法。英文平台的清单图片数组为总览和分布两页，周/月默认为六张中文卡片。无分布模式的钱包金额也只统计 BUY；旧历史包按自身元数据解释，详见[发布规则](updown_publishing.md)。

数据库使用只读连接；程序不会启动采集或修改原有成交。退出码 `0` 表示已生成，
`2` 表示所选日期未完整采集、此次未生成海报。该行为沿用现有完整日统计口径。

## 推文与浏览器发布

通过 GPT 桌面应用控制浏览器发布时，将同一日期的 `.png` 和 `_tweet.md` 一起交给 AI，
说明“将 Markdown 文件全文作为推文正文，保留换行，并附上这张海报”。
Markdown 文件仅包含待发布正文，不夹带标题标记、代码围栏、图片路径或操作说明。
相较 JSON，它便于直接阅读与粘贴，无须提取字段或处理换行转义。
现有 `.json` 继续用于数据核对和程序取数；将来需要通过程序接口发布时，可由程序读取正文文件。

推文与海报复用同批已加载数据。无分布时，机器人钱包金额及分母均为钱包侧原价 BUY 金额，SELL 不计入金额。含分布时，机器人模块使用重建 BUY 事件及 Up 等价权重，同一事件只计一次；原市场金额始终只计吃单方一次。
Up 等价事件金额与钱包自身原价买入金额是两个指标，不互换名称或分母。英文日推文使用 `Bot-buyer subset` 和 `bot-buyer participation` 标注买方参与，并明确 `Notional=Up-equivalent price*shares`。紧凑格式保留两位小数，精确值在 JSON 和分布图中。

推文成交额达到一百万 USDC 时用 `M` 表示，按未舍入金额保留两位小数；低于一百万时显示完整金额。
环比和机器人占比复用海报显示值。前日缺失或基数为零时，环比显示 `N/A` 并说明原因。
只有钱包数和成交额的环比都显示明确涨跌时才生成趋势句；缺失或显示 `0.00%` 时省略趋势句，
不推断变化原因，也不自动评价涨跌幅度。

## 统计与显示

- 范围：文档定义的 38 个 Polymarket 加密货币 Up/Down 系列。
- 日期：市场 `endDate` 所属 UTC 日，包含这些市场的全部提前和跨日成交。
- 市场数：当天已完整采集的全部目标市场数。
- 钱包数：跨当天全部目标市场去重。
- 市场成交额：只累计 `taker_only=true` 的 `price × size`，使用整数微 USDC，每笔计一次。
- 海报钱包买入金额：只累计原始钱包记录中 side=BUY 的原价金额，SELL 不进入金额分子或分母；保留在 current 的微 USDC 字段中，basis 为 wallet_buys_only。
- 默认机器人模块：使用 buy_event_summary（basis=reconstructed_buy_event_up_equivalent）。机器人买方参与的重建成交按事件一次计入，金额为 Up 等价价格乘成交份数，分母为全部重建 BUY 事件的 Up 等价金额。
- 环比：`(当日值 − 前日值) / 前日值 × 100%`，按未舍入金额计算。
- 疑似机器人：同一钱包在该批市场中的平均成交间隔不超过 60 秒，首末成交跨度至少 90 分钟，
  不要求参与特定数量的市场。平均间隔为 `(最后成交时间 − 最早成交时间) / (记录数 − 1)`。
- 疑似机器人比例：有分布时为 Up 权重的 BUY 成交参与覆盖率；无分布时为钱包自身原价 BUY 金额占比。两种模式的标签和分母必须分别说明。属于行为筛选，不能确认身份。

金额与百分比使用十进制 ROUND_HALF_UP 保留两位小数；金额完整显示、带千分位和 USDC 单位，
市场数及钱包数保持整数。正增长带 `+`，负增长带 `-`；舍入到零的变化显示 `0.00%`。

前日未完整采集时，当前日仍可生成海报，环比显示 `N/A` 和 `prior day unavailable`。
前日基数为零时显示 `N/A` 和 `prior day = 0`；当日钱包成交额为零时疑似机器人金额占比也显示 `N/A`。
市场单边数据缺失时，总额及相关环比显示 `N/A`，钱包及机器人统计仍可展示。
这些情况不会用零冒充未知变化，也不会与历史双方金额混比。

版式沿用确认稿：标题两行、成交额通栏、市场数和钱包数双栏、独立疑似机器人区块。
排版按字体实际宽度调整字号，不截断金额、不自动换成长短失衡的行。PNG 为不透明 RGB，
不同查看器的背景颜色不会改变成品外观。

## 素材和实现

- 固定背景：`assets/posters/updown_background_v1.png`，由内置 imagegen 生成并经用户确认。
- 字体：`assets/fonts/Inter.ttf`，随项目附带；许可为同目录的 `Inter-OFL.txt`。
- 字体来源：[Google Fonts 的 Inter](https://github.com/google/fonts/tree/main/ofl/inter)。
- 数据查询：`sql/updown_poster.sql`，复用现有系列目录和采集完整性视图。
- 图片排版使用 [Pillow 字体与文字测量接口](https://pillow.readthedocs.io/en/stable/reference/ImageFont.html)。

不需要再次调用图片生成服务，也不依赖本机安装特定字体。

## 验证

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

海报测试覆盖机器人筛选边界、跨市场去重、UTC 结束日边界、前日缺口、零基数、两位小数、
只读数据库、PNG 尺寸与透明度、长数字的文本边界及货币单位基线。
