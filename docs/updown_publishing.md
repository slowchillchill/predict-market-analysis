# 四平台海报发布

## 生成与时间

日、周、月现有 systemd 时间和启动命令保持不变。日报自动入口与周/月默认入口生成总览、全系列买入分布 PNG/JSON、英文推文、中文正文和发布清单；总览 JSON 的 `trade_distribution` 保存同周期分布引用与哈希。
周/月提供 `--without-distribution` 保留旧包；新版默认同步两组 BUY 的正文和分布图，整套校验后再写入最终目录，清单最后更新。
X、LinkedIn、TikTok 新版图片数组依次为英文总览、买入分布页。抖音日报4卡依次为总览、全部 BUY、机器人 BUY、方法；周/月6卡依次为总览、每日趋势、高频地址解释、全部 BUY、机器人 BUY、方法。
未附分布的旧日报及周/月包继续使用原内容和附件规则。按清单版本与数组选择图片，不凭文件名猜测张数。
分布 JSON 不符合总览发布侧文件的输入结构，不能直接传给 `polymarket_updown_captions.py`。

桌面线程任务到点按顺序处理 X、LinkedIn、抖音、TikTok：每天 11:00、每周一 12:00、每月 1 日 12:45，均 Asia/Tokyo。以运行开始的 UTC 时间固定昨日/上一完整周/上一完整自然月，等待和重试不改变目标。不叠加平台内预约，不自动补发历史周期。

先核对 systemctl --user show/status 和 journalctl --user：对应目标、实际运行时间、Result=success、ExecMainStatus=0。oneshot inactive/dead 正常。运行中每次最多等待 60 秒；明确失败或产物错期则停止该批。禁止在发布流程启动、重启或重复执行采集/海报生成。

已有源文件需要新增抖音文案及卡片时，可执行旁路转换：仅读取已有 JSON，不重画原英文图、不读数据库、不采集。预览使用独立目录，保留已发布版本的文案、清单和状态：

```bash
.venv-btc5m/bin/python scripts/polymarket_updown_captions.py --source outputs/posters/updown_weekly_2026-10-05_2026-10-11.json --output-dir outputs/previews/douyin_weekly_2026-10-05_2026-10-11_v1
```

每次对照 `_publish.json` 的目标日期及 source/image/english SHA256。图片必须可读取。PNG、JSON、英文正文变化后不得继续使用旧清单。

## 内容与账号

账号和背景音乐配置位于 `config/polymarket_publish.json`。

- X @slowchillchill，LinkedIn 邱秋：英文 `_tweet.md` 加清单中的平台图片数组；没有数组时用原 PNG。
- 抖音 slowchillchill / 抖音号 1350887770：清单 `platforms.douyin.title/body` 加 `platforms.douyin.image_paths`，日报新版4张、周/月新版6张，按数组顺序上传；逐张核对 `image_sha256s`。旧清单没有图片数组时代表旧单图版本，不能混用新文案。标题最多20、正文最多1000，以页面计数再次检查。
- TikTok @slowchillchill：清单 `platforms.tiktok.title/body` 加平台图片数组；没有数组时用原 PNG。标题90、正文4000，以页面计数再次检查。
- 中文来自原始统计值；金额 Decimal 四舍五入到分，环比不使用浮点；保留 UTC、去重钱包、疑似机器人、零基数/缺失、范围变更及可用时段调整。
- 含分布的日/周/月正文列出两组重建 BUY 事件的 Trades、Shares 和 Up 等价金额。Trade % 各除以本图总笔数，Notional % 均除以全部 BUY 事件 Up 等价金额。机器人比例是买方参与覆盖率，不是钱包自身资金贡献率。总览机器人模块与分布页必须共用同一 buy_event_summary，并显示对应 BUY 分母；钱包侧原价金额仅累计 BUY。标签从首次命中当日持续生效，不使用未来标签回算。
- 不截断、改写或补充营销承诺；不自动添加位置、热点、活动或商业推广标记。

## 浏览器执行

使用应用内浏览器现有会话和可用 CUA 工具。每次发现当前标签或打开新标签，不固定 tab ID，不提取登录 cookie、不调用网页私有接口。上传前读取工具 `file-uploads` 文档。

抖音入口：https://creator.douyin.com/ → 发布图文；TikTok：https://www.tiktok.com/tiktokstudio/upload → 照片。两平台支持单张 PNG。

