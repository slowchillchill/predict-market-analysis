# Regime Sentinel Agent — MongoDB Track Hackathon 方案

## 1. 项目定位

**项目名**：Regime Sentinel Agent
**参赛方向**：Google Cloud Rapid Agent Hackathon / MongoDB Track
**核心目标**：为 Polymarket BTC 5min Up/Down 市场构建一个实时 regime-shift 预警 agent。系统用事件级市场数据做低延迟预警，用 MongoDB 保存市场记忆、相似历史窗口和回测证据，再由 Google Cloud Agent Builder + Gemini 3 定期生成解释和行动摘要。

本项目不是高频交易系统，也不承诺稳定毫秒级盈利信号。它的可验证目标是：

- 在 5min 市场内实时识别 regime 变化风险。
- 给出 `1s / 5s / 30s` horizon 的提前预警证据。
- 用 MongoDB Vector Search 检索历史相似窗口。
- 用 Gemini 3 低频解释“为什么报警、历史相似案例是什么、当前风险等级如何”。
- 提供可访问的 Web dashboard 和英文 demo，满足 hackathon 提交要求。

## 2. Hackathon 合规要求

根据 Google Cloud Rapid Agent Hackathon 规则，项目必须：

- 构建 functional agent。
- 使用 Gemini 和 Google Cloud Agent Builder。
- 集成所选 Partner 的 MCP server。
- 选择一个 Partner track：Arize、Elastic、Fivetran、GitLab、MongoDB、Dynatrace。
- 使用 Google Cloud 和所选 Partner 产品。
- 提供 hosted project URL。
- 提供公开开源代码仓库和 license。
- 提供不超过 3 分钟的英文 demo video。
- 支持 Web、Android 或 iOS 至少一个平台。
- 必须是 contest period 内新建项目，不应只是现有项目的修改或扩展。

因此，实际参赛实现应放在新的 hackathon repo 中。`poly-market-analysis` 可以作为调研和参考来源，但提交项目应作为独立新项目呈现。

参考：

- Devpost rules: <https://rapid-agent.devpost.com/rules>
- Devpost overview: <https://rapid-agent.devpost.com/>

## 3. Partner / Track 选择

**推荐选择：MongoDB Track。**

原因：

- 本项目天然需要实时 operational memory：ticks、features、alerts、regime states、backtest runs。
- MongoDB Atlas 适合保存 time-series 数据、聚合窗口和 agent memory。
- MongoDB Vector Search 适合做“历史相似 regime 窗口检索”。
- MongoDB MCP Server 可以让 Agent Builder/Gemini 查询当前市场状态、报警历史、相似窗口和回测结果。
- MongoDB track 与项目核心能力强绑定，不是装饰性集成。

其他 track 评估：

| Track | 适配度 | 判断 |
|------|--------|------|
| MongoDB | 高 | 最适合作为市场记忆、相似历史检索和 agent 工具层 |
| Arize | 中 | 适合评估 agent/模型质量，但不是实时市场状态核心存储 |
| Elastic | 中 | 适合搜索和日志检索，但 MongoDB 更贴合 operational + vector memory |
| Fivetran | 低 | 更适合数据同步，不是本项目核心 |
| GitLab | 低 | 更适合 DevSecOps/代码工作流 |
| Dynatrace | 中低 | 适合 observability，但不如 MongoDB 贴合产品卖点 |

参考：

- MongoDB track resources: <https://rapid-agent.devpost.com/details/mongodb-resources>
- MongoDB Vector Search: <https://www.mongodb.com/docs/vector-search/>

## 4. 总体架构

系统拆成两条路径。

### 4.1 热路径：低延迟 deterministic warning

热路径不调用 Gemini，不依赖 Agent Builder，不等待 Vector Search。

数据流：

```text
Polymarket CLOB WS + Chainlink/price feed
  -> event normalizer
  -> feature engine
  -> warning engine
  -> MongoDB alerts/regime_states
  -> SSE dashboard
```

目标：

- event 到 warning engine 的 p95 延迟 `< 500ms`。
- dashboard snapshot 每 `1s` 更新。
- MongoDB 写入失败时不阻塞热路径，先写本地 NDJSON fallback。
- Gemini 失败或限流不影响预警。

### 4.2 冷路径：Agent explanation / historical reasoning

冷路径低频运行：

