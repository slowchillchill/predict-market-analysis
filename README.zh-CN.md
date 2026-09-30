# Polymarket 加密货币 Up/Down 数据分析

[English](README.md) | **简体中文**

采集 Polymarket 加密货币 Up/Down 市场逐笔成交，在 SQLite 中分析钱包活动，并生成日报、日海报和周海报，以及适用于 X 的英文推文。

主要流程覆盖 38 个市场系列，按**市场结束日（UTC）**统计。采集支持断点续传，报告和海报通过只读数据库连接离线生成。仓库包含工具、测试和海报素材；生成报告前需要自行下载市场数据。

## 功能

- 发现八种加密资产、五类周期的目标市场。
- 使用 SQLite 保存逐笔成交和分页进度，每页数据与进度在同一事务内提交。
- 导出每日钱包成交额、去重钱包数和前 200 钱包 CSV，以及中文 Markdown 报告。
- 生成 1600 × 2000 英文 PNG 海报、配套 JSON 和英文 X 推文正文。
- 生成酒红与香槟金配色的周海报，包含周环比、七天趋势、币种贡献和疑似机器人活动；周推文适配 X 普通推文长度。
- 通过 SQL 视图查询采集覆盖情况和钱包排名。
- 保留原有 BTC 5 分钟流程，便于对照历史结果。

## 市场范围

系列目录定义在 [`scripts/polymarket_updown_daily.py`](scripts/polymarket_updown_daily.py)。

| 资产 | 周期 | 系列数 |
| --- | --- | ---: |
| BTC、ETH、SOL、XRP、DOGE、BNB、HYPE | 5 分钟、15 分钟、1 小时、4 小时、日线 | 35 |
| ZEC | 5 分钟、15 分钟、4 小时 | 3 |

按代码中的周期安排，每个 UTC 日预期有 **3,295 个市场**，完整性按系列分别检查。系列尚未开放的历史日期或平台调整周期后的日期可能不满足该预期；有缺口的日期不会生成完整日金额统计。

## 快速开始

需要 Python 3.10 或更新版本。以下命令适用于 Linux 或 macOS 的 Bash，均在仓库根目录执行。直接运行 SQL 查询时，可选安装 `sqlite3` 命令行工具。

### 1. 安装

```bash
git clone https://github.com/slowchillchill/predict-market-analysis.git
cd predict-market-analysis
python3 -m venv .venv-btc5m
.venv-btc5m/bin/python -m pip install -r requirements-btc5m.txt
.venv-btc5m/bin/python -m pip install -r requirements-poster.txt
```

海报依赖为 Pillow；只需采集和 CSV、Markdown 日报时，可以省略海报依赖安装。离线日报仅使用 Python 标准库。

### 一次运行昨日完整流程

安装上述两项依赖后直接执行，脚本内置香港代理地址 `socks5h://127.0.0.1:20810`：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_yesterday.py
```

脚本启动时按系统时钟固定 UTC 昨日，复用已有下载进度采集当天市场，采集成功后生成英文海报、JSON 和推文。
它默认使用内置代理，其他环境可用 `--proxy` 覆盖地址，不切换 VPN 节点或检测出口所在地。已完成市场不重新下载成交；环比使用数据库已有前日数据，缺失时显示 `N/A`。
跨 UTC 午夜重跑会改为新的昨日，补旧日期请使用下方指定日期命令。

### 2. 采集数据

采集器要求显式传入 `--proxy`，现有命令行说明和使用流程采用本机香港代理，不会自动读取环境变量中的代理配置。请输入已配置的代理 URL，例如将 `socks5h://127.0.0.1:PORT` 中的端口替换为实际端口：

```bash
read -r -p '请输入本机香港代理 URL：' UPDOWN_PROXY

.venv-btc5m/bin/python scripts/polymarket_updown_daily.py collect \
  --start-date 2026-09-17 --end-date 2026-09-19 \
  --proxy "$UPDOWN_PROXY" \
  --db data/raw/btc5m.sqlite3
```

该示例采集 UTC 2026 年 9 月 17 日和 18 日结束的市场。请按需替换日期：包含开始日期，不包含结束日期。需要海报展示环比时，也应采集前一天的数据。

按 `Ctrl+C` 中断后，重跑同一命令即可续传，已完成市场自动跳过。同一数据库一次只运行一个采集进程。默认数据库沿用历史名称 `btc5m.sqlite3`，由两种采集流程共用，无须复制逐笔数据。

市场发现使用 Gamma `/events/keyset`，将服务端返回的 `next_cursor` 作为下一页的 `after_cursor`，直到没有后续游标。

### 3. 导出日报

采集流程创建数据库并安装全系列视图后，执行：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_daily.py report \
  --date 2026-09-18 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/updown
```

完整日期会生成：

- `updown_daily_2026-09-18.csv`：日汇总及前 200 钱包成交额占比。
- `updown_top200_2026-09-18.csv`：钱包地址、排名和成交额。
- `updown_daily_2026-09-18.md`：中文报告及采集覆盖情况。

有缺口时只生成覆盖报告，可重新运行采集命令补齐。

### 4. 生成英文海报与推文

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_poster.py \
  --date 2026-09-18 \
  --db data/raw/btc5m.sqlite3 \
  --output-dir outputs/posters
```

生成 `updown_2026-09-18.png`、`updown_2026-09-18.json` 和 `updown_2026-09-18_tweet.md`。Markdown 文件包含英文推文正文。脚本在本地生成文件，发布到 X 需另行操作。重复生成同一日期会覆盖对应输出文件。

