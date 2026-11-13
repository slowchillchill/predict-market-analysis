# X 与 LinkedIn 文字稿生成和发布

当前日、周、月生产入口只生成两个平台的英文文字稿：X 串联短帖和 LinkedIn 动态。抖音、TikTok 已退出当前流程。图片、分布图、中文卡片和配乐不再属于当前生成包。

## 生成入口

生产入口为 Kaggle Notebook。它先确认分析 Dataset 版本，再从该版本的完整结果生成文字，随后把文字与生成状态保存到月度分析 Dataset 的 `social.zip`。目录为 `social/<kind>/<target>/`，状态为 `social/state.json`。新会话自动解压并恢复；本地 Codex 关闭不会中断这个流程。

既有文字通过固定版本的私有迁移 Dataset 导入。正文和来源版本保持不变，清单路径改为相对路径。已有 `generated` / `preserved` 周期不重生成。文字生成成功不代表已经发布到社交平台。

日报每天 09:30 JST、周报周一 11:30 JST、月报每月 1 日 12:30 JST 是周期到期时间。Kaggle 只在会话运行且相应统计完成后生成，因此 Daily 调度不保证在这些时刻立即出稿。云端接管后停用旧本地定时器；下面的命令保留为手动生成与预览入口：

```bash
.venv-btc5m/bin/python scripts/polymarket_kaggle_reports.py --kind all
```

脚本固定同一个已处理完成的 Kaggle 分析 Dataset 版本，从中读取索引和结果 JSON。它不下载原始日库，不查询本地 SQLite，也不调用图片生成模块。采集、统计口径和云端发布保持原流程。

未就绪结果保留待处理状态，后续云端保存或新会话会重新检查，目标日期不会因跨日而替换。更换文字格式不会把过去所有周期重新加入队列。

本地预览的默认输出目录为 `outputs/social/`。云端归档中的日报文件相同，例如：

```text
updown_2026-11-10.json
updown_2026-11-10_x_01.txt
updown_2026-11-10_x_02.txt
updown_2026-11-10_x_03.txt
updown_2026-11-10_linkedin.txt
updown_2026-11-10_publish.json
```

X 通常为三条；较长数字或周期标题可能需要更多条。发布者按清单中的 `posts` / `text_paths` 顺序使用全部短帖，不凭示例张数推断。周报文件名包含周一至周日，月报文件名包含 `YYYY-MM`。

已有 `outputs/posters/` 图片与旧发布记录保留。新文字包使用独立目录，避免与旧图包同名冲突；同一文字包已存在时不覆盖。文字生成成功不代表已经发布。

## 文案格式

- X：按顺序发布英文串联帖，每条按普通 280 个加权字符限制生成。首条提供日期、市场范围、市场单边金额、钱包数和固定来源链接。后续列重建 BUY 事件、疑似机器人买方参与及筛选说明。
- LinkedIn：英文纵向指标块，使用真实换行和空行；不使用多列竖线表格、Markdown 表格或 Mermaid。正文包含总览、两组 BUY 汇总、三种参与覆盖率、十档价格分布、规则及来源，限制为 3,000 个 UTF-16 单位。
- 两个平台使用同一份完成结果。金额与份数使用 Decimal 四舍五入到两位小数；X 必要时用 K/M/B/T 紧凑数值。
- 日期按 UTC 市场结束日归属，包含这些市场更早发生的交易；钱包地址数不等于人数。
- 疑似机器人比例是买方参与事件的覆盖率，不是钱包自身支出占比。Up 等价金额与市场单边成交额分别命名，分母不混用。价格分布的两列金额占比均以全部 BUY 的 Up 等价金额为分母，总计按未舍入值计算。
- 历史覆盖不完整时保留说明；不声称已观察到全部历史。当前文字模板不输出环比，避免把覆盖范围变化表述为同范围增长。

依据：[X 字符规则](https://docs.x.com/fundamentals/counting-characters)、[LinkedIn 动态](https://www.linkedin.com/help/linkedin/answer/a528176)、[LinkedIn 文章也不支持原生表格](https://www.linkedin.com/help/linkedin/answer/a521719/)。

## 离线生成与审查

已下载的完整结果可直接生成文字，不进行网络请求：

```bash
.venv-btc5m/bin/python scripts/polymarket_kaggle_reports.py \
  --result-json /path/to/daily_2026-11-10.json \
  --source-dataset qiuqiuslowchillchill/pm-updown-analysis-2026-11 \
  --source-version 100 \
  --output-dir outputs/previews/text_2026-11-10
```

`--source-dataset` 和 `--source-version` 成对使用，必须填写该结果真正来自的分析 Dataset 版本。日报未提供分析版本时，可以使用目标日自带的固定原始日库引用；周报、月报需要完整周期的分析版本引用，不能把整周期指标指向某一个原始日库。若输入本身保存了 `source_reference`，可直接复用。

清单为 `schema_version: 2`、`format: text`，只包含 `x`、`linkedin`。原始日库格式和云端报告格式没有随本地清单改变。`source_path` 保存原结果及来源引用；平台项保存正文、逐条文本路径及内容摘要，不包含图片或配乐字段。

## 发布交接

发布由用户签发给专门的发布 AGENT，生成脚本不向平台提交内容。账号与当前启用平台见 `config/polymarket_publish.json`。

1. 固定目标周期，核对实际登录账号、清单和既有作品。
2. X 按 `platforms.x.posts` 顺序形成串联帖；LinkedIn 使用 `platforms.linkedin.body`，选择公开可见。
3. 提交前先记 `submitting`。结果不确定时核实既有作品，不盲目重发；审核中记 `pending_review`。
4. 明确发布成功后保存作品链接和页面证据。X 保存全部串联帖链接。

旧图包状态与新文字包沿用同一周期、同一平台的状态路径，因此旧已发布记录不会因改格式而失去去重作用。历史图包读取仅为核对既有记录，不代表重新启用旧平台。

文字包状态示例（只有核实平台尚未存在该稿后才使用 `--verified-absent`）：

```bash
.venv-btc5m/bin/python scripts/polymarket_publish_state.py \
  --manifest outputs/social/updown_2026-11-10_publish.json \
  --platform linkedin --status submitting --verified-absent
```

X 记录公开成功时，通过 `--post-urls-json` 提供按串联顺序排列的全部作品链接，并提供 `--evidence`；LinkedIn 使用 `--post-url` 与 `--evidence`。图片、配乐及 PNG 校验不适用于文字包。

## 本次验证（2026-11-11）

完整回归 438 项通过，独立代码审查通过。使用分析 Dataset 第 100 版的真实 `daily_2026-11-10.json` 离线生成了 `outputs/social/` 示例：X 三条分别为 227、245、271 个加权字符，LinkedIn 为 1,898 个 UTF-16 单位。文字包与发布状态入口兼容，无需图片或原始数据库。本次只生成本地稿件，没有提交平台发布。
