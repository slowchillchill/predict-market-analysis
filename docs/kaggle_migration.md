# Kaggle 云端采集、恢复与文字稿生成

生产流程将原始数据采集、SQL 统计、历史补采，以及日、周、月 X 串联推文和 LinkedIn 文字稿生成放在 Kaggle。进度、文字稿和生成状态随云端 Dataset 保存；关闭本地 Codex 会话不会中断已启动的云端任务，也不影响后续原生 Daily 调度。当前不再生成图片、抖音或 TikTok 内容，详见[文字发布说明](updown_publishing.md)。本地入口保留为可选预览工具，生产不依赖本地定时器。

项目名称为 **Polymarket Crypto Updown Markets Analysis**。公开入口使用 `qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis`。2026-11-13 已完成生产第 23、24 版验收，并部署第 25 版正式参数；Daily 已恢复，本地四个定时器已停用。首次恢复后的 v26 已确认 Scheduled 触发并从云端恢复，整次运行结果尚待验收；准确版本与证据见[本次修复记录](kaggle_cloud_autonomy_20261113.md)。

本文前半部分说明当前云端方案；后半部分保留 v3 迁移与旧部署的实际验收记录。旧记录中的 Secret 绑定和本地四个报表定时器属于当时部署，不是当前方案的必要条件。已发布的 2026-11-09 v2 日库与原报告仍可读取；历史范围尚未补齐。新方案的生产发布和 Daily 验收状态以准确版本的部署记录为准。

## 数据与代码

| 位置 | 内容 |
| --- | --- |
| `pm-updown-raw-YYYY-MM-DD` | 按 API 市场结束时间所属 UTC 日保存的 `data.sqlite3`、`collection_status.json` 和来源说明；本次工作格式为 v3，旧 v2 仍可读 |
| `pm-updown-analysis-YYYY-MM` | `features.sqlite3`、完成的日周月结果、`index.json`、可恢复文字包 `social.archive.json`，以及当前月份的全局 `progress.json` |
| 云端 `social.archive.json` | `social/state.json`、X 逐条文字、LinkedIn 正文、固定来源 JSON 和文字发布清单 |
| 本地 `outputs/social` | 可选预览的 X 逐条文字、LinkedIn 正文、结果 JSON 和文字发布清单 |
| 本地 `outputs/posters` | 切换前已生成的历史图片与发布产物，当前入口不再写入 |
| 本地 `outputs/kaggle_reports/state.json` | 旧本地调度状态；迁移时保留原部署日期、固定目标周期及其待处理、已生成或已保留状态 |

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

代码入口为 `kaggle/notebook.py`，配置为 `kaggle/kernel-metadata.json`。Notebook 启用 Internet，使用 CPU。固定 bootstrap 每次读取 GitHub `kaggle-release` 分支的 `config/kaggle-release.json`，校验 `schema_version`、40 位 `source_commit` 和 `auth_mode`，再以 detached checkout 加载该准确提交。读取或校验失败就停止，不退回移动的开发分支。日常更新发布清单即可选择已验证代码；bootstrap 自身变化才需要重新上传 Notebook。

先将 Python 入口打包为标准 Notebook，再用已配置的 Kaggle CLI 上传。必须加 `--no-run`，不执行采集代码：

```bash
python3 scripts/prepare_kaggle_notebook.py --output-dir /tmp/polymarket-kaggle-upload
kaggle kernels push -p /tmp/polymarket-kaggle-upload --no-run
kaggle kernels status qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis
```

2026-11-11 云端验收发现，直接以 script 类型 Quick Save 上传 `.py`，虽然 CLI 返回已保存，平台转换预览时仍会出现 `NotJSONError`。打包脚本只生成临时上传文件，保持项目源码和 Notebook 单元一致；不包含 Token。

### 认证来源

平台原生凭据已通过真实云端读写探针：`qiuqiuslowchillchill/pm-cloud-auth-probe-20261113` 第 2 版为 `COMPLETE`，独立 QA Dataset `qiuqiuslowchillchill/qa-cloud-auth-20261113-1794538912` 已产生第 1、2 版。证据保存在 `outputs/cloud-autonomy/auth-probe-result-v2/auth_probe.json`。探针第 1 版曾因 Notebook 缺少 `kernelspec` 在代码启动前失败，修正包装后才取得上述结果。这证明该云端环境提供的凭据可完成已测操作；完整 producer 和原生 Daily 仍需单独验收。

