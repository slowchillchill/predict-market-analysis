# Kaggle 采集、存储与本地图文生成

生产流程将原始数据采集、SQL 统计和历史补采放在 Kaggle。本地只下载完成的统计 JSON，生成现有日、周、月海报、推文和发布清单。

项目名称为 **Polymarket Crypto Updown Markets Analysis**。公开入口使用 `qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis`。部署版本和实际运行结果以部署记录为准。

本文描述本分支的 v3 历史恢复修复及操作顺序，尚不代表该修复已经验收或部署。已发布的 2026-11-09 v2 日库与原报告仍可读取；旧版验收证据和本次修复记录分开保存。

## 数据与代码

| 位置 | 内容 |
| --- | --- |
| `pm-updown-raw-YYYY-MM-DD` | 按 API 市场结束时间所属 UTC 日保存的 `data.sqlite3`、`collection_status.json` 和来源说明；本次工作格式为 v3，旧 v2 仍可读 |
| `pm-updown-analysis-YYYY-MM` | `features.sqlite3`、完成的日周月结果、`index.json`，以及当前月份的全局 `progress.json` |
| 本地 `outputs/posters` | 现有格式的图文、配套 JSON 和发布清单 |
| 本地 `outputs/kaggle_reports/state.json` | 首次部署日期、固定目标周期及其待处理、已生成或已保留状态 |

完成的 v2、v3 日库都只有三个实体表：

- `markets`：市场元数据。
- `collection_progress`：钱包分页游标、完成状态和错误。
- `trades`：双方完整钱包记录、原始页号和行号，以及市场内的短 `group_id`。

`group_id` 只在同一个市场内标识交易组，不是跨市场编号或链上交易哈希。采集未完成市场时，`pending_group_map` 持久保存交易哈希到短编号的映射，保证跨页、暂停和重启后的编号一致。市场完成后删除其映射；完成的日库不保留该临时表。

v3 允许 `markets.date_utc` 为 `NULL`，表示真实市场开始日未知；`event_start_time` 本来就可为空。只在取得真实开始时间时填入这些字段，不能使用创建相关的 `startDate`，也不能用结束时间减 24 小时推造。日库归属与全部 `updown_*` 统计仍以 API 原始 `end_time` 的 UTC 日为准，不因开始时间缺失而改变市场归属。

生产只请求 `taker_only=false`。单边金额由“市场＋group_id”分组后，累加组内原始页号、行号最早记录的微 USDC 金额。BUY 分布使用相同交易组和已有成交重建方法。所有钱包镜像行保留，供钱包和机器人统计使用。日库不保存独立 `market_volume` 实体表，也不独立请求 `taker_only=true`。

组内首行作为汇总方锚点是本项目的方法，不是对官方接口永久排序规则的承诺。金额和 BUY 重建继续执行项目既有的精度与重建检查。`incomplete` 版本只供恢复；钱包市场覆盖和成交重建均通过后，才发布正式结果。本次新工作日库及 `collection_status.json` 的格式元数据使用 v3；旧 v2 输入仍可保留在来源引用中。报告 JSON 接口保持 v1，group ID、钱包与 BUY 统计规则不变。

月度分析数据集每次发布完整快照，保留该月原有文件。周结果归入最后一个包含日期所属的 UTC 月份，例如 `2026-10-26` 至 `2026-11-01` 的周结果放在 `pm-updown-analysis-2026-11`。

