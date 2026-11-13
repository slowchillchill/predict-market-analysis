# 2026-11-13 Kaggle 云端运行修复与验收记录

本次修复已通过 508 项回归、平台原生认证探针、真实发布确认验收和两次独立云端会话恢复验收。生产 Notebook 第 23 版已开始有限验收，正在采集真实市场。**有限生产验收和下一次原生 Daily 验收尚未完成。**

本文记录截至已核对的生产分析 Dataset 第 282 版，更新时间为 **2026-11-13 03:25:50 UTC**。后续状态须追加准确版本和结果，不能将本记录中的 `running` 解释为成功结束。操作方案见 [Kaggle 部署说明](kaggle_migration.md)，代码变更见 [PR #5](https://github.com/slowchillchill/predict-market-analysis/pull/5)。

## 修复前的失败基线

Notebook 为 `qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis`。不同版本的失败发生在不同阶段，不能合并成同一个原因。

| Notebook 版本 | 已核对的失败阶段与原因 | 证据边界 |
| --- | --- | --- |
| v16 | Scheduled 运行约 2 小时 42 分钟后，发布确认失败 | 当时诊断未保留底层原始异常，不能认定也是 403 |
| v17 | bootstrap 读取未绑定的 `KAGGLE_API_TOKEN` Secret，返回 HTTP 400 | producer 尚未启动，未进入采集或 Dataset 发布 |
| v18 | 通过认证并完成新日期；历史 2025-07-06 的 XRP 条目缺少 `conditionId`，旧调度在保存缺口后抛出 `DiscoveryBlocked` | 不是凭据错误；保留 100/101 个市场，不把异常市场当作零成交或完整覆盖 |
| v19 | CLI 上传的新版本没有 Secret 授权，读取 Secret 返回 HTTP 400 | UI 总耗时约 22 秒；代码日志约 12.6 秒处报错，producer 尚未启动 |
| v20 | 已越过 2025-07-06 异常并继续历史日期；随后 create 获接受，第一次状态确认返回 403 | 后续固定 raw v1 的发布标记与提交记录一致，证明对应写入成功 |
| v21 | Scheduled 运行中，create 获接受后第一次状态确认返回 403 | 后续固定 raw v1 的发布标记同样吻合；403 的上游原因仍未确定 |

基线月库为 `pm-updown-analysis-2026-11` 第 279 版，其 `progress.json` 保存历史游标 `2025-09-10`。源码、进度和调度截图保存在 `outputs/cloud-autonomy/baseline/`。v17–v20 的详细过程见 [既有部署记录](kaggle_migration.md)。

## 本次代码与发布配置

| 项目 | 准确标识 |
| --- | --- |
| 云端恢复与文字生成主体 | `a1658d817a3ba26415a495c34590f4c0eed316c2` |
| JSON 归档修复后的生产源码 | `dfdc11c6e5b0f4fffb38614e524c556a744a438a` |
| 发布分支 | `kaggle-release` |
| 发布清单提交 | `952defb37d75b6a967c823dab9227f20d2243946` |
| 发布清单 | `config/kaggle-release.json`：`schema_version=1`、`source_commit=dfdc11c6e5b0f4fffb38614e524c556a744a438a`、`auth_mode=platform` |
| 私有文字迁移 seed | `qiuqiuslowchillchill/pm-updown-social-seed-20261113`，固定第 2 版 |

固定 bootstrap 读取发布清单后，校验字段并 checkout 准确 Git 提交。清单读取或校验失败就停止。日常源码发布通过清单选择已验收提交；不需要反复绑定个人 Secret。平台凭据在启动时校验有效性和 owner，日志只保留认证来源。

新建 Dataset 的 403 确认窗口只适用于本次 create 已明确获接受、目标属于当前 owner、预期版本为 1 的情况。程序每约 15 秒只读核对，最多 120 秒，并继续受总操作和会话截止时间限制。成功仍需平台 ready 和固定版本发布标记一致。它不重发 create，不放宽 version、401、配额错误或不确定写入的处理。

raw 已发布而月度进度尚未确认时，新会话重新检查覆盖和统计，并比较状态元数据、数据库 schema、全部表记录和覆盖视图。完全一致才复用原 raw 版本。统计失败仍保留 `incomplete`，不因版本存在就补标完成。

## 已完成的验收

### 回归与原生认证

最终修复通过 **508 项回归测试**。覆盖凭据来源选择、严格发布确认、会话截止、raw 孤立版本恢复、统计缺口保留、JSON 容器校验、文字状态恢复及主入口失败摘要。回归通过不替代真实云端或生产调度验收。

原生认证探针 [pm-cloud-auth-probe-20261113](https://www.kaggle.com/code/qiuqiuslowchillchill/pm-cloud-auth-probe-20261113) **第 2 版为 COMPLETE**。它使用平台 `KAGGLE_API_V1_TOKEN`，在独立 Dataset `qiuqiuslowchillchill/qa-cloud-auth-20261113-1794538912` 完成 create v1、ready 查询、version v2、ready 查询及固定 v2 下载。操作未写生产 Dataset，也未使用 Notebook Secret。

证据：[原生认证探针](evidence/kaggle-cloud-20261113/auth_probe.json)。首次探针包装缺少 `kernelspec`，在业务代码启动前失败；第 2 版修正包装后才取得上述结果。

仓库内 [验收证据目录](evidence/kaggle-cloud-20261113/) 保留准确 Dataset、版本、源码提交、摘要和检查结果。复制的验收 JSON 未包含凭据值；认证字段只是来源名称。发布确认记录中的绝对 journal 路径已替换为说明，异常原因和发布标记保留。固定生产进度和 seed 回读使用标明来源范围的摘要，未复制原始生产数据库或整份历史进度。

### 真实发布确认验收

QA Dataset 为 `qiuqiuslowchillchill/qa-publication-20261113-031558`。

| 检查 | 实际结果 |
| --- | --- |
| create 后模拟一次状态查询 403 | 恢复只读确认，v1 成功，仅提交一次 create |
| version 后模拟持续状态查询 403 | 立即停止该版本确认，保留 accepted journal，没有再次上传 |
| 关闭注入后的固定版本核对 | v2 发布标记一致，确认该写入已成功 |
| 写操作 | 一次 create、一次 version；生产写入为 0 |

v2 发布标记为 `ee0bf1407a994843adc55d9cc4cb01ad`。证据保留脱敏异常链和 journal 留存说明：[发布确认验收](evidence/kaggle-cloud-20261113/publication_acceptance.json)。这项验收在下述第一会话 Notebook 中执行。模拟 403 只验证处理逻辑，不用于证明线上 403 的上游成因。

### 两次独立云端会话

两次会话使用合成市场样本和相同私有 QA 名称，工作目录互不共享。测试守卫限制全部 Dataset 操作在 `qa-cloud-20261113-autonomy-*` 内，并移除 create 的 `--public`。

| 项目 | 第一会话 | 第二会话 |
| --- | --- | --- |
| Notebook | [pm-cloud-acceptance-20261113-phase-1](https://www.kaggle.com/code/qiuqiuslowchillchill/pm-cloud-acceptance-20261113-phase-1) 第 2 版 | [pm-cloud-acceptance-20261113-phase-2](https://www.kaggle.com/code/qiuqiuslowchillchill/pm-cloud-acceptance-20261113-phase-2) 第 1 版 |
| 源码 | `a1658d817a3ba26415a495c34590f4c0eed316c2` | `dfdc11c6e5b0f4fffb38614e524c556a744a438a` |
| 完成时间 UTC | 2026-11-13 03:18:09 | 2026-11-13 03:20:38 |
| 认证 | `platform` | `platform` |
| raw Dataset | `qa-cloud-20261113-autonomy-raw-2026-11-09` v1 | 复用同一 v1 |
| 月库 | `qa-cloud-20261113-autonomy-analysis-2026-11` v1 | 保存恢复进度与文字后为 v3 |
| 证据 Dataset | `qa-cloud-20261113-autonomy-acceptance` v1 | 同一 Dataset v2 |
| 交易请求 | 一次，保存 BUY/SELL 两条钱包记录 | 0 次 |
| 结果 | raw 确认后注入月度发布失败，月库尚未引用 raw | 只靠云端恢复，未再次上传 raw；月库准确引用 raw v1 |

上述 Dataset 的 owner 均为 `qiuqiuslowchillchill`。两次会话的 raw 发布标记完全一致：`f58a6b6afd7e4caabb6ca232ffb1f8e2`。第二会话比较了原始行、组号、schema 和数据库摘要；数据库摘要为 `77bc29f3107db67b69f1033533553bc10fad611e9346f92e3ce5e4d8a5c8f5e1`。

第二会话又从空目录下载月库，确认 `social.archive.json` 可以恢复逐条 X 文字、LinkedIn 正文、来源 JSON、清单及状态。禁止重新渲染后再次保存，文件摘要未变、没有新增上传、没有交易请求。

`a1658d8` 到 `dfdc11c` 的修改没有改变 raw 复用逻辑；第二会话实际覆盖了最终 JSON 容器实现。两份证据均为 `passed`，生产写入均为 0：[第一会话](evidence/kaggle-cloud-20261113/phase1.json)、[第二会话](evidence/kaggle-cloud-20261113/phase2.json)。

独立审查通过 SDK 的只读 `GetDatasetMetadata` 核对上述 raw、analysis、acceptance 三个 Dataset，以及 `qa-publication-20261113-031558`。四者均返回 `info.isPrivate=true`，见 [私有状态核对摘要](evidence/kaggle-cloud-20261113/qa_privacy.json)。这一核对没有创建或更新 Dataset；隐私结论不只依赖验收文件中的 `private_requested` 字段。

### JSON 容器与 seed v2 回读

第一次 seed 使用 `months.zip`，Kaggle 上传端自动展开 ZIP，无法按原文件名恢复。第 1 版未用于生产迁移。修复改用 `months.archive.json`，月库对应文件为 `social.archive.json`。JSON 保存格式版本、`base64+zip` 编码、压缩内容 SHA-256 和编码内容，上传端不再把它当作 ZIP 展开。

独立 JSON 上传验收创建了 [qa-archive-20261113-031742 第 1 版](https://www.kaggle.com/datasets/qiuqiuslowchillchill/qa-archive-20261113-031742/versions/1)。仅执行一次 create；固定版本下载后的 `social.archive.json`、`months.archive.json` 和 `seed.json` 与上传前字节一致。文字恢复和 seed 导入均通过，恢复 7 个文字与状态文件。服务器 metadata 确认该 Dataset 为私有，见 [归档验收结果](evidence/kaggle-cloud-20261113/archive_result.json) 和 [服务器私有状态](evidence/kaggle-cloud-20261113/archive_privacy.json)。

seed 第 2 版已 ready，并按固定版本下载回读。`seed.json` 为 schema 2，容器 SHA-256 为 `e66653e61697c1ab06f49787bc42c88f740f7ce6f7fec4c586c75b0c9a37d6ab`。回读校验通过：容器文件摘要、解码后 ZIP 摘要及全部 **13 个文件**摘要一致。恢复结果保留 `daily:2026-11-10`、`daily:2026-11-11` 的文字包，以及 `daily:2026-11-09` 的 `preserved` 状态。

仓库保留 [seed v2 回读校验摘要](evidence/kaggle-cloud-20261113/seed_v2_readback.json)。完整本地证据位于 `outputs/cloud-autonomy/seed-readback-v2/`；私有 metadata 回读为 `outputs/cloud-autonomy/seed-metadata/dataset-metadata.json`。源码配置已固定 seed 第 2 版，不再引用第 1 版。

本轮另一项失败是第一会话 QA Notebook 第 1 版的包装脚本 `SyntaxError`。它发生在业务执行前，未进入恢复测试。修正后第 2 版通过，不能把第 1 版计入成功次数。

## 当前生产部署状态

| 项目 | 已观察状态 |
| --- | --- |
| Notebook v22 | CLI `--no-run` 保存的 Quick Save；已回读源码，不是一次执行成功 |
| Notebook v23 | 有限生产验收正在运行，`RUN_ARGUMENTS = ["--max-days", "1"]` |
| v23 准确页面 | [scriptVersionId=368996884](https://www.kaggle.com/code/qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis?scriptVersionId=368996884) |
| producer 开始时间 | 2026-11-13 03:24:40.818474 UTC |
| run ID | `f0aba964b0cc48118646e9018a5bf49b` |
| 运行源码与认证 | `dfdc11c6e5b0f4fffb38614e524c556a744a438a`，`platform` |
| 历史恢复位置 | `2025-09-10` |
| 运行触发字段 | `Batch`，不能作为原生 Daily 已触发的证据 |
| 最新日期 | 已发现 2026-11-12 的 3,295 个市场，真实交易采集中，尚未宣称完整日完成 |
| 原生 Daily | 已暂停，防止与手动有限实例重叠 |
| 本地四个 timer | 已 disable；日、周、月和每 30 分钟重试任务不再驱动生产，已有状态与产物保留 |

生产月库第 281 版已经保存迁移后的 JSON 容器。回读恢复的状态为：11 月 9 日 `preserved`，11 月 10、11 日 `generated`，11 月 12 日 `pending`。该版进度尚未设置 `cloud_social_seed`。

第 282 版 `progress.json` 随后记录 `cloud_social_seed={dataset: qiuqiuslowchillchill/pm-updown-social-seed-20261113, version: 2}`，且 `last_run.status=running`。这一顺序符合“先确认受影响月库，再记录迁移采用完成”。证据：[v282 进度摘录](evidence/kaggle-cloud-20261113/production_v282_excerpt.json)、`outputs/cloud-autonomy/production-v281-migration/`。

这些结果已经证明生产认证和文字状态迁移进入实际运行。它们尚未证明本次有限实例正常结束，也未证明下一次原生 Daily 成功。当前仍需等待新日期采集、统计和发布确认，随后核对最终运行摘要与准确 Dataset 版本。

## 剩余验收与回退约束

有限生产实例完成后，先核对 `run-summary.json`、raw 状态、月库固定引用和文字来源版本。然后以 CLI `--no-run` 保存 `RUN_ARGUMENTS=[]` 的正式版本，确认没有活动生产实例，再恢复原生 Daily。必须实际观察下一次计划触发及云端恢复结果，才能把 Daily 验收记为完成。本地四个 timer 保持停用。

Daily 表示每天重新启动一次云端任务，不表示同一进程持续运行 24 小时，也不保证精确启动时刻。默认在 bootstrap 开始后 11.5 小时停止采集，整次会话使用 12 小时截止时间，为发布预留余量；这些工程参数不保证每次上传都能在截止前完成。

回退必须保留 `social.archive.json` 和 schema 2 seed 的读取能力。生产已写入 JSON 文字状态，不能直接把发布清单退回 `a1658d8` 或更早的 ZIP 实现；旧代码无法恢复这些状态，可能重新生成旧周期。原始 SQL 格式没有因此改变，不含文字容器的旧生产月库仍可读取。若没有兼容回退提交，应暂停后续写入并保留现有快照，不能删除历史版本或伪造完成状态。

真实 QA 使用合成市场。它验证了平台认证、真实 Dataset 上传、跨会话恢复和文字容器保存；不能据此断言每个真实历史市场都能采全，也不替代生产的统计覆盖与缺口检查。所有已知统计缺口继续保留，程序不会向社交平台自动发帖。