新方案可以使用平台凭据运行，无须绑定 `KAGGLE_API_TOKEN` Secret。`auth_mode=platform` 只接受 Kaggle 运行环境提供的凭据文件；缺失或校验失败就停止。`auto` 按平台凭据、个人环境变量或凭据文件、最后 Secret 的顺序选择可用来源；选中后验证失败不切换身份。`personal` 显式排除平台凭据，供有需要的独立部署使用。

启动时通过只读接口核对凭据有效且属于预期 owner，再进入采集。日志只记录实际来源，不输出凭据值。`bootstrap.json` 保存发布清单和认证来源，`run-summary.json` 保存 producer 的源码提交、触发方式、恢复位置和最终状态。凭据不写入源码或 Dataset。

早期 v17、v19 的 HTTP 400 发生在强制读取未绑定 Secret 时，尚未启动 producer。它们说明旧版本的 Secret 授权缺失，不说明 Kaggle 必须使用个人 Secret。下面的历史记录保留当时的修复过程。

生产 Notebook 每次启动先恢复最后成功确认的云端进度。本次恢复顺序为：

1. 优先处理最新已结束的 UTC 日期。
2. 续采并修复 **2025-03-15 至 2025-04-02 现有的 19 个 `incomplete` 日期**，只处理其未完成市场或分页。
3. 继续原历史补采计划；确实无法取得的市场保留 `gap`，不填零、不标完整。

首次历史发现固定各系列可取得的历史范围，后续会话沿用该范围。开始时间未知可以保存为空，不应仅因缺少真实开始时间就把结束时间明确的市场判为不可取得。

历史采集在每页事务提交后检查日更优先级。出现新的已结束 UTC 日期时，保存当前历史日库，处理新日期，然后恢复历史。单次会话结束后，下次 Daily 运行继续；生产没有每日补采日期数量上限。

所有 Polymarket 请求顺序发送，每次尝试结束后至少间隔 0.5 秒。临时错误最多尝试三次，重试等待约 2 秒、4 秒，并遵守有效的 `Retry-After`。持续限流时保存进度并退出；某市场失败时，只补该市场未完成的钱包分页。游标失效只重置对应未完成市场及其组映射。不可取得的数据保留缺口，不填零。

单个市场缺少真实标识、详情返回 404、系列目录异常或交易分页失败时，保存错误并继续处理其他市场和日期。历史日期的原始数据保持 `incomplete`，`collection_status.json` 和 `progress.json` 保留具体错误，`history.gaps` 保留待修复日期。确认发布原始数据和恢复进度后，扫描游标进入下一日；游标表示扫描位置，不表示之前所有日期都已完整。未完成日不产生正式日报，也不降低预期市场数来凑齐覆盖率。

新日期始终优先。完成历史日期扫描后，每个缺口在同一会话最多重试一次；本轮已经尝试的日期不会立即再次循环。新会话可再次修复缺口，成功后清除对应错误。首次建立历史范围时，某个系列的目录异常不会阻止扫描其他系列；有错误的系列不确认为完整边界，全部系列处理后仍有边界缺口就保存状态并正常结束本轮，下次先采新日期，再重试未确认的系列。持续限流、会话截止、主动停止和未确认的 Kaggle 发布继续遵守原有停止与恢复保护。

### 有变化才发布的检查点

采集每页仍先将明细、组映射和游标提交到同一个数据库事务。默认每 **30 分钟**检查一次是否有新的已提交状态；有变化才发布一致的 `incomplete` 日库检查点，不在每一页后上传。正常日期完成、日更抢占或可处理错误退出时，也保存需要发布的变化。