```text
MongoDB alerts/regime_states/backtest_runs
  -> MongoDB MCP + Vector Search
  -> Google Cloud Agent Builder
  -> Gemini 3 explanation
  -> MongoDB agent_summaries
  -> dashboard summary panel
```

用途：

- 每 15 或 30 分钟生成一次市场摘要。
- 手动查询当前 regime。
- 检索历史相似窗口。
- 解释最近 alert 的原因。
- 生成 demo-friendly narrative。

## 5. Gemini 调用节流策略

Gemini 不能每个 tick、每次盘口变化、每个 alert 都调用。默认策略：

```env
GEMINI_ENABLED=true
GEMINI_MODEL=gemini-3-flash-preview
GEMINI_DEEP_MODEL=gemini-3.1-pro-preview

# 默认每 30 分钟一次；demo 可改成 15 分钟
GEMINI_SUMMARY_INTERVAL_SECONDS=1800
GEMINI_MIN_SUMMARY_INTERVAL_SECONDS=900

# 手动请求冷却
GEMINI_MANUAL_COOLDOWN_SECONDS=300

# 紧急解释默认关闭，避免高费用；demo 可打开
GEMINI_EMERGENCY_EXPLAIN_ENABLED=false
GEMINI_EMERGENCY_MIN_INTERVAL_SECONDS=900

# 成本保护
GEMINI_MAX_CALLS_PER_HOUR=4
GEMINI_MAX_CALLS_PER_DAY=96
GEMINI_THINKING_LEVEL=LOW
```

规则：

- 默认每 30 分钟最多一次自动 Gemini summary。
- 如果 demo 需要更频繁，改为 15 分钟一次。
- 高严重度 alert 只进入 `alerts` 和 dashboard，不自动触发 Gemini，除非 `GEMINI_EMERGENCY_EXPLAIN_ENABLED=true`。
- 手动点击 “Explain now” 也要受 cooldown 和 hourly cap 限制。
- 低成本摘要用 `gemini-3-flash-preview` + `thinking_level=LOW`。
- 复杂赛演示或最终报告才使用 `gemini-3.1-pro-preview`。
- Gemini 输出缓存到 `agent_summaries`，同一时间桶重复打开 dashboard 不重复调用。

参考：

- Gemini 3 on Vertex AI: <https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/get-started-with-gemini-3>

## 6. Regime 类型

| Regime | 含义 | 触发来源 |
|--------|------|----------|
| EQUILIBRIUM | 市场接近 50/50，盘口正常 | fair probability、P_mid、spread |
| DIRECTIONAL_PRESSURE | 订单流或 fair probability 明确偏向一边 | OFI、microprice、P_fair |
| EARLY_SHIFT_RISK | 未来 1s/5s/30s 可能发生 regime shift | horizon classifier |
| SHIFT_DETECTED | 价格或 fair probability 已发生显著变化 | P_mid/P_fair jump |
| THIN_LIQUIDITY | spread 宽、深度差、报价陈旧 | spread/depth/staleness |
| LATE_LOCK | 剩余时间少且结果接近确定 | time remaining + P_up |
| WHALE_OR_FLOW_ANOMALY | 大额或集中成交异常 | trade pressure / wallet if available |

## 7. 实时指标设计

### P0 — Data Quality / Latency

记录每条事件的：

- source timestamp
- receive timestamp
- processing timestamp
- feed lag
- out-of-order count
- reconnect count
- stale quote age

用途：判断信号是否可信。延迟异常时降级为 `DATA_STALE`，不输出强预警。

### P1 — Fair Probability

替代原先粗糙的 `sigmoid(alpha * btc_return_30s)`。

输入：

- 当前 BTC 价格
- 市场 start/strike price
- 距离结算时间
- 短期 realized volatility
- 当前 P_mid
- Chainlink/price feed lag

输出：

```text
P_fair = probability(BTC_end > strike | current_price, time_remaining, realized_vol)
fair_gap = P_mid - P_fair
```

这是最重要的提前预警基础。

### P2 — Market-Fair Divergence

```text
fair_gap = P_mid - P_fair
fair_gap_velocity = fair_gap(t) - fair_gap(t-1s)
```

解释：

- `fair_gap > 0`：Polymarket 比 fair model 更偏 Up。
- `fair_gap < 0`：Polymarket 比 fair model 更偏 Down。
- gap 快速收敛或扩大可能预示 regime shift。

