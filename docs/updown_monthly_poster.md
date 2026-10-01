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

生成 `updown_monthly_2026-09.png`、`updown_monthly_2026-09.json` 和 `updown_monthly_2026-09_tweet.md`。同月重跑会覆盖相应文件。正文文件只含英文推文与换行，发布时配合同名 PNG 使用。脚本负责生成文件，发布到 X 是独立操作。

## 统计口径

- 市场归属取 `endDate` 所属 UTC 日，包含这些市场的全部提前及跨日成交。
- 月金额沿用整数微 USDC，买入支出加卖出收入；金额与百分比使用 `ROUND_HALF_UP`。
- 月钱包数取整月钱包并集。月环比为 `(本月值 / 前月值 − 1) × 100%`。
- 日均成交额分别除以各自自然月天数后比较；不会用四周或固定 30 天代替自然月。
- 疑似机器人沿用每日平均间隔不超过 60 秒、跨度至少 90 分钟的筛选。月钱包数取每日被标记钱包并集；金额只汇总被标记的钱包在对应日市场中的成交。
- 机器人月占比由月金额计算，占比差以百分点表示，不平均每日占比。
- 趋势图从零起算，按实际月天数绘制，标明峰值。币种构成展示前三名及其他合计，JSON 保留全部币种金额。
- 已核实的历史启用时间由共用系列目录提供，日、周、月报告共同使用。月份比较覆盖系列启用边界时，海报与推文注明范围变化，JSON 保存具体系列与首期结束时间。

当前月和前月取自同一个只读数据库事务。日级中间结果按日释放，仅保留整月钱包并集与汇总，避免将整月逐笔成交或全部钱包日特征加载到 Python。查询先选目标市场，再利用成交主键读取。

当前月有采集缺口时，返回退出码 `2`，列出未完成日期，不生成新的海报和推文；前月缺失时仍生成当前月，环比为 `N/A`。零比较基数和零成交额分母分别给出原因。正常完成返回 `0`。

首次使用或更新历史覆盖定义后，应先通过采集入口初始化数据库视图。月报入口始终只读，不负责修改视图或联网补采。

## 推文

正文包含月份与比较月份、成交额及月环比、月去重钱包及月环比、疑似机器人金额占比。范围变化提示也写入正文。采用无链接的 ASCII 模板，字符数包含换行；长数字时压缩辅助文字，将详细口径保留在配图，适配普通推文的 280 个加权字符预算。

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