GitHub 项目代码保留 MIT；Kaggle 启动脚本采用 Apache 2.0。整理数据的 CC0 1.0 声明适用于贡献者所拥有的权利，并保留 [Polymarket 来源条款](https://polymarket.com/tos)及来源说明。

此次迁移不上传本地旧库，不导入本地机器人标签缓存，也不删除、截断或压缩现有数据库。部署后本地旧库停止增长；删除和释放其原有空间由操作者另行安排。

### 旧云端日库兼容与迁移

旧云端日库仍可读取。完成的 v2 日库不因 v3 发布而全部重发，2026-11-09 的已完成 v2 示例保持其原版本引用。需要续写并表达未知开始日的旧工作副本在 Kaggle 升级为 v3，保留原已提交交易、组号、分页游标与错误状态。

更早的哈希格式需要转换时，在 Kaggle 根据保存的交易哈希和原始页、行顺序建立市场内 `group_id`，通过比较后发布同一日数据集的新版本。迁移不重新请求已完整采集的交易，也不使用本地历史数据。

v2 将市场金额改为从交易组派生，不保留旧 `market_volume` 实体表。发布新版本不会自动删除 Kaggle 上的历史旧版本，因此不能将转换后的文件缩小直接解释为平台累计存储已释放。

需要执行旧哈希格式迁移而不采集交易时，在 Kaggle 使用 `--migrate-only`。这不是要求重新发布全部已完成 v2 日库：

```bash
python scripts/polymarket_kaggle_pipeline.py --migrate-only
```

转换前后比较原记录数、组数、每市场金额、钱包特征和完整 BUY 结果；通过后先发布新日库，再更新月度结果引用。`migration_summary.json` 保存体积和比较结果。若新日库已发布而月度发布中断，下次复用该新日库版本继续，不重复转换或重新采集。

## Kaggle 任务

代码入口为 `kaggle/notebook.py`，配置为 `kaggle/kernel-metadata.json`。Notebook 启用 Internet，使用 CPU，按固定 Git 提交加载项目代码。

先将 Python 入口打包为标准 Notebook，再用已配置的 Kaggle CLI 上传。必须加 `--no-run`，不执行采集代码：

```bash
python3 scripts/prepare_kaggle_notebook.py --output-dir /tmp/polymarket-kaggle-upload
kaggle kernels push -p /tmp/polymarket-kaggle-upload --no-run
kaggle kernels status qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis
```

2026-11-11 云端验收发现，直接以 script 类型 Quick Save 上传 `.py`，虽然 CLI 返回已保存，平台转换预览时仍会出现 `NotJSONError`。打包脚本只生成临时上传文件，保持项目源码和 Notebook 单元一致；不包含 Token。

上传后，在网页完成以下操作：

1. 打开 Notebook 编辑页，在 Secrets 界面创建名为 `KAGGLE_API_TOKEN` 的 Secret。
2. 勾选该 Secret，允许当前 Notebook 访问；已有 Secret 也须确认已勾选。
3. 选择 **Save Version → Save & Run All**，运行带有 Secret 授权的版本。
4. 确认该版本能够读取 Secret，完成一次采集、迁移和统计验收。手动实例结束后，再启用原生 **Daily** 调度，使后续任务沿用该版本。

2026-11-10 的 v2 部署曾实测，直接通过 CLI `push` 运行的新版本未携带 Secret 授权，在 `get_secret` 处返回 HTTP 400，尚未发送采集请求。因此，CLI 上传或运行成功不代表 Secret 已授权；代码更新后仍需通过上述网页步骤保存运行版本。Token 不写入代码、数据集或日志。

生产 Notebook 每次启动先恢复最后成功确认的云端进度。本次恢复顺序为：

1. 优先处理最新已结束的 UTC 日期。
2. 续采并修复 **2025-03-15 至 2025-04-02 现有的 19 个 `incomplete` 日期**，只处理其未完成市场或分页。
3. 继续原历史补采计划；确实无法取得的市场保留 `gap`，不填零、不标完整。

首次历史发现固定各系列可取得的历史范围，后续会话沿用该范围。开始时间未知可以保存为空，不应仅因缺少真实开始时间就把结束时间明确的市场判为不可取得。

历史采集在每页事务提交后检查日更优先级。出现新的已结束 UTC 日期时，保存当前历史日库，处理新日期，然后恢复历史。单次会话结束后，下次 Daily 运行继续；生产没有每日补采日期数量上限。

所有 Polymarket 请求顺序发送，每次尝试结束后至少间隔 0.5 秒。临时错误最多尝试三次，重试等待约 2 秒、4 秒，并遵守有效的 `Retry-After`。持续限流时保存进度并退出；某市场失败时，只补该市场未完成的钱包分页。游标失效只重置对应未完成市场及其组映射。不可取得的数据保留缺口，不填零。

### 有变化才发布的检查点

采集每页仍先将明细、组映射和游标提交到同一个数据库事务。默认每 **30 分钟**检查一次是否有新的已提交状态；有变化才发布一致的 `incomplete` 日库检查点，不在每一页后上传。正常日期完成、日更抢占或可处理错误退出时，也保存需要发布的变化。

任务默认在运行 **11 小时 30 分钟**后停止后续采集，为 [Kaggle 当前 12 小时 CPU 会话上限](https://www.kaggle.com/docs/notebooks)预留发布与处理时间。从 bootstrap 开始计时，安装依赖和恢复数据也计入本次运行。`--checkpoint-minutes`、`--stop-after-hours` 和 `--session-hours` 可调整检查点、停采及整次运行截止时间。检查点间隔与停采时间是可调整的工程参数，不是平台规则，也不是防封要求；预留时间不保证网络上传和平台处理必定完成。

只有 Kaggle 已处理完成并确认成功的版本才是跨会话恢复点。下次从该版本续采，而不是从上次计划检查的时刻恢复。上传失败、平台强制终止或处理未确认时，可能重采超过 30 分钟的未发布部分，不能承诺最多只丢 30 分钟进度。

月度特征、报告或恢复状态没有变化时，不反复发布相同月库快照。原始日库检查点成功后，才更新引用该日库版本的恢复状态；未完成的日期仍不产生正式统计结果。

### 不确定发布结果的核对

恢复修复发布的快照带有小型 `publication.json` 标记。遇到 HTTP 499 等上传结果不确定的响应，先查询平台版本及处理状态，再读取标记确认是否为本次快照；已成功发布则复用该版本，避免盲目重复上传。未确认成功时不将其作为新的持久恢复点。

这个标记用于生产程序核对发布身份，不要求数据使用者读取整个数据库计算 SHA，也不改变报告的 `source_versions` 引用方式。

### 手动运行与暂停

1. 在 Notebook 设置中暂停 Daily 调度。
2. 确认没有正在运行的生产实例。
3. 手动运行同一个 Notebook；它会恢复已发布进度。
4. 手动任务结束后恢复 Daily 调度。

需要立即停止时使用 Kaggle 页面停止运行；这可能强制结束进程，恢复点以最后处理成功的数据集版本为准。不要同时启动多个生产实例。

仅用于有限日期验收时，可以在 Kaggle 环境运行：

```bash
python scripts/polymarket_kaggle_pipeline.py --max-days 2
```

该参数是显式验收入口，不用于生产调度。生产采集入口拒绝在本地电脑执行。

### 版本发布与恢复顺序

1. 保持云端运行、Daily 和本地四个 timer 暂停，先完成代码与旧格式兼容回归。
2. 在本次修复 PR 中固定最终源码提交，将 bootstrap 的 `SOURCE_COMMIT` 指向该准确提交；保留上次 Notebook、源码和数据集版本记录。
3. 使用 CLI `--no-run` 上传，通过网页授权 Secret 后，只启动一个手动验收实例。
4. 验收真实开始时间未知时仍按 API 结束日纳入、旧 v2 读取和工作副本升级、跨会话续采、变化检查点及不确定发布核对。确认正式结果引用处理完成的日库准确版本，不强制重发既有完成 v2 日库。
5. 本地从固定版本下载统计 JSON，在新预览目录生成图文，确认未读取原始数据库，已有图文也未被覆盖。
6. 手动实例结束后恢复云端 Daily，最后恢复本地日、周、月和每 30 分钟重试四个 timer。

运行失败时先停止新实例并暂停 Daily 和本地四个 timer，保留已发布版本与图文。回退代码前确认它能够读取并续写相应日库格式，特别是未知开始日的可空字段；不能假设旧代码可以续写所有 v3 工作库。需要回退时使用兼容的日库版本和对应分析进度；若没有兼容恢复点，采集保持暂停，本地仍可读取已经完成的旧结果 JSON。不要通过伪造开始日期或删除数据集历史版本来强行回退。

## 本地安装与定时出图

Kaggle CLI 2.2.5 需要 Python 3.11 或更新版本。在现有项目目录安装依赖：

```bash
.venv-btc5m/bin/python -m pip install -r requirements-kaggle.txt
```

本地 CLI 从 `~/.kaggle/access_token` 或 `KAGGLE_API_TOKEN` 环境变量读取凭据。凭据不提交到仓库。

首次运行时指定实际部署日；已有状态保留最初的部署日：

```bash
.venv-btc5m/bin/python scripts/polymarket_kaggle_reports.py \
  --rollout-date 2026-11-10 --kind all
```

`--rollout-date` 使用 Asia/Tokyo 日历日期。省略时，以首次运行的东京日期初始化。任务只补齐部署日以后已经到期的图文周期，不自动生成部署以前的全部历史图文。

| 首次检查时间（Asia/Tokyo） | 目标周期 |
| --- | --- |
| 每天 09:30 | UTC 昨天 |
| 周一 11:30 | 上一个完整 UTC 周，周一至周日 |
| 每月 1 日 12:30 | 上一个完整 UTC 自然月 |

尚未就绪的目标保存为 `pending`，每 30 分钟重试。即使跨日，原目标也不会被新日期替换。电脑错过定时时间后，后续运行会补登记部署以来到期的目标。`generated` 和 `preserved` 为终态，不再保留已过时的 `last_error`；已有终态在下一次结果检查时同样清理旧错误，而不重新生成图文或改变原部署日期。

任务先读取月度数据集状态，固定已处理完成的版本，再从同一版本下载 `index.json` 和指定结果文件。它不下载原始日库，也不打开本地原始 SQLite。

现成的 systemd 用户单元位于 `config/systemd/`。以下命令供云端验收通过后安装或恢复本地任务使用；执行前先按部署步骤确认单实例状态。在本项目位于 `~/poly-market-analysis` 时安装：

```bash
mkdir -p ~/.config/systemd/user
cp config/systemd/polymarket-updown-*.service ~/.config/systemd/user/
cp config/systemd/polymarket-updown-*.timer ~/.config/systemd/user/
cp config/systemd/polymarket-kaggle-reports-retry.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now polymarket-updown-yesterday.timer \
  polymarket-updown-weekly.timer polymarket-updown-monthly.timer \
  polymarket-kaggle-reports-retry.timer
```

三个原任务名称继续使用，但服务入口已改为云端结果读取。排查待处理周期：

```bash
cat outputs/kaggle_reports/state.json
journalctl --user -u polymarket-kaggle-reports-retry.service -n 50 --no-pager
systemctl --user list-timers --all
```

需要离线预览某个已经下载的结果时：

```bash
.venv-btc5m/bin/python scripts/polymarket_kaggle_reports.py \
  --result-json /path/to/daily_2026-11-09.json \
  --output-dir outputs/previews/kaggle_daily_2026-11-09
```

已有总览、分布或发布文件会被保留，定时任务不覆盖已生成、已提交或已发布的图文。历史统计修订需要手动预览并另行处理替换。本流程不向社交平台自动发帖，也不自动补发历史帖子。

## 统计覆盖与结果接口

统计保持原项目的市场 UTC 结束日、单边成交额、钱包金额、BUY 重建和 Decimal 精度口径。周月钱包数取并集，比例由合并后的分子、分母计算。

机器人首次命中记录从现有云端状态恢复，尚无记录时由完成日库建立。补采得到更早有效命中时，取更早日期并重算受影响的云端统计。较晚命中不会用于较早日期。`bot_history` 记录请求范围、实际观察范围与历史是否待补齐；历史未完成时不能将已观察范围解释为全部历史。

`index.json` 的 `results` 使用稳定键：

```json
{
  "schema_version": 1,
  "results": {
    "daily:2026-11-09": {
      "filename": "daily_2026-11-09.json",
      "status": "complete"
    },
    "weekly:2026-11-02": {
      "filename": "weekly_2026-11-02.json",
      "status": "complete"
    },
    "monthly:2026-11": {
      "filename": "monthly_2026-11.json",
      "status": "complete"
    }
  }
}
```

每个结果包含 `schema_version: 1`、`status: complete`、周期、`source_versions`、`payload` 和 `distribution`。原始日库格式 v2、v3 不改变这一报告接口。`payload` 沿用现有海报总览字段；`distribution` 沿用既有成交分布格式。本地仅接受周期匹配、带原始日库版本引用的完成结果。

## 验证

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -v
```

回归需要覆盖 JSON 出图、固定版本读取、跨日重试、已有文件保留、历史标签更新和周月合并；本次还需验证未知开始日、按原始结束日纳入、变化检查点、不确定发布与旧 v2 兼容。新测试结果和真实恢复运行证据尚待记录；下方 v2 转换与跨会话样本仅是历史证据，不表示本次修复已通过或历史范围已补齐。

## 历史 v2 验收证据（2026-11-10）

以下记录用于复现已经发布的 v2 数据，不代表本次 v3 恢复修复已经验收或部署。

- v2 实现提交为 [`c16edb8`](https://github.com/slowchillchill/predict-market-analysis/tree/c16edb8353aa78fe6229bc3e164f13788d1e1296)，当时完整回归 340 项通过。
- [2026-11-09 原始日库 Kaggle 版本 2](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2)是完成的原始格式 v2，保留 1,916,447 条钱包记录、686,988 个交易组与 3,295 个市场。各市场与旧 API 金额对照差异为零，钱包特征、机器人命中和 BUY 业务统计不变；公开 `migration_summary.json` 记录比较结果。
- 该日库从 547,946,496 字节缩小至 378,290,176 字节，减少 30.96%。旧 Dataset 版本仍保留，不表示平台累计占用释放同样大小。
- [2026-11 分析数据集 Kaggle 版本 3](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/3)引用上述原始日库版本 2。这里的 Kaggle 发布版本 3 不等于本次原始库格式 v3。
- 当时另以真实两会话续采验证 3,209 条记录、1,200 个组、四页数据，原行与组号不变；完成后清空映射并形成三表日库。这个样本证明当时的续采机制，不证明历史范围已经补齐。

## 本次 v3 修复与待验收部署记录（2026-11-11）

本次修复包括：未知真实开始时间保留为空；按 API 原始结束日采集；有变化才发布检查点；默认 11 小时 30 分钟停采并保存；用 `publication.json` 核对不确定发布；减少无变化月库版本；优先处理新日期，再修复既有 19 个未完成日期并继续历史。

以下项目须由实际验收与部署结果填写，当前不宣称通过：

| 项目 | 本次实际记录 |
| --- | --- |
| 最终源码提交、修复 PR、Notebook 保存版本 | 待验收后填写 |
| 新测试数量与结果 | 待验收后填写，不沿用旧 C16 的 340 项结果 |
| 未知开始时间保留 `NULL`、结束日归属与旧 v2 兼容 | 待验收后填写 |
| 变化检查点、跨会话续采和不确定发布核对 | 待验收后填写 |
| 2025-03-15 至 2025-04-02 修复结果与剩余真实缺口 | 待实跑后填写 |
| 完成日库和分析结果的准确 Dataset 版本引用 | 待实跑后填写 |
| Daily 恢复和本地结果读取兼容验证 | 待部署后填写 |