### P3 — Orderbook Microstructure

指标：

- spread
- top-N depth imbalance
- microprice
- quote update intensity
- quote staleness

用途：

- 判断盘口是否可靠。
- 捕捉价格变化前的挂单/撤单压力。
- 避免 thin book 下误判。

### P4 — Order Flow Pressure

```text
OFI_1s
OFI_5s
OFI_15s
signed_trade_pressure
trade_count_burst
```

原 OFI 保留，但窗口改短，并允许 event-driven 更新。

### P5 — Volume Acceleration

```text
volume_rate_5s / volume_rate_30s
trade_count_5s / trade_count_30s
```

用于捕捉爆量，不单独作为方向预测。

### P6 — Liquidity Reliability

```text
spread
depth
quote_age
book_update_gap
```

如果流动性不足，预警等级降级，dashboard 明确显示 `low confidence`。

### P7 — Wallet Concentration

仅在 live 数据源能可靠提供 wallet/session 信息时启用。否则只用于离线回测和历史报告，不放入热路径。

## 8. 预警算法

### 8.1 快速预警

每个事件到达时更新 feature state。每 `250ms-1s` 评估一次：

```text
score =
  w1 * abs(fair_gap_velocity)
+ w2 * abs(depth_imbalance)
+ w3 * abs(OFI_1s)
+ w4 * volume_acceleration
- w5 * stale_data_penalty
```

输出：

| State | 条件 |
|-------|------|
| WATCH | feature 异常但方向弱 |
| EARLY_RISK | 1s/5s horizon shift probability 超阈值 |
| SHIFT_DETECTED | P_mid 或 P_fair 已出现显著变化 |
| CONFIRMED | 变化持续多个 snapshot 且流动性可信 |

### 8.2 慢确认

CUSUM/Page-Hinkley 用作确认层，不再作为毫秒/秒级主预警。

```text
fast warning: event-driven, 250ms-1s
slow confirmation: 5s snapshot, Page-Hinkley/CUSUM
```

## 9. 标签和验证方法

原 AUC/Brier 不足以证明提前预警。修订后使用 event-based validation。

### 9.1 Shift 标签

独立定义 shift onset：

```text
shift_onset_time = first timestamp where:
  abs(P_mid(t + horizon) - P_mid(t)) >= 0.10
  and move persists for >= 3s
```

同时记录：

- direction
- magnitude
- duration
- liquidity state
- data quality state

### 9.2 关键验证指标

| 指标 | 目标 |
|------|------|
| median lead time | > 1s |
| p75 lead time | > 5s for major shifts |
| false alerts per market | <= 1 |
| recall@1 false alert/market | 明确报告 |
| precision | 明确报告 |
| detection delay | 如果没提前，报告滞后多久 |
| horizon PR-AUC | 1s / 5s / 30s 分开算 |
| ablation | P_fair、OFI、depth、volume 单独贡献 |

### 9.3 回测切分

- 训练/调参：早期历史窗口
- 验证：后续历史窗口
- demo：挑选一场可复现高波动窗口
- live：只作为展示，不作为唯一证据

## 10. MongoDB 数据模型

### `market_ticks` time-series

短 TTL，保存必要 tick，不无限保存 raw event。

```json
{
  "timestamp": "...",
  "meta": {
    "slug": "btc-updown-5m-...",
    "series": "btc-updown-5m",
    "source": "clob"
  },
  "price": 0.52,
  "size": 100,
  "side": "BUY",
  "outcome": "UP",
  "receive_lag_ms": 120
}
```

### `feature_windows`

```json
{
  "slug": "...",
  "window_ts": "...",
  "window_ms": 1000,
  "p_mid": 0.52,
  "p_fair": 0.49,
  "fair_gap": 0.03,
  "ofi_1s": 0.42,
  "depth_imbalance": 0.31,
  "spread": 0.03,
  "volume_acceleration": 2.1,
  "feature_vector": [0.03, 0.42, 0.31, 0.03, 2.1]
}
```

### `regime_states`

```json
{
  "_id": "btc-updown-5m-...",
  "regime": "EARLY_SHIFT_RISK",
  "confidence": 0.71,
  "updated_at": "...",
  "previous_regime": "EQUILIBRIUM",
  "indicators": {},
  "market_resolved": false
}
```

### `alerts`