当日必须采集完整；前日有缺口或比较指标的前日基数为零时，对应环比显示 `N/A`。背景和 Inter 字体随仓库提供，渲染无需调用图片生成服务或安装系统字体。

这些流程中，退出码 `0` 表示成功，`2` 表示采集覆盖不完整（命令行参数错误也使用 `2`），`130` 表示采集被中断。目前命令行帮助、进度提示和每日 Markdown 报告为中文，海报和推文为英文。

### 5. 生成上周海报与推文

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_weekly_poster.py
```

默认按启动时的 UTC 日期选择上一个完整周（周一至周日）。也可指定日期：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_weekly_poster.py --week-start 2026-09-14
```

生成 `updown_weekly_2026-09-14_2026-09-20.png`、同名 `.json` 和 `_tweet.md`，默认保存至 `outputs/posters/`。脚本只读本地数据库；目标周七天均须完整采集，周环比使用前一个完整周，前周缺失或零基数显示 `N/A`。

周钱包数整周去重。疑似机器人逐日判定，周钱包数取每日被标记钱包的并集，金额仅汇总被标记钱包在对应日市场中的成交额。推文保留核心数据，详细口径在海报中，不加入一句话观察；正文按 280 字符预算排版，较长数字时进一步压缩辅助说明。

定时执行可直接调用不带日期参数的命令，每次自动计算上周范围。定时器属于本机配置，不随仓库克隆自动安装。详见[周海报使用说明](docs/updown_weekly_poster.md)。

## 统计口径

| 指标或行为 | 定义 |
| --- | --- |
| 日期 | 市场 `endDate` 所属 UTC 日，不是确认结算日或逐笔成交日。包含目标市场返回的全部成交，包括提前和跨日成交。 |
| 钱包成交额 | 买入支出加卖出收入。每条记录的 `price × size` 以 `ROUND_HALF_UP` 舍入为整数微 USDC 后求和，不等同于单边市场成交量。 |
| 去重钱包数 | 钱包地址统一为小写，在当日全部目标市场中去重。 |
| 前 200 钱包 | 按汇总成交额降序排列，金额相同时按地址升序排列。 |
| 海报疑似机器人 | 在目标市场中，平均成交间隔不超过 60 秒、首末成交跨度至少 90 分钟的钱包；不要求最低参与市场数。这是行为筛选，不能确认身份。 |
| 完整性 | 各系列达到预期市场数，且成交分页全部完成。该指标表示接口采集覆盖情况，不代表完成全量链上对账。 |

采集器调用 `/v2/trades`，使用 `taker_only=false`、`filter_type=TOKENS`、`filter_amount=1e-18`、`limit=1000`，跟随游标直到 `next_cursor=null`。返回记录逐条保留，不按交易哈希去重。

原 [`README_BTC5M.md`](README_BTC5M.md) 描述另一套流程：BTC 5 分钟日报按**市场开始日（UTC）**统计前 10 钱包，其中手动机器人分析 SQL 的日期和阈值规则也不同。使用全系列流程时，应以上述定义为准。

## SQL 查询

安装可选的 `sqlite3` 命令行工具后，可以执行：

```bash
sqlite3 -readonly -header -column data/raw/btc5m.sqlite3 <<'SQL'
SELECT * FROM updown_coverage WHERE date_utc = '2026-09-18';
SELECT * FROM updown_series_coverage
WHERE date_utc = '2026-09-18' AND NOT is_complete;
SELECT * FROM updown_daily_summary WHERE date_utc = '2026-09-18';
SQL
```

日汇总与排名视图只返回完整日期。查询前需要通过全系列采集入口初始化数据库。

## 项目结构与文档

| 路径 | 用途 |
| --- | --- |
| [`scripts/`](scripts/) | 采集、离线报告和海报生成脚本 |
| [`sql/`](sql/) | SQLite 表结构、覆盖视图、钱包统计及海报查询 |
| [`tests/`](tests/) | 采集、计算和渲染测试 |
| [`assets/`](assets/) | 海报背景和字体；字体许可见 [`Inter-OFL.txt`](assets/fonts/Inter-OFL.txt) |
| `data/raw/` | 本地数据库，Git 默认忽略 |
| `outputs/` | 生成的报告和海报，Git 默认忽略 |

详细文档：

- [全系列采集与日报](docs/updown_daily.md)
- [海报生成与指标说明](docs/updown_poster.md)
- [周海报、周推文与定时运行](docs/updown_weekly_poster.md)
- [原 BTC 5 分钟使用指南](README_BTC5M.md)
- [BTC 采集器实现说明](docs/btc5m_daily.md)
- [市场系列清单](reports/crypto_updown_market_types_2026-09-17.md)

## 开发与验证

安装两个依赖文件后，运行离线测试：

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

测试覆盖分页与续传、分页原子提交、UTC 日期边界、共用数据库兼容性、十进制舍入、采集缺口、机器人阈值和海报排版。反馈问题或提出修改时，请附上执行命令、预期行为和可复现示例，避免包含本机凭据或原始数据库文件。

## 许可证

除另有许可声明的第三方材料外，本项目采用 [MIT 许可证](LICENSE)。在保留版权与许可声明的前提下，允许商用、修改和再分发，也允许集成到闭源项目中。软件不提供任何担保。

随附的 Inter 字体继续采用 [SIL 开放字体许可证 1.1](assets/fonts/Inter-OFL.txt)，依赖库保留各自的许可证。MIT 许可证不授予这些工具所访问的第三方市场数据或服务的使用权。