1. 核对登录身份和近期作品，结合本地记录去重。
2. 通过 filechooser 按平台 `image_paths` 顺序上传，逐项核对 `image_sha256s`。新版英文平台为总览和分布2张，抖音日报4张、周/月6张；无分布旧包的抖音仍为4张，英文仍为原 PNG。第一张为封面，等待每张完整预览，核对数据无裁切。
3. 填写清单标题/正文，核对页面实际文字及计数。新版抖音正文先解释本期真实变化，再列数字与方法；零基数、缺失数据时不推断变化或补造趋势。话题推荐弹层不得擅自选取无关话题。
4. 打开曲库，按配置顺序匹配曲名、作者和时长，主选不可用才用备用。抖音同作者同长度有重复时选择页面显示使用人数最多的版本。曲库可能变化，每次必须重查。
5. 主选：两平台均 Aesthetic / Tollan Kim / 01:00。候选均来自 2026-10-01 可见曲库。抖音全部指定候选不可用时，从曲库任选一首当前可用音乐继续发布，无需再次确认；记录实际曲名、作者、时长及 `selection_reason=configured_candidates_unavailable`。TikTok 全部候选不可用仍阻止发布。不静默改成无声。
6. 编辑页确认音乐已选中、公开/所有人、立即发布/现在。保存发布前截图。
7. 先写 submitting 再点击一次发布。确认管理页及作品详情，不把跳转、上传成功或审核中当公开成功。
8. TikTok 管理页在审查期间可能显示“仅自己”；先等待审核，不能未经核实重复上传或盲目改隐私。

## 发布状态与恢复

使用 `outputs/publish_state/{kind}_{target}_{platform}.json`；周目标为 S_E，月目标为 YYYY-MM。

推荐通过 `scripts/polymarket_publish_state.py` 写状态；它校验源文件哈希、加文件锁、原子写入并保留历史。`--verified-absent` 表示操作者已经在平台核对没有该目标作品，不是代替平台核验。
新版日/周/月还校验分布 JSON/PNG 的周期引用、统计口径、数值加总、平台图片顺序及内容哈希。周/月分布必须覆盖完整日集合，并与总览的市场数、钱包数及机器人规则一致。独立预览不覆盖已发布版本，也不写入发布状态。
每日批准文案版还记录 `copy_version=buy_activity_approved_v1`，校验总览六项汇总与分布数据一致，尤其是笔数、Shares、Up 等价金额的三项覆盖率，以及当天频率达标钱包数。份数比例不能改用机器人组自身份数作为分母。正文、图片和引用必须来自同一套产物。
每日全系列图表版另外记录 `trade_distribution.layout_version=buy_price_distribution_chart_v1`；英文平台资产版本为 `daily_buy_distribution_chart_v1`，抖音四卡为 `research_cards_daily_buy_distribution_chart_v1`。清单生成与状态检查共用同一个版本选择函数，避免新图引用旧版清单。旧包无版式标记时仍按自身版本读取核对；周/月沿用原版式和版本。

```bash
.venv-btc5m/bin/python scripts/polymarket_publish_state.py \
  --manifest outputs/posters/updown_monthly_2026-09_publish.json \
  --platform douyin --status submitting --verified-absent \
  --music-json '{"title":"Aesthetic","artist":"Tollan Kim","duration":"01:00"}'
```

状态含义：

- `submitting`：已记录即将提交；不能盲目重试。
- `pending_review`：平台已接收但审核未结束；保留作品链接。
- `published`：页面明确发布成功并核对作品链接，保存截图和页面文字证据。
- `unknown`：结果不确定；先查平台。
- `blocked` / `failed`：未能提交或明确失败，附具体原因。审核拒绝不得自动重发。

更新为 published 时必须提供 `--post-url` 和 `--evidence`，另提供 `--screenshot`。任何一个平台成功，其他平台失败也不覆盖成功记录。每次定时运行先核对已有 pending_review/unknown/submitting 的作品链接并更新状态，不能自动补发旧周期；仅对本次目标中尚未成功且经平台确认无作品的项操作。

登录失效、验证码、账号不符、无法确认音乐/图片/正文时停止该平台，继续独立平台并如实汇报。

## 首次验收（2026-10-01）

2026-09 月报已使用原英文 PNG：

- 抖音中文图文：https://www.douyin.com/note/7691551239418514729 ，管理页已发布、状态正常，公开作品可读取。
- TikTok 英文照片：https://www.tiktok.com/@slowchillchill/photo/7691551161123441926 ，经过内容审查后显示所有人可见，作品页确认 Aesthetic - Tollan Kim。
- 两平台主选配乐均 Aesthetic / Tollan Kim / 01:00；首发证据及提交历史保存在 outputs/publish_state/。
- 三个现有桌面发布任务已原位更新为四平台，时间不变。原采集/生成服务与定时器未更改或重启。
- 全部 61 项 Polymarket 测试通过，包括新增文案及状态恢复测试。
