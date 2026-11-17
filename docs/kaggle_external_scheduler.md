# 香港服务器上的 Kaggle 外部调度器

本方案在香港服务器通过 systemd 每天 UTC 00:05 发起 Kaggle Notebook。服务器只运行轻量控制脚本；采集、历史恢复、统计和文字稿生成仍在 Kaggle。无需 Codex 会话或浏览器常驻。

## 部署位置与时间

- 当前主机：`ubuntu@16.162.88.220`，Ubuntu 24.04.5、Python 3.12.3、systemd 255。旧主机 `18.163.200.203` 已由用户删除；2026-11-17 完成替换部署。
- 应用：`/opt/polymarket-kaggle-scheduler/`。
- 专用用户：`polymarket-scheduler`，无交互登录 shell。
- 凭据与配置：`/etc/polymarket-kaggle-scheduler/`。
- 持久状态：`/var/lib/polymarket-kaggle-scheduler/`。
- 定时器：`polymarket-kaggle-scheduler.timer`。
- 首个允许的生产调度日：`FIRST_DATE_UTC=2026-11-16`。
- 每日 UTC 00:05，即香港 08:05、日本 09:05。实际 Kaggle 启动仍可能受平台排队影响。

`Persistent=true` 在服务器错过计划后补触发一次，不代表逐日启动所有遗漏日期。producer 自行恢复未完成日期。首次日期限制阻止安装当天因补触发而重复提交已有原生任务。

2026-11-17 04:55 UTC，新服务器 timer 已为 **active / enabled**，下一次触发为 **2026-11-18 00:05:00 UTC**。首次部署于 11 月 15 日完成；用户当时确认原生 Daily 已取消。本次沿用该确认与用户重建同一调度器的授权，未把启用标记当成当日网页设置的独立核验。

迁移前生产 v30 已为 `RUNNING`，固定源码包含 `date_utc=2026-11-17` 的外部调度标记。新服务器只读预检和两次正式 service 执行均返回 `already_submitted_remote`，识别同一 v30，未新增生产版本。今天的生产任务已在旧调度器时期提交，本次迁移保留其运行；新主机下一次按时触发仍待 11 月 18 日实际发生。

固定分析 Dataset v534 与 11-15、11-16 raw v2 已核验：两天均完整采集 3,295/3,295 个市场，日报统计为 `complete`，文字稿为 `generated`。这表明最近两天无需补跑；文字生成不代表已发布到社交平台。

迁移后 04:59 UTC 再次只读查询，仍为 v30 `RUNNING`、同一提交标记，分析库仍为 ready v534。04:45:25 UTC 检查点显示历史补采游标为 2026-01-10，另有 280 个已记录历史缺口，包含 HTTP 404 和统计重建失败；历史数据尚未完整。正在运行的 Notebook 日志接口本次返回空内容，未据此宣称运行无错误。

## 提交流程

1. 取得本机文件锁，校验小型 Notebook 包与精确目标。
2. 通过官方 SDK 校验个人 API Token 的身份、有效状态及已返回的到期时间。
3. 查询云端 Notebook 版本与状态，恢复尚未确认的提交。已有排队或运行实例时不提交。
4. 校验 UTC 日期和已提交状态。在写入前，将唯一提交标记与版本基线原子写入磁盘，并执行 `fsync`。
5. 调用一次官方 `SaveKernel`，提交已验收的 bootstrap。生产继续通过 `kaggle-release` 中的固定提交清单加载业务代码。
6. 按固定版本回读 Notebook 源码标记，确认提交后记录版本并退出。服务器不等待整个采集完成。

SDK 请求使用连接 10 秒、读取 60 秒超时；禁止 HTTP 自动重试和重定向。状态读取失败或提交结果不明时保留 pending，后续只读核对，不直接重发。状态丢失时也检查当日云端标记。

本机文件锁不能阻止其他主机或人工同时提交。因此生产必须只有一个调度入口；原生 Daily 取消是切换前提。`native-daily-disabled` 只记录已确认的前提，不会调用 Kaggle 自动取消计划。

## 资源与凭据

服务使用 `Type=oneshot`、`Restart=no`；不运行时没有常驻 Python 进程。初始资源限制为 `MemoryHigh=192M`、`MemoryMax=256M`、`CPUQuota=25%`、`Nice=10`。CPUQuota 的 25% 表示一个核的四分之一。

