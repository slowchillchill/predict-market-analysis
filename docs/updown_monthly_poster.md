# 加密货币 Up/Down 月报

月报按上一个完整 UTC 自然月统计，与再前一个自然月比较，同时生成英文海报、统计 JSON 和 X 推文。使用米白、墨绿、石墨配色，与深蓝日报和酒红金色周报区分。图片为 1600 × 2000、4:5、不透明 RGB PNG，文字和图表由程序绘制。

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_monthly_poster.py
```

启动时按 UTC 日期固定目标月。例如在 2026-10-01 运行，统计区间为 `[2026-09-01, 2026-10-01)`，比较区间为 `[2026-08-01, 2026-09-01)`。支持跨年以及 28、29、30、31 天的月份。

指定历史月份及输出位置：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_monthly_poster.py \
  --month 2026-09 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/posters
```

默认生成原总览 `updown_monthly_2026-09.png`、同名 JSON 和 `_tweet.md`，以及 `updown_buy_trade_distribution_all_series_monthly_2026-09.png`、同名分布 JSON、中文正文、六张抖音卡片和发布清单。总览 JSON 保存分布资产的周期引用与 SHA256。
同月重跑会覆盖对应文件；预览请指定独立输出目录。`--without-distribution` 保留原总览、旧推文与旧四卡；`--with-distribution` 可显式选择默认新版。脚本只生成本地文件，平台发布是独立操作。

## 统计口径

- 市场归属取 `endDate` 所属 UTC 日，包含这些市场的全部提前及跨日成交。
- 月市场金额只计吃单方一次，使用整数微 USDC；金额与百分比使用 `ROUND_HALF_UP`。
- 月钱包数取整月钱包并集。月环比为 `(本月值 / 前月值 − 1) × 100%`。
- 日均成交额分别除以各自自然月天数后比较；不会用四周或固定 30 天代替自然月。
- 疑似机器人与日报、周报共用 `first_hit_day_onward` 标签。系统从现库最早相关市场结束日开始，逐日跨系列合并同一钱包的全部 BUY＋SELL 记录；记录数大于1、平均间隔不超过60秒、跨度至少90分钟时首次命中。
- 从首次命中当日及以后持续归类，后续未再达到频率条件的日期也纳入；首次命中之前不使用未来标签回算。月钱包数取当月实际出现、且在对应日已经命中的钱包并集，金额汇总这些钱包生效日及之后的当日记录。
- 首次命中仅指现库首次观察证据，不代表真实首次机器人操作日。缺失或未采完整的历史日期保留在共享名单与分布 JSON 的 `bot_history` 审计中。
- 机器人金额只统计 BUY，SELL 不进入分子或对应金额分母。默认模块与分布共用重建 BUY 事件的 Up 等价金额与分母，比例表示买方参与覆盖率。无同口径前月分布时，覆盖率变化显示 N/A，不以旧钱包原价金额比例代替。无分布模式的钱包金额也仅累计原价 BUY。
- 趋势图从零起算，按实际月天数绘制，标明峰值。币种构成展示前三名及其他合计，JSON 保留全部币种金额。
- 已核实的历史启用时间由共用系列目录提供，日、周、月报告共同使用。月份比较覆盖系列启用边界时，海报与推文注明范围变化，JSON 保存具体系列与首期结束时间。

当前月和前月取自同一个只读数据库事务。日级中间结果按日释放，仅保留整月钱包并集与汇总，避免将整月逐笔成交或全部钱包日特征加载到 Python。查询先选目标市场，再利用成交主键读取。
历史持续标签在外层只读事务之前加载，再按各自日期过滤；比较月不会使用目标月才出现的未来标签。

当前月有钱包采集缺口时，返回退出码 `2`，列出未完成日期，不生成新的海报和推文；前月缺失时仍生成当前月，环比为 `N/A`。零比较基数和零成交额分母分别给出原因。正常完成返回 `0`。

单边金额尚未补采的历史日显示 `N/A`。当月任一天缺失时，月总额、日均金额和币种占比均显示缺失，趋势图保留已知日并标出未知日，不把部分日之和当作完整月总额。钱包与机器人指标仍使用完整双方记录。