```json
{
  "slug": "...",
  "created_at": "...",
  "severity": "HIGH",
  "state": "EARLY_RISK",
  "direction": "UP",
  "trigger": "fair_gap_velocity+ofi_1s",
  "message": "Up-side pressure rising before price fully reprices",
  "gemini_explained": false
}
```

### `agent_summaries`

```json
{
  "bucket_start": "...",
  "bucket_seconds": 1800,
  "model": "gemini-3-flash-preview",
  "thinking_level": "LOW",
  "summary": "...",
  "alert_ids": [],
  "similar_window_ids": [],
  "token_usage": {}
}
```

### `backtest_runs`

保存参数、数据范围、lead-time 指标、误报率和消融结果。

## 11. MongoDB MCP / Agent Tools

Agent Builder 注册工具：

| Tool | 功能 |
|------|------|
| `get_current_regime(slug)` | 查询当前 regime |
| `query_recent_alerts(minutes)` | 查询最近 alerts |
| `find_similar_windows(slug, k)` | MongoDB Vector Search 查相似历史窗口 |
| `get_backtest_metrics(run_id)` | 查询验证结果 |
| `generate_market_summary(minutes)` | 汇总最近 15/30 分钟状态 |

MongoDB MCP 用于允许 Agent Builder/Gemini 对 MongoDB 进行受控查询。工具只读为主，写入仅允许写 `agent_summaries` 和解释字段。

## 12. 技术栈

后端：

```text
Rust 1.89+
Tokio
Axum
Tower / tower-http
tokio-tungstenite
reqwest
mongodb Rust Driver
serde / serde_json
tracing / tracing-subscriber
dotenvy
anyhow / thiserror
```

前端：

```text
SvelteKit
Tailwind CSS
lightweight-charts
SSE client
```

云服务：

```text
Google Cloud Run
Google Cloud Agent Builder
Vertex AI Gemini 3
Secret Manager
Cloud Logging
Firebase Hosting or Cloud Run static hosting
MongoDB Atlas
MongoDB MCP Server
MongoDB Vector Search
```

### 12.1 原方案框架到 Rust 框架映射

| 原方案组件 | 原框架 / 库 | Rust 方案 | 用途 |
|------------|-------------|-----------|------|
| 后端语言 | Python 3.12 | Rust 1.89+ | 统一后端、collector、feature engine、API server 的实现语言 |
| Async runtime | `asyncio` | `tokio` | WebSocket、HTTP、MongoDB、SSE、定时任务的异步运行时 |
| API server | FastAPI | `axum` | REST tools、SSE stream、health check、static frontend fallback |
| ASGI server | uvicorn | `axum` + `tokio::net::TcpListener` | Cloud Run HTTP 服务入口 |
| Middleware | FastAPI middleware | `tower` / `tower-http` | timeout、trace、CORS、compression、request limit |
| HTTP client | `aiohttp` | `reqwest` | Gamma API、Gemini REST API、Cloud Run webhook、外部 price feed |
| WebSocket client | `websockets` | `tokio-tungstenite` | Polymarket CLOB WS、Chainlink/price WS |
| MongoDB async driver | `motor` | `mongodb` Rust Driver | Atlas collections、alerts、feature windows、agent summaries |
| MongoDB sync/admin helper | `pymongo` | `mongodb` Rust Driver | index bootstrap、ping、admin checks |
| JSON / schema | Python `dict` / pydantic | `serde` / `serde_json` | typed event schema、config、API request/response |
| Gemini SDK | `google-genai` | `reqwest` 调 Gemini REST API | 低频 Gemini summary；不放在热路径 |
| Structured logging | `structlog` | `tracing` / `tracing-subscriber` | event latency、collector state、alert lifecycle |
| `.env` loading | `python-dotenv` | `dotenvy` | local dev config；Cloud Run 生产环境用 Secret Manager 注入 |
| Error handling | Python exceptions | `anyhow` / `thiserror` | app-level error context + typed domain errors |
| Tests | `unittest` / `pytest` | `cargo test` | feature engine、time grid、replay、alert scoring |
| Formatting / lint | black / ruff | `cargo fmt` / `cargo clippy` | Rust code quality gate |
| Container build | Python Dockerfile | Rust multi-stage Dockerfile | `cargo build --release` 后复制单二进制到 runtime image |

### 12.2 Rust-first 语言选型