应用与 Notebook 包由 root 持有，服务用户只读。状态目录由服务用户持有且权限为 0700。`credentials.env` 和 `settings.env` 为 root:root 0600；前者只保存 `KAGGLE_API_TOKEN`，由 systemd 注入环境。凭据不进入代码、Notebook、日志或仓库。个人 API Token 与 Kaggle 会话内的平台凭据不同，不能互换。

依赖由 `requirements-kaggle-scheduler.txt` 固定。SDK 0.1.38 要求 Python 3.11 或更高版本；香港服务器复用系统 Python 3.12。完整安装结果保留为应用目录的 `installed-requirements.txt`。

## 运行检查

```bash
sudo systemctl status polymarket-kaggle-scheduler.timer
sudo systemctl list-timers polymarket-kaggle-scheduler.timer --all
sudo journalctl -u polymarket-kaggle-scheduler.service --since today --no-pager
```

日志中的 `submitted` 表示固定版本提交已确认，不表示采集结束或日报完成。最终日报仍需以云端 `progress.json`、`index.json` 和文字稿状态核对。

`--check-only` 查询身份、版本和恢复结果，不提交 Notebook，也不写入调度状态。该模式仍可能创建本地状态目录和锁文件。

`already_submitted_remote` 表示云端最新版本已有当天、同 bundle 的标记；此路径不写 `state.json`。因此替换主机可能只有 `scheduler.lock`，这是正常状态，不能据此判断生产未提交。不要手工伪造状态文件；结合日志、固定版本标记与当前运行状态核验。服务为 oneshot，成功执行后显示 inactive 属于正常；应检查 timer 为 active/enabled、service 的退出码及 journal。

若日志报告 `pending_unresolved` 或 `submission_unconfirmed`，先核对 pending 中的固定版本与源码标记。不要删除状态或重新 push 来绕过这一检查。凭据失效时更新受保护的凭据文件，再做只读检查。

## 暂停与回退

```bash
sudo systemctl disable --now polymarket-kaggle-scheduler.timer
```

这只停止后续外部触发，不取消已经在 Kaggle 运行的 Notebook。需要恢复原生 Daily 时，先确认外部 timer 已停用，再在生产 Notebook Settings 中恢复 Daily。保留状态文件、云端数据和现有 release 清单。

## 验收证据

2026-11-17 替换部署证据：`outputs/kaggle-scheduler-recovery-20261117/`，含云端固定版本核验、安装和服务日志、源码摘要与独立审查。复用原生产 bundle，调度代码和业务发布清单均未更改；**32 项专项测试全部通过，无跳过**。新机补装 `python3-venv` 并按锁定版本安装依赖，`pip check` 通过。凭据通过 SSH 标准输入写入 root:root 0600 文件，没有进入部署包或证据。

VPN 验收：安装前后 `sing-box.service` 均为 active，PID 1680、启动时间 `2026-11-17 04:12:53 UTC` 保持不变。本地四个旧报表 timer 继续为 disabled/inactive。

旧部署证据目录：`outputs/kaggle-scheduler-deployment-20261115/`。以下 QA 与资源测量均为 11 月 15 日旧服务器上的历史验收，不是新主机再次提交的结果。

私有 QA 为 `qiuqiuslowchillchill/private-external-scheduler-acceptance-20261115`。种子 Quick Save 由平台按标题生成上述 slug，首次预期 slug 校验失败后停止；通过只读核对私有状态与源码后采用实际 slug，没有重复创建。服务器使用正式调度脚本提交第 2 版，固定版本源码标记与本地状态一致，运行完成并产出 `scheduler-acceptance.json`。

再次执行返回 `already_submitted`，版本仍为 2。另一独立本地状态目录模拟“已提交但未记录返回版本”的 pending；脚本固定回读第 2 版、恢复状态，仍没有新增版本。真实 QA 不写生产 Dataset。

香港服务器真实提交的 `/usr/bin/time -v` 结果：峰值 RSS **45,576 KiB（约 44.5 MiB）**、CPU 时间 **0.35 秒**、总耗时 **6.60 秒**。该结果是小型 QA Notebook 的提交耗时，不是完整采集耗时。应用与虚拟环境合计约 **26.5 MiB**，复用系统 Python。安装前后 `sing-box.service` 均为 active，PID 8195、启动时间 2026-11-03 04:14:22 UTC 均未改变。