首次使用或更新历史覆盖定义后，应先通过采集入口初始化数据库视图。月报入口始终只读，不负责修改视图或联网补采。

## 买入交易分布

原月总览继续保留整月趋势、日均金额、币种构成等内容。分布已接入原月报默认入口。仅需单独生成分布页时：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_bot_price_poster.py \
  --month 2026-10 \
  --output-dir outputs/previews/monthly_buy_distribution_2026-10_v1
```

生成米白与墨绿风格的 `updown_buy_trade_distribution_all_series_monthly_2026-10.png` 和同名 JSON。
日期为完整自然月，不按固定30天替代；市场范围是数据库全部加密 Up/Down 系列。
两组都只统计 BUY 参与，使用[与日报、周报相同的成交重建和镜像去重内核](updown_bot_price_poster.md)。
每条对手明细形成一笔成交，汇总行不另计；Trades 不等于唯一订单数。

十档分别展示 Trades、Trade %、Shares、Notional (USDC)、Notional %，不加入 NOTIONAL MIX。
笔数用整数加总，份数和金额先以 Decimal 合并未舍入原值；周期比例重算，不能平均每日百分比。
Trade % 分母为本图月总笔数；两图 Notional % 分母均为全部钱包本月 BUY 名义金额。
Up 等价名义金额为归一化 Up 价格乘 Shares，与原总览吃单方市场成交额不同。
目标月任何一天缺失、重建不通过或源数据变化时，拒绝导出完整月分布。
总览读取事务关闭后再执行分布短查询；整套图文先在暂存目录生成并校验，全部成功后写入最终目录，发布清单最后更新。

## 推文

新版正文包含目标月与 UTC、原市场成交额、去重钱包数、两组重建 BUY 事件的 Trades、Shares 和 Up 等价金额，比例明确为机器人买方参与覆盖率。范围变化提示继续写入正文；总览保留市场月环比和日均值，机器人模块直接复用分布汇总。采用无链接 ASCII 模板，含换行不超过280字符；数字可用两位小数的 K/M/B/T 格式，超限则拒绝生成，不截断正文。
英文平台依清单使用总览和分布两图；抖音依清单使用六卡：总览、整月趋势、高频地址解释、全部 BUY 分布、机器人 BUY 分布、方法。中文正文同样同步新指标和持续标签，见[发布规则](updown_publishing.md)。

规则依据：[X 官方字符计算说明](https://docs.x.com/fundamentals/counting-characters)。

## 本机定时运行

`polymarket-updown-monthly.timer` 每月 1 日 **12:30，Asia/Tokyo** 启动同名服务，相当于北京时间 11:30、UTC 03:30。位于每日 09:30 采集及每周一 11:30 周报之后。

定时器采用 `OnCalendar=*-*-01 12:30:00 Asia/Tokyo`、`AccuracySec=1s`、`Persistent=true`。关机错过触发后，用户定时器恢复时补触发一次；它不会逐月补齐多个月份，也不会主动唤醒电脑。延迟运行仍选启动时上一个完整 UTC 月，补旧月使用 `--month`。

定时器和服务属于本机配置，不随仓库克隆自动安装。服务使用已有虚拟环境，运行默认月报入口，日志可通过以下命令查看：

```bash
systemctl --user status polymarket-updown-monthly.timer
journalctl --user -u polymarket-updown-monthly.service
```

## 素材与验证

固定背景为 `assets/posters/updown_monthly_background_v1.png`，由内置 imagegen 生成一次，日常运行无需生成服务。字体复用 `assets/fonts/Inter.ttf`。聚合查询为 `sql/updown_period.sql`，周报与月报共用。

背景生成要求：4:5 竖版、米白色纸感占绝大部分画面，仅在外缘使用克制的墨绿和鼠尾草绿几何装饰，中央保留干净排版空间，不含文字、数字、标志、图表、蓝色、金色、酒红色或发光效果。完整提示词见 `assets/posters/updown_monthly_background_v1.prompt.txt`。

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

月报测试覆盖自然月边界、跨年与闰年、跨日去重、月份上下界、日均环比、范围提示、缺口与零基数、推文长度、31 天和大金额排版，以及只读入口的配套输出。