后端统一切换为 Rust-first。原因：

- 热路径需要尽量稳定的 p95/p99 延迟，Rust 更适合 WebSocket collector、orderbook parser、feature engine 和长期运行服务。
- Tokio 生态适合同时处理 CLOB WS、price feed、MongoDB 写入、SSE fanout 和定时 summary。
- 单二进制部署到 Cloud Run 更简单，镜像更小，冷启动和内存占用更容易控制。
- 本地已有 `poly-tx` 的 Rust WebSocket 采集代码，可复用时间网格和市场切换经验。

需要接受的代价：

- Google Gemini / Agent Builder 的 Rust 官方 SDK 支持不如 Python 直接；本方案用 `reqwest` 调 REST API，Agent Builder 侧通过 OpenAPI/MCP 集成。
- Rust 开发速度低于 Python，需要更明确的模块边界和测试。
- 前端仍保留 SvelteKit + Tailwind + lightweight-charts；把前端也改成 Rust/WASM 会增加交付风险，不利于 hackathon 设计/UX 评分。

Rust 后端模块建议：

```text
crates/regime-core      # typed events, indicators, fair probability, alert scoring
crates/regime-collector # CLOB/price WebSocket collector + reconnect/backpressure
crates/regime-api       # Axum REST tools + SSE + static frontend serving
crates/regime-agent     # Gemini scheduler, MongoDB MCP-adjacent tool handlers
crates/regime-replay    # historical replay + validation metrics
```

参考：

- Tokio: <https://tokio.rs/>
- Axum: <https://docs.rs/axum/latest/axum/>
- tokio-tungstenite: <https://docs.rs/tokio-tungstenite/latest/tokio_tungstenite/>
- reqwest: <https://docs.rs/reqwest/latest/reqwest/>
- MongoDB Rust Driver: <https://www.mongodb.com/docs/drivers/rust/>
- Serde: <https://docs.rs/serde/latest/serde/>
- tracing: <https://docs.rs/tracing/>

## 13. 资源和容量规划

Demo 默认配置：

| 组件 | 配置 |
|------|------|
| `regime-collector` | Cloud Run min=1 max=1, 1 vCPU, 1GiB RAM |
| `regime-agent-app` | Cloud Run min=1, 0.5-1 vCPU, 512MiB-1GiB RAM |
| Web dashboard | Firebase Hosting 或 Cloud Run static |
| MongoDB Atlas | 先按 M10 dedicated 规划；若实测低 tier 支持所需功能再降级 |
| SSE 并发 | demo 限制 10-25 clients |
| 写入能力 | 单市场 50-100 writes/sec burst |
| TTL 存储 | 7 天 1-10GB 起步 |
| Gemini | 默认 2 calls/hour，最高 4 calls/hour |

Cloud Run WebSocket/SSE 注意事项：

- WebSocket/SSE 属于长 HTTP request，要设置 request timeout。
- 客户端必须支持 reconnect。
- 不依赖单个连接永久不断。
- collector 使用单实例，避免重复订阅同一个市场。

参考：

- Cloud Run WebSockets: <https://docs.cloud.google.com/run/docs/triggering/websockets>

### 13.1 当前已搭建环境

截至 2026-05-22，当前本机和云端已经完成以下基础配置：

| 项目 | 当前状态 |
|------|----------|
| Google Cloud project | `poly-market-analysis` |
| Billing / budget | 已由用户配置完成 |
| gcloud CLI | 已安装，版本 `Google Cloud SDK 569.0.0` |
| gcloud active account | `awgcoder@gmail.com` |
| Cloud Run region | `asia-northeast1`（东京） |
| Compute region | `asia-northeast1`（东京） |
| Application Default Credentials | 已配置，quota project 为 `poly-market-analysis` |
| 已启用 API | Cloud Run、Cloud Build、Artifact Registry、Secret Manager、Vertex AI、Compute Engine |
| Secret Manager | 已创建 `mongodb-uri` 和 `mongodb-db` |
| MongoDB Atlas | 已创建连接信息，当前本机 `ping` 验证通过 |
| 本地 `.env` | 已创建，权限 `600`，已加入 `.gitignore` |
| 本地环境记录 | `LOCAL_ENVIRONMENT.md` 已创建，包含明文环境信息，已加入 `.gitignore` |
| 本地测试 venv | `.venv-gcp/` 已创建并加入 `.gitignore`，用于 GCP/MongoDB 连通性检查 |