任务默认在运行 **11 小时 30 分钟**后停止后续采集，整次运行默认截止时间为 **12 小时**，余量用于发布与处理。从 bootstrap 开始计时，安装依赖和恢复数据也计入本次运行。`--checkpoint-minutes`、`--stop-after-hours` 和 `--session-hours` 可调整检查点、停采及整次运行截止时间。这些时间是工程参数，不能替代对当前 [Kaggle 会话限制](https://www.kaggle.com/docs/notebooks)的确认；预留时间不保证网络上传和平台处理必定完成。

原生 **Daily** 表示每天由 Kaggle 再启动一次任务，不表示进程连续运行 24 小时，也不保证精确启动时刻。每次运行在截止前退出，下一次从已确认的 Dataset 继续。关闭本地电脑或 Codex 会话不影响这一调度；仍须保持单个生产实例。

只有 Kaggle 已处理完成并确认成功的版本才是跨会话恢复点。下次从该版本续采，而不是从上次计划检查的时刻恢复。上传失败、平台强制终止或处理未确认时，可能重采超过 30 分钟的未发布部分，不能承诺最多只丢 30 分钟进度。

月度特征、报告或恢复状态没有变化时，不反复发布相同月库快照。原始日库检查点成功后，才更新引用该日库版本的恢复状态；未完成的日期仍不产生正式统计结果。

### 不确定发布结果的核对

恢复修复发布的快照带有小型 `publication.json` 标记。遇到 HTTP 499 等上传结果不确定的响应，先查询平台版本及处理状态，再读取标记确认是否为本次快照；已成功发布则复用该版本，避免盲目重复上传。未确认成功时不将其作为新的持久恢复点。

发布确认查询失败时，日志会记录 Dataset、目标版本、发布标记、提交阶段和脱敏后的底层异常链。生产 v20、v21 曾在 create 已获接受后，第一次状态查询收到 403；后续固定版本的 `publication.json` 与提交标记吻合，说明不能将该错误直接解释为上传失败。403 的上游原因仍未确定。

修复只对“本次 create 已明确获接受、目标属于当前 owner、预期版本为 1”的确认过程放宽：遇到状态读取 403，每约 **15 秒**只读核对一次，最多 **120 秒**，同时受原有总操作和会话截止时间约束。成功仍须同时满足平台 ready 和固定版本发布标记一致。它不会重新提交 create，也不会全局重试 403。version 上传、不确定是否获接受的写入、owner 列表认证失败、401、配额错误和标记不符均不使用这一窗口；持续 403 到期后停止并保留 journal。

如果 raw 已成功发布、月度进度尚未保存，新会话会读取该日最新固定版本。它重新检查目录覆盖、分页和统计，再比较状态元数据及数据库的 schema、全部表记录和覆盖视图。内容完全一致才复用 raw 版本并补写月度引用；统计缺口继续保持 `incomplete`。这允许在没有本地 journal 的情况下恢复孤立 raw 版本，也避免仅凭记录数相同就误认数据未变。

默认工作目录为 `/kaggle/working/polymarket-pipeline`。未确认发布会保留 `downloads/pending-publications/<publication_id>.json`，其中记录目标版本、源目录和发布标记，不包含认证凭据。原始检查点的不可变副本仅在 raw 发布确认且月度进度保存成功后删除；失败时保留在 `checkpoints/`。这些文件是故障核对材料，程序不会自动重放 journal。它们是否可下载取决于 Kaggle 是否保留该次运行的输出，不能保证强制终止后仍存在；下一次新会话仍从已处理成功的云端 Dataset 恢复。

可显式运行小型真实发布验收，它创建一个独立的私有 QA Dataset，仅上传合成 JSON，不读写生产数据集：

```bash
python3 tests/kaggle_publication_acceptance.py --work-dir /tmp/kaggle-publication-acceptance
```

当前验收在真实 create 后模拟一次状态查询 403，检查读取恢复后仅 create 一次；随后在真实 version 后模拟持续 403，检查立即停止、journal 保留和没有再次上传，最后通过固定版本标记只读核对。它不会将模拟的 403 当作线上故障根因，也不代替生产认证、单实例恢复和 Daily 验收。工作目录必须不存在，结果写入该目录的 `acceptance.json`。以下 2026-11-12 记录使用的是修复前的验收脚本。

2026-11-12 的发布确认修复通过全部 437 项回归和独立代码审查。真实验收 Dataset 为 `qiuqiuslowchillchill/qa-publication-20261112-003858`：第 1 版正常确认，第 2 版上传后模拟查询 403，再通过相同发布标记只读确认；总共只有一次 create 和一次 version。另行下载的云端 metadata 确认 `info.isPrivate = true`。这次验收没有修改生产 Dataset，也不能单独证明生产 Notebook 的 Secret 和 Daily 设置有效；后续生产运行结果见下文。

2026-11-12 01:24 UTC 的部署复核：按授权通过本机已认证 CLI 上传并启动生产 Notebook 第 17 版，拉回该版源码确认固定 `2b3941e2c1de44d1581004c3ec504c322ea4351f`，`RUN_ARGUMENTS = []`。运行曾进入 `RUNNING`，随后变为 `ERROR`；日志在 bootstrap 的 `UserSecretsClient().get_secret("KAGGLE_API_TOKEN")` 处记录 HTTP 400。错误发生在安装依赖和启动 producer 之前，未进入采集或 Dataset 发布。本机 CLI 上传认证成功不证明云端 Secret 可读。

随后第 18 版使用同一源码，已成功读取 Secret 并运行约 92 分钟。2026-11-11 raw 第 3 版完成 3,295/3,295 个市场、4,843 页，正式日报已发布。历史补采推进到 2025-07-06，发现 XRP 日线条目 `xrp-up-or-down-on-july-6` 的 `conditionId` 为空；该日 raw 第 1 版保存 100/101 个市场，分析第 204 版保存缺口后，旧调度逻辑抛出 `DiscoveryBlocked`。官方详情显示该条目未激活、无交易代币标识、成交量为 0，但这些事实不足以直接把它排除并声称当天完整。本次后续修复按用户要求保留这种数据异常并继续采集其他日期。原生 Daily 的配置未在这次复核中重新验收。

市场异常隔离修复 [`4663a5c`](https://github.com/slowchillchill/predict-market-analysis/commit/4663a5c044ad70c9f1371fadbf54ba848dea27bd) 通过全部 **443 项**回归和独立审查，固定源码后另外通过 3 项 Notebook 打包测试。行为测试覆盖 101 个市场中一个失败时保留另外 100 个市场、继续下一健康日期、重启后仅补失败市场、异常修复后清除缺口并生成正式日报；同时验证 raw 或月度发布未确认时不能继续下一日期。首次历史边界发现还覆盖异常系列隔离、失败分页从头重试、重复游标检测和持续限流保护。

2026-11-12 03:28 UTC 的第 19 版验收：通过 CLI 上传并启动，拉回准确版本确认 `SOURCE_COMMIT = 4663a5c044ad70c9f1371fadbf54ba848dea27bd` 和 `RUN_ARGUMENTS = []`。该版在约 12.6 秒读取 `KAGGLE_API_TOKEN` 时返回 HTTP 400，尚未启动 producer。当时只读核对确认 2025-07-06 raw 仍为第 1 版（100/101 个市场、131 页、`incomplete`），月库仍为第 204 版，历史游标仍为 2025-07-06。刷新编辑页后确认第 19 版草稿的 Secret 列表为空；旧的第 18 版编辑页仍显示已勾选，不能用未刷新的页面推断新版本授权有效。CLI 不支持配置 Notebook Secret，不能靠反复 push 修复授权。

随后通过已授权的浏览器操作，在同一 Notebook 的 Secret 中恢复本机已有 Token，确认勾选后 **Save & Run All**，生成[第 20 版](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis?scriptVersionId=368646498)。拉回源码确认仍固定 `4663a5c044ad70c9f1371fadbf54ba848dea27bd`，生产参数无日期数量限制。该版通过认证，运行日志在 **94.8 秒**记录 2025-07-06 保留异常、继续其他日期，紧接着出现 `Collecting 2025-07-07 (historical)` 并取得该日交易。固定版本核对确认 [07-06 raw 第 2 版](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2025-07-06/versions/2)仍为 `incomplete`、100/101 个市场、131 页，错误保留；[月库第 205 版](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/205)在 03:37:45 UTC 保存 `next_date_utc = 2025-07-07`、对应 raw v2 引用及 07-06 缺口。验收时第 20 版仍在运行，未把启动与越过故障点等同于全历史采集完成。网页原生计划仍为 Daily，活动面板显示下一次计划运行日期为 2026-11-13；本次未修改计划。

上述步骤是旧版强制使用 Secret 时的操作记录。过去的第 13、15 版也采用网页授权；本机 CLI 认证不能替代该授权。当前平台认证方案不需要重复这些 Secret 步骤，日常代码更新通过固定 bootstrap 读取发布清单完成。

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

1. 暂停 Daily，确认没有生产实例，再完成代码和旧格式兼容回归。停止旧本地日、周、月和重试定时器，保留已有状态与产物。
2. 用独立私有 QA Dataset 验收实际认证、create 确认恢复和两次独立云端会话续采。两个会话共享固定 QA 名称，但不共享本地工作目录；不得写生产数据集。
3. 将旧文字稿和调度状态制作成固定版本的私有迁移 seed。`config/kaggle-social-seed.json` 固定其 Dataset 和版本；云端核对 archive 和文件摘要后导入，所有受影响月库确认成功后才记录采用完成。当前 seed 为 `qiuqiuslowchillchill/pm-updown-social-seed-20261113` 第 2 版，使用 schema 2 和 JSON 归档。第 1 版的 ZIP 被 Kaggle 自动展开，未用于生产迁移。
4. 固定最终源码提交，将发布清单指向该准确提交和已经验收的 `auth_mode`。首次部署新 bootstrap 时，使用 CLI `--no-run` 上传，核对准确源码和配置，再用临时 `--max-days 1` 启动一个有限生产验收实例。保留上次 Notebook、发布清单、源码和 Dataset 版本，供回退核对。
5. 核对完整新日期、历史续采、raw 孤立版本恢复、月库准确引用和云端文字包。保留既有 v2 完整日库及统计缺口，不为统一格式强制重发。检查 `bootstrap.json`、`run-summary.json` 和发布 journal，不把进程启动视为验收完成。
6. 有限验收结束后，以 CLI `--no-run` 保存 `RUN_ARGUMENTS = []` 的正式版本，核对完成后恢复原生 Daily，并等待一次真实计划触发的运行完成必要验收。本地四个定时器保持停用；需要预览时可手动运行本地读取工具。

运行失败时先暂停 Daily，确认旧实例结束，保留已发布版本、文字稿与故障证据。`run-summary.json` 将发布或发现失败记为 `failed`，持续限流记为 `rate_limited`；未知异常保留摘要并继续抛出。远端 `progress.json` 的 `last_run` 可能停在上一已确认快照，故障核对应同时读取本次运行输出和 journal。回退发布清单前确认旧代码能够读取并续写相应日库格式，特别是未知开始日的可空字段、结算规则列和新的覆盖策略；不能假设旧代码可以续写所有 v3 工作库。若没有兼容恢复点，采集保持暂停，已完成的旧结果 JSON 仍可读取。不要伪造开始日期或删除 Dataset 历史版本。

## 云端文字稿与跨会话状态

本次容器修复之后的代码才可作为云端文字状态的兼容回退基线。不能直接回退到使用 ZIP 的 `a1658d8` 或更早版本：它们不会恢复 JSON 容器内的逐周期状态，可能重新生成旧稿。原始 SQL 格式未改变；不含文字容器的既有月库仍可读取。

每次保存先确认月度统计快照，再用它的固定 Dataset 版本生成文字稿。文字中的来源链接指向包含该报告的已确认版本；生成完成后再次发布含文字稿和状态的月库。例如，v1 已保存报告，文字稿引用 v1，v2 再保存这些文字稿。后续保存和重启不会仅因版本号变化重新生成旧稿。

云端把 `social/` 压缩后编码到平面 JSON 文件 `social.archive.json`，随月库完整上传。JSON 容器包含格式版本、编码方式、SHA-256 和内容，避免 Kaggle 上传端自动展开 ZIP。新会话校验并恢复相对路径清单、逐条 X `.txt`、LinkedIn `.txt`、固定来源 JSON 和 `social/state.json`。文字稿只读取已完成的报告 JSON，不需要把原始 raw Dataset 下载到本地。

目标周期沿用原部署日期和东京时区规则：日报为已到期的 UTC 昨天，周报为上一个完整 UTC 周，月报为上一个完整 UTC 自然月。到期但统计未完成的周期保持 `pending`，后续云端保存或新会话重试；跨日不会覆盖旧目标。Daily 不提供精确到分钟的文字稿生成时间。

迁移保留已有文字稿和 `generated`、`preserved` 状态，不覆盖旧图片、旧文字或已发布记录，也不生成部署以前的全部历史文案。已有文字包损坏或缺失时保留错误供核对。这里的 `generated` 只表示文字稿已生成；程序不会自动向 X、LinkedIn 或其他社交平台发帖。

## 可选本地安装与文字预览

以下保留原本地读取入口和 systemd 单元的使用方法。它们适用于独立本地方案；当前云端生产不安装或恢复这些定时器。已有本地状态与文字稿只作为迁移来源和历史证据保留。

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

`--rollout-date` 使用 Asia/Tokyo 日历日期。省略时，以首次运行的东京日期初始化。任务只补齐部署日以后已经到期的文字稿周期，不自动生成部署以前的全部历史文案。

| 首次检查时间（Asia/Tokyo） | 目标周期 |
| --- | --- |
| 每天 09:30 | UTC 昨天 |
| 周一 11:30 | 上一个完整 UTC 周，周一至周日 |
| 每月 1 日 12:30 | 上一个完整 UTC 自然月 |

尚未就绪的目标保存为 `pending`，每 30 分钟重试。即使跨日，原目标也不会被新日期替换。电脑错过定时时间后，后续运行会补登记部署以来到期的目标。`generated` 和 `preserved` 为终态，不再保留已过时的 `last_error`；已有终态在下一次结果检查时同样清理旧错误，而不重新生成内容或改变原部署日期。切换文字格式不会重置这些状态，也不会自动重新发布旧周期。

任务先读取月度数据集状态，固定已处理完成的版本，再从同一版本下载 `index.json` 和指定结果文件。它不下载原始日库，也不打开本地原始 SQLite。

现成的 systemd 用户单元位于 `config/systemd/`。以下命令仅供明确选择独立本地调度的部署使用，不属于当前云端生产步骤。在本项目位于 `~/poly-market-analysis` 时安装：

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
  --source-dataset qiuqiuslowchillchill/pm-updown-analysis-2026-11 \
  --source-version 100 \
  --output-dir outputs/previews/kaggle_daily_2026-11-09
```

示例中的分析版本须填写结果实际来源；日报若没有分析来源引用，可省略这两个参数并使用目标日的固定原始日库引用。周报、月报需要固定分析版本，不能用某一天的原始日库代表整个周期。默认文字目录为 `outputs/social/`，包含原结果、逐条 X `.txt`、LinkedIn `.txt` 与 `format: text` 的本地清单 v2，不要求 Pillow 或图片资源。已有同周期文字文件会保留；旧海报目录和已发布记录不覆盖。历史统计修订需要手动预览并另行处理替换。本流程不向社交平台自动发帖，也不自动补发历史帖子。

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

本次完整回归 **420 项通过**，覆盖 JSON 出图、固定版本读取、跨日重试、已有文件保留、历史标签更新、周月合并、未知开始日、原始结束日归属、变化检查点、不确定发布和旧 v2 兼容。云端恢复验收与已知统计缺口见下方记录。下方旧 v2 转换数据仍只说明当时的样本，不证明全历史已补齐。

## 历史 v2 验收证据（2026-11-10）

以下记录用于复现已经发布的 v2 数据。本次 v3 恢复修复的验收另见下一节，两者不能混作同一测试结果。

- v2 实现提交为 [`c16edb8`](https://github.com/slowchillchill/predict-market-analysis/tree/c16edb8353aa78fe6229bc3e164f13788d1e1296)，当时完整回归 340 项通过。
- [2026-11-09 原始日库 Kaggle 版本 2](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-09/versions/2)是完成的原始格式 v2，保留 1,916,447 条钱包记录、686,988 个交易组与 3,295 个市场。各市场与旧 API 金额对照差异为零，钱包特征、机器人命中和 BUY 业务统计不变；公开 `migration_summary.json` 记录比较结果。
- 该日库从 547,946,496 字节缩小至 378,290,176 字节，减少 30.96%。旧 Dataset 版本仍保留，不表示平台累计占用释放同样大小。
- [2026-11 分析数据集 Kaggle 版本 3](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/3)引用上述原始日库版本 2。这里的 Kaggle 发布版本 3 不等于本次原始库格式 v3。
- 当时另以真实两会话续采验证 3,209 条记录、1,200 个组、四页数据，原行与组号不变；完成后清空映射并形成三表日库。这个样本证明当时的续采机制，不证明历史范围已经补齐。

## 本次 v3 修复验收与生产恢复记录（2026-11-11）

本次修复允许未知真实开始时间保持为空，保留 API 原始结束日；有变化才发布检查点，用 `publication.json` 核对不确定发布，减少无变化月库版本。市场结束日归属、钱包和 BUY 统计口径没有改变。代码经过独立审查，完整回归 **420 项通过**。

| 项目 | 实际记录 |
| --- | --- |
| 生产采集源码 | [`9c7578b09a788ed0542ffe5ea9c3b78c17f2c04a`](https://github.com/slowchillchill/predict-market-analysis/tree/9c7578b09a788ed0542ffe5ea9c3b78c17f2c04a)，含目录兼容及新日期错误隔离 |
| 修复 PR | [PR #3](https://github.com/slowchillchill/predict-market-analysis/pull/3) |
| Notebook 上传与退出 | CLI 上传前打包标准 `.ipynb`；bootstrap 的 [`19f8567`](https://github.com/slowchillchill/predict-market-analysis/commit/19f856788579ffae62d9c2d0a8e2d99d14b44562) 修正使成功返回不会触发 `SystemExit(0)` 单元错误 |
| 生产恢复实例 | [Notebook 版本 13](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis?scriptVersionId=368303307)，2026-11-11 05:20:29 UTC／14:20:29 JST 启动，从上一实例的 raw 第 2 版继续；本轮状态 `COMPLETE`，正常结束 |
| 新日期实际验收 | [2026-11-10 raw 版本 3](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-raw-2026-11-10/versions/3)：`complete`，3,298/3,298 市场、4,741 页，`official_series_all_conditions_v1`，无发现或结构错误；[分析版本 81](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/81)首次发布正式日报，后续最终快照为[分析版本 100](https://www.kaggle.com/datasets/qiuqiuslowchillchill/pm-updown-analysis-2026-11/versions/100) |
| 本次有限恢复 | 临时 `--max-days 20`，处理最新日期和原有 19 个历史日期；19 日中 18 日交易分页完整，6 日正式完成、12 日保留统计缺口，另 1 日目录不可用；最终 `repair_queue = []`，`next_date_utc = 2025-04-03`，并非全历史完成 |
| 旧完整日库 | 2026-11-09 仍引用原始库格式 v2、Dataset 版本 2，没有为改格式而重发 |
| 本地读取与出图 | 从分析版本 100 下载 `daily_2026-11-10.json`，2026-11-11 05:42:43 UTC 成功生成 `outputs/posters/updown_2026-11-10.png`、推文与发布清单；原 11 月 9 日产物保留。只下载统计 JSON，未读取或下载原始日库 |
| 历史标签保留 | 原有 299 个标签全部保留，首次命中没有被推迟；新增后共有 380 个，本轮原标签也没有提前 |
| 云端调度恢复 | [Notebook 版本 15](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis?scriptVersionId=368308743)使用相同 `9c7578b` 源码，Quick Save 保存 `RUN_ARGUMENTS = []` 与 Secret 授权；原生 Daily 页面确认日期为 2026-11-12、频率 Daily，并显示 `Notebook scheduled.`，不保证精确 UTC 启动时刻 |
| 本地调度恢复 | 日报、周报、月报和每 30 分钟重试四个 timer 均已启用且 active；2026-11-11 05:45 UTC 后复核。下一次日报 11 月 12 日 09:30 JST，周报 11 月 16 日 11:30 JST，月报 12 月 1 日 12:30 JST；重试任务执行结果为成功 |

`--max-days 20` 只限制本次处理的日期任务数量，不表示 20 个日期全部统计成功。正式生产版本已移除这个数量上限。后续实际完成日库、分析 Dataset 版本和剩余缺口，以 `progress.json`、`collection_status.json` 和 `index.json` 为准。下一次运行先处理新日期，再从 2025-04-03 继续历史补采；本轮已记录的缺口仍保留，清空修复队列不等于消除了这些缺口。

### 云端验收结果

私有验收 Notebook 为 `qiuqiuslowchillchill/polymarket-v3-recovery-acceptance-20261111`。验收使用独立私有 Dataset，不写生产数据集；方法见 [验收脚本](../tests/kaggle_recovery_acceptance.py)。

1. **版本 4，第一会话：**从真实旧 v2 工作副本升级为 v3，未知开始日和开始时刻为 `NULL`，市场结束时刻保留 API 原始 `12:00 UTC`。采集首 10 条记录后发布第 1 版检查点。真实上传成功后模拟客户端 499，程序按固定版本标记确认结果，没有再次上传。
2. **版本 6，第二会话：**只靠云端检查点恢复，保留原游标、页序和组号。第二页检查点发布时再次模拟 499，更新结果得到确认。最终取得 500 页、4,996 条记录，分页覆盖完整；但既有 BUY 重建条件拒绝其中一个交易组。因此原始库保留 `incomplete`，版本 6 的“必须统计完成”验收断言未通过，不能把这次运行整体记为通过。
3. **版本 10，只读复核：**固定读取 QA raw Dataset 版本 1、2、3 和月度证据版本 4；前两个已持久化分页的原始十进制值、页序、组号及映射保持一致，三个发布标记各不相同。全部 4,996 行只将分组列一一替换为旧键后，正式重建仍出现相同错误。这个复核不重新采集，也不发布 Dataset。
4. **现代样本正例：**固定读取旧续采 Notebook `polymarket-group-id-resume-phase2-20261110/1` 的真实 3,209 行、1,200 组样本。实际升级 v2→v3 后，原单市场目录不变，正式特征提取、报告导出和最终三表检查全部通过。

只读复核源码固定为 [`d015062`](https://github.com/slowchillchill/predict-market-analysis/tree/d0150624d19adda1fd87f405b0386a9872d04b0d)。结果为 `passed_with_existing_reconstruction_gap`，保留的原始日库状态仍是 `incomplete`。直接恢复前后比较只覆盖前两个已保存分页；不能将其描述为全部 4,996 行都有恢复前基线。

### 已知历史统计缺口

本轮 2025-03-15 至 2025-04-02 的 19 日中，**3 月 15、17、23、27、28、30 日**已正式完成。**3 月 16、18、19、20、21、22、24、25、26、29 日，以及 4 月 1、2 日**已取得全部交易分页，但现有 BUY 重建检查拒绝其中的交易组，因此这 12 日保留原始记录和统计错误，不发布正式报告。

**3 月 31 日**未取得 BTC、ETH 两个目标市场的官方目录；带年份、不带年份和 `-noon` 三种 slug 均返回 404。当前发现数为 0、计划数为 2，保留该缺口，不填零，也不据此断言这些市场从未存在。

2025-04-02 样本中，`bitcoin-up-or-down-on-april-2` 的组 59 出现 Up 等价价值差 `0.0000010795418280 USDC`。它超过原有的 1 微 USDC 容差，也没有通过后续均价反算检查。这段计算、原始价格和份数处理未因 v3 修复而改变；相同记录以旧分组列重放仍拒绝。

当前证据只说明该输入不满足现有重建条件，不能断言上游数据错误。程序保留完整分页、原始记录和错误，不跳过交易组，不放宽容差，不生成该日正式报告。该缺口会记录在历史进度中，后续日期仍可继续处理。已完成的现代样本正例也不能推断每个历史日期都能成功出报。


### 本次生产发现的官方目录变化

2026-11-10 的官方 Gamma 完整目录有 3,298 个目标市场，原配置名义数量为 3,295。差异全部在 BTC：日线 2 个（原名义 1 个），小时线 26 个（原名义 24 个）。新增 Chainlink TWAP 合约与旧 Binance 合约有不同 condition，均属于相同官方系列；其中一个新增小时合约已有成交量，不能当作别名或零成交删除。

首轮 v11 使用旧唯一性检查，保存该日 raw 第 2 版后明确退出：3,296 个市场已完成，4,737 页已提交，未伪报完整。补充的新日期错误隔离修复 `ffe7774` 允许保存失败新日期后继续历史任务，历史结构错误仍不推进历史游标；其后目录兼容代码通过全部 420 项回归与独立审查。v13 从 raw 第 2 版恢复，实际只补采两个 BTC 日线市场，原已完成分页保留；新增 4 页后发布 raw 第 3 版，全部 3,298 个市场和正式统计均完成。完整目录已确认这些合约都在原先授权的 38 个系列范围内，无需扩大系列范围或任意选择一个 condition。目录兼容修复 [`9c7578b`](https://github.com/slowchillchill/predict-market-analysis/commit/9c7578b09a788ed0542ffe5ea9c3b78c17f2c04a) 全部保存它们及真实 `resolution_source`、`description`，并用完整目录证明、计划时段和每个 condition 的分页共同判定完成。旧完整 v2 不自动改写或宣称满足新覆盖策略。

官方元数据示例：[Binance BTC 日线](https://gamma-api.polymarket.com/events/slug/bitcoin-up-or-down-on-november-10-2026)、[Chainlink BTC 日线](https://gamma-api.polymarket.com/events/slug/btc-updown-1d-1794243600)。上述链接用于复核两种不同结算合约。

现代覆盖依据保存在 `updown_coverage_policy`、`updown_expected_slots`、`updown_discovery_status` 三个 SQL 视图中，不增加实体表。只有目录完整读取且不存在未解决的元数据错误，才能确认该系列的目录状态；猜中常规 slug 不等于完整目录。恢复后重新读取目录并重建本轮证明，旧成功位不能替代本轮结果。检查点也会保存只有这些视图发生变化的进度。缺时段时的 `expected_markets` 为已发现合约数加尚缺时段数，避免额外合约掩盖缺口。
