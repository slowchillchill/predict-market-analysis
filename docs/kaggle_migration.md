# Kaggle 采集、存储与本地图文生成

生产流程将原始数据采集、SQL 统计和历史补采放在 Kaggle。本地只下载完成的统计 JSON，生成现有日、周、月海报、推文和发布清单。

项目名称为 **Polymarket Crypto Updown Markets Analysis**。公开入口使用 `qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis`。部署版本和实际运行结果以部署记录为准。

## 数据与代码

| 位置 | 内容 |
| --- | --- |
| `pm-updown-raw-YYYY-MM-DD` | 按市场 UTC 结束日保存的 `data.sqlite3`、`collection_status.json` 和来源说明 |
| `pm-updown-analysis-YYYY-MM` | `features.sqlite3`、完成的日周月结果、`index.json`，以及当前月份的全局 `progress.json` |
| 本地 `outputs/posters` | 现有格式的图文、配套 JSON 和发布清单 |
| 本地 `outputs/kaggle_reports/state.json` | 首次部署日期、固定目标周期及其待处理、已生成或已保留状态 |

日库保留钱包交易、单边成交额和两条独立的分页进度。`incomplete` 版本供恢复使用；只有市场覆盖、两类采集和既有 BUY 重建检查均通过后，才发布正式结果。

月度分析数据集每次发布完整快照，保留该月原有文件。周结果归入最后一个包含日期所属的 UTC 月份，例如 `2026-10-26` 至 `2026-11-01` 的周结果放在 `pm-updown-analysis-2026-11`。

GitHub 项目代码保留 MIT；Kaggle 启动脚本采用 Apache 2.0。整理数据的 CC0 1.0 声明适用于贡献者所拥有的权利，并保留 [Polymarket 来源条款](https://polymarket.com/tos)及来源说明。

此次迁移不上传本地旧库，不导入本地机器人标签缓存，也不删除、截断或压缩现有数据库。部署后本地旧库停止增长；删除和释放其原有空间由操作者另行安排。

## Kaggle 任务

代码入口为 `kaggle/notebook.py`，配置为 `kaggle/kernel-metadata.json`。Notebook 启用 Internet，使用 CPU，按固定 Git 提交加载项目代码。

在 Kaggle 的 Secrets 界面创建名为 `KAGGLE_API_TOKEN` 的 Secret，并对该 Notebook 启用访问。Token 不写入代码、数据集或日志。然后在 Notebook 的调度设置中启用 Daily。

使用已配置的 Kaggle CLI 更新代码：

```bash
kaggle kernels push -p kaggle
kaggle kernels status qiuqiuslowchillchill/polymarket-crypto-updown-markets-analysis
```

`push` 会触发运行。生产 Notebook 每次启动先恢复云端进度，再采集最新已结束的 UTC 日期，并修复近期未完成市场。新日期处理后，继续从旧到新补采历史。首次历史发现固定各系列可取得的历史范围，后续会话沿用该范围。

历史采集在每页事务提交后检查日更优先级。出现新的已结束 UTC 日期时，保存当前历史日库，处理新日期，然后恢复历史。单次会话结束后，下次 Daily 运行继续；生产没有每日补采日期数量上限。

所有 Polymarket 请求顺序发送，每次尝试结束后至少间隔 0.5 秒。临时错误最多尝试三次，重试等待约 2 秒、4 秒，并遵守有效的 `Retry-After`。持续限流时保存进度并退出；某市场失败时，只补该市场缺失的采集类型。游标失效只重置对应未完成市场、对应采集类型。不可取得的数据保留缺口，不填零。

正常暂停、日更抢占或可处理错误退出时，将一致的日库发布为新版本，等待 Kaggle 处理完成后再将其作为恢复点。平台强制终止时，尚未发布的部分可能需要重采。

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

尚未就绪的目标保存为 `pending`，每 30 分钟重试。即使跨日，原目标也不会被新日期替换。电脑错过定时时间后，后续运行会补登记部署以来到期的目标。

任务先读取月度数据集状态，固定已处理完成的版本，再从同一版本下载 `index.json` 和指定结果文件。它不下载原始日库，也不打开本地原始 SQLite。

现成的 systemd 用户单元位于 `config/systemd/`。在本项目位于 `~/poly-market-analysis` 时安装：

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

机器人首次命中记录从云端重新建立。补采得到更早有效命中时，取更早日期并重算受影响的云端统计。较晚命中不会用于较早日期。`bot_history` 记录请求范围、实际观察范围与历史是否待补齐；历史未完成时不能将已观察范围解释为全部历史。

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

每个结果包含 `schema_version: 1`、`status: complete`、周期、`source_versions`、`payload` 和 `distribution`。`payload` 沿用现有海报总览字段；`distribution` 沿用既有成交分布格式。本地仅接受周期匹配、带原始日库版本引用的完成结果。

## 验证

```bash
.venv-btc5m/bin/python -m unittest discover -s tests -p 'test_polymarket_kaggle_*.py' -v
```

本地回归覆盖 JSON 出图、固定版本读取、跨日重试、已有文件保留、历史标签更新和周月合并。实际部署另验收一个完整新日期、至少一个历史日期及跨会话续采。运行日志、数据集版本和实际图文产物作为部署证据。