方案文档不记录 MongoDB 密码或完整连接串；本地明文记录只放在被忽略的 `LOCAL_ENVIRONMENT.md`。

## 14. Dashboard 设计

默认暗色，支持浅色切换。

主要区域：

- 当前市场状态卡片
- P_mid vs P_fair 曲线
- Regime timeline
- Alert stream
- Feature bars：fair_gap、OFI、spread、depth、volume acceleration
- Gemini summary：最近 15/30 分钟摘要
- Similar history：MongoDB Vector Search 返回的相似窗口
- Validation panel：lead time、false alert、precision、recall

关键 UI 原则：

- 快速预警先显示 deterministic message。
- Gemini summary 明确标注生成时间和覆盖窗口。
- 如果数据延迟或盘口不可信，界面必须显示 degraded confidence。

## 15. 实施阶段

### Phase 0 — Hackathon 合规骨架

- 新建独立参赛 repo。
- 添加 LICENSE。
- 添加英文 README。
- 添加 hosted URL 占位。
- 明确 MongoDB Track。
- 明确 Google Cloud + Agent Builder + Gemini 3 + MongoDB MCP。

### Phase 1 — Replay + Feature Engine

- 实现 event schema。
- 实现 replay runner。
- 实现 fair probability baseline。
- 实现 feature windows。
- 实现 alert engine。
- 输出 backtest CSV/JSON。

可演示物：历史窗口 replay 时 dashboard 动起来。

### Phase 2 — MongoDB Core

- 写入 `market_ticks`。
- 写入 `feature_windows`。
- 写入 `regime_states`。
- 写入 `alerts`。
- 建 Vector Search index。
- 实现 `find_similar_windows`。

可演示物：dashboard 可以显示历史相似窗口。

### Phase 3 — Live Collector

- 接入 Polymarket CLOB WS。
- 接入 BTC price feed。
- 实现 reconnect。
- 实现 stale data 降级。
- 实现本地 NDJSON fallback。
- 跑 3 个真实 5min 窗口验证市场切换。

可演示物：live 市场实时显示 alert 和 regime。

### Phase 4 — Agent Builder + Gemini

- 配置 MongoDB MCP。
- 配置 Agent Builder tools。
- 实现 Gemini summary scheduler。
- 实现 15/30 分钟可配置节流。
- 实现 manual explain cooldown。
- 写入 `agent_summaries`。

可演示物：Agent 能回答当前状态、最近风险、相似历史和验证结果。

### Phase 5 — Validation + Demo

- 生成 lead-time 报告。
- 生成 false alert 报告。
- 生成 ablation 报告。
- 部署 hosted app。
- 录制英文 3 分钟 demo。
- 确认 public repo、license、README、setup instructions 完整。

## 16. 成功标准

Hackathon 成功：

- Hosted web app 可访问。
- Agent Builder + Gemini 3 可用。
- MongoDB MCP 被实际用于查询。
- MongoDB Vector Search 被用于相似窗口检索。
- Demo 展示 live/replay dashboard。
- Demo 展示 Gemini summary 不是高频调用，而是受控低频分析。
- README 说明配置、运行、成本保护和验证方法。

技术成功：

- 热路径不依赖 Gemini。
- p95 feature processing latency `<500ms`。
- 每市场 false alerts 可统计。
- 能给出 1s/5s/30s horizon lead-time 指标。
- 能说明哪些预警是提前、哪些只是同步检测。

## 17. 主要风险和处理

| 风险 | 处理 |
|------|------|
| Gemini 调用费用过高 | 默认 30 分钟一次，最多 4 calls/hour，支持关闭 emergency explain |
| 原指标只能事后检测 | 加入 fair probability、microstructure、lead-time validation |
| MongoDB 写入阻塞热路径 | 异步队列 + NDJSON fallback + backpressure |
| Live 数据不稳定 | replay demo 作为稳定演示，live 作为附加展示 |
| Hackathon 新项目规则 | 独立 repo、独立 app、独立 README，不把旧项目包装成扩展 |
| Vector Search 集成耗时 | Phase 2 先做固定 feature vector，相似窗口检索只查已聚合窗口 |
| Agent Builder 配置风险 | 先用 Axum 暴露 OpenAPI-compatible tools，再接 MongoDB MCP |
